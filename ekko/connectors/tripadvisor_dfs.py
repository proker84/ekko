"""Connettore TripAdvisor (corsia B, licensed_provider) — via DataForSEO.

Stesso account e stesso pattern task-based del connettore Google:
task_post -> (coda 2-4 min) -> task_get. Endpoint:
  https://api.dataforseo.com/v3/business_data/tripadvisor/reviews

Attivo quando DATAFORSEO_AUTH è impostata e EKKO_ENABLE_TRIPADVISOR != "0"
(default: acceso — utile per ristorazione/ospitalità/attività locali).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Source,
                              make_feedback_id, pseudonymize_author)
from .base import ConnectorRun

BASE = "https://api.dataforseo.com/v3/business_data/tripadvisor/reviews"
DEPTH = int(os.environ.get("EKKO_TRIPADVISOR_DEPTH", "0") or 0)


def enabled() -> bool:
    return bool(os.environ.get("DATAFORSEO_AUTH")) and \
        os.environ.get("EKKO_ENABLE_TRIPADVISOR", "1") != "0"


def _auth_header() -> dict:
    return {"Authorization": f"Basic {os.environ.get('DATAFORSEO_AUTH')}",
            "Content-Type": "application/json"}


def post_task(business: BusinessRef) -> str | None:
    """Posta il task recensioni TripAdvisor e ritorna il task_id."""
    if not enabled():
        return None
    depth = DEPTH or min(business.review_depth or 100, 1000)
    task = {"language_code": "it", "depth": depth, "priority": 2}
    if business.tripadvisor_url_path:
        # pagina CONFERMATA nello step di identificazione: match esatto
        task["url_path"] = business.tripadvisor_url_path
    else:
        task["keyword"] = f"{business.name} {business.city or ''}".strip()
        task["location_name"] = "Italy"
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


IN_PROGRESS_CODES = {20100, 40601, 40602}   # created / handed / in queue


def collect(task_id: str, expect_name: str | None = None):
    """(items, total) se pronto; (None, None) se in coda; ([], None) se in
    errore terminale. Con expect_name valida che l'attività trovata sia
    DAVVERO quella cercata: la ricerca per keyword su TripAdvisor può
    agganciare un omonimo (es. un ristorante invece del concessionario) —
    in quel caso i risultati vengono SCARTATI (meglio zero che sporchi)."""
    if not (task_id and os.environ.get("DATAFORSEO_AUTH")):
        return [], None
    try:
        g = httpx.get(f"{BASE}/task_get/{task_id}", headers=_auth_header(), timeout=30)
        g.raise_for_status()
        t = (g.json().get("tasks") or [])
        if not t:
            return None, None
        sc = t[0].get("status_code")
        if sc == 20000 and t[0].get("result"):
            res = t[0]["result"][0] or {}
            title = res.get("title") or ""
            if expect_name and title:
                from ekko.core.matching import confidence
                if confidence(expect_name, title) < 50:
                    return [], None   # attività sbagliata: scarta tutto
            return (res.get("items") or []), res.get("reviews_count")
        if sc in IN_PROGRESS_CODES:
            return None, None
        return [], None
    except httpx.HTTPError:
        return None, None


def _parse_ts(item: dict) -> datetime | None:
    for key in ("timestamp", "date_of_visit", "date"):
        v = item.get(key)
        if not v:
            continue
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def normalize_items(items: list, business: BusinessRef, run: ConnectorRun):
    """Converte gli item DataForSEO TripAdvisor in FeedbackObject."""
    for item in items:
        d = _parse_ts(item)
        rating = item.get("rating")
        if isinstance(rating, dict):
            value, vmax = rating.get("value"), rating.get("rating_max") or 5
        else:
            value, vmax = rating, 5
        if d is None or value is None:
            continue
        author = ((item.get("user_profile") or {}).get("name")
                  or (item.get("user_profile") or {}).get("url") or "anon")
        native = item.get("review_id") or item.get("url") or \
            f"{author}|{d.isoformat()}|{value}"
        text = item.get("review_text") or item.get("title")
        run.fetched += 1
        yield FeedbackObject(
            id=make_feedback_id("tripadvisor", business.id, str(native)),
            source=Source.TRIPADVISOR,
            source_native_id=str(native),
            business_id=business.id,
            author_hash=pseudonymize_author(str(author)),
            text=text,
            rating=FeedbackObject.normalize_rating(float(value), float(vmax)),
            published_at=d,
            lineage=Lineage(connector="tripadvisor_dfs", run_id=run.run_id,
                            license="licensed_provider"),
        )
