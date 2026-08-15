"""Connettore Trustpilot (corsia A) — API ufficiale.

Autenticazione (secondo la doc Trustpilot):
  - API PUBBLICHE: header `apikey: {key}` (mai in query string). Basta
    TRUSTPILOT_API_KEY. Copre find business-unit e recensioni pubbliche.
  - API PRIVATE: OAuth 2.0 Client Credentials (TRUSTPILOT_API_KEY +
    TRUSTPILOT_API_SECRET). Dà accesso alle recensioni private del profilo
    rivendicato e alla pubblicazione delle risposte. Gestita da
    trustpilot_auth.TrustpilotAuth (cache token, refresh, anti-429).

Senza chiave: modalità demo.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Reply,
                              Source, make_feedback_id, pseudonymize_author)
from .base import BaseConnector, ConnectorRun
from .trustpilot_auth import TrustpilotAuth, TrustpilotAuthError

API_BASE = "https://api.trustpilot.com/v1"
DEMO_FILE = Path(__file__).resolve().parents[2] / "data" / "demo_reviews_trustpilot.json"


class TrustpilotConnector(BaseConnector):
    source_name = "trustpilot"
    lane = "official_api"
    cost_per_record_eur = 0.0  # API key gratuita a bassi volumi

    def __init__(self, api_key: str | None = None,
                 api_secret: str | None = None):
        self.api_key = api_key or os.environ.get("TRUSTPILOT_API_KEY")
        self.auth = TrustpilotAuth(api_key=self.api_key, api_secret=api_secret)

    # --- header corretti (apikey in header, non in query) ---
    def _public_headers(self) -> dict:
        return {"apikey": self.api_key} if self.api_key else {}

    def _private_headers(self) -> dict | None:
        """Header con Bearer token se OAuth privato è configurato, altrimenti None."""
        if not self.auth.available():
            return None
        try:
            token = self.auth.get_access_token()
        except TrustpilotAuthError:
            return None
        return {"apikey": self.api_key, "Authorization": f"Bearer {token}"}

    def health(self) -> dict:
        h = super().health()
        h["private_oauth"] = self.auth.available()
        return h

    def resolve_business_unit(self, business: BusinessRef) -> str | None:
        if business.trustpilot_business_unit_id:
            return business.trustpilot_business_unit_id
        if not (self.api_key and business.domain):
            return None
        resp = httpx.get(
            f"{API_BASE}/business-units/find",
            params={"name": business.domain},          # solo il nome dominio
            headers=self._public_headers(),            # apikey come header
            timeout=20,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("id")

    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        self._check_kill_switch()
        if not self.api_key:
            if os.environ.get("EKKO_DEMO_FALLBACK") == "1":
                yield from self._fetch_demo(business, since, run)
            return

        bu_id = self.resolve_business_unit(business)
        if not bu_id:
            return

        # Se OAuth privato è disponibile usa l'endpoint privato (recensioni
        # complete del profilo rivendicato); altrimenti l'endpoint pubblico.
        priv = self._private_headers()
        if priv is not None:
            url = f"{API_BASE}/private/business-units/{bu_id}/reviews"
            headers = priv
        else:
            url = f"{API_BASE}/business-units/{bu_id}/reviews"
            headers = self._public_headers()

        page = 1
        while True:
            resp = httpx.get(
                url,
                params={"page": page, "perPage": 100, "orderBy": "createdat.desc"},
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            reviews = resp.json().get("reviews", [])
            if not reviews:
                return
            for rv in reviews:
                fo = self._normalize_api_review(rv, business, run)
                if since and fo.published_at <= since:
                    return  # ordinate desc: possiamo fermarci
                run.fetched += 1
                yield fo
            page += 1

    def _normalize_api_review(
        self, rv: dict, business: BusinessRef, run: ConnectorRun
    ) -> FeedbackObject:
        reply = rv.get("companyReply")
        return FeedbackObject(
            id=make_feedback_id("trustpilot", business.id, str(rv["id"])),
            source=Source.TRUSTPILOT,
            source_native_id=str(rv["id"]),
            business_id=business.id,
            author_hash=pseudonymize_author(str((rv.get("consumer") or {}).get("id", "anon"))),
            lang=rv.get("language", "it"),
            text=rv.get("text"),
            rating=FeedbackObject.normalize_rating(float(rv.get("stars", 0))),
            published_at=datetime.fromisoformat(rv["createdAt"].replace("Z", "+00:00")),
            reply=Reply(text=reply["text"]) if reply else None,
            lineage=Lineage(connector=self.source_name, run_id=run.run_id,
                            license="official_api"),
        )

    def _fetch_demo(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        if not DEMO_FILE.exists():
            return
        for rv in json.loads(DEMO_FILE.read_text()):
            published = datetime.fromisoformat(rv["published_at"])
            if since and published <= since:
                continue
            run.fetched += 1
            yield FeedbackObject(
                id=make_feedback_id("trustpilot", business.id, f"demo/{rv['id']}"),
                source=Source.TRUSTPILOT,
                source_native_id=f"demo/{rv['id']}",
                business_id=business.id,
                author_hash=pseudonymize_author(rv["author"]),
                lang=rv.get("lang", "it"),
                text=rv.get("text"),
                rating=FeedbackObject.normalize_rating(rv["stars"]),
                published_at=published,
                reply=Reply(text=rv["reply"]) if rv.get("reply") else None,
                lineage=Lineage(connector=self.source_name, run_id=run.run_id,
                                license="demo"),
            )
