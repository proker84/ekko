"""Connettore AutoScout24 (corsia C, public_crawl) — recensioni concessionari.

Fonte verticale n.1 per l'automotive in Italia. Pagina pubblica:
  https://www.autoscout24.it/concessionari/<slug>/recensioni

Risoluzione: `business.autoscout24_url` esplicito (campo nel form di ricerca),
altrimenti si prova lo slug derivato dal nome. Attivazione:
EKKO_ENABLE_AUTOSCOUT24=1 (scraping di pagine pubbliche: valgono le stesse
avvertenze ToS del connettore Trustpilot pubblico).
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Iterator

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Source,
                              make_feedback_id, pseudonymize_author)
from . import pubscrape
from .base import BaseConnector, ConnectorRun

BASE = "https://www.autoscout24.it/concessionari"


def enabled() -> bool:
    return os.environ.get("EKKO_ENABLE_AUTOSCOUT24") == "1"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s


def resolve_url(business: BusinessRef) -> str | None:
    if business.autoscout24_url:
        u = business.autoscout24_url.rstrip("/")
        return u if u.endswith("/recensioni") else u + "/recensioni"
    return f"{BASE}/{_slug(business.name)}/recensioni"


def _review_key(rv: dict) -> tuple:
    return (rv["author"], rv["date"].isoformat(), rv["stars"])


def scrape_all(url: str, max_pages: int = 15) -> tuple[list[dict], str]:
    """Recensioni da tutte le pagine (?page=N); si ferma quando non arriva
    niente di nuovo (se il sito ignora il parametro, si ferma alla 2ª)."""
    seen, out, method = set(), [], "none"
    for page in range(1, max_pages + 1):
        page_url = url if page == 1 else f"{url}?page={page}"
        try:
            reviews, m = pubscrape.scrape_reviews(page_url)
        except httpx.HTTPError:
            break
        fresh = [rv for rv in reviews if _review_key(rv) not in seen]
        if not fresh:
            break
        method = m
        for rv in fresh:
            seen.add(_review_key(rv))
            out.append(rv)
    return out, method


def diagnose(url_or_name: str) -> dict:
    """`python -m ekko.cli as24test "<url o nome>"` — test senza DB."""
    if url_or_name.startswith("http"):
        url = url_or_name.rstrip("/")
        if not url.endswith("/recensioni"):
            url += "/recensioni"
    else:
        url = f"{BASE}/{_slug(url_or_name)}/recensioni"
    try:
        reviews, method = scrape_all(url)
    except httpx.HTTPStatusError as e:
        return {"ok": False, "url": url, "http": e.response.status_code,
                "hint": "404 = slug sbagliato: passa l'URL completo della pagina concessionario"}
    except httpx.HTTPError as e:
        return {"ok": False, "url": url, "error": str(e)}
    sample = ({"stars": reviews[0]["stars"], "date": reviews[0]["date"].isoformat(),
               "text": (reviews[0]["text"] or "")[:80]} if reviews else None)
    return {"ok": bool(reviews), "url": url, "method": method,
            "reviews_total": len(reviews), "sample": sample}


class AutoScout24Connector(BaseConnector):
    source_name = "autoscout24"
    lane = "public_crawl"
    cost_per_record_eur = 0.0

    def health(self) -> dict:
        return {"source": self.source_name, "lane": self.lane, "ok": enabled()}

    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        self._check_kill_switch()
        if not enabled():
            return
        url = resolve_url(business)
        if not url:
            return
        try:
            reviews, _method = scrape_all(url)
        except httpx.HTTPError:
            return
        for rv in reviews:
            if since and rv["date"] <= since:
                continue
            native = f"{rv['author']}|{rv['date'].isoformat()}|{rv['stars']}"
            run.fetched += 1
            yield FeedbackObject(
                id=make_feedback_id("autoscout24", business.id, native),
                source=Source.AUTOSCOUT24,
                source_native_id=native,
                business_id=business.id,
                author_hash=pseudonymize_author(rv["author"]),
                text=rv["text"],
                rating=FeedbackObject.normalize_rating(rv["stars"], rv["scale_max"]),
                published_at=rv["date"],
                lineage=Lineage(connector=self.source_name, run_id=run.run_id,
                                license="public_crawl"),
            )
