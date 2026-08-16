"""Entity matching — quanto è probabile che il candidato trovato su una
piattaforma sia DAVVERO l'azienda cercata.

Segnali: ragione sociale normalizzata (forme societarie rimosse), città
nell'indirizzo, dominio/URL forniti dall'utente (=100%).
Il punteggio è 0-100; la soglia di auto-conferma è 90 (sotto: scelta manuale).
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

AUTO_THRESHOLD = 90

# forme societarie e stop-word che non aiutano a distinguere le aziende
_LEGAL = re.compile(
    r"\b(s\.?r\.?l\.?s?|s\.?p\.?a\.?|s\.?n\.?c\.?|s\.?a\.?s\.?|s\.?s\.?|"
    r"societa|società|soc|coop|group|gruppo|holding|italia|the|di|del|della|"
    r"dei|delle|and|e|&)\b")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _LEGAL.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", " ", s)


def similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    base = SequenceMatcher(None, na, nb).ratio()
    # bonus se una è contenuta nell'altra ("Rossi Auto" vs "Rossi Auto di Mario Rossi")
    if na in nb or nb in na:
        base = max(base, 0.92)
    return base


def confidence(query_name: str, cand_name: str, city: str | None = None,
               cand_detail: str = "", user_supplied: bool = False) -> int:
    """0-100. user_supplied=True (URL/dominio inserito a mano) => 100."""
    if user_supplied:
        return 100
    s = similarity(query_name, cand_name)
    if city:
        in_city = norm(city) in norm(cand_detail or cand_name)
        conf = 100 * (0.78 * s + (0.22 if in_city else 0.0))
    else:
        conf = 100 * s
    return max(0, min(100, round(conf)))
