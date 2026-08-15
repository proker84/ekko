"""Connettore Google (corsia A/B) — Places API (New).

Con GOOGLE_MAPS_API_KEY impostata usa l'API reale:
  - resolve: Text Search per trovare il place_id dell'azienda
  - fetch:   Place Details con campo `reviews`

NOTA IMPORTANTE (piano §2.5): la Places API pubblica restituisce al massimo
~5 recensioni per luogo. La copertura completa richiede: (a) OAuth del
cliente sulla Google Business Profile API per i profili propri, oppure
(b) un data provider licenziato (corsia B) per i profili di terzi.
Questo connettore è il punto d'ingresso della corsia A; senza chiave API
opera in modalità demo leggendo `data/demo_reviews_google.json`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Reply,
                              Source, make_feedback_id, pseudonymize_author)
from .base import BaseConnector, ConnectorRun

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
DEMO_FILE = Path(__file__).resolve().parents[2] / "data" / "demo_reviews_google.json"


class GoogleConnector(BaseConnector):
    source_name = "google"
    lane = "official_api"
    cost_per_record_eur = 0.004  # stima SKU Place Details ripartito

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")

    # ---------- entity resolution (nucleo Business Graph) ----------
    def resolve_place_id(self, business: BusinessRef) -> str | None:
        if business.google_place_id:
            return business.google_place_id
        if not self.api_key:
            return None
        query = f"{business.name} {business.city or ''}".strip()
        resp = httpx.post(
            PLACES_SEARCH_URL,
            headers={
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
            },
            json={"textQuery": query, "languageCode": "it"},
            timeout=20,
        )
        resp.raise_for_status()
        places = resp.json().get("places", [])
        return places[0]["id"] if places else None

    # ---------- fetch ----------
    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        self._check_kill_switch()
        if not self.api_key:
            if os.environ.get("EKKO_DEMO_FALLBACK") == "1":
                yield from self._fetch_demo(business, since, run)
            return

        place_id = self.resolve_place_id(business)
        if not place_id:
            return
        resp = httpx.get(
            PLACES_DETAILS_URL.format(place_id=place_id),
            headers={
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": "reviews,rating,userRatingCount",
            },
            params={"languageCode": "it"},
            timeout=20,
        )
        resp.raise_for_status()
        run.cost_eur += 0.015  # una chiamata Place Details (stima)
        for rv in resp.json().get("reviews", []):
            fo = self._normalize_api_review(rv, business, run)
            if since and fo.published_at <= since:
                continue
            run.fetched += 1
            run.cost_eur += self.cost_per_record_eur
            yield fo

    def _normalize_api_review(
        self, rv: dict, business: BusinessRef, run: ConnectorRun
    ) -> FeedbackObject:
        native_id = rv.get("name", "")  # es. places/xxx/reviews/yyy
        published = rv.get("publishTime") or datetime.now(timezone.utc).isoformat()
        return FeedbackObject(
            id=make_feedback_id("google", business.id, native_id),
            source=Source.GOOGLE,
            source_native_id=native_id,
            business_id=business.id,
            author_hash=pseudonymize_author(
                rv.get("authorAttribution", {}).get("uri", "anon")
            ),
            lang=(rv.get("originalText") or {}).get("languageCode", "it"),
            text=(rv.get("originalText") or {}).get("text")
            or (rv.get("text") or {}).get("text"),
            rating=FeedbackObject.normalize_rating(float(rv.get("rating", 0))),
            published_at=datetime.fromisoformat(published.replace("Z", "+00:00")),
            lineage=Lineage(connector=self.source_name, run_id=run.run_id,
                            license="official_api"),
        )

    # ---------- demo mode ----------
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
                id=make_feedback_id("google", business.id, f"demo/{rv['id']}"),
                source=Source.GOOGLE,
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
