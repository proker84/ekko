"""Collega pagina Facebook (OAuth 2.0 Meta) — recensioni delle pagine proprie.

LIMITE DI PIATTAFORMA (non aggirabile): Meta espone le recensioni SOLO delle
pagine di cui si possiede un token. Questo flusso serve quindi all'agenzia per
collegare le pagine dei PROPRI clienti: l'utente clicca "Collega Facebook",
autorizza, e Ekko salva il Page Access Token (di lunga durata) per ogni pagina.

Variabili d'ambiente:
  FACEBOOK_APP_ID / FACEBOOK_APP_SECRET   (app Meta — una tantum)
  EKKO_BASE_URL                            (per il redirect_uri)

Permessi richiesti: pages_show_list, pages_read_engagement,
pages_read_user_content (le recensioni), business_management (facoltativo).
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

import httpx

GRAPH = "https://graph.facebook.com/v19.0"
AUTH_URL = "https://www.facebook.com/v19.0/dialog/oauth"
SCOPES = ("pages_show_list,pages_read_engagement,pages_read_user_content")


def enabled() -> bool:
    return bool(os.environ.get("FACEBOOK_APP_ID")
                and os.environ.get("FACEBOOK_APP_SECRET"))


def redirect_uri(base_url: str) -> str:
    base = (os.environ.get("EKKO_BASE_URL") or base_url or "").rstrip("/")
    return f"{base}/auth/facebook/callback"


def authorization_url(base_url: str, state: str) -> str:
    params = {
        "client_id": os.environ.get("FACEBOOK_APP_ID", ""),
        "redirect_uri": redirect_uri(base_url),
        "state": state,
        "scope": SCOPES,
        "response_type": "code",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(base_url: str, code: str) -> str | None:
    """code -> user access token (breve durata)."""
    try:
        r = httpx.get(f"{GRAPH}/oauth/access_token", params={
            "client_id": os.environ.get("FACEBOOK_APP_ID", ""),
            "client_secret": os.environ.get("FACEBOOK_APP_SECRET", ""),
            "redirect_uri": redirect_uri(base_url),
            "code": code}, timeout=20)
        r.raise_for_status()
        return r.json().get("access_token")
    except httpx.HTTPError:
        return None


def long_lived(token: str) -> str:
    """Converte in token di lunga durata (~60 giorni)."""
    try:
        r = httpx.get(f"{GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": os.environ.get("FACEBOOK_APP_ID", ""),
            "client_secret": os.environ.get("FACEBOOK_APP_SECRET", ""),
            "fb_exchange_token": token}, timeout=20)
        r.raise_for_status()
        return r.json().get("access_token") or token
    except httpx.HTTPError:
        return token


def list_pages(user_token: str) -> list[dict]:
    """Pagine gestite dall'utente: [{id, name, access_token, category}].
    I Page Access Token derivati da un user token di lunga durata NON scadono."""
    out, url = [], f"{GRAPH}/me/accounts"
    params = {"access_token": user_token, "limit": 100,
              "fields": "id,name,category,access_token"}
    for _ in range(5):
        try:
            r = httpx.get(url, params=params, timeout=20)
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPError:
            break
        for p in body.get("data", []):
            if p.get("id") and p.get("access_token"):
                out.append({"id": p["id"], "name": p.get("name") or p["id"],
                            "category": p.get("category") or "",
                            "token": p["access_token"]})
        nxt = (body.get("paging") or {}).get("next")
        if not nxt:
            break
        url, params = nxt, {}
    return out
