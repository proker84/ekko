"""Connettori "recensioni certificate" (corsia C, public_crawl):
  - Feedaty (Zoorate)             https://www.feedaty.com/feedaty/reviews/<nomesito>
  - Recensioni Verificate (Avis Vérifiés)
                                  https://www.recensioni-verificate.com/recensioni-clienti/<dominio>.html

Entrambe le pagine certificato sono pubbliche e pensate per i rich snippet,
quindi in genere espongono JSON-LD/microdata (strategie di pubscrape).
Chiave di risoluzione: il dominio dell'azienda (campo "Dominio" del form).
Attivazione: EKKO_ENABLE_FEEDATY=1 / EKKO_ENABLE_RECENSIONI_VERIFICATE=1.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Iterator

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Source,
                              make_feedback_id, pseudonymize_author)
from . import pubscrape
from .base import BaseConnector, ConnectorRun


class _CertifiedBase(BaseConnector):
    lane = "public_crawl"
    cost_per_record_eur = 0.0
    source: Source = Source.DEMO   # override

    def _url(self, business: BusinessRef) -> str | None:  # override
        raise NotImplementedError

    def _enabled(self) -> bool:  # override
        raise NotImplementedError

    def health(self) -> dict:
        return {"source": self.source_name, "lane": self.lane, "ok": self._enabled()}

    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        self._check_kill_switch()
        if not self._enabled():
            return
        url = self._url(business)
        if not url:
            return
        try:
            reviews, _m = pubscrape.scrape_reviews(url)
        except httpx.HTTPError:
            return
        for rv in reviews:
            if since and rv["date"] <= since:
                continue
            native = f"{rv['author']}|{rv['date'].isoformat()}|{rv['stars']}"
            run.fetched += 1
            yield FeedbackObject(
                id=make_feedback_id(self.source_name, business.id, native),
                source=self.source,
                source_native_id=native,
                business_id=business.id,
                author_hash=pseudonymize_author(rv["author"]),
                text=rv["text"],
                rating=FeedbackObject.normalize_rating(rv["stars"], rv["scale_max"]),
                published_at=rv["date"],
                lineage=Lineage(connector=self.source_name, run_id=run.run_id,
                                license="public_crawl"),
            )


class FeedatyConnector(_CertifiedBase):
    source_name = "feedaty"
    source = Source.FEEDATY

    def _enabled(self) -> bool:
        return os.environ.get("EKKO_ENABLE_FEEDATY") == "1"

    def _url(self, business: BusinessRef) -> str | None:
        if not business.domain:
            return None
        site = business.domain.split(".")[0] if "." in business.domain else business.domain
        return f"https://www.feedaty.com/feedaty/reviews/{site}"


class RecensioniVerificateConnector(_CertifiedBase):
    source_name = "recensioni_verificate"
    source = Source.VERIFIED_REVIEWS

    def _enabled(self) -> bool:
        return os.environ.get("EKKO_ENABLE_RECENSIONI_VERIFICATE") == "1"

    def _url(self, business: BusinessRef) -> str | None:
        if not business.domain:
            return None
        return ("https://www.recensioni-verificate.com/recensioni-clienti/"
                f"{business.domain}.html")


def feedaty_enabled() -> bool:
    return os.environ.get("EKKO_ENABLE_FEEDATY") == "1"


def rv_enabled() -> bool:
    return os.environ.get("EKKO_ENABLE_RECENSIONI_VERIFICATE") == "1"
