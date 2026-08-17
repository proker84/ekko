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

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Reply,
                              Source, make_feedback_id, pseudonymize_author)
from . import pubscrape
from .base import BaseConnector, ConnectorRun

BASE = "https://www.autoscout24.it/concessionari"
# API interna usata dal pulsante "Mostra più recensioni" (scoperta via DevTools):
#   POST /api/dealer-detail/fetch-reviews  {"customerId": <id>, "skip": N}
# risponde con blocchi di 10: {reviewId, stars, created "dd.mm.yyyy", name,
# reviewText, replyText, grades[...]}
API_URL = "https://www.autoscout24.it/api/dealer-detail/fetch-reviews"
CID_RE = re.compile(r'"customerId"\s*:\s*(\d+)')
API_MAX = int(os.environ.get("EKKO_AS24_MAX_REVIEWS", "1000"))


def enabled() -> bool:
    # Attivo di DEFAULT (fonte chiave per l'automotive); si spegne con =0.
    return os.environ.get("EKKO_ENABLE_AUTOSCOUT24", "1") != "0"


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


def _api_reviews(page_html: str, page_url: str) -> list[dict] | None:
    """Recensioni COMPLETE via API interna; None se il customerId non si trova."""
    import time as _time
    m = CID_RE.search(page_html)
    if not m:
        return None
    cid = int(m.group(1))
    out = []
    with httpx.Client(headers={"User-Agent": pubscrape.UA, "Referer": page_url,
                               "Accept-Language": "it"}, timeout=20) as cl:
        for skip in range(0, API_MAX, 10):
            try:
                r = cl.post(API_URL, json={"customerId": cid, "skip": skip})
                if r.status_code != 200:
                    break
                batch = r.json()
            except (httpx.HTTPError, ValueError):
                break
            if not isinstance(batch, list) or not batch:
                break
            for it in batch:
                d = pubscrape.parse_date(it.get("created"))
                stars = it.get("stars")
                if d is None or stars is None:
                    continue
                out.append({"author": it.get("name") or "anon",
                            "stars": float(stars), "scale_max": 5.0,
                            "text": (it.get("reviewText") or "").strip() or None,
                            "date": d,
                            "native_id": it.get("reviewId"),
                            "reply": (it.get("replyText") or "").strip() or None})
            if len(batch) < 10:
                break
            _time.sleep(0.4)   # rate limit gentile
    return out


def scrape_all(url: str) -> tuple[list[dict], str]:
    """Tutte le recensioni del concessionario.
    1) API interna fetch-reviews (complete, con risposte del venditore);
    2) fallback: parsing della pagina (prime ~10, strategie pubscrape)."""
    html = pubscrape.fetch_html(url)
    api = _api_reviews(html, url)
    if api:
        return api, "api"
    for method, fn in (("jsonld", pubscrape.iter_jsonld_reviews),
                       ("microdata", pubscrape.iter_microdata_reviews),
                       ("deep", pubscrape.deep_harvest)):
        found = list(fn(html))
        if found:
            return found, method
    return [], "none"


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
        # gruppi/catene: più concessionari analizzati insieme
        urls = [u for u in (business.autoscout24_urls or []) if u] or \
            [resolve_url(business)]
        for url in urls:
            if not url:
                continue
            u = url.rstrip("/")
            if not u.endswith("/recensioni"):
                u += "/recensioni"
            label = None
            if "/concessionari/" in u:
                slug = u.split("/concessionari/")[-1].replace("/recensioni", "")
                label = slug.replace("-", " ").title()   # etichetta leggibile
            yield from self._fetch_one(u, business, since, run,
                                       label if len(urls) > 1 else None)

    def _fetch_one(self, url: str, business: BusinessRef,
                   since: datetime | None, run: ConnectorRun,
                   location: str | None) -> Iterator[FeedbackObject]:
        try:
            reviews, _method = scrape_all(url)
        except httpx.HTTPError:
            return
        for rv in reviews:
            if since and rv["date"] <= since:
                continue
            native = rv.get("native_id") or \
                f"{rv['author']}|{rv['date'].isoformat()}|{rv['stars']}"
            if location:
                native = f"{location}|{native}"
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
                location=location,
                reply=Reply(text=rv["reply"]) if rv.get("reply") else None,
                lineage=Lineage(connector=self.source_name, run_id=run.run_id,
                                license="public_crawl"),
            )
