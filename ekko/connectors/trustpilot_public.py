"""Connettore Trustpilot PUBBLICO (corsia C — crawler proprietario, sperimentale).

Legge le recensioni dalle pagine pubbliche it.trustpilot.com/review/<dominio>
estraendo il JSON __NEXT_DATA__ embedded, senza chiave API.

⚠ ATTENZIONE — questo connettore è DISATTIVATO di default:
  - viola i ToS di Trustpilot (rischio ban IP / contestazioni);
  - il markup può cambiare senza preavviso (parsing best-effort).
Si attiva consapevolmente con EKKO_ENABLE_PUBLIC_TRUSTPILOT=1.
Rate limit conservativo (1 req/s, max 10 pagine per run) e kill-switch
standard del framework restano attivi.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Iterator

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Reply,
                              Source, make_feedback_id, pseudonymize_author)
from .base import BaseConnector, ConnectorRun

PAGE_URL = "https://it.trustpilot.com/review/{domain}"
UA = "EkkoBot/0.1 (reputation analytics; contact: info@immox-online.com)"
MAX_PAGES = int(os.environ.get("EKKO_TP_PUBLIC_MAX_PAGES", "10"))
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def enabled() -> bool:
    return os.environ.get("EKKO_ENABLE_PUBLIC_TRUSTPILOT") == "1"


class TrustpilotPublicConnector(BaseConnector):
    source_name = "trustpilot"          # stessa fonte logica: dedup cross-corsia
    lane = "public_crawl"
    cost_per_record_eur = 0.0

    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        self._check_kill_switch()
        if not business.domain:
            return
        client = httpx.Client(headers={"User-Agent": UA}, timeout=25,
                              follow_redirects=True)
        try:
            for page in range(1, MAX_PAGES + 1):
                url = PAGE_URL.format(domain=business.domain)
                resp = client.get(url, params={"page": page} if page > 1 else None)
                if resp.status_code in (403, 404, 429):
                    run.errors += 1
                    return
                resp.raise_for_status()
                reviews = self._extract_reviews(resp.text)
                if not reviews:
                    return
                stop = False
                for rv in reviews:
                    fo = self._normalize(rv, business, run)
                    if fo is None:
                        continue
                    if since and fo.published_at <= since:
                        stop = True
                        break
                    run.fetched += 1
                    yield fo
                if stop:
                    return
                time.sleep(1.0)          # rate limit conservativo
        finally:
            client.close()

    @staticmethod
    def _extract_reviews(html: str) -> list[dict]:
        m = NEXT_DATA_RE.search(html)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
            return (data.get("props", {}).get("pageProps", {})
                    .get("reviews", []) or [])
        except (json.JSONDecodeError, AttributeError):
            return []

    def _normalize(self, rv: dict, business: BusinessRef,
                   run: ConnectorRun) -> FeedbackObject | None:
        try:
            native_id = str(rv.get("id") or rv.get("reviewId") or "")
            if not native_id:
                return None
            dates = rv.get("dates") or {}
            published = dates.get("publishedDate") or rv.get("publishedDate")
            reply = rv.get("reply") or None
            consumer = rv.get("consumer") or {}
            return FeedbackObject(
                id=make_feedback_id("trustpilot", business.id, native_id),
                source=Source.TRUSTPILOT,
                source_native_id=native_id,
                business_id=business.id,
                author_hash=pseudonymize_author(str(consumer.get("id", "anon"))),
                lang=rv.get("language", "it"),
                text=" ".join(x for x in (rv.get("title"), rv.get("text")) if x) or None,
                rating=FeedbackObject.normalize_rating(float(rv.get("rating", 0))),
                published_at=datetime.fromisoformat(
                    str(published).replace("Z", "+00:00")),
                reply=Reply(text=reply.get("message", "")) if reply else None,
                lineage=Lineage(connector="trustpilot_public", run_id=run.run_id,
                                license="public_crawl"),
            )
        except (ValueError, TypeError, KeyError):
            run.errors += 1
            return None
