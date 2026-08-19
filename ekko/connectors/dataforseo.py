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
from .base import BaseConnector, ConnectorRun, google_reviews_since

BASE = "https://api.dataforseo.com/v3/business_data/google/reviews"
DEPTH = int(os.environ.get("EKKO_DATAFORSEO_DEPTH", "200"))
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

        # 3) normalizza le recensioni (solo dal 2025 in poi — richiesta prodotto)
        cutoff = google_reviews_since()
        for res in result:
            for item in (res.get("items") or []):
                fo = self._normalize(item, business, run)
                if fo is None:
                    continue
                if fo.published_at < cutoff:
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


def post_task(business: BusinessRef, keyword_override: str | None = None,
              place_id: str | None = None) -> str | None:
    """Posta il task recensioni Google (priority) e ritorna il task_id.

    place_id: identificativo DataForSEO (namespace "Gh…", ottenuto dal loro
      endpoint Maps) -> MATCH ESATTO, è la via preferita.
    keyword_override: nome di UNA sede specifica (fallback senza place_id).
    """
    if not os.environ.get("DATAFORSEO_AUTH"):
        return None
    depth = business.review_depth or DEPTH
    if place_id:
        task = {"language_code": "it", "depth": depth, "priority": 2,
                "place_id": place_id}
        try:
            r = httpx.post(f"{BASE}/task_post", headers=_auth_header(),
                           json=[task], timeout=30)
            r.raise_for_status()
            tasks = r.json().get("tasks") or []
            if tasks and tasks[0].get("status_code") in (20000, 20100):
                return tasks[0]["id"]
            _log_task_error(tasks, f"place_id={place_id}")
        except httpx.HTTPError:
            pass
        # se il place_id viene rifiutato si prosegue con la keyword
    # NB: NON si passa google_place_id a DataForSEO — il loro `place_id` usa il
    # namespace Google Maps ("GhIJ…"), diverso da quello della Places API
    # ("ChIJ…") che risolviamo noi: il task fallirebbe restituendo 0 recensioni.
    # Si usa invece il NOME ESATTO della scheda confermata nello step di
    # identificazione (google_match_name), che è già disambiguato.
    keyword = (keyword_override or business.google_match_name
               or business.name).strip()
    keyword = keyword.replace(" - ", " ").replace(" – ", " ")
    if business.city and business.city.lower() not in keyword.lower():
        keyword = f"{keyword} {business.city}".strip()
    task = {"language_code": "it", "depth": depth, "priority": 2,
            "keyword": keyword[:700], "location_name": "Italy"}
    try:
        r = httpx.post(f"{BASE}/task_post", headers=_auth_header(),
                       json=[task], timeout=30)
        r.raise_for_status()
        tasks = r.json().get("tasks") or []
        if tasks and tasks[0].get("status_code") in (20000, 20100):
            return tasks[0]["id"]
        _log_task_error(tasks, keyword)
    except httpx.HTTPError:
        pass
    return None


def _log_task_error(tasks: list, keyword: str) -> None:
    """Traccia il motivo del rifiuto (visibile nei log Render / terminale)."""
    try:
        t = (tasks or [{}])[0]
        print(f"[ekko][dataforseo] task rifiutato per '{keyword}': "
              f"{t.get('status_code')} {t.get('status_message')}", flush=True)
    except Exception:  # noqa: BLE001
        pass


# Stati "in lavorazione" DataForSEO: tutto il resto è terminale.
IN_PROGRESS_CODES = {20100, 40601, 40602}   # created / handed / in queue


def collect(task_id: str, expect_name: str | None = None):
    """(items, total) se pronto; (None, None) se in coda.
    expect_name è ignorato qui (il place_id confermato garantisce già il
    match); la firma è uniforme a quella del connettore TripAdvisor.
    Un task in ERRORE terminale ritorna ([], None): sblocca la dashboard
    invece di restare 'in raccolta' per sempre."""
    if not (task_id and os.environ.get("DATAFORSEO_AUTH")):
        return [], None   # niente credenziali: non resterà mai pronto
    try:
        g = httpx.get(f"{BASE}/task_get/{task_id}", headers=_auth_header(), timeout=30)
        g.raise_for_status()
        t = (g.json().get("tasks") or [])
        if not t:
            return None, None
        sc = t[0].get("status_code")
        if sc == 20000 and t[0].get("result"):
            res = t[0]["result"][0] or {}
            return (res.get("items") or []), res.get("reviews_count")
        if sc in IN_PROGRESS_CODES:
            return None, None
        # terminale (es. 40501 invalid field, 40200 credito, 20000 senza result)
        return [], None
    except httpx.HTTPError:
        return None, None


def normalize_items(items: list, business: BusinessRef, run: ConnectorRun,
                    location: str | None = None):
    """Converte gli item DataForSEO in FeedbackObject.
    location: etichetta della sede (gruppi multi-sede).
    Le recensioni precedenti al cut-off (2025) vengono scartate."""
    conn = DataForSeoGoogleConnector()
    cutoff = google_reviews_since()
    for item in items:
        fo = conn._normalize(item, business, run)
        if fo is not None and fo.published_at >= cutoff:
            if location:
                fo.location = location
                # id/dedup per sede: due sedi possono avere id nativi uguali
                fo.source_native_id = f"{location}|{fo.source_native_id}"
                fo.id = make_feedback_id("google", business.id,
                                         fo.source_native_id)
            run.fetched += 1
            yield fo
