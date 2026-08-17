"""Connettore Facebook SENZA login del cliente (corsia B, provider licenziato).

PERCHÉ ESISTE: la Graph API di Meta espone le recensioni solo delle pagine di
cui si possiede un token — quindi richiederebbe che ogni azienda analizzata
faccia login. Per l'analisi di aziende TERZE si usa quindi un provider di dati
(stessa logica del connettore DataForSEO per Google): il provider raccoglie e
noi consumiamo un'API. Nessun accesso richiesto all'azienda analizzata.

Provider di riferimento: Bright Data — Web Scraper API (dataset "Facebook
company reviews"). Flusso asincrono, identico nello spirito a DataForSEO:
  POST https://api.brightdata.com/datasets/v3/trigger?dataset_id=...   -> snapshot_id
  GET  https://api.brightdata.com/datasets/v3/progress/<snapshot_id>   -> status
  GET  https://api.brightdata.com/datasets/v3/snapshot/<snapshot_id>   -> dati

Configurazione (.env):
  BRIGHTDATA_TOKEN=...                 chiave API dell'account
  BRIGHTDATA_FB_REVIEWS_DATASET=gd_... id del dataset "Facebook reviews"
                                       (lo si copia dalla Scraper Library)
Il connettore resta spento finché entrambe non sono impostate.

Nota: la mappatura dei campi è volutamente TOLLERANTE (i provider cambiano i
nomi delle colonne senza preavviso): si riusano le euristiche di pubscrape.
"""
from __future__ import annotations

import os
from datetime import datetime

import httpx

from ekko.core.models import (BusinessRef, FeedbackObject, Lineage, Reply,
                              Source, make_feedback_id, pseudonymize_author)
from . import pubscrape
from .base import ConnectorRun

TRIGGER = "https://api.brightdata.com/datasets/v3/trigger"
PROGRESS = "https://api.brightdata.com/datasets/v3/progress/{sid}"
SNAPSHOT = "https://api.brightdata.com/datasets/v3/snapshot/{sid}"


def enabled() -> bool:
    return bool(os.environ.get("BRIGHTDATA_TOKEN")
                and os.environ.get("BRIGHTDATA_FB_REVIEWS_DATASET"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('BRIGHTDATA_TOKEN')}",
            "Content-Type": "application/json"}


def post_task(business: BusinessRef, url_override: str | None = None) -> str | None:
    """Avvia la raccolta per una pagina Facebook; ritorna lo snapshot_id."""
    if not enabled():
        return None
    url = url_override or business.facebook_url
    if not url:
        return None
    limit = business.review_depth or 200
    try:
        r = httpx.post(
            TRIGGER,
            headers=_headers(),
            params={"dataset_id": os.environ["BRIGHTDATA_FB_REVIEWS_DATASET"],
                    "format": "json"},
            json=[{"url": url, "num_of_reviews": limit}],
            timeout=30)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(body, dict):
        return body.get("snapshot_id") or body.get("id")
    return None


def collect(snapshot_id: str, expect_name: str | None = None):
    """(items, total) se pronto; (None, None) se in corso; ([], None) se fallito."""
    if not (snapshot_id and enabled()):
        return [], None
    try:
        p = httpx.get(PROGRESS.format(sid=snapshot_id), headers=_headers(),
                      timeout=25)
        p.raise_for_status()
        status = (p.json() or {}).get("status")
    except (httpx.HTTPError, ValueError):
        return None, None
    if status in ("starting", "running", "collecting", "building"):
        return None, None
    if status != "ready":
        return [], None                     # failed / cancelled
    try:
        d = httpx.get(SNAPSHOT.format(sid=snapshot_id), headers=_headers(),
                      params={"format": "json"}, timeout=60)
        d.raise_for_status()
        data = d.json()
    except (httpx.HTTPError, ValueError):
        return None, None
    items = data if isinstance(data, list) else (data.get("data") or [])
    return items, len(items)


def _pick(item: dict, *keys):
    low = {k.lower().replace("_", ""): v for k, v in item.items()}
    for k in keys:
        v = low.get(k.replace("_", ""))
        if v not in (None, ""):
            return v
    return None


def normalize_items(items: list, business: BusinessRef, run: ConnectorRun,
                    location: str | None = None):
    """Converte i record del provider in FeedbackObject (mappatura tollerante)."""
    for it in items:
        if not isinstance(it, dict):
            continue
        d = pubscrape.parse_date(
            _pick(it, "date", "review_date", "created", "created_time",
                  "post_date", "timestamp"))
        if d is None:
            continue
        raw_rating = _pick(it, "rating", "stars", "score", "rating_value")
        recommends = _pick(it, "is_recommended", "recommendation_type",
                           "recommends")
        if raw_rating is None and recommends is not None:
            pos = str(recommends).lower() in ("true", "1", "positive", "yes",
                                              "recommended")
            raw_rating = 5 if pos else 1
        try:
            stars = float(raw_rating)
        except (TypeError, ValueError):
            continue
        text = _pick(it, "review_text", "text", "comment", "content", "body")
        author = _pick(it, "author", "user_name", "username", "reviewer",
                       "name") or "anon"
        reply = _pick(it, "owner_reply", "reply_text", "response")
        native = str(_pick(it, "review_id", "id", "url")
                     or f"{author}|{d.isoformat()}|{stars}")
        if location:
            native = f"{location}|{native}"
        run.fetched += 1
        yield FeedbackObject(
            id=make_feedback_id("meta", business.id, native),
            source=Source.META,
            source_native_id=native,
            business_id=business.id,
            author_hash=pseudonymize_author(str(author)),
            text=str(text) if text else None,
            rating=FeedbackObject.normalize_rating(stars),
            published_at=d,
            location=location,
            reply=Reply(text=str(reply)) if reply else None,
            lineage=Lineage(connector="facebook_public", run_id=run.run_id,
                            license="licensed_provider"),
        )


def diagnose(url: str) -> dict:
    """`python -m ekko.cli fbtest <url pagina facebook>` — test end-to-end."""
    import time
    if not enabled():
        return {"ok": False, "error": "BRIGHTDATA_TOKEN e/o "
                "BRIGHTDATA_FB_REVIEWS_DATASET non impostate in .env"}
    biz = BusinessRef(id="diag", name="diag", facebook_url=url, review_depth=50)
    sid = post_task(biz)
    if not sid:
        return {"ok": False, "url": url,
                "error": "trigger rifiutato: controlla token e dataset_id"}
    for i in range(30):
        time.sleep(10)
        items, total = collect(sid)
        if items is None:
            continue
        if not items:
            return {"ok": False, "snapshot": sid,
                    "error": "raccolta fallita o vuota"}
        run = ConnectorRun()
        norm = list(normalize_items(items, biz, run))
        sample = ({"stars": round((norm[0].rating or 0) * 5, 1),
                   "date": norm[0].published_at.isoformat(),
                   "text": (norm[0].text or "")[:80]} if norm else None)
        return {"ok": bool(norm), "snapshot": sid, "records": len(items),
                "normalizzate": len(norm), "secondi": (i + 1) * 10,
                "sample": sample}
    return {"ok": False, "snapshot": sid, "error": "timeout (5 min)"}
