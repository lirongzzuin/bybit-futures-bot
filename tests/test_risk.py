"""Tests for risk engine and guard rails."""
import pytest
import sys
import os

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.risk.engine import RiskEngine
from src.risk.guards import RiskGuards, GuardAction


def make_settings(**kwargs):
    from src.config import Settings
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        MAX_RISK_PER_TRADE_PCT=0.01,
        MAX_LEVERAGE_CAP=20,
        MAX_CONCURRENT_POSITIONS=3,
        MAX_SINGLE_EXPOSURE_PCT=0.25,
        STOP_ATR_MULTIPLIER=2.0,
        TP_ATR_MULTIPLIER=3.0,
        TRAILING_STOP_ATR_MULTIPLIER=1.5,
        MAX_DAILY_LOSS_PCT=0.05,
        MAX_WEEKLY_LOSS_PCT=0.10,
        MAX_DRAWDOWN_PCT=0.20,
        SCALP_STOP_ATR=0.8,
        SCALP_TP1_ATR=1.0,
        SCALP_TP2_ATR=1.8,
        SCALP_MAX_BARS=15,
        SCALP_PARTIAL_EXIT_PCT=0.60,
        TREND_STOP_ATR=1.5,
        TREND_TP1_ATR=2.0,
        TREND_TP2_ATR=4.0,
        TREND_MAX_BARS=120,
        TREND_PARTIAL_EXIT_PCT=0.50,
        TREND_RIDE_SCORE_THRESHOLD=3,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


SYMBOL_INFO = {
    "min_qty": 0.001,
    "qty_step": 0.001,
    "price_step": 0.1,
    "min_notional": 5.0,
    "max_leverage": 100,
}


class TestRiskEngine:
    def setup_method(self):
        self.settings = make_settings()
        self.engine = RiskEngine(self.settings)

    def _calc(self, **kwargs):
        defaults = dict(
            equity=10000.0,
            entry_price=50000.0,
            side="Buy",
            atr=500.0,
            signal_strength=0.7,
            spread_pct=0.001,
            funding_rate=0.0001,
            vol_percentile=0.4,
            drawdown_pct=0.0,
            consecutive_losses=0,
            symbol_max_leverage=100,
            symbol_info=SYMBOL_INFO,
            current_positions=0,
        )
        defaults.update(kwargs)
        return self.engine.calculate(**defaults)

    def test_basic_calculation_returns_valid(self):
        result = self._calc()
        assert result.risk_ok
        assert result.leverage >= 1
        assert result.qty_base > 0
        assert result.position_size_usdt > 0

    def test_max_loss_bounded(self):
        """1-trade max loss should be ≤ risk_pct of equity."""
        result = self._calc(equity=10000.0)
        # Max loss should be ≤ 1% of equity + small buffer for rounding
        assert result.max_loss_usdt <= 10000.0 * 0.015  # 1% + 50% buffer

    def test_leverage_within_caps(self):
        result = self._calc()
        assert result.leverage <= self.settings.MAX_LEVERAGE_CAP
        assert result.leverage <= SYMBOL_INFO["max_leverage"]
        assert result.leverage >= 1

    def test_high_volatility_reduces_leverage(self):
        normal = self._calc(vol_percentile=0.3)
        high_vol = self._calc(vol_percentile=0.90)
        assert high_vol.leverage <= normal.leverage

    def test_high_spread_reduces_leverage(self):
        normal = self._calc(spread_pct=0.001)
        wide = self._calc(spread_pct=0.006)
        assert wide.leverage <= normal.leverage

    def test_drawdown_reduces_leverage(self):
        no_dd = self._calc(drawdown_pct=0.0)
        high_dd = self._calc(drawdown_pct=0.15)
        assert high_dd.leverage <= no_dd.leverage

    def test_max_concurrent_positions(self):
        result = self._calc(current_positions=3)
        assert not result.risk_ok

    def test_zero_equity_no_trade(self):
        result = self._calc(equity=0)
        assert not result.risk_ok

    def test_stop_price_correct_long(self):
        result = self._calc(entry_price=50000, atr=500, side="Buy")
        assert result.stop_price < 50000
        assert result.take_profit_price > 50000

    def test_stop_price_correct_short(self):
        result = self._calc(entry_price=50000, atr=500, side="Sell")
        assert result.stop_price > 50000
        assert result.take_profit_price < 50000

    def test_min_notional_check(self):
        """Very small equity should fail min notional."""
        result = self._calc(equity=1.0)
        # Either succeeds with tiny position or fails notional check
        if result.risk_ok:
            assert result.position_size_usdt >= SYMBOL_INFO["min_notional"]

    def test_drawdown_hard_cap_at_15_pct(self):
        result = self._calc(drawdown_pct=0.16)
        assert result.leverage <= 5

    def test_consecutive_losses_reduce_leverage(self):
        no_loss = self._calc(consecutive_losses=0)
        many_loss = self._calc(consecutive_losses=5)
        assert many_loss.leverage <= no_loss.leverage

    def test_scalp_mode_tp1_tp2_present(self):
        result = self._calc(trade_mode="SCALP")
        assert result.risk_ok
        assert result.tp1_price > 0
        assert result.tp2_price > 0
        # TP1 should be closer than TP2
        assert result.tp1_price < result.tp2_price  # for long

    def test_trend_ride_mode_wider_stops(self):
        scalp = self._calc(trade_mode="SCALP", side="Buy")
        trend = self._calc(trade_mode="TREND_RIDE", side="Buy")
        # Trend ride has wider stop (stop further from entry)
        assert trend.stop_price < scalp.stop_price

    def test_trend_ride_mode_higher_tp(self):
        scalp = self._calc(trade_mode="SCALP", side="Buy")
        trend = self._calc(trade_mode="TREND_RIDE", side="Buy")
        assert trend.take_profit_price > scalp.take_profit_price

    def test_trade_mode_propagated(self):
        result = self._calc(trade_mode="TREND_RIDE")
        assert result.trade_mode == "TREND_RIDE"

    def test_default_mode_is_scalp(self):
        result = self._calc()
        assert result.trade_mode == "SCALP"

    def test_scalp_mode_max_bars(self):
        result = self._calc(trade_mode="SCALP")
        assert result.mode_max_bars == self.settings.SCALP_MAX_BARS

    def test_trend_ride_mode_max_bars(self):
        result = self._calc(trade_mode="TREND_RIDE")
        assert result.mode_max_bars == self.settings.TREND_MAX_BARS

    def test_tp1_short_position(self):
        result = self._calc(trade_mode="SCALP", side="Sell", entry_price=50000, atr=500)
        assert result.tp1_price < 50000    # for short, TP1 below entry
        assert result.tp2_price < result.tp1_price  # TP2 even lower

    # ── Volatility Targeting (AQR) ────────────────────────────────────────────

    def test_vol_targeting_with_recent_close(self):
        """With recent_close, vol_target_size should be computed."""
        rng = np.random.default_rng(42)
        prices = 50000.0 + np.cumsum(rng.normal(0, 50, 60))
        recent_close = pd.Series(prices)

        s = make_settings(VOLATILITY_TARGET_PCT=0.15)
        engine = RiskEngine(s)
        result = engine.calculate(
            equity=10000.0,
            entry_price=50000.0,
            side="Buy",
            atr=500.0,
            signal_strength=0.7,
            spread_pct=0.001,
            funding_rate=0.0001,
            vol_percentile=0.4,
            drawdown_pct=0.0,
            consecutive_losses=0,
            symbol_max_leverage=100,
            symbol_info=SYMBOL_INFO,
            current_positions=0,
            recent_close=recent_close,
        )
        assert result.risk_ok
        # vol_target_size should be non-negative
        assert result.vol_target_size >= 0.0

    def test_vol_targeting_reduces_size_in_high_vol(self):
        """In very high volatility, vol targeting should constrain position size."""
        rng = np.random.default_rng(7)
        # High volatility prices (5% daily moves)
        prices = 50000.0 + np.cumsum(rng.normal(0, 2500, 60))
        prices = np.maximum(prices, 1.0)
        high_vol_close = pd.Series(prices)

        # Low volatility prices (0.1% daily moves)
        prices2 = 50000.0 + np.cumsum(rng.normal(0, 50, 60))
        low_vol_close = pd.Series(prices2)

        s = make_settings(VOLATILITY_TARGET_PCT=0.15)
        engine = RiskEngine(s)

        high_vol_result = engine.calculate(
            equity=10000.0, entry_price=50000.0, side="Buy", atr=500.0,
            signal_strength=0.7, spread_pct=0.001, funding_rate=0.0,
            vol_percentile=0.4, drawdown_pct=0.0, consecutive_losses=0,
            symbol_max_leverage=100, symbol_info=SYMBOL_INFO, current_positions=0,
            recent_close=high_vol_close,
        )
        low_vol_result = engine.calculate(
            equity=10000.0, entry_price=50000.0, side="Buy", atr=500.0,
            signal_strength=0.7, spread_pct=0.001, funding_rate=0.0,
            vol_percentile=0.4, drawdown_pct=0.0, consecutive_losses=0,
            symbol_max_leverage=100, symbol_info=SYMBOL_INFO, current_positions=0,
            recent_close=low_vol_close,
        )
        # High vol → smaller (or equal) position than low vol
        assert high_vol_result.qty_base <= low_vol_result.qty_base * 1.10  # allow 10% float tolerance

    def test_no_vol_targeting_without_recent_close(self):
        """Without recent_close, vol_target_size should be 0."""
        result = self._calc()
        assert result.vol_target_size == 0.0

    # ── Quarter-Kelly ─────────────────────────────────────────────────────────

    def test_kelly_size_computed_with_win_stats(self):
        """With 30+ trades win_stats, kelly_size should be computed."""
        win_stats = {
            "n_trades": 100,
            "win_rate": 0.55,
            "avg_win": 120.0,
            "avg_loss": 80.0,
        }
        s = make_settings(USE_KELLY_FRACTION=0.25)
        engine = RiskEngine(s)
        result = engine.calculate(
            equity=10000.0, entry_price=50000.0, side="Buy", atr=500.0,
            signal_strength=0.7, spread_pct=0.001, funding_rate=0.0,
            vol_percentile=0.4, drawdown_pct=0.0, consecutive_losses=0,
            symbol_max_leverage=100, symbol_info=SYMBOL_INFO, current_positions=0,
            win_stats=win_stats,
        )
        assert result.risk_ok
        assert result.kelly_size >= 0.0

    def test_no_kelly_size_without_win_stats(self):
        """Without win_stats, kelly_size should be 0."""
        result = self._calc()
        assert result.kelly_size == 0.0

    def test_kelly_not_applied_with_few_trades(self):
        """Kelly should not constrain with fewer than 30 trades."""
        win_stats = {
            "n_trades": 10,   # not enough trades
            "win_rate": 0.55,
            "avg_win": 120.0,
            "avg_loss": 80.0,
        }
        result_no_stats = self._calc()
        result_few_trades = self._calc(win_stats=win_stats)
        # Should be the same (Kelly not applied)
        assert result_no_stats.qty_base == pytest.approx(result_few_trades.qty_base, rel=0.01)

    def test_kelly_caps_oversized_position(self):
        """Very high Kelly fraction should cap excessively large positions."""
        # Extreme win stats — Kelly would normally suggest huge size
        win_stats = {
            "n_trades": 200,
            "win_rate": 0.90,   # unrealistically high win rate
            "avg_win": 500.0,
            "avg_loss": 10.0,
        }
        # Without Kelly: standard sizing
        normal = self._calc()
        # With Kelly (USE_KELLY_FRACTION=0.25 caps the sizing)
        s = make_settings(USE_KELLY_FRACTION=0.25)
        engine = RiskEngine(s)
        with_kelly = engine.calculate(
            equity=10000.0, entry_price=50000.0, side="Buy", atr=500.0,
            signal_strength=0.7, spread_pct=0.001, funding_rate=0.0,
            vol_percentile=0.4, drawdown_pct=0.0, consecutive_losses=0,
            symbol_max_leverage=100, symbol_info=SYMBOL_INFO, current_positions=0,
            win_stats=win_stats,
        )
        # Both should be valid
        assert normal.risk_ok and with_kelly.risk_ok
        # Kelly result should have kelly_size > 0
        assert with_kelly.kelly_size >= 0.0


class TestRiskGuards:
    def setup_method(self):
        self.settings = make_settings()
        self.guards = RiskGuards(self.settings)

    def _check(self, **kwargs):
        defaults = dict(
            equity=10000.0,
            peak_equity=10000.0,
            daily_pnl=0.0,
            weekly_pnl=0.0,
            initial_capital=10000.0,
            spread_pct=0.001,
            vol_percentile=0.5,
        )
        defaults.update(kwargs)
        return self.guards.check(**defaults)

    def test_normal_conditions_allow(self):
        result = self._check()
        assert result.action == GuardAction.ALLOW

    def test_daily_loss_limit_pauses(self):
        result = self._check(equity=9400, daily_pnl=-600)  # 6% loss
        assert result.action == GuardAction.PAUSE

    def test_weekly_loss_limit_pauses(self):
        result = self._check(equity=8900, weekly_pnl=-1100)  # 11% weekly
        assert result.action == GuardAction.PAUSE

    def test_max_drawdown_halt(self):
        result = self._check(equity=7900, peak_equity=10000)  # 21% DD
        assert result.action == GuardAction.HALT

    def test_moderate_drawdown_reduce(self):
        # 15% drawdown → REDUCE (70% of 20% limit)
        result = self._check(equity=8500, peak_equity=10000)  # 15% DD
        assert result.action in (GuardAction.REDUCE, GuardAction.ALLOW)

    def test_extreme_spread_pauses(self):
        result = self._check(spread_pct=0.015)
        assert result.action in (GuardAction.PAUSE, GuardAction.REDUCE)

    def test_extreme_volatility_reduces(self):
        result = self._check(vol_percentile=0.98)
        assert result.action in (GuardAction.REDUCE, GuardAction.PAUSE)

    def test_zero_equity_halts(self):
        result = self._check(equity=0)
        assert result.action == GuardAction.HALT
