"""Connettore DataForSEO (corsia B — provider licenziato).

Scarica TUTTE le recensioni Google di un'azienda tramite l'API Business Data
di DataForSEO (a differenza dell'API ufficiale Google che ne dà max ~5).
Task-based: post del task -> polling del risultato.

Auth: header Basic con DATAFORSEO_AUTH (base64 di login:password).
Attivo solo se DATAFORSEO_AUTH è impostata.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Iterator

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Reply,
                              Source, make_feedback_id, pseudonymize_author)
from .base import BaseConnector, ConnectorRun

BASE = "https://api.dataforseo.com/v3/business_data/google/reviews"
DEPTH = int(os.environ.get("EKKO_DATAFORSEO_DEPTH", "100"))
POLL_TIMEOUT_S = int(os.environ.get("EKKO_DATAFORSEO_TIMEOUT", "240"))


def enabled() -> bool:
    return bool(os.environ.get("DATAFORSEO_AUTH"))


class DataForSeoGoogleConnector(BaseConnector):
    source_name = "google"
    lane = "licensed_provider"
    cost_per_record_eur = 0.001

    def __init__(self, auth: str | None = None):
        self.auth = auth or os.environ.get("DATAFORSEO_AUTH")

    def _headers(self) -> dict:
        return {"Authorization": f"Basic {self.auth}",
                "Content-Type": "application/json"}

    def health(self) -> dict:
        h = super().health()
        h["provider"] = "dataforseo"
        return h

    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        self._check_kill_switch()
        if not self.auth:
            return
        task = {"language_code": "it", "depth": DEPTH, "priority": 2,
                "keyword": f"{business.name} {business.city or ''}".strip(),
                "location_name": "Italy"}

        # 1) posta il task
        r = httpx.post(f"{BASE}/task_post", headers=self._headers(),
                       json=[task], timeout=30)
        r.raise_for_status()
        body = r.json()
        tasks = body.get("tasks") or []
        if not tasks or tasks[0].get("status_code") not in (20000, 20100):
            # account non verificato / errore provider -> fallback all'API Google (max 5)
            run.errors += 1
            from .google import GoogleConnector
            yield from GoogleConnector().fetch_incremental(business, since, run)
            return
        task_id = tasks[0]["id"]
        run.cost_eur += 0.02

        # 2) polling del risultato
        deadline = time.time() + POLL_TIMEOUT_S
        result = None
        while time.time() < deadline:
            g = httpx.get(f"{BASE}/task_get/{task_id}",
                          headers=self._headers(), timeout=30)
            g.raise_for_status()
            gt = (g.json().get("tasks") or [])
            if gt and gt[0].get("status_code") == 20000 and gt[0].get("result"):
                result = gt[0]["result"]
                break
            time.sleep(3)
        if not result:
            run.errors += 1
            raise RuntimeError("DataForSEO: risultato non pronto entro il timeout")

        # totale recensioni dell'azienda (per la dashboard)
        try:
            total = result[0].get("reviews_count") if result else None
            if total:
                from ekko.storage import db as _db
                business.total_reviews_google = int(total)
                _db.upsert_business(business)
        except Exception:
            pass

        # 3) normalizza le recensioni
        for res in result:
            for item in (res.get("items") or []):
                fo = self._normalize(item, business, run)
                if fo is None:
                    continue
                if since and fo.published_at <= since:
                    continue
                run.fetched += 1
                yield fo

    def _normalize(self, item: dict, business: BusinessRef,
                   run: ConnectorRun) -> FeedbackObject | None:
        try:
            native_id = str(item.get("review_id") or item.get("id")
                            or item.get("timestamp") or "")
            if not native_id:
                return None
            ts = item.get("timestamp")  # es "2026-05-01 12:00:00 +00:00"
            if ts:
                published = datetime.fromisoformat(ts.replace(" +", "+").replace(" ", "T", 1))
            else:
                published = datetime.now(timezone.utc)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            rating_val = ((item.get("rating") or {}).get("value")) or item.get("rating_value")
            reply = None
            owner = item.get("owner_answer") or item.get("owner_response")
            if owner:
                reply = Reply(text=owner if isinstance(owner, str) else str(owner))
            return FeedbackObject(
                id=make_feedback_id("google", business.id, native_id),
                source=Source.GOOGLE,
                source_native_id=native_id,
                business_id=business.id,
                author_hash=pseudonymize_author(str(item.get("profile_name") or "anon")),
                lang=item.get("language") or "it",
                text=item.get("review_text"),
                rating=FeedbackObject.normalize_rating(float(rating_val or 0)),
                published_at=published,
                reply=reply,
                lineage=Lineage(connector="dataforseo", run_id=run.run_id,
                                license="licensed_provider"),
            )
        except (ValueError, TypeError, KeyError):
            run.errors += 1
            return None


# --------------------------------------------------------------------------- #
#  API asincrona (per il web): posta il task, poi raccogli quando è pronto
# --------------------------------------------------------------------------- #
def _auth_header() -> dict:
    return {"Authorization": f"Basic {os.environ.get('DATAFORSEO_AUTH')}",
            "Content-Type": "application/json"}


def post_task(business: BusinessRef) -> str | None:
    """Posta il task recensioni Google (priority) e ritorna il task_id."""
    if not os.environ.get("DATAFORSEO_AUTH"):
        return None
    task = {"language_code": "it", "depth": DEPTH, "priority": 2,
            "keyword": f"{business.name} {business.city or ''}".strip(),
            "location_name": "Italy"}
    try:
        r = httpx.post(f"{BASE}/task_post", headers=_auth_header(),
                       json=[task], timeout=30)
        r.raise_for_status()
        tasks = r.json().get("tasks") or []
        if tasks and tasks[0].get("status_code") in (20000, 20100):
            return tasks[0]["id"]
    except httpx.HTTPError:
        pass
    return None


def collect(task_id: str):
    """Ritorna (items, total) se il task è pronto, altrimenti (None, None)."""
    if not (task_id and os.environ.get("DATAFORSEO_AUTH")):
        return None, None
    try:
        g = httpx.get(f"{BASE}/task_get/{task_id}", headers=_auth_header(), timeout=30)
        g.raise_for_status()
        t = (g.json().get("tasks") or [])
        if t and t[0].get("status_code") == 20000 and t[0].get("result"):
            res = t[0]["result"][0]
            return (res.get("items") or []), res.get("reviews_count")
    except httpx.HTTPError:
        pass
    return None, None


def normalize_items(items: list, business: BusinessRef, run: ConnectorRun):
    """Converte gli item DataForSEO in FeedbackObject."""
    conn = DataForSeoGoogleConnector()
    for item in items:
        fo = conn._normalize(item, business, run)
        if fo is not None:
            run.fetched += 1
            yield fo
