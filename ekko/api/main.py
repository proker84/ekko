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
from ekko.auth import gbp_oauth, google_oauth
from ekko.connectors import gbp as gbp_api
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


def login_required_api(fn):
    """Variante per endpoint chiamati via fetch/form dal frontend (/match, /search):
    senza login risponde 401 JSON invece del redirect HTML, così il client
    può intercettare l'errore e mandare l'utente alla pagina di login."""
    @functools.wraps(fn)
    def _wrap(*a, **k):
        owner, ok = current_owner()
        if not ok:
            return jsonify(error="login_required",
                           login_url=url_for("login")), 401
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


def default_connectors(owner_id: str | None = None) -> list:
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
    if facebook.enabled(owner_id):
        conns.append(facebook.FacebookConnector(owner_id))    # pagine collegate
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


@app.get("/auth/facebook")
@login_required
def auth_facebook():
    """Avvia il collegamento delle pagine Facebook dell'agenzia."""
    from ekko.auth import facebook_oauth as fb
    if not fb.enabled():
        return _auth_error("Facebook non configurato: mancano FACEBOOK_APP_ID "
                           "e FACEBOOK_APP_SECRET.")
    state = secrets.token_urlsafe(24)
    session["fb_state"] = state
    return redirect(fb.authorization_url(_base_url(), state))


@app.get("/auth/facebook/callback")
@login_required
def auth_facebook_callback():
    from ekko.auth import facebook_oauth as fb
    owner, _ = current_owner()
    if request.args.get("error"):
        return _auth_error("Collegamento Facebook annullato.")
    state = request.args.get("state")
    if not state or state != session.pop("fb_state", None):
        return _auth_error("Sessione scaduta: riprova a collegare Facebook.")
    code = request.args.get("code")
    tok = fb.exchange_code(_base_url(), code) if code else None
    if not tok:
        return _auth_error("Scambio del token Facebook non riuscito.")
    pages = fb.list_pages(fb.long_lived(tok))
    if not pages:
        return _auth_error("Nessuna pagina Facebook trovata per questo account "
                           "(servono i permessi sulle pagine).")
    db.upsert_fb_pages(owner, pages, datetime.now(timezone.utc))
    return redirect("/?fb=" + str(len(pages)))


# --------------------------------------------------------------------------
# Google Business Profile (Fase 2): il cliente proprietario collega il suo
# account Google, scarica TUTTE le recensioni delle sue sedi e risponde con
# bozze AI che approva/modifica prima dell'invio (MAI invio automatico).
# --------------------------------------------------------------------------
@app.get("/gbp/connect")
@login_required
def gbp_connect():
    """Avvia l'OAuth GBP (scope business.manage, offline+consent)."""
    business_id = (request.args.get("business_id") or "").strip()
    if not business_id:
        abort(400)
    _require_owner_of(business_id)
    if not gbp_oauth.enabled():
        return _auth_error("Google OAuth non configurato: servono "
                           "GOOGLE_OAUTH_CLIENT_ID e GOOGLE_OAUTH_CLIENT_SECRET.")
    state = secrets.token_urlsafe(24)
    session["gbp_state"] = state
    session["gbp_business"] = business_id
    return redirect(gbp_oauth.authorization_url(_base_url(), state))


@app.get("/gbp/callback")
@login_required
def gbp_callback():
    """Callback OAuth GBP: scambia il code, salva i token per l'agenzia."""
    owner, _ = current_owner()
    business_id = session.pop("gbp_business", None)
    if request.args.get("error"):
        return _auth_error("Collegamento Google Business Profile annullato.")
    state = request.args.get("state")
    if not state or state != session.pop("gbp_state", None):
        return _auth_error("Sessione scaduta: riprova a collegare il profilo.")
    code = request.args.get("code")
    if not code:
        return _auth_error("Codice di autorizzazione mancante.")
    try:
        tok = gbp_oauth.exchange_code(_base_url(), code)
    except Exception as e:  # noqa: BLE001
        return _auth_error(f"Scambio del token non riuscito: {str(e)[:160]}")
    db.upsert_oauth_token(owner, gbp_api.PROVIDER, tok.get("access_token"),
                          tok.get("refresh_token"), tok.get("expires_at"),
                          tok.get("scopes"), datetime.now(timezone.utc))
    if business_id:
        return redirect(f"/businesses/{business_id}/dashboard?gbp=connected")
    return redirect("/?gbp=connected")


def _gbp_not_connected():
    return jsonify(ok=False, error="gbp_not_connected"), 409


def _gbp_not_linked():
    return jsonify(ok=False, error="gbp_not_linked"), 409


@app.get("/api/gbp/status/<business_id>")
@login_required
def gbp_status(business_id: str):
    """Stato GBP del business: connesso? collegato a una location? quali
    location sono disponibili (solo se connesso e non ancora collegato)."""
    owner = _require_owner_of(business_id)
    is_connected = gbp_api.connected(owner)
    link = db.get_gbp_link(business_id)
    settings = db.get_gbp_settings(business_id)
    locations = []
    if is_connected and not link:
        conn = gbp_api.GbpConnector(owner)
        try:
            for acc in conn.list_accounts(owner):
                for loc in conn.list_locations(owner, acc["name"]):
                    locations.append({
                        "account": acc["name"],
                        "name": loc.get("name") or "",
                        "title": loc.get("title") or "",
                        "address": gbp_api.format_address(loc),
                    })
        except Exception:  # quota 0 / token revocato: la UI mostra 0 sedi
            locations = []
    return jsonify(
        connected=is_connected,
        linked={"location": link["location_name"] if link else None,
                "title": link["location_title"] if link else None},
        locations=locations,
        auto_draft=bool(settings.get("auto_draft")),
    )


@app.post("/api/gbp/link/<business_id>")
@login_required
def gbp_link(business_id: str):
    """Collega il business Ekko a una location GBP scelta dall'utente."""
    owner = _require_owner_of(business_id)
    if not gbp_api.connected(owner):
        return _gbp_not_connected()
    payload = request.get_json(silent=True) or {}
    account = (payload.get("account") or "").strip()
    location = (payload.get("location") or "").strip()
    if not account or not location:
        return jsonify(ok=False, error="account e location obbligatori"), 400
    db.upsert_gbp_link(business_id, account, location,
                       (payload.get("title") or "").strip() or None,
                       datetime.now(timezone.utc))
    return jsonify(ok=True)


def _gbp_link_or_none(business_id: str, owner: str):
    """(link, errore JSON) — errore 409 se non connesso o non collegato."""
    if not gbp_api.connected(owner):
        return None, _gbp_not_connected()
    link = db.get_gbp_link(business_id)
    if not link:
        return None, _gbp_not_linked()
    return link, None


@app.post("/api/gbp/sync/<business_id>")
@login_required
def gbp_sync(business_id: str):
    """Scarica le recensioni della location collegata nel feedback store
    (stesso percorso della pipeline: stadio 0 + dedup su insert)."""
    from ekko.connectors.base import ConnectorRun
    from ekko.core.sentiment import enrich_stage0
    owner = _require_owner_of(business_id)
    link, err = _gbp_link_or_none(business_id, owner)
    if err:
        return err
    conn = gbp_api.GbpConnector(owner)
    try:
        raw = conn.fetch_reviews(owner, link["account_name"],
                                 link["location_name"])
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)[:200]), 502
    run = ConnectorRun()
    stored = duplicates = 0
    for rv in raw:
        fo = conn.normalize_review(rv, business_id, run)
        if db.insert_feedback(enrich_stage0(fo)):
            stored += 1
        else:
            duplicates += 1
    return jsonify(ok=True, fetched=len(raw), stored=stored,
                   duplicates=duplicates)


def _gbp_review_row(rv: dict, drafts: dict) -> dict:
    """Recensione v4 -> riga per la UI (rating 1..5, bozza se presente)."""
    rid = gbp_api.review_native_id(rv)
    reply = (rv.get("reviewReply") or {}).get("comment")
    return {
        "review_id": rid,
        "author": (rv.get("reviewer") or {}).get("displayName")
        or "Utente Google",
        "rating": gbp_api.star_value(rv.get("starRating")),
        "text": rv.get("comment") or "",
        "published_at": rv.get("createTime") or rv.get("updateTime"),
        "has_reply": bool(reply),
        "reply_text": reply,
        "draft": drafts.get(rid),
    }


@app.get("/api/gbp/reviews/<business_id>")
@login_required
def gbp_reviews(business_id: str):
    """Recensioni LIVE da GBP (has_reply sempre aggiornato) + bozze locali.
    ?only=unanswered filtra quelle ancora senza risposta."""
    owner = _require_owner_of(business_id)
    link, err = _gbp_link_or_none(business_id, owner)
    if err:
        return err
    conn = gbp_api.GbpConnector(owner)
    try:
        raw = conn.fetch_reviews(owner, link["account_name"],
                                 link["location_name"])
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)[:200]), 502
    drafts = db.list_reply_drafts(business_id)
    rows = [_gbp_review_row(rv, drafts) for rv in raw]
    if request.args.get("only") == "unanswered":
        rows = [r for r in rows if not r["has_reply"]]
    rows.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return jsonify(reviews=rows)


@app.post("/api/gbp/draft/<business_id>/<review_id>")
@login_required
def gbp_draft(business_id: str, review_id: str):
    """Genera la bozza AI per una recensione e la salva (stato 'draft').
    La bozza NON viene mai inviata da qui: l'utente la approva/modifica."""
    import json as _json
    owner = _require_owner_of(business_id)
    link, err = _gbp_link_or_none(business_id, owner)
    if err:
        return err
    conn = gbp_api.GbpConnector(owner)
    try:
        raw = conn.fetch_reviews(owner, link["account_name"],
                                 link["location_name"])
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)[:200]), 502
    rv = next((r for r in raw if gbp_api.review_native_id(r) == review_id),
              None)
    if rv is None:
        return jsonify(ok=False, error="review_not_found"), 404
    settings = db.get_gbp_settings(business_id)
    settings["author"] = (rv.get("reviewer") or {}).get("displayName") or ""
    name = db.get_business_name(business_id) or link.get("location_title") \
        or business_id
    text = AIGateway().generate_review_reply(
        name, rv.get("comment"), gbp_api.star_value(rv.get("starRating")),
        settings)
    db.upsert_reply_draft(business_id, review_id,
                          _json.dumps(rv, ensure_ascii=False), text,
                          datetime.now(timezone.utc))
    return jsonify(draft={"text": text, "status": "draft"})


@app.post("/api/gbp/draft/<business_id>/<review_id>/save")
@login_required
def gbp_draft_save(business_id: str, review_id: str):
    """Salva la bozza modificata dall'utente (resta in stato 'draft')."""
    _require_owner_of(business_id)
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="testo vuoto"), 400
    db.upsert_reply_draft(business_id, review_id, None, text,
                          datetime.now(timezone.utc))
    return jsonify(ok=True)


@app.post("/api/gbp/reply/<business_id>/<review_id>")
@login_required
def gbp_reply(business_id: str, review_id: str):
    """Invia la risposta APPROVATA dall'utente via GBP e marca 'sent'."""
    owner = _require_owner_of(business_id)
    link, err = _gbp_link_or_none(business_id, owner)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="testo vuoto"), 400
    conn = gbp_api.GbpConnector(owner)
    ok, msg = conn.send_reply(owner, link["account_name"],
                              link["location_name"], review_id, text)
    if not ok:
        return jsonify(ok=False, error=msg), 502
    db.mark_reply_sent(business_id, review_id, text,
                       datetime.now(timezone.utc))
    return jsonify(ok=True)


@app.get("/api/gbp/settings/<business_id>")
@login_required
def gbp_settings_get(business_id: str):
    _require_owner_of(business_id)
    s = db.get_gbp_settings(business_id)
    s["auto_draft"] = bool(s.get("auto_draft"))
    return jsonify(**s)


@app.post("/api/gbp/settings/<business_id>")
@login_required
def gbp_settings_post(business_id: str):
    _require_owner_of(business_id)
    payload = request.get_json(silent=True) or {}
    db.upsert_gbp_settings(business_id, payload)
    return jsonify(ok=True)


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
def home():
    # Home PUBBLICA: anche senza login si vede il motore di ricerca; le azioni
    # che avviano lavoro (/match, /search) restano protette e rispondono 401.
    from ekko.connectors import (autoscout24, certified, dataforseo,
                                 facebook, facebook_public, tripadvisor_dfs)
    gw = AIGateway()
    owner, logged = current_owner()   # owner=None se non loggato (OAuth attivo)
    tpl = _templates.get_template("search.html")
    return tpl.render(
        # senza owner niente "recenti": nessun dato altrui esposto agli anonimi
        recent=db.list_businesses(owner_id=owner) if owner else [],
        google_on=bool(os.environ.get("GOOGLE_MAPS_API_KEY")) or dataforseo.enabled(),
        tp_on=bool(os.environ.get("TRUSTPILOT_API_KEY")),
        tp_public_on=trustpilot_public.enabled(),
        ta_on=tripadvisor_dfs.enabled(),
        as24_on=autoscout24.enabled(),
        feedaty_on=certified.feedaty_enabled(),
        rv_on=certified.rv_enabled(),
        fb_on=facebook_public.enabled() or facebook.enabled(owner),
        fb_direct=facebook_public.enabled(),
        fb_connectable=facebook.connectable(),
        fb_pages=len(db.list_fb_pages(owner)) if owner else 0,
        ai_on=gw.available(),
        ai_label=f" · {gw.provider}" if gw.available() else "",
        auth_on=google_oauth.enabled(),
        user_name=session.get("name"),
        # --- contratto con il frontend (home pubblica + login in pagina) ---
        logged=logged,                                        # bool
        user_email=session.get("email") or session.get("name"),  # str | None
        login_url=url_for("login"),                           # flusso di login esistente
        logout_url=url_for("logout"),                         # route /logout esistente
    )


# --------------------------------------------------------------------------
# Step di identificazione: candidati per fonte con % di accuratezza.
# Ogni _match_* ritorna [{token,label,detail,conf}] (token = ciò che serve
# alla fonte: place_id, dominio, URL). Funzioni separate = testabili/stubbabili.
# --------------------------------------------------------------------------
def _match_google_dfs(name: str, city: str | None) -> list[dict]:
    """Candidati Google via DataForSEO Maps (live).

    FONDAMENTALE: il `place_id` restituito qui è nel namespace di DataForSEO
    ("Gh…"), lo stesso accettato dall'API recensioni — quindi il match è
    ESATTO e non dipende più da una keyword indovinata (era la causa dei
    task Google a 0 recensioni).
    """
    import httpx as _hx
    from ekko.core import matching
    auth = os.environ.get("DATAFORSEO_AUTH")
    if not auth:
        return []
    try:
        r = _hx.post(
            "https://api.dataforseo.com/v3/serp/google/maps/live/advanced",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/json"},
            json=[{"keyword": f"{name} {city or ''}".strip(),
                   "location_name": "Italy", "language_code": "it",
                   "depth": 20}],
            timeout=45)
        r.raise_for_status()
        tasks = r.json().get("tasks") or []
        if not tasks or tasks[0].get("status_code") != 20000:
            return []
        items = ((tasks[0].get("result") or [{}])[0].get("items")) or []
    except (_hx.HTTPError, ValueError, IndexError):
        return []
    out = []
    for it in items:
        pid = it.get("place_id")
        title = it.get("title")
        if not (pid and title):
            continue
        addr = it.get("address") or ""
        rating = it.get("rating") or {}
        votes = rating.get("votes_count")
        detail = addr + (f" · {votes} recensioni" if votes else "")
        if rating.get("value"):
            detail += f" · ★ {rating['value']}"
        out.append({"token": pid, "label": title, "detail": detail,
                    "dfs": True, "reviews": votes or 0,
                    "conf": matching.confidence(name, title, city, addr)})
    return sorted(out, key=lambda c: (-c["conf"], -c["reviews"]))[:10]


def _match_google_places(name: str, city: str | None) -> list[dict]:
    """Fallback: Google Places API (usato solo senza credenziali DataForSEO)."""
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
                  "languageCode": "it", "maxResultCount": 20},
            timeout=12)
        resp.raise_for_status()
    except _hx.HTTPError:
        return []
    out = []
    for p in resp.json().get("places", [])[:20]:
        label = (p.get("displayName") or {}).get("text") or "?"
        detail = p.get("formattedAddress") or ""
        nrev = p.get("userRatingCount")
        if nrev:
            detail += f" · {nrev} recensioni"
        out.append({"token": p.get("id"), "label": label, "detail": detail,
                    "dfs": False,
                    "conf": matching.confidence(name, label, city, detail)})
    return sorted(out, key=lambda c: -c["conf"])


def _match_google(name: str, city: str | None) -> list[dict]:
    """Preferisce DataForSEO Maps (place_id compatibile con l'API recensioni);
    ripiega su Google Places solo se DataForSEO non è configurato."""
    return _match_google_dfs(name, city) or _match_google_places(name, city)


def _serp_urls(query: str, limit: int = 8) -> list[dict]:
    """Ricerca web istantanea: [{url,title}].
    È il motore che TROVA da solo le pagine delle aziende sulle piattaforme
    (TripAdvisor, AutoScout24, Trustpilot…) senza chiedere nulla all'utente.

    Primario: ricerca web gratuita (DuckDuckGo con fallback Bing) — zero
    costi e zero credenziali, quindi il discovery funziona SEMPRE, anche
    senza DATAFORSEO_AUTH. Ultima spiaggia: la vecchia SERP live DataForSEO
    (a pagamento), solo se riattivata esplicitamente con EKKO_USE_DFS_SERP=1
    e con le credenziali presenti."""
    from ekko.connectors import websearch
    try:
        hits = websearch.search(query, num=limit)
    except Exception:
        hits = []
    if hits:
        return hits[:limit]
    if os.environ.get("EKKO_USE_DFS_SERP") == "1" and \
            os.environ.get("DATAFORSEO_AUTH"):
        return _serp_urls_dfs(query, limit)
    return []


def _serp_urls_dfs(query: str, limit: int = 8) -> list[dict]:
    """Ultima spiaggia: SERP Google via DataForSEO live (A PAGAMENTO).
    Usata solo se la ricerca gratuita non trova nulla, EKKO_USE_DFS_SERP=1
    e DATAFORSEO_AUTH è impostata (vedi _serp_urls)."""
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
    for hit in _serp_urls(q, limit=12):
        path = urlparse(hit["url"]).path.lstrip("/")
        # solo pagine-scheda (Review) — esclude liste/categorie
        if "_Review-" not in path:
            continue
        label = _clean_serp_title(hit["title"])
        out.append({"token": path, "label": label, "detail": hit["url"],
                    "conf": matching.confidence(name, label, city,
                                                hit["title"] + " " + hit["url"])})
    return sorted(out, key=lambda c: -c["conf"])[:10]


def _match_facebook_public(name: str, city: str | None) -> list[dict]:
    """Trova la pagina Facebook dell'azienda via SERP: nessun login richiesto."""
    from ekko.core import matching
    out = []
    for hit in _serp_urls(f"site:facebook.com {name} {city or ''}".strip(), limit=12):
        u = hit["url"]
        m = re.search(r"facebook\.com/(?:pg/)?([A-Za-z0-9_.\-]+)", u)
        if not m or m.group(1).lower() in (
                "profile.php", "people", "groups", "events", "marketplace",
                "watch", "photo", "share", "story.php", "permalink.php"):
            continue
        page_url = f"https://www.facebook.com/{m.group(1)}"
        if any(c["token"] == page_url for c in out):
            continue
        label = _clean_serp_title(hit["title"])
        out.append({"token": page_url, "label": label, "detail": page_url,
                    "conf": matching.confidence(name, label, city,
                                                hit["title"] + " " + u)})
    return sorted(out, key=lambda c: -c["conf"])[:10]


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
    for hit in _serp_urls(f"site:trustpilot.com {name}", limit=12):
        m = re.search(r"/review/([a-z0-9.\-]+)", hit["url"])
        if not m:
            continue
        dom = m.group(1)
        label = _clean_serp_title(hit["title"])
        out.append({"token": dom, "label": label,
                    "detail": f"trustpilot.com/review/{dom}",
                    "conf": matching.confidence(name, label, city,
                                                hit["title"] + " " + hit["url"])})
    return sorted(out, key=lambda c: -c["conf"])[:10]


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
    for hit in _serp_urls(f"site:autoscout24.it/concessionari {name} {city or ''}".strip(), limit=12):
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
    return sorted(out, key=lambda c: -c["conf"])[:10]


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
@login_required_api          # chiamata via fetch: 401 JSON, non redirect HTML
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
        # fino a 10 candidati: le catene (concessionarie multi-marca) hanno
        # molte sedi con lo stesso nome — la scelta giusta può non essere top-3
        return {"key": key, "label": label, "candidates": cands[:10],
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
    from ekko.connectors import facebook_public as _fbp
    owner_now, _ = current_owner()
    if _fbp.enabled():
        # via provider dati: nessun accesso richiesto all'azienda analizzata
        sources.append(pack("meta", "Facebook",
                            _match_facebook_public(name, city),
                            none_hint="Pagina Facebook non trovata per questo nome"))
    elif _fb.enabled(owner_now):
        from ekko.storage import db as _db
        pages = _db.list_fb_pages(owner_now) if owner_now else []
        from ekko.core.matching import confidence as _conf
        cands = [{"token": p["id"], "label": p["name"] or p["id"],
                  "detail": "pagina collegata",
                  "conf": _conf(name, p.get("name") or "")} for p in pages]
        if not cands and os.environ.get("FACEBOOK_PAGE_TOKEN"):
            cands = [{"token": "own", "label": "Pagina collegata (token)",
                      "detail": "", "conf": 100}]
        sources.append(pack("meta", "Facebook", sorted(
            cands, key=lambda c: -c["conf"])[:10],
            none_hint="Nessuna pagina collegata corrisponde a questo nome"))
    return jsonify(ok=True, threshold=AUTO_THRESHOLD, sources=sources)


@app.post("/search")
@login_required_api          # chiamata via fetch/form: 401 JSON, non redirect HTML
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
        google_match_name=(request.form.get("google_label") or "").strip() or None,
        tripadvisor_url_path=(request.form.get("tripadvisor_url_path") or "").strip() or None,
        facebook_url=(request.form.get("facebook_url") or "").strip() or None,
        skipped_sources=sorted(skips),
        # "È la mia azienda": sblocca il collegamento Google Business Profile
        is_own=(request.form.get("is_own") or "").strip() == "1",
    )
    try:
        _d = int(request.form.get("depth") or 0)
        if _d > 0:
            business.review_depth = max(10, min(_d, 4490))
    except (TypeError, ValueError):
        pass
    # GRUPPI/CATENE: liste di sedi scelte nello step di identificazione
    def _multi(field: str) -> list[str]:
        import json as _j
        raw = (request.form.get(field) or "").strip()
        if not raw:
            return []
        try:
            vals = _j.loads(raw)
            return [str(v).strip() for v in vals if str(v).strip()]
        except ValueError:
            return [raw]

    g_labels = _multi("google_labels")
    ta_paths = _multi("tripadvisor_url_paths")
    as24_urls = _multi("autoscout24_urls")
    if as24_urls:
        business.autoscout24_urls = as24_urls
        business.autoscout24_url = as24_urls[0]
    save_business(business, owner_id=owner)

    from ekko.connectors import dataforseo as _dfs
    from ekko.connectors import tripadvisor_dfs as _ta
    # 1) task asincroni DataForSEO: UNO PER SEDE (Google + TripAdvisor)
    if _dfs.enabled() and "google" not in skips:
        g_pids = _multi("google_dfs_place_ids")   # id nel namespace DataForSEO
        labels = g_labels or [business.google_match_name or business.name]
        multi = len(labels) > 1 or len(g_pids) > 1
        n = max(len(labels), len(g_pids))
        for i in range(n):
            lbl = labels[i] if i < len(labels) else None
            pid = g_pids[i] if i < len(g_pids) else None
            tid = _dfs.post_task(business, keyword_override=lbl, place_id=pid)
            if tid:
                business.dfs_tasks.append({
                    "id": tid, "label": lbl if multi else None,
                    "place_id": pid, "keyword": lbl,
                    "pending": True, "total": None, "retried": False})
        if business.dfs_tasks:
            business.dfs_task_id = business.dfs_tasks[0]["id"]   # compat
            business.dfs_pending = True
    if _ta.enabled() and "tripadvisor" not in skips:
        paths = ta_paths or ([business.tripadvisor_url_path]
                             if business.tripadvisor_url_path else [None])
        multi_ta = len([p for p in paths if p]) > 1
        for p in paths:
            ta_tid = _ta.post_task(business, url_path_override=p)
            if ta_tid:
                business.ta_tasks.append({
                    "id": ta_tid, "label": (p or "")[:60] if multi_ta else None,
                    "pending": True, "total": None})
        if business.ta_tasks:
            business.ta_task_id = business.ta_tasks[0]["id"]     # compat
            business.ta_pending = True
    from ekko.connectors import facebook_public as _fbp
    if _fbp.enabled() and "meta" not in skips:
        fb_urls = _multi("facebook_urls") or ([business.facebook_url]
                                              if business.facebook_url else [])
        multi_fb = len(fb_urls) > 1
        for u in fb_urls:
            sid = _fbp.post_task(business, url_override=u)
            if sid:
                business.fb_tasks.append({
                    "id": sid, "label": u.rstrip("/").split("/")[-1] if multi_fb else None,
                    "pending": True, "total": None})
        if business.fb_tasks:
            business.fb_pending = True
    if business.dfs_tasks or business.ta_tasks or business.fb_tasks:
        save_business(business)
    # 2) fonti veloci subito (scraper/API), escluse quelle saltate dall'utente
    fast = [c for c in default_connectors(owner)
            if type(c).__name__ != "DataForSeoGoogleConnector"
            and c.source_name not in skips]
    if fast:
        ingest(business, fast)
    # richiesta via fetch dalla home -> JSON per l'overlay con le progress bar
    if request.headers.get("X-Requested-With") == "fetch":
        sources = _source_states(business.id, owner_id=owner)
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
    """Ingerisce i task DataForSEO pronti — UNO PER SEDE (gruppi/catene)."""
    from ekko.connectors import dataforseo as _dfs
    from ekko.connectors import tripadvisor_dfs as _ta
    from ekko.connectors.base import ConnectorRun
    from ekko.core.sentiment import enrich_stage0
    payload = db.get_business_payload(business_id) or {}
    biz = BusinessRef.model_validate(payload)
    changed = False

    from ekko.connectors import facebook_public as _fbp
    for tasks_attr, pend_attr, mod, total_attr in (
            ("dfs_tasks", "dfs_pending", _dfs, "total_reviews_google"),
            ("ta_tasks", "ta_pending", _ta, "total_reviews_tripadvisor"),
            ("fb_tasks", "fb_pending", _fbp, "total_reviews_facebook")):
        tasks = getattr(biz, tasks_attr) or []
        # compat: business creati prima del multi-sede
        legacy_id_attr = ("dfs_task_id" if mod is _dfs
                          else "ta_task_id" if mod is _ta else None)
        if not tasks and legacy_id_attr and getattr(biz, pend_attr) and \
                getattr(biz, legacy_id_attr):
            tasks = [{"id": getattr(biz, legacy_id_attr),
                      "label": None, "pending": True, "total": None,
                      "retried": False}]
            setattr(biz, tasks_attr, tasks)
            changed = True
        grand_total = 0
        for t in tasks:
            if not t.get("pending"):
                grand_total += t.get("total") or 0
                continue
            items, total = mod.collect(t["id"], expect_name=biz.name)
            if items is None:            # ancora in coda
                continue
            run = ConnectorRun()
            for fo in mod.normalize_items(items, biz, run,
                                          location=t.get("label")):
                db.insert_feedback(enrich_stage0(fo))
            t["pending"] = False
            t["total"] = int(total) if total else 0
            grand_total += t["total"]
            changed = True
            # AUTO-RECUPERO Google: sede andata a vuoto -> un solo nuovo
            # tentativo con il nome "grezzo" dell'azienda.
            if mod is _dfs and run.fetched == 0 and not t.get("retried"):
                t["retried"] = True
                # se avevamo usato il place_id, riprova con la keyword (e
                # viceversa): una delle due vie porta quasi sempre a casa
                if t.get("place_id"):
                    new_tid = _dfs.post_task(
                        biz, keyword_override=t.get("keyword") or biz.name)
                else:
                    new_tid = _dfs.post_task(biz, keyword_override=biz.name)
                if new_tid:
                    t.update(id=new_tid, pending=True, total=None,
                             place_id=None)
        if grand_total:
            setattr(biz, total_attr, grand_total)
        still = any(t.get("pending") for t in tasks)
        if getattr(biz, pend_attr) != still:
            setattr(biz, pend_attr, still)
            changed = True
    if changed:
        db.upsert_business(biz)
        payload = db.get_business_payload(business_id) or payload
    return payload


def _source_states(business_id: str, payload: dict | None = None,
                   owner_id: str | None = None) -> list[dict]:
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
    from ekko.connectors import facebook_public as _fbp
    if _fbp.enabled():
        out.append({"key": "meta", "label": "Facebook",
                    "state": "running" if payload.get("fb_pending") else "done",
                    "count": counts.get("meta", 0),
                    "total": payload.get("total_reviews_facebook")})
    elif facebook.enabled(owner_id):
        out.append({"key": "meta", "label": "Facebook", "state": "done",
                    "count": counts.get("meta", 0), "total": None})
    return [s for s in out if s["key"] not in skips]


@app.get("/businesses/<business_id>/progress")
@login_required
def get_progress(business_id: str):
    """Polling della home: raccoglie DataForSEO se pronto e riporta lo stato fonti."""
    _require_owner_of(business_id)
    payload = _collect_dfs_if_ready(business_id)
    owner, _ = current_owner()
    sources = _source_states(business_id, payload, owner_id=owner)
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
                            pending=pending,
                            is_own=bool(payload.get("is_own")))


def render_dashboard(business_name: str, breakdown, feedback,
                     business_id: str | None = None, total_reviews=None,
                     pending: bool = False, is_own: bool = False) -> str:
    """Dashboard interattiva self-contained: dati embedded, zero dipendenze esterne."""
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    data = {
        "generated_ts": now.isoformat(),
        "business_id": business_id or _slugify(business_name),
        "business_name": business_name,
        "total_reviews": total_reviews,
        # attività di proprietà dell'utente: il frontend apre in automatico
        # la sezione "Risposte Google" quando DATA.is_own è true
        "is_own": is_own,
        "feedback": [
            {
                "d": f.published_at.isoformat(),
                "s": f.source.value,
                "st": round((f.rating or 0) * 5, 1),
                "sent": f.enrichment.sentiment if f.enrichment.sentiment is not None else 0,
                "topics": f.enrichment.topics,
                "rep": f.reply is not None,
                "loc": f.location,          # sede (gruppi multi-sede)
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
