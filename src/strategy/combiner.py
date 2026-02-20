"""
Signal Combiner — applies regime-based weights to the signal bundle
and produces a final CombinedSignal with direction and strength.

Category weights per regime (vwap=0.10, orderbook=0.05 added; others renormalized):
  TRENDING_UP/DOWN : trend(0.35) + ict(0.30) + vwap(0.10) + volume(0.12) + volatility(0.08) + reversion(0.05)
  RANGING          : reversion(0.30) + ict(0.25) + vwap(0.10) + volume(0.18) + orderbook(0.05) + trend(0.07) + volatility(0.05)
  HIGH_VOLATILITY  : all weights reduced; reversion suppressed; volatility elevated
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from src.config import Settings
from src.strategy.regime import Regime, RegimeResult
from src.strategy.signals import SignalBundle, Signal


@dataclass
class CombinedSignal:
    direction: int       # +1 long, -1 short, 0 neutral
    strength: float      # 0.0 – 1.0 (how confident we are)
    regime: Regime
    long_score: float    # raw long score
    short_score: float   # raw short score
    category_scores: Dict[str, float]

    @property
    def is_long(self) -> bool:
        return self.direction == 1 and self.strength > 0

    @property
    def is_short(self) -> bool:
        return self.direction == -1 and self.strength > 0


# ── Category weights by regime ────────────────────────────────────────────────

_REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
    Regime.TRENDING_UP: {
        "trend": 0.30, "ict": 0.38, "vwap": 0.10, "volume": 0.12,
        "volatility": 0.07, "reversion": 0.03, "orderbook": 0.00,
    },
    Regime.TRENDING_DOWN: {
        "trend": 0.30, "ict": 0.38, "vwap": 0.10, "volume": 0.12,
        "volatility": 0.07, "reversion": 0.03, "orderbook": 0.00,
    },
    Regime.RANGING: {
        "trend": 0.06, "ict": 0.30, "vwap": 0.12, "volume": 0.25,
        "volatility": 0.04, "reversion": 0.20, "orderbook": 0.03,
    },
    Regime.HIGH_VOLATILITY: {
        "trend": 0.18, "ict": 0.27, "vwap": 0.10, "volume": 0.22,
        "volatility": 0.13, "reversion": 0.05, "orderbook": 0.05,
    },
}


class SignalCombiner:
    def __init__(self, settings: Settings):
        self._s = settings

    def combine(
        self,
        bundle: SignalBundle,
        regime_result: RegimeResult,
    ) -> CombinedSignal:
        regime = regime_result.regime
        weights = _REGIME_WEIGHTS.get(regime, _REGIME_WEIGHTS[Regime.RANGING])

        # ── Per-category weighted scores ──────────────────────────────────────
        category_long: Dict[str, float] = {}
        category_short: Dict[str, float] = {}
        category_scores: Dict[str, float] = {}

        for cat in ("trend", "reversion", "volatility", "volume", "ict", "vwap", "orderbook"):
            cat_signals = bundle.by_category(cat)
            if not cat_signals:
                category_long[cat] = 0.0
                category_short[cat] = 0.0
                category_scores[cat] = 0.0
                continue

            total_w = sum(s.confidence for s in cat_signals)
            if total_w == 0:
                category_scores[cat] = 0.0
                continue

            # Separate long and short components
            long_score = sum(
                s.confidence for s in cat_signals if s.direction == 1
            ) / total_w
            short_score = sum(
                s.confidence for s in cat_signals if s.direction == -1
            ) / total_w

            category_long[cat] = long_score
            category_short[cat] = short_score
            category_scores[cat] = (long_score - short_score)  # net: -1 to +1

        # ── Aggregate with regime weights (renormalized over active categories) ─
        # Categories with no signal data (vwap=0, oi=0, orderbook=0 in backtest)
        # are excluded so inactive data doesn't dilute the scores.
        active_weight_sum = sum(
            weights.get(c, 0) for c in weights
            if category_long.get(c, 0) > 0 or category_short.get(c, 0) > 0
        )
        if active_weight_sum > 0:
            norm = 1.0 / active_weight_sum
            total_long = sum(weights.get(c, 0) * norm * category_long.get(c, 0) for c in weights)
            total_short = sum(weights.get(c, 0) * norm * category_short.get(c, 0) for c in weights)
        else:
            total_long = 0.0
            total_short = 0.0

        # ── Regime directional filter ─────────────────────────────────────────
        # In a strong trend, suppress counter-trend signals
        if regime == Regime.TRENDING_UP:
            total_short *= 0.4   # only allow very strong shorts
        elif regime == Regime.TRENDING_DOWN:
            total_long *= 0.4

        # High volatility: reduce all scores (caution mode)
        if regime == Regime.HIGH_VOLATILITY:
            total_long *= 0.6
            total_short *= 0.6

        # ── Determine direction and strength ──────────────────────────────────
        threshold = self._s.SIGNAL_ENTRY_THRESHOLD

        if total_long >= threshold and total_long > total_short:
            direction = 1
            strength = total_long
        elif total_short >= threshold and total_short > total_long:
            direction = -1
            strength = total_short
        else:
            direction = 0
            strength = max(total_long, total_short)

        # Regime confidence amplifies strength (1.0–1.3x).
        # Range: 1.0 (conf=0, no boost) → 1.3 (conf=1, 30% boost).
        # Never reduces strength (was 0.7–1.0 before, which penalized low-confidence regimes).
        strength = min(1.0, strength * (1.0 + 0.3 * regime_result.confidence))

        return CombinedSignal(
            direction=direction,
            strength=strength,
            regime=regime,
            long_score=total_long,
            short_score=total_short,
            category_scores=category_scores,
        )
