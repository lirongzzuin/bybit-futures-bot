"""
Tests for RegimeRouter (src/strategy/regime_router.py).

Tests:
  1-6:  장세 분류 — STRONG_BULL, BULL, STRONG_BEAR, BEAR, SIDEWAYS, VOLATILE
  7-8:  라우팅 테이블 검증
  9-13: 확정 로직 (TRANSITION → 확정 전환)
  14:   같은 그룹 내 전환 즉시 확정
  15:   후보 카운터 리셋
  16-17: direction_bias 검증
  18-19: macro_bear 오버라이드
  20-22: 엣지 케이스 (데이터 부족, None, 심볼 독립성)
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.config import Settings
from src.strategy.regime_router import (
    DynamicRegime,
    RegimeRouter,
    RouterResult,
    _ROUTING_TABLE,
    _SymbolState,
    _regime_group,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_settings(**overrides) -> Settings:
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        REGIME_ROUTER_ADX_STRONG=27.0,
        REGIME_ROUTER_ADX_MODERATE=18.0,
        REGIME_ROUTER_ATR_VOLATILE_PCT=0.82,
        REGIME_ROUTER_CONFIRM_BARS=3,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_ohlcv(
    n: int = 500,
    trend: str = "up",     # "up" | "down" | "flat" | "choppy"
    base: float = 50_000.0,
) -> pd.DataFrame:
    """Synthetic OHLCV for regime router tests."""
    np.random.seed(42)
    if trend == "up":
        drift = np.linspace(0.0, base * 0.30, n)
    elif trend == "down":
        drift = np.linspace(0.0, -base * 0.30, n)
    elif trend == "choppy":
        drift = np.zeros(n)
        noise_scale = base * 0.05   # 5% per bar → extreme vol → VOLATILE
    else:
        drift = np.zeros(n)

    noise_scale = base * 0.002 if trend != "choppy" else base * 0.05
    noise = np.random.randn(n) * noise_scale
    close = np.maximum(base + drift + noise, base * 0.1)
    high  = close * (1 + np.abs(np.random.randn(n)) * 0.003)
    low   = close * (1 - np.abs(np.random.randn(n)) * 0.003)
    open_ = np.roll(close, 1); open_[0] = close[0]
    vol   = np.abs(np.random.randn(n)) * 1000 + 500

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


def _force_regime(
    router: RegimeRouter,
    symbol: str,
    regime: DynamicRegime,
    df: pd.DataFrame,
    macro_bear: bool = False,
) -> RouterResult:
    """_classify를 mock해서 특정 레짐을 강제 주입."""
    with patch.object(router, "_classify",
                      return_value=(regime, 0.80, f"forced_{regime.value}")):
        return router.detect(symbol, df, macro_bear=macro_bear)


# ── 1-6: 기본 장세 분류 ──────────────────────────────────────────────────────

class TestBasicClassification:

    def test_strong_bull_routing(self):
        """STRONG_BULL → strategy=trend, TREND_RIDE, +1 bias, 5× cap."""
        s = _ROUTING_TABLE[DynamicRegime.STRONG_BULL]
        assert s[0] == "trend"
        assert s[1] == "TREND_RIDE"
        assert s[2] == +1
        assert s[3] == 1.00
        assert s[4] == 5

    def test_bull_routing(self):
        """BULL → strategy=trend, SCALP, +1 bias, 80% risk, 5× cap."""
        s = _ROUTING_TABLE[DynamicRegime.BULL]
        assert s[0] == "trend"
        assert s[1] == "SCALP"
        assert s[2] == +1
        assert s[3] == 0.80
        assert s[4] == 5

    def test_strong_bear_routing(self):
        """STRONG_BEAR → strategy=trend, TREND_RIDE, -1 bias, 5× cap."""
        s = _ROUTING_TABLE[DynamicRegime.STRONG_BEAR]
        assert s[0] == "trend"
        assert s[1] == "TREND_RIDE"
        assert s[2] == -1
        assert s[3] == 1.00
        assert s[4] == 5

    def test_bear_routing(self):
        """BEAR → strategy=trend, SCALP, -1 bias, 80% risk, 5× cap."""
        s = _ROUTING_TABLE[DynamicRegime.BEAR]
        assert s[0] == "trend"
        assert s[1] == "SCALP"
        assert s[2] == -1
        assert s[3] == 0.80
        assert s[4] == 5

    def test_sideways_routing(self):
        """SIDEWAYS → skip (횡보장 진입 금지)."""
        s = _ROUTING_TABLE[DynamicRegime.SIDEWAYS]
        assert s[0] == "skip"

    def test_volatile_routing(self):
        """VOLATILE → skip (변동성 급등 진입 금지)."""
        s = _ROUTING_TABLE[DynamicRegime.VOLATILE]
        assert s[0] == "skip"

    def test_transition_routing(self):
        """TRANSITION → strategy=skip, risk=0."""
        s = _ROUTING_TABLE[DynamicRegime.TRANSITION]
        assert s[0] == "skip"
        assert s[2] == 0
        assert s[3] == 0.00


# ── 7-8: 라우팅 테이블 완전성 ─────────────────────────────────────────────────

class TestRoutingTableCompleteness:

    def test_all_regimes_in_routing_table(self):
        """모든 DynamicRegime 값이 라우팅 테이블에 존재해야 한다."""
        for r in DynamicRegime:
            assert r in _ROUTING_TABLE, f"{r} not in _ROUTING_TABLE"

    def test_regime_group_coverage(self):
        """_regime_group이 모든 레짐에 대해 예상 그룹을 반환."""
        assert _regime_group(DynamicRegime.STRONG_BULL) == "bull"
        assert _regime_group(DynamicRegime.BULL)        == "bull"
        assert _regime_group(DynamicRegime.STRONG_BEAR) == "bear"
        assert _regime_group(DynamicRegime.BEAR)        == "bear"
        assert _regime_group(DynamicRegime.SIDEWAYS)    == "neutral"
        assert _regime_group(DynamicRegime.VOLATILE)    == "volatile"
        assert _regime_group(DynamicRegime.TRANSITION)  == "neutral"


# ── 9-13: 확정 로직 (디바운스) ───────────────────────────────────────────────

class TestConfirmationLogic:

    def test_first_different_group_returns_transition(self):
        """다른 그룹 첫 감지 → TRANSITION 반환."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=3))
        df = make_ohlcv(500)

        _force_regime(router, "BTC", DynamicRegime.SIDEWAYS, df)  # 초기 확정
        r = _force_regime(router, "BTC", DynamicRegime.BULL, df)  # 첫 번째 BULL
        assert r.regime == DynamicRegime.TRANSITION
        assert r.strategy == "skip"

    def test_second_cycle_still_transition(self):
        """두 번째도 TRANSITION (3회 필요)."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=3))
        df = make_ohlcv(500)

        _force_regime(router, "BTC", DynamicRegime.SIDEWAYS, df)
        _force_regime(router, "BTC", DynamicRegime.BULL, df)
        r = _force_regime(router, "BTC", DynamicRegime.BULL, df)
        assert r.regime == DynamicRegime.TRANSITION

    def test_third_cycle_confirms_regime(self):
        """세 번째 연속 → 확정 전환."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=3))
        df = make_ohlcv(500)

        _force_regime(router, "BTC", DynamicRegime.SIDEWAYS, df)
        _force_regime(router, "BTC", DynamicRegime.BULL, df)
        _force_regime(router, "BTC", DynamicRegime.BULL, df)
        r = _force_regime(router, "BTC", DynamicRegime.BULL, df)
        assert r.regime == DynamicRegime.BULL
        assert r.direction_bias == +1

    def test_confirm_bars_1_means_immediate_different_group(self):
        """CONFIRM_BARS=1 → 첫 감지에서 즉시 확정."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)

        _force_regime(router, "BTC", DynamicRegime.SIDEWAYS, df)
        r = _force_regime(router, "BTC", DynamicRegime.BULL, df)
        assert r.regime == DynamicRegime.BULL

    def test_candidate_reset_on_different_regime_mid_confirmation(self):
        """확정 중 다른 레짐 → 카운터 리셋."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=3))
        df = make_ohlcv(500)

        _force_regime(router, "BTC", DynamicRegime.SIDEWAYS, df)  # 초기 확정
        _force_regime(router, "BTC", DynamicRegime.BULL, df)      # 카운터=1
        _force_regime(router, "BTC", DynamicRegime.BULL, df)      # 카운터=2
        # BEAR 감지로 후보 리셋
        r = _force_regime(router, "BTC", DynamicRegime.BEAR, df)
        assert r.regime == DynamicRegime.TRANSITION
        # BTC 상태: BEAR 카운터=1, SIDEWAYS 확정 유지
        state = router._state["BTC"]
        assert state.candidate_regime == DynamicRegime.BEAR
        assert state.candidate_count == 1
        assert state.confirmed_regime == DynamicRegime.SIDEWAYS


# ── 14: 같은 그룹 즉시 확정 ──────────────────────────────────────────────────

class TestSameGroupImmediate:

    def test_bull_to_strong_bull_immediate(self):
        """BULL → STRONG_BULL (같은 bull 그룹): TRANSITION 없이 즉시."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=3))
        df = make_ohlcv(500)

        # BULL 확정 (SIDEWAYS 초기 → BULL 3회)
        _force_regime(router, "BTC", DynamicRegime.SIDEWAYS, df)
        for _ in range(3):
            _force_regime(router, "BTC", DynamicRegime.BULL, df)

        # STRONG_BULL → 즉시
        r = _force_regime(router, "BTC", DynamicRegime.STRONG_BULL, df)
        assert r.regime == DynamicRegime.STRONG_BULL

    def test_bear_to_strong_bear_immediate(self):
        """BEAR → STRONG_BEAR (같은 bear 그룹): 즉시."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)

        _force_regime(router, "BTC", DynamicRegime.BEAR, df)
        r = _force_regime(router, "BTC", DynamicRegime.STRONG_BEAR, df)
        assert r.regime == DynamicRegime.STRONG_BEAR


# ── 16-17: direction_bias ─────────────────────────────────────────────────────

class TestDirectionBias:

    def test_strong_bull_direction_plus_one(self):
        """STRONG_BULL RouterResult → direction_bias=+1."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)
        router._state["BTC"] = _SymbolState(confirmed_regime=DynamicRegime.STRONG_BULL)
        r = _force_regime(router, "BTC", DynamicRegime.STRONG_BULL, df)
        assert r.direction_bias == +1
        assert r.leverage_cap == 5

    def test_strong_bear_direction_minus_one(self):
        """STRONG_BEAR RouterResult → direction_bias=-1."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)
        router._state["BTC"] = _SymbolState(confirmed_regime=DynamicRegime.STRONG_BEAR)
        r = _force_regime(router, "BTC", DynamicRegime.STRONG_BEAR, df)
        assert r.direction_bias == -1

    def test_sideways_is_skip(self):
        """SIDEWAYS → strategy=skip (횡보장 진입 금지)."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)
        router._state["BTC"] = _SymbolState(confirmed_regime=DynamicRegime.SIDEWAYS)
        r = _force_regime(router, "BTC", DynamicRegime.SIDEWAYS, df)
        assert r.strategy == "skip"


# ── 18-19: macro_bear 오버라이드 ─────────────────────────────────────────────

class TestMacroBearOverride:

    def test_strong_bull_overridden_to_bear_when_macro_bear(self):
        """macro_bear=True + STRONG_BULL raw → BEAR (또는 TRANSITION)."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)
        # macro_bear=True → STRONG_BULL → BEAR 변환 → 즉시 확정 (confirm_bars=1)
        r = _force_regime(router, "BTC", DynamicRegime.STRONG_BULL, df, macro_bear=True)
        assert r.regime in (DynamicRegime.BEAR, DynamicRegime.TRANSITION)

    def test_bull_overridden_to_sideways_when_macro_bear(self):
        """macro_bear=True + BULL raw → SIDEWAYS (neutral, 즉시 확정)."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)
        r = _force_regime(router, "BTC", DynamicRegime.BULL, df, macro_bear=True)
        # BULL → SIDEWAYS (same neutral group as default SIDEWAYS) → immediate
        assert r.regime == DynamicRegime.SIDEWAYS

    def test_no_override_when_macro_bear_false(self):
        """macro_bear=False → STRONG_BULL 그대로 유지."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)
        router._state["BTC"] = _SymbolState(confirmed_regime=DynamicRegime.STRONG_BULL)
        r = _force_regime(router, "BTC", DynamicRegime.STRONG_BULL, df, macro_bear=False)
        assert r.regime == DynamicRegime.STRONG_BULL
        assert r.direction_bias == +1


# ── 20-22: 엣지 케이스 ────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_none_data_returns_sideways_skip_no(self):
        """mtf_df=None → SIDEWAYS fallback (skip 아님, 정상 진입 허용)."""
        router = RegimeRouter(make_settings())
        result = router.detect("BTC", None)
        assert result.regime == DynamicRegime.SIDEWAYS
        assert result.confidence <= 0.5

    def test_short_data_returns_sideways(self):
        """데이터 100행 (< 210) → SIDEWAYS fallback."""
        router = RegimeRouter(make_settings())
        df = make_ohlcv(100)
        result = router.detect("BTC", df)
        assert result.regime == DynamicRegime.SIDEWAYS

    def test_symbol_state_independent(self):
        """BTC와 ETH 상태가 서로 영향을 미치지 않아야 한다."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)

        router._state["BTCUSDT"] = _SymbolState(confirmed_regime=DynamicRegime.BULL)
        router._state["ETHUSDT"] = _SymbolState(confirmed_regime=DynamicRegime.BEAR)

        with patch.object(router, "_classify",
                          return_value=(DynamicRegime.BULL, 0.8, "forced")):
            btc_r = router.detect("BTCUSDT", df)

        with patch.object(router, "_classify",
                          return_value=(DynamicRegime.BEAR, 0.8, "forced")):
            eth_r = router.detect("ETHUSDT", df)

        assert btc_r.regime == DynamicRegime.BULL
        assert eth_r.regime == DynamicRegime.BEAR

    def test_get_confirmed_regime(self):
        """get_confirmed_regime()가 현재 확정 레짐을 반환한다."""
        router = RegimeRouter(make_settings())
        router._state["BTC"] = _SymbolState(confirmed_regime=DynamicRegime.STRONG_BEAR)
        assert router.get_confirmed_regime("BTC") == DynamicRegime.STRONG_BEAR
        # 미등록 심볼 → 기본값 SIDEWAYS
        assert router.get_confirmed_regime("NEWUSDT") == DynamicRegime.SIDEWAYS

    def test_volatile_is_skip(self):
        """VOLATILE 레짐 → strategy=skip (변동성 급등 진입 금지)."""
        router = RegimeRouter(make_settings(REGIME_ROUTER_CONFIRM_BARS=1))
        df = make_ohlcv(500)
        router._state["BTC"] = _SymbolState(confirmed_regime=DynamicRegime.VOLATILE)
        r = _force_regime(router, "BTC", DynamicRegime.VOLATILE, df)
        assert r.strategy == "skip"

    def test_classify_pure_function_returns_tuple(self):
        """_classify가 (DynamicRegime, float, str) 튜플을 반환한다."""
        router = RegimeRouter(make_settings())
        df_60 = make_ohlcv(300, trend="up")
        result = router._classify(df_60)
        assert isinstance(result, tuple) and len(result) == 3
        regime, conf, reason = result
        assert isinstance(regime, DynamicRegime)
        assert 0.0 <= conf <= 1.0
        assert isinstance(reason, str)

    def test_resample_to_60m_reduces_bars(self):
        """15m 400봉 → 60m 리샘플 시 ~100봉으로 감소."""
        router = RegimeRouter(make_settings())
        df_15m = make_ohlcv(400)
        df_60m = router._resample_to_60m(df_15m)
        assert 80 <= len(df_60m) <= 105
