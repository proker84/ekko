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
import re
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


# --------------------------------------------------------------------------
# Step di identificazione: candidati per fonte con % di accuratezza.
# Ogni _match_* ritorna [{token,label,detail,conf}] (token = ciò che serve
# alla fonte: place_id, dominio, URL). Funzioni separate = testabili/stubbabili.
# --------------------------------------------------------------------------
def _match_google(name: str, city: str | None) -> list[dict]:
    import httpx as _hx
    from ekko.core import matching
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return []
    try:
        resp = _hx.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={"X-Goog-Api-Key": key,
                     "X-Goog-FieldMask":
                         "places.id,places.displayName,places.formattedAddress,"
                         "places.userRatingCount"},
            json={"textQuery": f"{name} {city or ''}".strip(),
                  "languageCode": "it", "maxResultCount": 5},
            timeout=10)
        resp.raise_for_status()
    except _hx.HTTPError:
        return []
    out = []
    for p in resp.json().get("places", [])[:5]:
        label = (p.get("displayName") or {}).get("text") or "?"
        detail = p.get("formattedAddress") or ""
        nrev = p.get("userRatingCount")
        if nrev:
            detail += f" · {nrev} recensioni"
        out.append({"token": p.get("id"), "label": label, "detail": detail,
                    "conf": matching.confidence(name, label, city, detail)})
    return sorted(out, key=lambda c: -c["conf"])


def _serp_urls(query: str, limit: int = 8) -> list[dict]:
    """Ricerca Google istantanea via DataForSEO SERP live: [{url,title}].
    È il motore che TROVA da solo le pagine delle aziende sulle piattaforme
    (TripAdvisor, AutoScout24, Trustpilot…) senza chiedere nulla all'utente."""
    import httpx as _hx
    auth = os.environ.get("DATAFORSEO_AUTH")
    if not auth:
        return []
    try:
        r = _hx.post(
            "https://api.dataforseo.com/v3/serp/google/organic/live/advanced",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/json"},
            json=[{"keyword": query, "location_name": "Italy",
                   "language_code": "it", "depth": 10}],
            timeout=20)
        r.raise_for_status()
        tasks = r.json().get("tasks") or []
        if not tasks or tasks[0].get("status_code") != 20000:
            return []
        items = ((tasks[0].get("result") or [{}])[0].get("items")) or []
    except (_hx.HTTPError, ValueError, IndexError):
        return []
    out = []
    for it in items:
        if it.get("type") != "organic":
            continue
        u, t = it.get("url"), it.get("title")
        if u and t:
            out.append({"url": u, "title": t})
        if len(out) >= limit:
            break
    return out


def _clean_serp_title(title: str) -> str:
    """'Rossi Auto - Recensioni | TripAdvisor' -> 'Rossi Auto'."""
    t = re.split(r"\s*[|–—·]\s*", title.strip())[0]
    t = re.sub(r"\s*[-:]?\s*(Recensioni|Reviews|Impressioni e valutazioni"
               r"|Leggi le recensioni)\b.*$", "", t, flags=re.I)
    return t.strip() or title.strip()


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _page_title(url: str) -> str | None:
    """Titolo della pagina (None = pagina inesistente/irraggiungibile)."""
    import httpx as _hx
    from ekko.connectors.pubscrape import UA
    try:
        r = _hx.get(url, headers={"User-Agent": UA, "Accept-Language": "it"},
                    timeout=8, follow_redirects=True)
        if r.status_code != 200:
            return None
        m = _TITLE_RE.search(r.text)
        if not m:
            return ""
        return re.split(r"\s*[|·–-]\s*", m.group(1).strip())[0]
    except _hx.HTTPError:
        return None


def _match_tripadvisor(name: str, city: str | None) -> list[dict]:
    """Trova la pagina TripAdvisor dell'azienda via ricerca SERP (autonoma)."""
    from ekko.core import matching
    from urllib.parse import urlparse
    q = f"site:tripadvisor.it {name} {city or ''}".strip()
    out = []
    for hit in _serp_urls(q):
        path = urlparse(hit["url"]).path.lstrip("/")
        # solo pagine-scheda (Review) — esclude liste/categorie
        if "_Review-" not in path:
            continue
        label = _clean_serp_title(hit["title"])
        out.append({"token": path, "label": label, "detail": hit["url"],
                    "conf": matching.confidence(name, label, city,
                                                hit["title"] + " " + hit["url"])})
    return sorted(out, key=lambda c: -c["conf"])[:5]


def _match_trustpilot(name: str, domain: str | None, city: str | None) -> list[dict]:
    from ekko.core import matching
    if domain:
        title = _page_title(f"https://it.trustpilot.com/review/{domain}")
        if title is None:
            return []
        conf = max(matching.confidence(name, title or domain, city)
                   if title else 75, 80)   # dominio fornito dall'utente
        return [{"token": domain, "label": title or domain,
                 "detail": f"trustpilot.com/review/{domain}", "conf": conf}]
    # nessun dominio: lo trova la SERP
    out = []
    for hit in _serp_urls(f"site:trustpilot.com {name}"):
        m = re.search(r"/review/([a-z0-9.\-]+)", hit["url"])
        if not m:
            continue
        dom = m.group(1)
        label = _clean_serp_title(hit["title"])
        out.append({"token": dom, "label": label,
                    "detail": f"trustpilot.com/review/{dom}",
                    "conf": matching.confidence(name, label, city,
                                                hit["title"] + " " + hit["url"])})
    return sorted(out, key=lambda c: -c["conf"])[:5]


def _match_autoscout24(name: str, url: str | None, city: str | None) -> list[dict]:
    from ekko.connectors.autoscout24 import BASE, _slug
    from ekko.core import matching
    if url:
        return [{"token": url, "label": "Pagina indicata da te",
                 "detail": url, "conf": 100}]
    out = []
    # 1) tentativo diretto sullo slug del nome
    guess = f"{BASE}/{_slug(name)}"
    title = _page_title(guess + "/recensioni")
    if title:
        out.append({"token": guess, "label": title, "detail": guess,
                    "conf": matching.confidence(name, title, city)})
    # 2) ricerca SERP autonoma sulle pagine concessionario
    for hit in _serp_urls(f"site:autoscout24.it/concessionari {name} {city or ''}".strip()):
        m = re.search(r"autoscout24\.it/concessionari/([a-z0-9\-]+)", hit["url"])
        if not m:
            continue
        u = f"{BASE}/{m.group(1)}"
        if any(c["token"] == u for c in out):
            continue
        label = _clean_serp_title(hit["title"])
        out.append({"token": u, "label": label, "detail": u,
                    "conf": matching.confidence(name, label, city,
                                                hit["title"] + " " + hit["url"])})
    return sorted(out, key=lambda c: -c["conf"])[:5]


def _match_certified(name: str, domain: str | None, which: str) -> list[dict]:
    from ekko.core import matching
    if not domain:
        return []
    if which == "feedaty":
        site = domain.split(".")[0]
        url = f"https://www.feedaty.com/feedaty/reviews/{site}"
    else:
        url = f"https://www.recensioni-verificate.com/recensioni-clienti/{domain}.html"
    title = _page_title(url)
    if title is None:
        return []
    return [{"token": domain, "label": title or domain, "detail": url,
             "conf": max(matching.confidence(name, title or domain), 80)}]


@app.post("/match")
@login_required
def match():
    """Identificazione azienda: candidati e confidenza per ogni fonte attiva."""
    from ekko.connectors import (autoscout24 as _as24, certified as _cert,
                                 dataforseo as _dfs, facebook as _fb,
                                 tripadvisor_dfs as _ta)
    from ekko.core.matching import AUTO_THRESHOLD
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, reason="no_name")
    city = (request.form.get("city") or "").strip() or None
    domain = (request.form.get("domain") or "").strip() or None
    as24_url = (request.form.get("autoscout24_url") or "").strip() or None

    def pack(key, label, cands, none_hint=None, keyword_mode=False):
        auto = bool(cands) and cands[0]["conf"] >= AUTO_THRESHOLD and \
            (len(cands) == 1 or cands[0]["conf"] - cands[1]["conf"] >= 10)
        return {"key": key, "label": label, "candidates": cands[:5],
                "auto": auto, "none_hint": none_hint,
                "keyword_mode": keyword_mode}

    sources = []
    if _dfs.enabled() or os.environ.get("GOOGLE_MAPS_API_KEY"):
        sources.append(pack("google", "Google", _match_google(name, city),
                            none_hint="Nessun risultato: precisa nome o città"))
    if _ta.enabled():
        ta_cands = _match_tripadvisor(name, city)
        # con candidati: scelta normale; senza: fallback ricerca per nome
        sources.append(pack("tripadvisor", "TripAdvisor", ta_cands,
                            keyword_mode=not ta_cands))
    if os.environ.get("TRUSTPILOT_API_KEY") or trustpilot_public.enabled():
        sources.append(pack("trustpilot", "Trustpilot",
                            _match_trustpilot(name, domain, city),
                            none_hint="Inserisci il dominio (es. azienda.it)"))
    if _as24.enabled():
        sources.append(pack("autoscout24", "AutoScout24",
                            _match_autoscout24(name, as24_url, city),
                            none_hint="Incolla l'URL della pagina concessionario"))
    if _cert.feedaty_enabled():
        sources.append(pack("feedaty", "Feedaty",
                            _match_certified(name, domain, "feedaty"),
                            none_hint="Serve il dominio; pagina certificato non trovata"))
    if _cert.rv_enabled():
        sources.append(pack("recensioni_verificate", "Recensioni Verificate",
                            _match_certified(name, domain, "rv"),
                            none_hint="Serve il dominio; pagina certificato non trovata"))
    if _fb.enabled():
        sources.append(pack("meta", "Facebook",
                            [{"token": "own", "label": "Pagina collegata (token)",
                              "detail": "", "conf": 100}]))
    return jsonify(ok=True, threshold=AUTO_THRESHOLD, sources=sources)


@app.post("/search")
@login_required
def search():
    owner, _ = current_owner()
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect("/")
    skips = {s.strip() for s in (request.form.get("skip_sources") or "").split(",")
             if s.strip()}
    business = BusinessRef(
        id=f"{owner}-{_slugify(name)}",  # namespace per-agenzia -> isolamento dati
        name=name,
        city=(request.form.get("city") or "").strip() or None,
        domain=(request.form.get("domain") or "").strip() or None,
        autoscout24_url=(request.form.get("autoscout24_url") or "").strip() or None,
        # identità confermate nello step di identificazione (se presenti)
        google_place_id=(request.form.get("google_place_id") or "").strip() or None,
        tripadvisor_url_path=(request.form.get("tripadvisor_url_path") or "").strip() or None,
        skipped_sources=sorted(skips),
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
    if _dfs.enabled() and "google" not in skips:
        tid = _dfs.post_task(business)
        if tid:
            business.dfs_task_id = tid
            business.dfs_pending = True
    if _ta.enabled() and "tripadvisor" not in skips:
        ta_tid = _ta.post_task(business)
        if ta_tid:
            business.ta_task_id = ta_tid
            business.ta_pending = True
    if business.dfs_pending or business.ta_pending:
        save_business(business)
    # 2) fonti veloci subito (scraper/API), escluse quelle saltate dall'utente
    fast = [c for c in default_connectors()
            if type(c).__name__ != "DataForSeoGoogleConnector"
            and c.source_name not in skips]
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
            items, total = mod.collect(payload[task_key],
                                       expect_name=payload.get("name"))
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
    skips = set(payload.get("skipped_sources") or [])
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
    return [s for s in out if s["key"] not in skips]


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
