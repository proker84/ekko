"""Tassonomia Ekko v1 (sottoinsieme skeleton) — piano §5.3.

Nella Fase 0 completa: ~80 topic per 6 verticali con definizioni ed esempi.
Qui: nucleo cross-verticale con matching keyword (stadio 0). In Fase 3 il
matching keyword viene sostituito dal classificatore multi-label.
"""
from __future__ import annotations

TAXONOMY_VERSION = "1.0-skeleton"

TOPICS: dict[str, list[str]] = {
    "prodotto.qualita": ["qualità", "scadente", "ottimo prodotto", "difettoso", "eccellente"],
    "servizio.personale": ["personale", "staff", "gentile", "scortese", "maleducat", "cordiale", "disponibil"],
    "servizio.attesa": ["attesa", "aspettare", "lento", "veloce", "coda", "tempi lunghi", "puntuale"],
    "consegna.spedizione": ["consegna", "spedizione", "corriere", "pacco", "ritardo", "mai arrivato"],
    "prezzo.valore": ["prezzo", "caro", "costoso", "economico", "rapporto qualità prezzo", "conveniente"],
    "locale.pulizia": ["pulizia", "pulito", "sporco", "igiene"],
    "assistenza.postvendita": ["assistenza", "rimborso", "reso", "garanzia", "supporto", "nessuna risposta"],
    "prenotazione.booking": ["prenotazione", "prenotare", "appuntamento", "disdetta"],
}


def match_topics(text_lower: str) -> list[str]:
    if not text_lower:
        return []
    return [topic for topic, kws in TOPICS.items() if any(k in text_lower for k in kws)]
