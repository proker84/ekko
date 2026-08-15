"""Ekko Score v1 — motore di scoring interamente deterministico (piano §7.1).

Zero AI, zero token: formula versionata, spiegabile, con decomposizione
dei contributi. Componenti:

  1. base        - media rating ponderata per peso fonte e recency decay
  2. volume      - correzione bayesiana per basse numerosità
  3. trend       - confronto finestra 90gg vs 90gg precedenti
  4. engagement  - tasso di risposta del brand alle recensioni

Output: score 0-100 + breakdown esplicativo per la UI
("il tuo score è 68: -9 da consegne, -4 da customer care...").
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

from pydantic import BaseModel

from .models import SOURCE_WEIGHTS, FeedbackObject

SCORING_VERSION = "1.0.0"

# Parametri della formula (versionati insieme al codice).
HALF_LIFE_DAYS = 180          # recency half-life: 6 mesi
BAYES_PRIOR_MEAN = 0.70       # prior: 3.5 stelle
BAYES_PRIOR_WEIGHT = 20       # equivalente a 20 recensioni "neutre"
TREND_WINDOW_DAYS = 90
W_BASE, W_TREND, W_ENGAGEMENT = 0.70, 0.15, 0.15


class SourceBreakdown(BaseModel):
    source: str
    count: int
    weighted_mean_rating: float  # 0..1
    contribution: float          # punti score attribuibili alla fonte


class ScoreBreakdown(BaseModel):
    version: str
    score: float                 # 0..100
    base_component: float        # 0..100 (peso 70%)
    trend_component: float       # 0..100 (peso 15%)
    engagement_component: float  # 0..100 (peso 15%)
    n_feedback: int
    n_recent_90d: int
    response_rate: float
    trend_delta: float           # variazione rating medio 90d vs 90d precedenti
    by_source: list[SourceBreakdown]
    explanations: list[str]


def _recency_weight(published_at: datetime, now: datetime) -> float:
    age_days = max(0.0, (now - published_at).total_seconds() / 86400)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def compute_score(
    feedback: Iterable[FeedbackObject], now: datetime | None = None
) -> ScoreBreakdown:
    now = now or datetime.now(timezone.utc)
    items = [f for f in feedback if f.rating is not None]
    if not items:
        return ScoreBreakdown(
            version=SCORING_VERSION, score=50.0, base_component=50.0,
            trend_component=50.0, engagement_component=50.0, n_feedback=0,
            n_recent_90d=0, response_rate=0.0, trend_delta=0.0, by_source=[],
            explanations=["Nessun feedback con rating disponibile: score neutro."],
        )

    # --- 1) Base: media ponderata (fonte x recency), con correzione bayesiana
    num = den = 0.0
    per_source: dict[str, list[tuple[float, float]]] = {}
    for f in items:
        w = SOURCE_WEIGHTS.get(f.source, 0.5) * _recency_weight(f.published_at, now)
        num += w * f.rating
        den += w
        per_source.setdefault(f.source.value, []).append((w, f.rating))

    observed_mean = num / den if den else BAYES_PRIOR_MEAN
    effective_n = den  # numerosità efficace post-pesatura
    bayes_mean = (
        (BAYES_PRIOR_WEIGHT * BAYES_PRIOR_MEAN + effective_n * observed_mean)
        / (BAYES_PRIOR_WEIGHT + effective_n)
    )
    base = bayes_mean * 100

    # --- 2) Trend: 90 giorni recenti vs 90 precedenti
    recent_cut = now - timedelta(days=TREND_WINDOW_DAYS)
    prev_cut = now - timedelta(days=2 * TREND_WINDOW_DAYS)
    recent = [f.rating for f in items if f.published_at >= recent_cut]
    prev = [f.rating for f in items if prev_cut <= f.published_at < recent_cut]
    if recent and prev:
        delta = (sum(recent) / len(recent)) - (sum(prev) / len(prev))
    else:
        delta = 0.0
    # delta in [-1, 1] -> componente 0..100 con saturazione morbida
    trend = 50 + 50 * math.tanh(delta * 4)

    # --- 3) Engagement: tasso di risposta del brand
    n_replied = sum(1 for f in items if f.reply is not None)
    response_rate = n_replied / len(items)
    engagement = min(100.0, response_rate / 0.6 * 100)  # 60% risposte = pieno punteggio

    score = W_BASE * base + W_TREND * trend + W_ENGAGEMENT * engagement

    # --- Decomposizione per fonte (contributo alla base)
    by_source = []
    for src, pairs in sorted(per_source.items()):
        sw = sum(w for w, _ in pairs)
        sm = sum(w * r for w, r in pairs) / sw if sw else 0.0
        by_source.append(SourceBreakdown(
            source=src, count=len(pairs), weighted_mean_rating=round(sm, 4),
            contribution=round(W_BASE * (sw / den) * sm * 100, 2),
        ))

    explanations = _explain(score, base, trend, engagement, delta, response_rate, len(recent))
    return ScoreBreakdown(
        version=SCORING_VERSION,
        score=round(score, 1),
        base_component=round(base, 1),
        trend_component=round(trend, 1),
        engagement_component=round(engagement, 1),
        n_feedback=len(items),
        n_recent_90d=len(recent),
        response_rate=round(response_rate, 3),
        trend_delta=round(delta, 4),
        by_source=by_source,
        explanations=explanations,
    )


def _explain(score, base, trend, engagement, delta, response_rate, n_recent) -> list[str]:
    out = [f"Ekko Score {score:.1f}/100 (formula v{SCORING_VERSION})."]
    stars = base / 100 * 5
    out.append(f"La qualità media pesata delle recensioni equivale a ~{stars:.1f}/5 stelle.")
    if delta > 0.02:
        out.append(f"Trend positivo: il rating degli ultimi 90 giorni è in crescita (+{delta*5:.2f} stelle equivalenti).")
    elif delta < -0.02:
        out.append(f"Trend negativo: il rating degli ultimi 90 giorni è in calo ({delta*5:.2f} stelle equivalenti).")
    else:
        out.append("Trend stabile negli ultimi 90 giorni.")
    if response_rate < 0.3:
        out.append(f"Solo il {response_rate*100:.0f}% delle recensioni riceve risposta: rispondere di più è la leva più rapida per salire.")
    else:
        out.append(f"Buon tasso di risposta del brand: {response_rate*100:.0f}%.")
    if n_recent < 5:
        out.append("Poche recensioni recenti: una campagna di sollecito aumenterebbe volume e affidabilità dello score.")
    return out
