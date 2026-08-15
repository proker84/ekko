"""Stadio 0 dell'arricchimento: gratuito e deterministico (piano §2.6).

- sentiment dal rating (una recensione 1 stella è già etichettata)
- assegnazione topic via keyword della tassonomia v1
- urgenza euristica
Niente modelli, niente token: gli stadi 1-3 arriveranno in Fase 3.
"""
from __future__ import annotations

from .models import Enrichment, FeedbackObject, Urgency
from .taxonomy import match_topics

NEGATIVE_URGENT_HINTS = (
    "truffa", "denuncia", "avvocato", "rimborso negato", "pericoloso",
    "intossica", "frode", "mai arrivato", "soldi spariti",
)


def enrich_stage0(f: FeedbackObject) -> FeedbackObject:
    e = Enrichment(stage=0)
    if f.rating is not None:
        e.sentiment = round((f.rating - 0.5) * 2, 3)  # 0..1 -> -1..+1
    text = (f.text or "").lower()
    e.topics = match_topics(text)
    if f.rating is not None and f.rating <= 0.4:
        e.urgency = Urgency.HIGH if any(h in text for h in NEGATIVE_URGENT_HINTS) else Urgency.MEDIUM
    else:
        e.urgency = Urgency.LOW
    f.enrichment = e
    return f
