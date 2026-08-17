"""Feedback Object v1 — lo schema normalizzato unico di Ekko.

Ogni menzione/recensione/commento, da qualunque fonte, viene convertito
in questo schema prima di entrare nella piattaforma (vedi piano §2.3).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Source(str, Enum):
    GOOGLE = "google"
    TRUSTPILOT = "trustpilot"
    META = "meta"                      # Facebook/Instagram
    TRIPADVISOR = "tripadvisor"
    AUTOSCOUT24 = "autoscout24"        # recensioni concessionari (automotive)
    FEEDATY = "feedaty"                # recensioni certificate (Zoorate)
    VERIFIED_REVIEWS = "recensioni_verificate"  # Avis Vérifiés Italia
    DEMO = "demo"
    CONVERSATIONAL = "conversational"


# Pesi di affidabilità/traffico per fonte, usati dallo scoring (0..1).
SOURCE_WEIGHTS: dict[Source, float] = {
    Source.GOOGLE: 1.0,
    Source.TRUSTPILOT: 0.85,
    Source.META: 0.6,
    Source.TRIPADVISOR: 0.8,
    Source.AUTOSCOUT24: 0.9,           # verticale forte per l'automotive
    Source.FEEDATY: 0.7,
    Source.VERIFIED_REVIEWS: 0.7,
    Source.DEMO: 1.0,
    Source.CONVERSATIONAL: 0.4,
}


class BusinessRef(BaseModel):
    """Riferimento all'azienda target (nucleo del futuro Business Graph)."""

    id: str
    name: str
    vat_number: Optional[str] = None  # P.IVA
    city: Optional[str] = None
    country: str = "IT"
    vertical: Optional[str] = None
    # Identità sulle piattaforme esterne (entity resolution)
    google_place_id: Optional[str] = None
    trustpilot_business_unit_id: Optional[str] = None
    domain: Optional[str] = None
    total_reviews_google: Optional[int] = None
    dfs_task_id: Optional[str] = None
    dfs_pending: bool = False
    review_depth: Optional[int] = None
    # nome ESATTO della scheda Google confermata (keyword precisa per DataForSEO)
    google_match_name: Optional[str] = None
    dfs_retried: bool = False        # un solo tentativo di recupero automatico
    # --- GRUPPI / CATENE: più sedi analizzate insieme -------------------
    # ogni task: {"id","label","pending","total","retried"}
    dfs_tasks: list[dict] = Field(default_factory=list)      # Google, una per sede
    ta_tasks: list[dict] = Field(default_factory=list)       # TripAdvisor
    autoscout24_urls: list[str] = Field(default_factory=list)  # più concessionari
    fb_tasks: list[dict] = Field(default_factory=list)        # Facebook, una per pagina
    # TripAdvisor via DataForSEO (task asincrono separato da Google)
    ta_task_id: Optional[str] = None
    ta_pending: bool = False
    total_reviews_tripadvisor: Optional[int] = None
    # url_path TripAdvisor confermato nello step di identificazione (match esatto)
    tripadvisor_url_path: Optional[str] = None
    # Automotive: URL pagina concessionario AutoScout24 (…/concessionari/<slug>)
    autoscout24_url: Optional[str] = None
    # Facebook: id pagina di proprietà (Graph API, richiede token)
    facebook_page_id: Optional[str] = None
    # Facebook SENZA login del cliente: URL pagina pubblica + task provider
    facebook_url: Optional[str] = None
    fb_pending: bool = False
    total_reviews_facebook: Optional[int] = None
    # Fonti escluse dall'utente nello step di identificazione
    skipped_sources: list[str] = Field(default_factory=list)


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRISIS = "crisis"


class Enrichment(BaseModel):
    """Popolato dagli stadi 0-3 della cascata AI. Nello skeleton: solo stadio 0."""

    sentiment: Optional[float] = None  # -1..+1
    topics: list[str] = Field(default_factory=list)
    urgency: Optional[Urgency] = None
    fake_score: Optional[float] = None
    stage: int = 0  # ultimo stadio di arricchimento applicato


class Reply(BaseModel):
    text: str
    published_at: Optional[datetime] = None


class Lineage(BaseModel):
    """Audit e compliance: da dove viene questo record (piano §2.3)."""

    connector: str
    run_id: str
    license: str = "official_api"  # official_api | licensed_provider | public_crawl | demo


def pseudonymize_author(author_identifier: str) -> str:
    """Pseudonimizzazione autore alla frontiera d'ingresso (HMAC-SHA256).

    Nessuna PII in chiaro oltre questo punto. La chiave vive solo nel
    secret manager (env var nello skeleton).
    """
    key = os.environ.get("EKKO_AUTHOR_HMAC_KEY", "dev-only-key-change-me").encode()
    return hmac.new(key, author_identifier.encode(), hashlib.sha256).hexdigest()[:32]


def make_feedback_id(source: str, business_id: str, native_id: str) -> str:
    """Id interno deterministico e univoco per (azienda, fonte, id nativo)."""
    digest = hashlib.sha256(f"{business_id}:{source}:{native_id}".encode()).hexdigest()[:16]
    return f"{source[:2]}_{digest}"


class FeedbackObject(BaseModel):
    id: str
    source: Source
    source_native_id: str
    business_id: str
    author_hash: str
    lang: str = "it"
    text: Optional[str] = None
    rating: Optional[float] = None  # normalizzato 0..1 (5 stelle -> 1.0)
    published_at: datetime
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # sede/insegna di provenienza (gruppi multi-sede); None = azienda singola
    location: Optional[str] = None
    reply: Optional[Reply] = None
    likes: int = 0
    enrichment: Enrichment = Field(default_factory=Enrichment)
    lineage: Lineage

    def dedup_key(self) -> str:
        """Chiave di dedup: stessa recensione arrivata due volte (o da due corsie).

        Include business_id: gli id nativi sono unici per fonte, ma la stessa
        recensione non deve mai collidere tra aziende diverse.
        """
        basis = f"{self.business_id}:{self.source}:{self.source_native_id}"
        return hashlib.sha256(basis.encode()).hexdigest()

    @staticmethod
    def normalize_rating(stars: float, scale_max: float = 5.0) -> float:
        return max(0.0, min(1.0, stars / scale_max))
