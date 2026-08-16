"""API Ekko — walking skeleton (Flask; migrazione a FastAPI quando il
registro pacchetti è di nuovo raggiungibile — stessa forma delle route).

  GET  /                             pagina di ricerca (motore "cerca un'azienda")
  POST /search                       nome azienda -> ingestion -> dashboard
  POST /businesses                   registra un'azienda target (JSON)
  POST /businesses/<id>/ingest       esegue l'ingestion
  POST /businesses/<id>/analyze      analisi AI (stadio 3) sull'insieme filtrato
  GET  /businesses/<id>/score        Ekko Score con breakdown
  GET  /businesses/<id>/dashboard    dashboard interattiva
  GET  /businesses/<id>/report       report statico
  GET  /health                       stato connettori + AI

Avvio:  python -m ekko.api.main   (porta 8000)
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import os
import secrets
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, request, session,
                   url_for)
from jinja2 import Environment, FileSystemLoader

from ekko.ai.gateway import AIGateway
from ekko.auth import google_oauth
from ekko.connectors.google import GoogleConnector
from ekko.connectors.trustpilot import TrustpilotConnector
from ekko.connectors import trustpilot_public
from ekko.core.models import BusinessRef
from ekko.ingestion.pipeline import (ingest, load_feedback, save_business,
                                     score_business)
from ekko.storage import db

app = Flask("ekko")
# Chiave di firma dei cookie di sessione. In produzione impostare EKKO_SECRET_KEY
# (env var su Render) così le sessioni sopravvivono ai riavvii; in locale si
# genera al volo.
app.secret_key = os.environ.get("EKKO_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # cookie sicuro quando siamo dietro il proxy TLS di Render
    SESSION_COOKIE_SECURE=bool(os.environ.get("EKKO_BASE_URL", "").startswith("https")),
)
_templates = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    autoescape=True,
)


# --------------------------------------------------------------------------
# Multi-tenant: ogni agenzia vede solo le proprie aziende/recensioni.
# --------------------------------------------------------------------------
def _base_url() -> str:
    return os.environ.get("EKKO_BASE_URL") or request.url_root.rstrip("/")


def owner_key(sub: str) -> str:
    """ID agenzia breve e stabile, derivato dal 'sub' Google (o 'pub' se il
    login è disattivato). Prefissa gli id-azienda così due agenzie che
    analizzano la stessa impresa restano isolate."""
    if not google_oauth.enabled():
        return "pub"
    h = hashlib.sha256(sub.encode("utf-8")).hexdigest()
    return "a" + h[:10]


def current_owner():
    """Ritorna (owner_id, is_logged) per la richiesta corrente.
    Login disattivato -> owner condiviso 'pub' (comportamento single-tenant)."""
    if not google_oauth.enabled():
        return "pub", True
    uid = session.get("uid")
    if not uid:
        return None, False
    return owner_key(uid), True


def login_required(fn):
    @functools.wraps(fn)
    def _wrap(*a, **k):
        owner, ok = current_owner()
        if not ok:
            return redirect(url_for("login"))
        return fn(*a, **k)
    return _wrap


def _require_owner_of(business_id: str) -> str:
    """Verifica che l'azienda appartenga all'agenzia loggata; altrimenti 403/redirect."""
    owner, ok = current_owner()
    if not ok:
        abort(401)
    biz_owner = db.get_business_owner(business_id)
    # aziende legacy senza proprietario (pre multi-tenant) sono visibili a tutti
    if biz_owner is not None and biz_owner != owner:
        abort(403)
    return owner


def default_connectors() -> list:
    from ekko.connectors import autoscout24, certified, dataforseo, facebook
    conns = []
    if dataforseo.enabled():
        conns.append(dataforseo.DataForSeoGoogleConnector())  # recensioni Google complete
    else:
        conns.append(GoogleConnector())                       # API ufficiale (max ~5)
    if os.environ.get("TRUSTPILOT_API_KEY"):
        conns.append(TrustpilotConnector())
    if trustpilot_public.enabled():
        conns.append(trustpilot_public.TrustpilotPublicConnector())
    if autoscout24.enabled():
        conns.append(autoscout24.AutoScout24Connector())      # automotive (concessionari)
    if certified.feedaty_enabled():
        conns.append(certified.FeedatyConnector())
    if certified.rv_enabled():
        conns.append(certified.RecensioniVerificateConnector())
    if facebook.enabled():
        conns.append(facebook.FacebookConnector())            # pagine di proprietà
    return conns


def _resolve_place_id(business):
    """Risolve il Google place_id (match esatto) da passare a DataForSEO."""
    try:
        if not business.google_place_id and os.environ.get("GOOGLE_MAPS_API_KEY"):
            pid = GoogleConnector().resolve_place_id(business)
            if pid:
                business.google_place_id = pid
    except Exception:
        pass
    return business


def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "azienda"


@app.get("/login")
def login():
    # login disattivato (nessuna credenziale OAuth) -> vai diretto alla home
    if not google_oauth.enabled():
        return redirect("/")
    owner, ok = current_owner()
    if ok:
        return redirect("/")
    tpl = _templates.get_template("login.html")
    return tpl.render(login_url=url_for("auth_login"))


@app.get("/auth/login")
def auth_login():
    if not google_oauth.enabled():
        return redirect("/")
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    return redirect(google_oauth.authorization_url(_base_url(), state))


@app.get("/auth/callback")
def auth_callback():
    if not google_oauth.enabled():
        return redirect("/")
    if request.args.get("error"):
        return _auth_error("Accesso annullato o negato da Google.")
    state = request.args.get("state")
    if not state or state != session.pop("oauth_state", None):
        return _auth_error("Sessione di login scaduta o non valida. Riprova.")
    code = request.args.get("code")
    if not code:
        return _auth_error("Codice di autorizzazione mancante.")
    try:
        profile = google_oauth.exchange_code(_base_url(), code)
    except Exception as e:  # noqa: BLE001
        return _auth_error(f"Errore nello scambio del token: {str(e)[:160]}")
    if not profile.get("sub"):
        return _auth_error("Profilo Google incompleto (manca l'identificativo).")
    db.upsert_user(profile["sub"], profile.get("email"), profile.get("name"),
                   datetime.now(timezone.utc))
    session["uid"] = profile["sub"]
    session["email"] = profile.get("email")
    session["name"] = profile.get("name")
    return redirect("/")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login" if google_oauth.enabled() else "/")


def _auth_error(msg: str):
    return (
        "<html><head><meta charset='utf-8'><title>Ekko · Login</title>"
        "<style>body{font-family:system-ui;max-width:520px;margin:80px auto;"
        "padding:0 20px;color:#1a1f2e;text-align:center}a{color:#2456e6}</style>"
        f"</head><body><h2>Accesso non riuscito</h2><p>{msg}</p>"
        "<p><a href='/login'>← Riprova ad accedere</a></p></body></html>"), 400


@app.get("/health")
def health():
    gw = AIGateway()
    return jsonify(status="ok",
                   ai={"available": gw.available(), "provider": gw.provider,
                       "model": gw.model},
                   connectors=[c.health() for c in default_connectors()])


@app.get("/")
@login_required
def home():
    from ekko.connectors import (autoscout24, certified, dataforseo,
                                 facebook, tripadvisor_dfs)
    gw = AIGateway()
    owner, _ = current_owner()
    tpl = _templates.get_template("search.html")
    return tpl.render(
        recent=db.list_businesses(owner_id=owner),
        google_on=bool(os.environ.get("GOOGLE_MAPS_API_KEY")) or dataforseo.enabled(),
        tp_on=bool(os.environ.get("TRUSTPILOT_API_KEY")),
        tp_public_on=trustpilot_public.enabled(),
        ta_on=tripadvisor_dfs.enabled(),
        as24_on=autoscout24.enabled(),
        feedaty_on=certified.feedaty_enabled(),
        rv_on=certified.rv_enabled(),
        fb_on=facebook.enabled(),
        ai_on=gw.available(),
        ai_label=f" · {gw.provider}" if gw.available() else "",
        auth_on=google_oauth.enabled(),
        user_name=session.get("name"),
        user_email=session.get("email"),
    )


@app.post("/search")
@login_required
def search():
    owner, _ = current_owner()
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect("/")
    business = BusinessRef(
        id=f"{owner}-{_slugify(name)}",  # namespace per-agenzia -> isolamento dati
        name=name,
        city=(request.form.get("city") or "").strip() or None,
        domain=(request.form.get("domain") or "").strip() or None,
        autoscout24_url=(request.form.get("autoscout24_url") or "").strip() or None,
    )
    try:
        _d = int(request.form.get("depth") or 0)
        if _d > 0:
            business.review_depth = max(10, min(_d, 4490))
    except (TypeError, ValueError):
        pass
    save_business(business, owner_id=owner)
    from ekko.connectors import dataforseo as _dfs
    from ekko.connectors import tripadvisor_dfs as _ta
    # 1) task asincroni DataForSEO (Google + TripAdvisor): partono in background
    if _dfs.enabled():
        tid = _dfs.post_task(business)
        if tid:
            business.dfs_task_id = tid
            business.dfs_pending = True
    if _ta.enabled():
        ta_tid = _ta.post_task(business)
        if ta_tid:
            business.ta_task_id = ta_tid
            business.ta_pending = True
    if business.dfs_pending or business.ta_pending:
        save_business(business)
    # 2) fonti veloci subito (Trustpilot pubblico / Google API), NON DataForSEO
    fast = [c for c in default_connectors()
            if type(c).__name__ != "DataForSeoGoogleConnector"]
    if fast:
        ingest(business, fast)
    # richiesta via fetch dalla home -> JSON per l'overlay con le progress bar
    if request.headers.get("X-Requested-With") == "fetch":
        sources = _source_states(business.id)
        return jsonify(ok=True, id=business.id, sources=sources,
                       all_done=all(s["state"] == "done" for s in sources) if sources else True,
                       dashboard_url=f"/businesses/{business.id}/dashboard")
    return redirect(f"/businesses/{business.id}/dashboard")


@app.post("/businesses/<business_id>/analyze")
@login_required
def analyze(business_id: str):
    """Endpoint AI stadio 3: recensioni filtrate (PII-free) -> findings+suggestions.
    Il body è la lista già filtrata lato client. Se l'AI non è configurata o
    fallisce, il client resta sul motore locale (degrada con grazia)."""
    _require_owner_of(business_id)
    gw = AIGateway()
    if not gw.available():
        return jsonify(ok=False, reason="ai_not_configured")
    payload = request.get_json(force=True) or {}
    rows = payload.get("rows", [])
    name = payload.get("business_name", business_id)
    if not rows:
        return jsonify(ok=False, reason="no_rows")
    try:
        result = gw.analyze(name, rows)
        return jsonify(ok=True, **result)
    except Exception as e:  # degrada con grazia -> il client usa il motore locale
        return jsonify(ok=False, reason="ai_error", detail=str(e)[:200])


@app.post("/businesses")
def create_business():
    business = BusinessRef.model_validate(request.get_json(force=True))
    save_business(business)
    return jsonify(created=business.id)


@app.post("/businesses/<business_id>/ingest")
def run_ingest(business_id: str):
    payload = request.get_json(silent=True)
    b = (BusinessRef.model_validate(payload) if payload
         else BusinessRef(id=business_id, name=business_id))
    if b.id != business_id:
        return jsonify(error="business.id non coincide con il path"), 400
    return jsonify(asdict(ingest(b, default_connectors())))


@app.get("/businesses/<business_id>/score")
@login_required
def get_score(business_id: str):
    _require_owner_of(business_id)
    breakdown = score_business(business_id)
    if breakdown.n_feedback == 0:
        return jsonify(error="Nessun feedback: eseguire prima l'ingestion"), 404
    return app.response_class(breakdown.model_dump_json(),
                              mimetype="application/json")


def _collect_dfs_if_ready(business_id: str) -> dict:
    """Ingerisce i task DataForSEO (Google e TripAdvisor) se pronti."""
    from ekko.connectors import dataforseo as _dfs
    from ekko.connectors import tripadvisor_dfs as _ta
    from ekko.connectors.base import ConnectorRun
    from ekko.core.sentiment import enrich_stage0
    payload = db.get_business_payload(business_id) or {}
    for pend_key, task_key, mod, total_attr in (
            ("dfs_pending", "dfs_task_id", _dfs, "total_reviews_google"),
            ("ta_pending", "ta_task_id", _ta, "total_reviews_tripadvisor")):
        if payload.get(pend_key) and payload.get(task_key):
            items, total = mod.collect(payload[task_key])
            if items is not None:  # task pronto -> ingerisci
                biz = BusinessRef.model_validate(payload)
                run = ConnectorRun()
                for fo in mod.normalize_items(items, biz, run):
                    db.insert_feedback(enrich_stage0(fo))
                setattr(biz, pend_key, False)
                if total:
                    setattr(biz, total_attr, int(total))
                db.upsert_business(biz)
                payload = db.get_business_payload(business_id) or payload
    return payload


def _source_states(business_id: str, payload: dict | None = None) -> list[dict]:
    """Stato per-fonte per le progress bar della home."""
    from ekko.connectors import (autoscout24, certified, dataforseo as _dfs,
                                 facebook, tripadvisor_dfs as _ta)
    payload = payload if payload is not None else (db.get_business_payload(business_id) or {})
    counts = db.count_by_source(business_id)
    out = []
    if _dfs.enabled():
        out.append({"key": "google", "label": "Google",
                    "state": "running" if payload.get("dfs_pending") else "done",
                    "count": counts.get("google", 0),
                    "total": payload.get("total_reviews_google")})
    elif os.environ.get("GOOGLE_MAPS_API_KEY"):
        out.append({"key": "google", "label": "Google", "state": "done",
                    "count": counts.get("google", 0), "total": None})
    if _ta.enabled():
        out.append({"key": "tripadvisor", "label": "TripAdvisor",
                    "state": "running" if payload.get("ta_pending") else "done",
                    "count": counts.get("tripadvisor", 0),
                    "total": payload.get("total_reviews_tripadvisor")})
    if os.environ.get("TRUSTPILOT_API_KEY") or trustpilot_public.enabled():
        out.append({"key": "trustpilot", "label": "Trustpilot", "state": "done",
                    "count": counts.get("trustpilot", 0), "total": None})
    if autoscout24.enabled():
        out.append({"key": "autoscout24", "label": "AutoScout24", "state": "done",
                    "count": counts.get("autoscout24", 0), "total": None})
    if certified.feedaty_enabled():
        out.append({"key": "feedaty", "label": "Feedaty", "state": "done",
                    "count": counts.get("feedaty", 0), "total": None})
    if certified.rv_enabled():
        out.append({"key": "recensioni_verificate", "label": "Recensioni Verificate",
                    "state": "done", "count": counts.get("recensioni_verificate", 0),
                    "total": None})
    if facebook.enabled():
        out.append({"key": "meta", "label": "Facebook", "state": "done",
                    "count": counts.get("meta", 0), "total": None})
    return out


@app.get("/businesses/<business_id>/progress")
@login_required
def get_progress(business_id: str):
    """Polling della home: raccoglie DataForSEO se pronto e riporta lo stato fonti."""
    _require_owner_of(business_id)
    payload = _collect_dfs_if_ready(business_id)
    sources = _source_states(business_id, payload)
    all_done = all(s["state"] == "done" for s in sources) if sources else True
    return jsonify(ok=True, sources=sources, all_done=all_done,
                   dashboard_url=f"/businesses/{business_id}/dashboard")


@app.get("/businesses/<business_id>/dashboard")
@login_required
def get_dashboard(business_id: str):
    _require_owner_of(business_id)
    payload = _collect_dfs_if_ready(business_id)
    pending = bool(payload.get("dfs_pending") and payload.get("dfs_task_id"))
    feedback = load_feedback(business_id)
    if not feedback and not pending:
        return jsonify(error="Nessun feedback: eseguire prima l'ingestion"), 404
    breakdown = score_business(business_id)
    return render_dashboard(payload.get("name") or business_id, breakdown, feedback,
                            business_id=business_id,
                            total_reviews=payload.get("total_reviews_google"),
                            pending=pending)


def render_dashboard(business_name: str, breakdown, feedback,
                     business_id: str | None = None, total_reviews=None,
                     pending: bool = False) -> str:
    """Dashboard interattiva self-contained: dati embedded, zero dipendenze esterne."""
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    data = {
        "generated_ts": now.isoformat(),
        "business_id": business_id or _slugify(business_name),
        "business_name": business_name,
        "total_reviews": total_reviews,
        "feedback": [
            {
                "d": f.published_at.isoformat(),
                "s": f.source.value,
                "st": round((f.rating or 0) * 5, 1),
                "sent": f.enrichment.sentiment if f.enrichment.sentiment is not None else 0,
                "topics": f.enrichment.topics,
                "rep": f.reply is not None,
                "txt": f.text,
            }
            for f in feedback
        ],
    }
    tpl = _templates.get_template("dashboard.html")
    return tpl.render(
        business_name=business_name,
        generated_at=now.strftime("%d/%m/%Y %H:%M UTC"),
        score=breakdown,
        total_reviews=total_reviews,
        analyzed_count=len(feedback),
        pending=pending,
        data_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
    )


@app.get("/businesses/<business_id>/report")
@login_required
def get_report(business_id: str):
    _require_owner_of(business_id)
    feedback = load_feedback(business_id)
    if not feedback:
        return jsonify(error="Nessun feedback: eseguire prima l'ingestion"), 404
    breakdown = score_business(business_id)
    return render_report(business_id, breakdown, feedback)


def render_report(business_id: str, breakdown, feedback) -> str:
    topics = Counter()
    neg_topics = Counter()
    for f in feedback:
        for t in f.enrichment.topics:
            topics[t] += 1
            if (f.enrichment.sentiment or 0) < -0.2:
                neg_topics[t] += 1
    recent = sorted(feedback, key=lambda f: f.published_at, reverse=True)[:12]
    tpl = _templates.get_template("report.html")
    return tpl.render(
        business_id=business_id, b=breakdown,
        top_topics=topics.most_common(8),
        neg_topics=neg_topics.most_common(5),
        recent=recent,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
