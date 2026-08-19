"""Connector Framework — interfaccia standard di ogni connettore (piano §2.5, §6.1).

Ogni connettore dichiara la propria "corsia" legale e implementa la stessa
interfaccia: fetch incrementale, backfill, health, cost meter. Il kill-switch
per fonte è un requisito legale: EKKO_DISABLED_SOURCES="google,trustpilot".
"""
from __future__ import annotations

import abc
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from ekko.core.models import BusinessRef, FeedbackObject

# Data minima delle recensioni GOOGLE gestite dalla piattaforma (richiesta
# prodotto: si parte dal 2025). Vale per tutte le vie di acquisizione Google
# (GBP corsia A e DataForSEO corsia B). Configurabile via env, formato ISO.
GOOGLE_REVIEWS_SINCE_DEFAULT = "2025-01-01"


def google_reviews_since() -> datetime:
    """Cut-off (tz-aware UTC) sotto il quale le recensioni Google si scartano."""
    raw = os.environ.get("EKKO_GOOGLE_REVIEWS_SINCE",
                         GOOGLE_REVIEWS_SINCE_DEFAULT)
    try:
        d = datetime.fromisoformat(raw)
    except ValueError:
        d = datetime.fromisoformat(GOOGLE_REVIEWS_SINCE_DEFAULT)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class ConnectorRun:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    fetched: int = 0
    errors: int = 0
    cost_eur: float = 0.0  # cost meter: € spesi in questa run (provider/API)


class ConnectorDisabled(RuntimeError):
    """Sollevata quando il kill-switch della fonte è attivo."""


class BaseConnector(abc.ABC):
    """Interfaccia comune. Sottoclassi: una per fonte."""

    source_name: str = "base"
    lane: str = "official_api"  # official_api | licensed_provider | public_crawl | demo
    cost_per_record_eur: float = 0.0

    def _check_kill_switch(self) -> None:
        disabled = os.environ.get("EKKO_DISABLED_SOURCES", "")
        if self.source_name in [s.strip() for s in disabled.split(",") if s.strip()]:
            raise ConnectorDisabled(f"Fonte '{self.source_name}' disabilitata da kill-switch")

    @abc.abstractmethod
    def fetch_incremental(
        self, business: BusinessRef, since: datetime | None, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        """Nuovi feedback dall'ultima esecuzione."""

    def fetch_backfill(
        self, business: BusinessRef, run: ConnectorRun
    ) -> Iterator[FeedbackObject]:
        """Storico completo (default: delega all'incrementale senza cursore)."""
        yield from self.fetch_incremental(business, since=None, run=run)

    def health(self) -> dict:
        return {"source": self.source_name, "lane": self.lane, "ok": True}
