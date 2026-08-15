"""Pipeline di ingestion: connettore -> stadio 0 -> dedup -> storage.

Nello skeleton è sincrona; in Fase 1 il bus (Kafka) disaccoppia i passi
senza cambiare i contratti (stessa interfaccia connettore, stesso
FeedbackObject).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ekko.connectors.base import BaseConnector, ConnectorDisabled, ConnectorRun
from ekko.core.models import BusinessRef, FeedbackObject
from ekko.core.scoring import ScoreBreakdown, compute_score
from ekko.core.sentiment import enrich_stage0
from ekko.storage import db


@dataclass
class IngestReport:
    business_id: str
    runs: list[dict] = field(default_factory=list)
    stored: int = 0
    duplicates: int = 0
    total_cost_eur: float = 0.0


def save_business(business: BusinessRef, owner_id: str | None = None) -> None:
    db.init_db()
    db.upsert_business(business, owner_id=owner_id)


def ingest(business: BusinessRef, connectors: list[BaseConnector],
           backfill: bool = True) -> IngestReport:
    save_business(business)
    report = IngestReport(business_id=business.id)

    for connector in connectors:
        run = ConnectorRun()
        try:
            since = None if backfill else db.max_published(
                business.id, connector.source_name)
            stream = (connector.fetch_backfill(business, run) if backfill
                      else connector.fetch_incremental(business, since, run))
            for fo in stream:
                fo = enrich_stage0(fo)
                if db.insert_feedback(fo):
                    report.stored += 1
                else:
                    report.duplicates += 1
            status = "ok"
        except ConnectorDisabled as e:
            status = f"disabled: {e}"
        except Exception as e:
            # una fonte che fallisce non deve far crashare la ricerca
            status = f"error: {type(e).__name__}: {e}"
        report.runs.append({
            "source": connector.source_name, "run_id": run.run_id,
            "fetched": run.fetched, "cost_eur": round(run.cost_eur, 4),
            "status": status,
        })
        report.total_cost_eur += run.cost_eur
    return report


def load_feedback(business_id: str) -> list[FeedbackObject]:
    db.init_db()
    return db.load_feedback(business_id)


def score_business(business_id: str) -> ScoreBreakdown:
    feedback = load_feedback(business_id)
    breakdown = compute_score(feedback)
    db.save_score(business_id, datetime.now(timezone.utc), breakdown.score,
                  breakdown.model_dump_json())
    return breakdown
