"""Accedi con Google (OAuth 2.0, flusso server-side "authorization code").

Perché server-side e non il pulsante JS di Google Identity:
  - una sola dipendenza (httpx, già presente), zero SDK lato browser;
  - il segreto del client resta sul server;
  - l'`id_token` arriva DIRETTAMENTE dall'endpoint token di Google su TLS,
    quindi (come da doc Google) possiamo leggerne il payload senza verificare
    la firma: non è passato dal browser dell'utente.

Variabili d'ambiente:
  GOOGLE_OAUTH_CLIENT_ID       (obbligatoria per abilitare il login)
  GOOGLE_OAUTH_CLIENT_SECRET   (obbligatoria per abilitare il login)
  EKKO_BASE_URL                (es. https://ekko-l58k.onrender.com) — opzionale;
                               se assente si deriva dalla richiesta.
"""
from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "openid email profile"


def enabled() -> bool:
    """Login attivo solo se sono configurate entrambe le credenziali."""
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
                and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def client_id() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")


def redirect_uri(base_url: str) -> str:
    """URI di callback esatto, deve combaciare con quello registrato in console."""
    base = (os.environ.get("EKKO_BASE_URL") or base_url or "").rstrip("/")
    return f"{base}/auth/callback"


def authorization_url(base_url: str, state: str) -> str:
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(base_url),
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _decode_id_token(id_token: str) -> dict:
    """Legge il payload del JWT (senza verifica firma: arriva da Google su TLS)."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("id_token malformato")
    return json.loads(_b64url_decode(parts[1]))


def exchange_code(base_url: str, code: str) -> dict:
    """Scambia il `code` con i token e restituisce il profilo utente.

    Ritorna: {sub, email, name, picture} — 'sub' è l'ID Google stabile.
    """
    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id(),
            "client_secret": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            "redirect_uri": redirect_uri(base_url),
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    resp.raise_for_status()
    tok = resp.json()
    claims = _decode_id_token(tok["id_token"])
    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name") or (claims.get("email") or "").split("@")[0],
        "picture": claims.get("picture"),
        "email_verified": claims.get("email_verified", False),
    }
