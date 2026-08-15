"""OAuth 2.0 per le API private di Trustpilot — grant type Client Credentials.

Doc Trustpilot:
- API pubbliche: header `apikey: {key}` (nessun token). MAI in query string.
- API private:  OAuth 2.0. access_token valido 100h, refresh_token 30 giorni.
  Client Credentials usa API Key + Secret del dominio per ottenere il token.
- ⚠ NON richiedere un nuovo access token prima della scadenza: refresh troppo
  frequenti → HTTP 429. Perciò qui il token è messo in cache su file con il suo
  `expires_in` e si rinnova solo quando è effettivamente scaduto.

Config (.env):
  TRUSTPILOT_API_KEY      (Client ID)   — serve già per le API pubbliche
  TRUSTPILOT_API_SECRET   (Client Secret) — abilita le API private
  EKKO_TP_TOKEN_CACHE     percorso file cache token (default .ekko_tp_token.json)
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import httpx

TOKEN_URL = ("https://api.trustpilot.com/v1/oauth/"
             "oauth-business-users-for-applications/accesstoken")
REFRESH_URL = ("https://api.trustpilot.com/v1/oauth/"
               "oauth-business-users-for-applications/refresh")
REVOKE_URL = ("https://api.trustpilot.com/v1/oauth/"
              "oauth-business-users-for-applications/revoke")

# Margine di sicurezza: consideriamo il token scaduto un po' prima, ma senza
# rinnovare "troppo spesso" (la doc penalizza i refresh anticipati con 429).
EXPIRY_SKEW_S = 300


class TrustpilotAuthError(RuntimeError):
    pass


class TrustpilotAuth:
    """Gestore token OAuth con cache su file. Thread-unsafe (skeleton);
    in Fase 1 il token vive nel secret manager con lock distribuito."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 cache_path: str | None = None):
        self.api_key = api_key or os.environ.get("TRUSTPILOT_API_KEY")
        self.api_secret = api_secret or os.environ.get("TRUSTPILOT_API_SECRET")
        self.cache_path = Path(
            cache_path or os.environ.get("EKKO_TP_TOKEN_CACHE",
                                         ".ekko_tp_token.json"))

    def available(self) -> bool:
        """True se possiamo fare OAuth private (servono sia key sia secret)."""
        return bool(self.api_key and self.api_secret)

    # ------------------------------------------------------------------ #
    def _basic_header(self) -> str:
        raw = f"{self.api_key}:{self.api_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _load_cache(self) -> dict | None:
        if not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text())
            return data if data.get("client_id") == self.api_key else None
        except (json.JSONDecodeError, OSError):
            return None

    def _save_cache(self, tok: dict, now: float) -> None:
        payload = {
            "client_id": self.api_key,
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            # expires_in può arrivare come stringa: normalizziamo a float
            "expires_at": now + float(tok.get("expires_in", 360000)),
        }
        try:
            self.cache_path.write_text(json.dumps(payload))
            os.chmod(self.cache_path, 0o600)  # il token è un segreto
        except OSError:
            pass  # cache best-effort

    def _valid_cached(self, now: float) -> str | None:
        cache = self._load_cache()
        if cache and cache.get("access_token") and \
                cache.get("expires_at", 0) - EXPIRY_SKEW_S > now:
            return cache["access_token"]
        return None

    # ------------------------------------------------------------------ #
    def get_access_token(self, force: bool = False,
                         _now: float | None = None) -> str:
        """Ritorna un access token valido, dalla cache o richiedendolo.
        Rispetta la regola: non rinnovare finché non è scaduto."""
        if not self.available():
            raise TrustpilotAuthError(
                "OAuth privato non configurato: servono TRUSTPILOT_API_KEY e "
                "TRUSTPILOT_API_SECRET")
        now = _now if _now is not None else time.time()
        if not force:
            cached = self._valid_cached(now)
            if cached:
                return cached
        # cache scaduta/assente → prova refresh, poi client_credentials
        cache = self._load_cache()
        if cache and cache.get("refresh_token"):
            try:
                return self._refresh(cache["refresh_token"], now)
            except TrustpilotAuthError:
                pass  # refresh fallito/scaduto → nuovo client_credentials
        return self._client_credentials(now)

    def _client_credentials(self, now: float) -> str:
        resp = httpx.post(
            TOKEN_URL,
            headers={"Authorization": self._basic_header(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        return self._handle_token_response(resp, now)

    def _refresh(self, refresh_token: str, now: float) -> str:
        resp = httpx.post(
            REFRESH_URL,
            headers={"Authorization": self._basic_header(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=30,
        )
        return self._handle_token_response(resp, now)

    def _handle_token_response(self, resp: httpx.Response, now: float) -> str:
        if resp.status_code == 429:
            raise TrustpilotAuthError(
                "429: refresh troppo frequente. Attendere la scadenza del token.")
        if resp.status_code >= 400:
            raise TrustpilotAuthError(
                f"OAuth Trustpilot fallito: HTTP {resp.status_code} {resp.text[:200]}")
        tok = resp.json()
        if "access_token" not in tok:
            raise TrustpilotAuthError("Risposta OAuth senza access_token")
        self._save_cache(tok, now)
        return tok["access_token"]

    def revoke(self, refresh_token: str) -> bool:
        resp = httpx.post(
            REVOKE_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"token": refresh_token}, timeout=30)
        return resp.status_code == 200
