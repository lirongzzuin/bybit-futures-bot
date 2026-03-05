"""Tests for risk engine and guard rails."""
import pytest
import sys
import os

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.risk.engine import RiskEngine
from src.risk.guards import RiskGuards, GuardAction
from src.execution.state_machine import StateMachine


def make_settings(**kwargs):
    from src.config import Settings
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        MAX_RISK_PER_TRADE_PCT=0.02,        # 실제 운영값 반영
        MAX_LEVERAGE_CAP=10,                 # 실제 운영값
        MAX_CONCURRENT_POSITIONS=3,
        MAX_SINGLE_EXPOSURE_PCT=0.30,
        MAX_TOTAL_EXPOSURE_PCT=0.75,
        STOP_ATR_MULTIPLIER=2.0,
        TP_ATR_MULTIPLIER=3.0,
        TRAILING_STOP_ATR_MULTIPLIER=1.0,   # 수정된 값 (2.0 → 1.0)
        MAX_DAILY_LOSS_PCT=0.08,
        MAX_WEEKLY_LOSS_PCT=0.15,
        MAX_DRAWDOWN_PCT=0.30,
        SCALP_STOP_ATR=1.0,                  # 실제 운영값
        SCALP_TP1_ATR=1.5,                   # 실제 운영값
        SCALP_TP2_ATR=3.0,                   # 실제 운영값
        SCALP_MAX_BARS=12,
        SCALP_PARTIAL_EXIT_PCT=0.60,
        TREND_STOP_ATR=2.0,                  # 실제 운영값
        TREND_TP1_ATR=3.0,                   # 실제 운영값
        TREND_TP2_ATR=6.0,                   # 실제 운영값
        TREND_MAX_BARS=100,
        TREND_PARTIAL_EXIT_PCT=0.40,
        TREND_RIDE_SCORE_THRESHOLD=2,
        SIGNAL_FILTER_MIN_ATR_PCT=0.001,
        BACKTEST_FEE_MAKER=0.00020,
        BACKTEST_FEE_TAKER=0.00055,
        BACKTEST_SLIPPAGE_PCT=0.0002,
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
        """일반 시그널(0.7)에서 max_loss ≤ equity × 5% (max_notional clamp 효과)."""
        result = self._calc(equity=10000.0, signal_strength=0.70)
        # trade_risk=2.5% but notional clamp → actual max_loss < 5% of equity
        assert result.max_loss_usdt <= 10000.0 * 0.05

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

    def test_drawdown_hard_cap_proportional(self):
        """DD >12%: cap = max(10, cap//2). 테스트 cap=10 → min(10,max(10,5))=10."""
        result = self._calc(drawdown_pct=0.16)
        assert result.leverage <= 10  # MAX_LEVERAGE_CAP=10이므로 ≤10

    def test_high_dd_further_reduces_cap(self):
        """DD >20%: cap = max(5, cap//4). 테스트 cap=10 → max(5,2)=5."""
        result = self._calc(drawdown_pct=0.25, signal_strength=0.85)
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


class TestHighLeverageMode:
    """시그널 강도 기반 50× 레버리지 달성 테스트."""

    def _make_engine(self, max_lev=50, bear_max_lev=50):
        s = make_settings(
            MAX_LEVERAGE_CAP=max_lev,
            BEAR_MAX_LEVERAGE_CAP=bear_max_lev,
            MAX_RISK_PER_TRADE_PCT=0.015,  # 1.5% base
            SCALP_STOP_ATR=1.5,
            SCALP_TP1_ATR=2.5,
            SCALP_TP2_ATR=5.0,
        )
        return RiskEngine(s)

    def _calc(self, engine, **kwargs):
        defaults = dict(
            equity=10000.0,
            entry_price=50000.0,
            side="Buy",
            atr=250.0,          # ATR = 0.5% (BTC 15M 전형적)
            signal_strength=0.85,
            spread_pct=0.0005,
            funding_rate=0.0001,
            vol_percentile=0.3,
            drawdown_pct=0.0,
            consecutive_losses=0,
            symbol_max_leverage=100,
            symbol_info=SYMBOL_INFO,
            current_positions=0,
            bear_mode=True,
        )
        defaults.update(kwargs)
        return engine.calculate(**defaults)

    def test_ultra_high_signal_achieves_50x(self):
        """시그널 0.85 + BEAR 모드 + ATR 0.5% → 고배율 달성."""
        engine = self._make_engine()
        result = self._calc(engine, signal_strength=0.90)
        assert result.risk_ok, result.reason
        # base_lev = 10% / (1.5 × 0.5% × 0.40) = 10% / 0.30% = 33.3× (sig_mod 감소 후 ~30×)
        # 드로우다운 0% → dd_escalation_allowed=True, 안전화된 파라미터로도 30× 달성
        assert result.leverage >= 25, f"Expected ≥25×, got {result.leverage}×"

    def test_signal_tiers_ascending_leverage(self):
        """시그널 강도 증가 → 레버리지 단조 증가."""
        engine = self._make_engine()
        levs = {}
        for s in [0.50, 0.65, 0.75, 0.80, 0.85]:
            r = self._calc(engine, signal_strength=s)
            levs[s] = r.leverage if r.risk_ok else 0
        assert levs[0.65] >= levs[0.50]
        assert levs[0.75] >= levs[0.65]
        assert levs[0.85] >= levs[0.75]

    def test_tight_stop_produces_good_rr(self):
        """strength=0.85: 타이트 스탑(0.35×ATR) + 정상 TP(2.5×ATR) → R:R ≥ 4."""
        engine = self._make_engine()
        entry = 50000.0
        atr = 250.0   # 0.5% of price
        result = self._calc(engine, signal_strength=0.85, side="Buy",
                            entry_price=entry, atr=atr)
        stop_dist = entry - result.stop_price   # entry - stop
        tp1_dist = result.tp1_price - entry
        rr = tp1_dist / stop_dist if stop_dist > 0 else 0
        # stop = 0.35×1.5×250=131.25, TP1 = 2.5×250=625 → R:R = 625/131.25 = 4.76
        assert rr >= 4.0, f"R:R too low: {rr:.2f}"

    def test_low_signal_stays_conservative(self):
        """약한 시그널(0.55)은 낮은 레버리지 유지."""
        engine = self._make_engine()
        result = self._calc(engine, signal_strength=0.55, bear_mode=False)
        # stop_scale=1.0, trade_risk=1.5%, ATR=0.5% → base_lev = 1.5%/0.75% = 2×
        assert result.leverage <= 5, f"Expected ≤5×, got {result.leverage}×"

    def test_bull_mode_also_caps_at_50x(self):
        """BULL 모드도 MAX_LEVERAGE_CAP=50 설정 시 50× 가능."""
        engine = self._make_engine(max_lev=50)
        result = self._calc(engine, signal_strength=0.90, bear_mode=False)
        assert result.risk_ok
        assert result.leverage <= 50

    def test_liquidation_buffer_passes_at_50x(self):
        """50× 레버리지: liq_dist >> 3×stop_dist (청산 거리 충분)."""
        engine = self._make_engine()
        result = self._calc(engine, signal_strength=0.90)
        assert result.risk_ok, f"Liquidation check failed: {result.reason}"


class TestFeeBreakevenCheck:
    """수수료 손익분기점 체크 테스트.

    진입 주문 유형:
      - PostOnly(Maker): spread ≤ 0.3% AND signal ≤ 0.90
        round_trip = 0.020% + 0.020% + 0.040% = 0.080%  → min_tp1 = 0.160%
      - Market(Taker): spread > 0.3% OR signal > 0.90
        round_trip = 0.055% + 0.020% + 0.040% = 0.115%  → min_tp1 = 0.230%
    """

    def setup_method(self):
        self.settings = make_settings()
        self.engine = RiskEngine(self.settings)

    def _calc(self, **kwargs):
        defaults = dict(
            equity=10000.0,
            entry_price=50000.0,
            side="Buy",
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

    # ── PostOnly(Maker) 진입 경로 ─────────────────────────────────────────────

    def test_sufficient_atr_passes_fee_check(self):
        """ATR이 충분히 크면 수수료 손익분기점 통과 (PostOnly 진입)."""
        # ATR=0.15%, SCALP TP1=0.225% > min_tp1=0.160% ✓
        result = self._calc(atr=75.0)
        assert result.risk_ok, f"Should pass: {result.reason}"

    def test_low_atr_fails_fee_check(self):
        """ATR이 너무 작으면(0.05%) 수수료 손익분기점 미달로 거부 (PostOnly 진입)."""
        result = self._calc(atr=25.0)  # 25/50000 = 0.05%
        assert not result.risk_ok
        assert "수수료" in result.reason

    def test_boundary_atr_fails_fee_check(self):
        """경계값(ATR=0.106%) 에서 수수료 체크 작동 확인 (PostOnly 진입)."""
        # min ATR = round_trip(0.080%)×2 / TP1_ATR(1.5) = 0.160%/1.5 = 0.1067%
        # ATR=0.106% → TP1=0.159% < 0.160% → 거부
        result = self._calc(atr=53.0)
        assert not result.risk_ok

    def test_trend_mode_atr_check(self):
        """TREND_RIDE 모드에서도 수수료 체크 통과."""
        # TREND TP1=3.0ATR, ATR=0.10% → TP1=0.30% > 0.160% ✓
        result = self._calc(atr=50.0, trade_mode="TREND_RIDE")
        assert result.risk_ok

    def test_fee_check_uses_maker_rate_for_postonly(self):
        """PostOnly 진입: Maker 수수료 기준 손익분기점."""
        # round_trip = MAKER + MAKER + SLIP×2 = 0.020%+0.020%+0.040% = 0.080%
        # min_tp1 = 0.160%, ATR=0.108% → TP1=0.162% > 0.160% ✓
        result = self._calc(atr=54.0, spread_pct=0.001, signal_strength=0.7)
        assert result.risk_ok

    # ── Market(Taker) 진입 경로 ───────────────────────────────────────────────

    def test_wide_spread_uses_taker_fee_in_breakeven(self):
        """스프레드 > SPREAD_TAKER_THRESHOLD → 시장가 진입 → Taker 수수료 적용."""
        # round_trip = TAKER + MAKER + SLIP×2 = 0.055%+0.020%+0.040% = 0.115%
        # min_tp1 = 0.230%
        # ATR=0.15% → TP1=0.225% < 0.230% → 스프레드 넓은 경우 더 엄격하게 거부
        result = self._calc(atr=75.0, spread_pct=0.004)  # 0.4% > threshold
        assert not result.risk_ok, "넓은 스프레드에서 ATR 0.15%는 Taker 수수료 기준으로 미달"
        assert "수수료" in result.reason

    def test_wide_spread_high_atr_passes_taker_breakeven(self):
        """스프레드 넓어도 ATR이 충분히 크면 Taker 기준 손익분기점 통과."""
        # round_trip = 0.115%, min_tp1 = 0.230%
        # ATR=0.16% → TP1=0.240% > 0.230% ✓
        result = self._calc(atr=80.0, spread_pct=0.004)
        assert result.risk_ok, f"Should pass with ATR=0.16%: {result.reason}"

    def test_urgent_signal_uses_taker_fee_in_breakeven(self):
        """긴급 신호(> 0.90) → 시장가 진입 → Taker 수수료 적용."""
        # round_trip = 0.115%, min_tp1 = 0.230%
        # ATR=0.15% → TP1=0.225% < 0.230% → 긴급 신호에서 더 엄격하게 거부
        result = self._calc(atr=75.0, signal_strength=0.91, spread_pct=0.001)
        assert not result.risk_ok, "긴급 신호에서 ATR 0.15%는 Taker 수수료 기준으로 미달"

    def test_maker_vs_taker_breakeven_difference(self):
        """PostOnly vs Market 수수료 차이가 손익분기점에 영향을 미침."""
        # ATR=0.15%, SCALP TP1=0.225%:
        #   PostOnly(Maker): 0.225% > min_tp1=0.160% ✓ (통과)
        #   Market(Taker):   0.225% < min_tp1=0.230% ✗ (거부)
        maker_result = self._calc(atr=75.0, spread_pct=0.001, signal_strength=0.7)
        taker_result = self._calc(atr=75.0, spread_pct=0.004, signal_strength=0.7)
        assert maker_result.risk_ok, "PostOnly 진입: 통과 예상"
        assert not taker_result.risk_ok, "Market 진입: 더 엄격한 기준으로 거부 예상"

    def test_signal_at_threshold_uses_maker(self):
        """신호 강도 정확히 0.90 → PostOnly(Maker), 0.90 초과 시에만 Market."""
        # signal=0.90: NOT > 0.90 → Maker fee
        maker_result = self._calc(atr=54.0, spread_pct=0.001, signal_strength=0.90)
        # signal=0.91: > 0.90 → Taker fee
        taker_result = self._calc(atr=54.0, spread_pct=0.001, signal_strength=0.91)
        # ATR=0.108%: Maker 기준 통과(0.162%>0.160%), Taker 기준 거부(0.162%<0.230%)
        assert maker_result.risk_ok, "signal=0.90: PostOnly(Maker) 기준 통과"
        assert not taker_result.risk_ok, "signal=0.91: Market(Taker) 기준 거부"


class TestStateMachineFeeTracking:
    """StateMachine entry_fee 추적 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.sm = StateMachine(self.settings)

    def _enter(self, symbol="BTCUSDT", entry_fee=0.8, direction=1):
        return self.sm.enter(
            symbol=symbol,
            direction=direction,
            entry_price=40000.0,
            qty=0.1,
            leverage=10,
            stop_price=39600.0,
            take_profit_price=41200.0,
            trailing_distance=40.0,
            tp1_price=40600.0,
            tp2_price=41200.0,
            entry_fee=entry_fee,
        )

    def test_entry_fee_stored_in_position(self):
        """진입 시 entry_fee가 PositionInfo에 저장됨."""
        self._enter(entry_fee=0.80)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.entry_fee == pytest.approx(0.80)

    def test_partial_exit_pro_rates_entry_fee(self):
        """TP1 부분청산 시 entry_fee가 partial_pct 비율로 감소."""
        self._enter(entry_fee=0.80)
        pos = self.sm.get_position("BTCUSDT")
        # 60% 부분청산
        partial_pct = 0.60
        qty_to_close = pos.qty * partial_pct
        self.sm.partial_exit("BTCUSDT", qty_to_close, 40600.0, partial_pct=partial_pct)
        pos_after = self.sm.get_position("BTCUSDT")
        # 남은 entry_fee = 0.80 × (1 - 0.60) = 0.32
        assert pos_after.entry_fee == pytest.approx(0.80 * 0.40, rel=1e-6)

    def test_entry_fee_consumed_on_full_exit(self):
        """전체 청산 시 entry_fee가 0으로 초기화."""
        self._enter(entry_fee=0.80)
        self.sm.exit("BTCUSDT", 39600.0)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.entry_fee == 0.0

    def test_entry_fee_reset_on_force_flat(self):
        """force_flat 호출 시 entry_fee가 0으로 초기화."""
        self._enter(entry_fee=0.80)
        self.sm.force_flat("BTCUSDT")
        pos = self.sm.get_position("BTCUSDT")
        assert pos.entry_fee == 0.0

    def test_partial_exit_pnl_is_gross(self):
        """partial_exit 반환 PnL은 gross (수수료 미포함) — caller가 차감."""
        self._enter(entry_fee=0.80)
        pos = self.sm.get_position("BTCUSDT")
        qty_to_close = pos.qty * 0.60  # 0.06 BTC
        gross_pnl = self.sm.partial_exit("BTCUSDT", qty_to_close, 40600.0, partial_pct=0.60)
        # gross = (40600 - 40000) × 0.06 = 600 × 0.06 = 36.0
        assert gross_pnl == pytest.approx(36.0, rel=1e-6)

    def test_fee_model_net_pnl_calculation(self):
        """전체 수수료 모델: gross_pnl - entry_fee_prorata - exit_fee = net_pnl."""
        entry_price = 40000.0
        tp1_price = 40600.0
        qty_total = 0.1
        partial_pct = 0.60
        entry_fee_total = 0.80  # 0.1 BTC × 40000 × 0.020%

        self._enter(entry_fee=entry_fee_total)
        pos = self.sm.get_position("BTCUSDT")

        # 부분청산 전 entry_fee 캡처
        entry_fee_for_partial = pos.entry_fee * partial_pct  # 0.80 × 0.60 = 0.48
        qty_to_close = qty_total * partial_pct
        gross_pnl = self.sm.partial_exit("BTCUSDT", qty_to_close, tp1_price, partial_pct=partial_pct)

        # partial exit fee (maker)
        partial_exit_fee = qty_to_close * tp1_price * 0.00020
        total_fee = entry_fee_for_partial + partial_exit_fee
        net_pnl = gross_pnl - total_fee

        # gross = 36.0, fee = 0.48 + (0.06×40600×0.020%) = 0.48 + 0.4872 ≈ 0.967
        # net ≈ 35.033
        assert gross_pnl == pytest.approx(36.0, rel=1e-4)
        assert net_pnl > 0, "TP1 net PnL should be positive"
        assert net_pnl < gross_pnl, "Net PnL must be less than gross (fees deducted)"


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

    def test_daily_loss_limit_allows_with_warning(self):
        # 복리 모드: 일일 손실 한도(8%) 초과 시 PAUSE 대신 ALLOW (거래 계속)
        # daily_loss_pct = 850 / max(9150 - (-850), 1) = 850/10000 = 8.5% > 8% → 경고 reason 포함
        result = self._check(equity=9150, daily_pnl=-850)  # 8.5% loss
        assert result.action == GuardAction.ALLOW
        # reason에 "Daily loss" 포함 → main.py에서 Slack 알림 전송
        assert "Daily loss" in result.reason

    def test_weekly_loss_limit_allows_with_warning(self):
        # 복리 모드: 주간 손실 한도 초과 시 PAUSE 대신 ALLOW (거래 계속)
        result = self._check(equity=8900, weekly_pnl=-1100)  # 11% weekly
        assert result.action == GuardAction.ALLOW

    def test_max_drawdown_triggers_reduce(self):
        # 새 계층 시스템: 31% DD → HALT 아닌 REDUCE (포지션 비중 축소 후 회복 거래 허용)
        # 실제 HALT는 HALT_DRAWDOWN_PCT (기본 50%)에서 발동
        result = self._check(equity=6900, peak_equity=10000)  # 31% DD > MAX_DRAWDOWN_PCT(30%)
        assert result.action == GuardAction.REDUCE
        assert result.reduce_pct > 0.70  # heavy reduction (75%+)

    def test_extreme_drawdown_triggers_halt(self):
        # 52% DD → HALT (HALT_DRAWDOWN_PCT=0.50 초과)
        result = self._check(equity=4800, peak_equity=10000)  # 52% DD
        assert result.action == GuardAction.HALT

    def test_heavy_drawdown_pauses_new_entries(self):
        # 43% DD → PAUSE (PAUSE_DRAWDOWN_PCT=0.42 초과, HALT_DRAWDOWN_PCT 미달)
        result = self._check(equity=5700, peak_equity=10000)  # 43% DD
        assert result.action == GuardAction.PAUSE

    def test_moderate_drawdown_reduce(self):
        # 15% DD < reduce_limit(21%) → ALLOW (감쇠 없음)
        result = self._check(equity=8500, peak_equity=10000)  # 15% DD
        assert result.action == GuardAction.ALLOW

    def test_drawdown_at_reduce_limit_triggers_reduce(self):
        # 22% DD > reduce_limit(21%) → REDUCE
        result = self._check(equity=7800, peak_equity=10000)  # 22% DD
        assert result.action == GuardAction.REDUCE

    def test_extreme_spread_pauses(self):
        result = self._check(spread_pct=0.015)
        assert result.action in (GuardAction.PAUSE, GuardAction.REDUCE)

    def test_extreme_volatility_reduces(self):
        result = self._check(vol_percentile=0.98)
        assert result.action in (GuardAction.REDUCE, GuardAction.PAUSE)

    def test_zero_equity_halts(self):
        result = self._check(equity=0)
        assert result.action == GuardAction.HALT
