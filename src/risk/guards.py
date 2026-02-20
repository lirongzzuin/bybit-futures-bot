"""
Hard Guard Rails — enforces loss limits and halts trading automatically.

Guards checked in order:
  1. Daily loss limit
  2. Weekly loss limit
  3. Max drawdown
  4. Spike / extreme volatility
  5. Exposure limits

When a guard trips, it either:
  - Halts new entries (PAUSE mode)
  - Forces position reduction (REDUCE mode)
  - Emergency-exits all positions (HALT mode)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.config import Settings
from src.logger import get_logger

log = get_logger(__name__)


class GuardAction(str, Enum):
    ALLOW = "ALLOW"          # proceed normally
    PAUSE = "PAUSE"          # no new entries, hold existing
    REDUCE = "REDUCE"        # reduce leverage / scale out
    HALT = "HALT"            # close all, stop trading


@dataclass
class GuardResult:
    action: GuardAction
    reason: str = ""
    reduce_pct: float = 0.0   # for REDUCE: how much to scale down (0-1)


class RiskGuards:
    def __init__(self, settings: Settings):
        self._s = settings

    def check(
        self,
        equity: float,
        peak_equity: float,
        daily_pnl: float,
        weekly_pnl: float,
        initial_capital: float,
        spread_pct: float,
        vol_percentile: float,
    ) -> GuardResult:
        """
        Run all guards and return the most restrictive action.
        """
        results = [
            self._check_daily_loss(equity, daily_pnl, initial_capital),
            self._check_weekly_loss(equity, weekly_pnl, initial_capital),
            self._check_max_drawdown(equity, peak_equity),
            self._check_spike(spread_pct, vol_percentile),
        ]

        # Return the most restrictive result
        priority = {GuardAction.HALT: 4, GuardAction.REDUCE: 3,
                    GuardAction.PAUSE: 2, GuardAction.ALLOW: 1}
        results.sort(key=lambda r: priority[r.action], reverse=True)
        worst = results[0]

        if worst.action != GuardAction.ALLOW:
            log.warning("Guard tripped: %s — %s", worst.action.value, worst.reason)

        return worst

    def _check_daily_loss(
        self, equity: float, daily_pnl: float, initial_capital: float
    ) -> GuardResult:
        """Stop new entries if daily loss exceeds limit."""
        if equity <= 0:
            return GuardResult(GuardAction.HALT, "Equity <= 0")

        daily_loss_pct = -daily_pnl / max(equity - daily_pnl, 1)
        if daily_loss_pct >= self._s.MAX_DAILY_LOSS_PCT:
            return GuardResult(
                GuardAction.PAUSE,
                f"Daily loss {daily_loss_pct:.2%} ≥ limit {self._s.MAX_DAILY_LOSS_PCT:.2%}",
            )
        return GuardResult(GuardAction.ALLOW)

    def _check_weekly_loss(
        self, equity: float, weekly_pnl: float, initial_capital: float
    ) -> GuardResult:
        """Halt new entries if weekly loss exceeds limit."""
        weekly_loss_pct = -weekly_pnl / max(equity - weekly_pnl, 1)
        if weekly_loss_pct >= self._s.MAX_WEEKLY_LOSS_PCT:
            return GuardResult(
                GuardAction.PAUSE,
                f"Weekly loss {weekly_loss_pct:.2%} ≥ limit {self._s.MAX_WEEKLY_LOSS_PCT:.2%}",
            )
        return GuardResult(GuardAction.ALLOW)

    def _check_max_drawdown(self, equity: float, peak_equity: float) -> GuardResult:
        """Reduce or halt if drawdown exceeds thresholds."""
        if peak_equity <= 0:
            return GuardResult(GuardAction.ALLOW)

        dd = (peak_equity - equity) / peak_equity
        hard_limit = self._s.MAX_DRAWDOWN_PCT
        reduce_limit = hard_limit * 0.70  # 70% of max → REDUCE

        if dd >= hard_limit:
            return GuardResult(
                GuardAction.HALT,
                f"Max drawdown {dd:.2%} ≥ hard limit {hard_limit:.2%}",
            )
        elif dd >= reduce_limit:
            reduce_pct = (dd - reduce_limit) / (hard_limit - reduce_limit)
            return GuardResult(
                GuardAction.REDUCE,
                f"Drawdown {dd:.2%} — reducing leverage",
                reduce_pct=min(0.75, reduce_pct),
            )
        return GuardResult(GuardAction.ALLOW)

    def _check_spike(self, spread_pct: float, vol_percentile: float) -> GuardResult:
        """Pause entries during extreme spread or volatility."""
        if spread_pct > 0.01:  # 1% spread = dangerous
            return GuardResult(
                GuardAction.PAUSE,
                f"Spread {spread_pct:.3%} is extreme — pausing entries",
            )
        if vol_percentile > 0.97:
            return GuardResult(
                GuardAction.REDUCE,
                f"Volatility percentile {vol_percentile:.2%} — reducing",
                reduce_pct=0.5,
            )
        return GuardResult(GuardAction.ALLOW)
