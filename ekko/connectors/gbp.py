"""Connettore Google Business Profile (corsia A, official_api) — profili PROPRI.

È la via "corsia A" citata in google.py: il cliente che POSSIEDE il profilo
collega il proprio account Google (OAuth, scope business.manage) e Ekko
scarica gratuitamente TUTTE le recensioni delle sue sedi — senza il limite
delle ~5 recensioni della Places API — e pubblica le risposte approvate
dall'utente (mai invio automatico).

API usate (tutte ufficiali, costo 0):
  - Account:   https://mybusinessaccountmanagement.googleapis.com/v1/accounts
  - Location:  https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations
  - Recensioni e risposte: https://mybusiness.googleapis.com/v4 (unica API
    che espone reviews/reply; non ha ancora un equivalente v1).

NOTA: richiede l'approvazione della quota GBP da parte di Google
(vedi docs/GBP_SETUP.md).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import httpx

from ekko.auth import gbp_oauth
from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Reply,
                              Source, make_feedback_id, pseudonymize_author)
from ekko.storage import db
from .base import BaseConnector, ConnectorRun, google_reviews_since

# provider nella tabella oauth_tokens
PROVIDER = "gbp"

ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL = ("https://mybusinessbusinessinformation.googleapis.com/v1/"
                 "{account}/locations")
REVIEWS_URL = "https://mybusiness.googleapis.com/v4/{account}/{location}/reviews"
REPLY_URL = ("https://mybusiness.googleapis.com/v4/{account}/{location}/"
             "reviews/{review_id}/reply")

# mapping enum v4 starRating -> stelle 1..5 (UNSPECIFIED -> None)
STAR_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


class GbpError(RuntimeError):
    """Errore GBP (token mancante/rifiutato, API non raggiungibile...)."""


class GbpNotConnected(GbpError):
    """L'agenzia non ha (ancora) collegato l'account Google Business."""


def connected(owner_id: str | None) -> bool:
    """True se l'agenzia ha completato il collegamento OAuth GBP."""
    if not owner_id:
        return False
    tok = db.get_oauth_token(owner_id, PROVIDER)
    return bool(tok and (tok.get("access_token") or tok.get("refresh_token")))


def star_value(star_rating: str | None) -> int | None:
    """'FOUR' -> 4; 'STAR_RATING_UNSPECIFIED'/None -> None."""
    return STAR_MAP.get(star_rating or "")


def review_native_id(rv: dict) -> str:
    """Id nativo della recensione: reviewId o ultimo segmento di `name`."""
    return rv.get("reviewId") or (rv.get("name") or "").rsplit("/", 1)[-1]


def format_address(loc: dict) -> str:
    """storefrontAddress -> stringa leggibile 'via, città (prov)'."""
    addr = loc.get("storefrontAddress") or {}
    parts = list(addr.get("addressLines") or [])
    if addr.get("postalCode") or addr.get("locality"):
        parts.append(" ".join(p for p in (addr.get("postalCode"),
                                          addr.get("locality")) if p))
    if addr.get("administrativeArea"):
        parts.append(addr["administrativeArea"])
    return ", ".join(p for p in parts if p)


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


class GbpConnector(BaseConnector):
    source_name = "gbp"
    lane = "official_api"
    cost_per_record_eur = 0.0

    def __init__(self, owner_id: str | None = None):
        self.owner_id = owner_id

    def health(self) -> dict:
        return {"source": self.source_name, "lane": self.lane,
                "ok": connected(self.owner_id)}

    # ---------- token: refresh automatico quando scaduto ----------
    def _access_token(self, owner_id: str) -> str:
        tok = db.get_oauth_token(owner_id, PROVIDER)
        if not tok:
            raise GbpNotConnected("Account Google Business non collegato")
        expires_at = tok.get("expires_at")
        expired = True
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                expired = exp <= datetime.now(timezone.utc)
            except ValueError:
                expired = True
        if tok.get("access_token") and not expired:
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise GbpNotConnected("Token GBP scaduto e refresh_token assente: "
                                  "ricollegare l'account")
        new = gbp_oauth.refresh_access_token(tok["refresh_token"])
        db.upsert_oauth_token(owner_id, PROVIDER, new["access_token"], None,
                              new["expires_at"], tok.get("scopes"),
                              datetime.now(timezone.utc))
        return new["access_token"]

    def _headers(self, owner_id: str) -> dict:
        return {"Authorization": f"Bearer {self._access_token(owner_id)}"}

    # ---------- discovery: account e location del cliente ----------
    def list_accounts(self, owner_id: str) -> list[dict]:
        """Account GBP dell'utente: [{'name': 'accounts/123', ...}]."""
        resp = httpx.get(ACCOUNTS_URL, headers=self._headers(owner_id),
                         timeout=20)
        resp.raise_for_status()
        return resp.json().get("accounts", [])

    def list_locations(self, owner_id: str, account_name: str) -> list[dict]:
        """Location (sedi) di un account, paginato: [{'name','title',...}]."""
        out: list[dict] = []
        params = {"readMask": "name,title,storefrontAddress", "pageSize": 100}
        while True:
            resp = httpx.get(LOCATIONS_URL.format(account=account_name),
                             headers=self._headers(owner_id), params=params,
                             timeout=20)
            resp.raise_for_status()
            data = resp.json()
            out.extend(data.get("locations", []))
            token = data.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    # ---------- recensioni: fetch completo e paginato ----------
    def fetch_reviews(self, owner_id: str, account_name: str,
                      location_name: str) -> list[dict]:
        """Recensioni della location (v4, pageSize=50, paginato).

        Filtro prodotto: si gestiscono solo le recensioni dal 2025 in poi
        (google_reviews_since, override con EKKO_GOOGLE_REVIEWS_SINCE).
        NB: l'ordinamento v4 è per updateTime, non createTime, quindi non
        si può interrompere la paginazione in anticipo: si filtra e basta.
        """
        cutoff = google_reviews_since()
        out: list[dict] = []
        params = {"pageSize": 50}
        url = REVIEWS_URL.format(account=account_name, location=location_name)
        while True:
            resp = httpx.get(url, headers=self._headers(owner_id),
                             params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            out.extend(
                rv for rv in data.get("reviews", [])
                if _parse_time(rv.get("createTime")
                               or rv.get("updateTime")) >= cutoff)
            token = data.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    def normalize_review(self, rv: dict, business_id: str,
                         run: ConnectorRun) -> FeedbackObject:
        """Recensione v4 -> FeedbackObject (schema unico di Ekko)."""
        native_id = review_native_id(rv)
        stars = star_value(rv.get("starRating"))
        reply = (rv.get("reviewReply") or {}).get("comment")
        run.fetched += 1
        return FeedbackObject(
            id=make_feedback_id("google", business_id, native_id),
            source=Source.GOOGLE,
            source_native_id=native_id,
            business_id=business_id,
            author_hash=pseudonymize_author(
                (rv.get("reviewer") or {}).get("displayName") or "anon"),
            text=rv.get("comment"),
            rating=(FeedbackObject.normalize_rating(float(stars))
                    if stars is not None else None),
            published_at=_parse_time(rv.get("createTime")
                                     or rv.get("updateTime")),
            reply=Reply(
                text=reply,
                published_at=_parse_time(
                    (rv.get("reviewReply") or {}).get("updateTime")),
            ) if reply else None,
            lineage=Lineage(connector="gbp", run_id=run.run_id,
                            license="official_api"),
        )

    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        """Interfaccia standard: usa il link business→location salvato."""
        self._check_kill_switch()
        if not self.owner_id or not connected(self.owner_id):
            return
        link = db.get_gbp_link(business.id)
        if not link:
            return
        for rv in self.fetch_reviews(self.owner_id, link["account_name"],
                                     link["location_name"]):
            fo = self.normalize_review(rv, business.id, run)
            if since and fo.published_at <= since:
                run.fetched -= 1
                continue
            yield fo

    # ---------- risposta a una recensione (approvata dall'utente) ----------
    def send_reply(self, owner_id: str, account_name: str, location_name: str,
                   review_id: str, text: str) -> tuple[bool, str]:
        """PUT della risposta su GBP. Ritorna (ok, messaggio)."""
        url = REPLY_URL.format(account=account_name, location=location_name,
                               review_id=review_id)
        try:
            resp = httpx.put(url, headers=self._headers(owner_id),
                             json={"comment": text}, timeout=30)
        except GbpError as e:
            return False, str(e)
        except httpx.HTTPError as e:
            return False, f"Errore di rete verso GBP: {e}"
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")
            except ValueError:
                detail = resp.text[:120]
            return False, f"GBP HTTP {resp.status_code}: {detail}".strip()
        return True, "ok"
