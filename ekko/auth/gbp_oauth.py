"""Collegamento Google Business Profile (OAuth 2.0) — flusso SEPARATO dal login.

Il cliente che POSSIEDE profili Google Business collega il proprio account:
da lì Ekko scarica gratuitamente TUTTE le recensioni delle sue sedi (corsia A,
official_api) e può pubblicare le risposte approvate dall'utente.

Riusa le stesse credenziali del login "Accedi con Google"
(GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET) ma con:
  - scope aggiuntivo https://www.googleapis.com/auth/business.manage;
  - access_type=offline + prompt=consent per ottenere SEMPRE il refresh_token
    (serve a sincronizzare le recensioni anche quando l'access token scade).

ATTENZIONE: l'accesso alla Google Business Profile API richiede l'approvazione
di Google (quota iniziale 0) — passi operativi in docs/GBP_SETUP.md.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from . import google_oauth

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/business.manage"


def enabled() -> bool:
    """Il collegamento GBP usa le stesse credenziali OAuth del login."""
    return google_oauth.enabled()


def redirect_uri(base_url: str) -> str:
    """URI di callback esatto, da registrare in Google Cloud Console."""
    base = (os.environ.get("EKKO_BASE_URL") or base_url or "").rstrip("/")
    return f"{base}/gbp/callback"


def authorization_url(base_url: str, state: str) -> str:
    params = {
        "client_id": google_oauth.client_id(),
        "redirect_uri": redirect_uri(base_url),
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        # offline + consent: Google restituisce il refresh_token a ogni giro
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _expires_at(expires_in: int | None) -> str:
    """Istante di scadenza (ISO, UTC) con 60s di margine di sicurezza."""
    secs = max(0, int(expires_in or 0) - 60)
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()


def exchange_code(base_url: str, code: str) -> dict:
    """Scambia il `code` con i token GBP.

    Ritorna: {access_token, refresh_token, expires_at, scopes}.
    """
    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": google_oauth.client_id(),
            "client_secret": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            "redirect_uri": redirect_uri(base_url),
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    resp.raise_for_status()
    tok = resp.json()
    return {
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_at": _expires_at(tok.get("expires_in")),
        "scopes": tok.get("scope") or SCOPE,
    }


def refresh_access_token(refresh_token: str) -> dict:
    """Rinnova l'access token con il refresh_token (grant refresh_token).

    Ritorna: {access_token, expires_at}. Solleva httpx.HTTPStatusError se
    Google rifiuta (es. consenso revocato dall'utente).
    """
    resp = httpx.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": google_oauth.client_id(),
            "client_secret": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    resp.raise_for_status()
    tok = resp.json()
    return {
        "access_token": tok.get("access_token"),
        "expires_at": _expires_at(tok.get("expires_in")),
    }
