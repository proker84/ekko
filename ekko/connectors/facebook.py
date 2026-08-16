"""Connettore Facebook/Meta (corsia A, official_api) — recensioni pagina.

LIMITE DI PIATTAFORMA: la Graph API espone le recensioni ("ratings") SOLO
delle pagine di cui si possiede un token (permesso pages_read_user_content).
Non esiste una via ufficiale per leggere le recensioni di pagine di terzi.
Questo connettore quindi copre il caso "agenzia/cliente proprietario della
pagina": perfetto per il multi-tenant (ogni agenzia collega le pagine dei
propri clienti).

Config MVP (env): FACEBOOK_PAGE_TOKEN + FACEBOOK_PAGE_ID
(oppure business.facebook_page_id per-azienda).
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Iterator

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Source,
                              make_feedback_id, pseudonymize_author)
from .base import BaseConnector, ConnectorRun

GRAPH = "https://graph.facebook.com/v19.0"


def enabled(owner_id: str | None = None) -> bool:
    """Attivo se c'è un token globale OPPURE l'agenzia ha collegato pagine."""
    if os.environ.get("FACEBOOK_PAGE_TOKEN"):
        return True
    if owner_id:
        try:
            from ekko.storage import db
            return bool(db.list_fb_pages(owner_id))
        except Exception:  # noqa: BLE001
            return False
    return False


def connectable() -> bool:
    """L'app Meta è configurata: si può mostrare "Collega Facebook"."""
    return bool(os.environ.get("FACEBOOK_APP_ID")
                and os.environ.get("FACEBOOK_APP_SECRET"))


class FacebookConnector(BaseConnector):
    source_name = "meta"
    lane = "official_api"
    cost_per_record_eur = 0.0

    def __init__(self, owner_id: str | None = None):
        self.owner_id = owner_id

    def health(self) -> dict:
        return {"source": self.source_name, "lane": self.lane,
                "ok": enabled(self.owner_id)}

    def _targets(self, business: BusinessRef) -> list[tuple[str, str, str | None]]:
        """[(page_id, token, label)] — pagine collegate dall'agenzia, filtrate
        per somiglianza col nome dell'azienda analizzata."""
        env_tok = os.environ.get("FACEBOOK_PAGE_TOKEN")
        pid = business.facebook_page_id or os.environ.get("FACEBOOK_PAGE_ID")
        if env_tok and pid:
            return [(pid, env_tok, None)]
        if not self.owner_id:
            return []
        from ekko.core.matching import confidence
        from ekko.storage import db
        pages = db.list_fb_pages(self.owner_id)
        if business.facebook_page_id:
            pages = [p for p in pages if p["id"] == business.facebook_page_id]
        else:   # aggancia solo le pagine che corrispondono all'azienda
            pages = [p for p in pages
                     if confidence(business.name, p.get("name") or "") >= 70]
        multi = len(pages) > 1
        return [(p["id"], p["token"], p.get("name") if multi else None)
                for p in pages]

    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        self._check_kill_switch()
        for page_id, token, label in self._targets(business):
            yield from self._fetch_page(page_id, token, label, business,
                                        since, run)

    def _fetch_page(self, page_id: str, token: str, label: str | None,
                    business: BusinessRef, since: datetime | None,
                    run: ConnectorRun) -> Iterator[FeedbackObject]:
        url = (f"{GRAPH}/{page_id}/ratings")
        params = {"access_token": token, "limit": 100,
                  "fields": "created_time,rating,recommendation_type,review_text"}
        pages = 0
        while url and pages < 10:
            try:
                r = httpx.get(url, params=params, timeout=25)
                r.raise_for_status()
            except httpx.HTTPError:
                return
            body = r.json()
            for item in body.get("data", []):
                created = item.get("created_time")
                try:
                    d = datetime.fromisoformat(
                        str(created).replace("Z", "+00:00").replace("+0000", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                if since and d <= since:
                    continue
                stars = item.get("rating")
                if stars is None:   # nuove pagine: solo consiglia/sconsiglia
                    stars = 5 if item.get("recommendation_type") == "positive" else 1
                text = item.get("review_text")
                native = hashlib.sha1(
                    f"{page_id}|{created}|{stars}|{text or ''}".encode()
                ).hexdigest()[:16]
                run.fetched += 1
                yield FeedbackObject(
                    id=make_feedback_id("meta", business.id, native),
                    source=Source.META,
                    source_native_id=native,
                    business_id=business.id,
                    author_hash=pseudonymize_author(native),  # Graph non espone l'autore
                    text=text,
                    rating=FeedbackObject.normalize_rating(float(stars)),
                    published_at=d,
                    location=label,
                    lineage=Lineage(connector=self.source_name, run_id=run.run_id,
                                    license="official_api"),
                )
            url = (body.get("paging") or {}).get("next")
            params = {}   # il next contiene già i parametri
            pages += 1
