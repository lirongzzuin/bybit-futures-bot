"""Tests for TradeModeDetector (scalp.py)."""
import pytest
import sys
import os
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategy.scalp import TradeModeDetector, TradeMode, ScalpModeParams
from src.strategy.regime import RegimeResult, Regime
from src.strategy.signals import SignalBundle, Signal


def make_settings(**kwargs):
    from src.config import Settings
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        TREND_RIDE_SCORE_THRESHOLD=3,
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
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def make_regime(adx=20, ema_up=False, ema_down=False, vol_pct=0.5,
                trend_score=0.0, bos_bull=False, bos_bear=False):
    """Create a minimal RegimeResult-like object for testing."""
    from dataclasses import dataclass

    @dataclass
    class MockRegime:
        adx_val: float
        ema_aligned_up: bool
        ema_aligned_down: bool
        vol_percentile: float
        trend_score: float
        regime: object = Regime.RANGING

    r = MockRegime(
        adx_val=adx,
        ema_aligned_up=ema_up,
        ema_aligned_down=ema_down,
        vol_percentile=vol_pct,
        trend_score=trend_score,
        regime=Regime.TRENDING_UP if ema_up else Regime.RANGING,
    )
    return r


def make_ict(bos_bullish=False, bos_bearish=False):
    from dataclasses import dataclass

    @dataclass
    class MockICT:
        bos_bullish: bool
        bos_bearish: bool

    return MockICT(bos_bullish=bos_bullish, bos_bearish=bos_bearish)


def make_bundle(net_dir=0.0, oi_conf=0.0, oi_dir=0):
    bundle = SignalBundle()
    # Add a signal that produces the desired net direction
    if net_dir != 0:
        bundle.signals.append(Signal(
            name="test_trend",
            direction=1 if net_dir > 0 else -1,
            confidence=abs(net_dir),
            category="trend",
        ))
    if oi_conf > 0:
        bundle.signals.append(Signal(
            name="oi_flow",
            direction=oi_dir,
            confidence=oi_conf,
            category="volume",
        ))
    return bundle


class TestTradeModeDetector:
    def setup_method(self):
        self.settings = make_settings()
        self.detector = TradeModeDetector(self.settings)

    def test_scalp_by_default_with_low_score(self):
        regime = make_regime(adx=15)
        ict = make_ict()
        bundle = make_bundle(net_dir=0.3)
        result = self.detector.detect(regime, ict, bundle, 0.0001, 0.001, 1)
        assert result.mode == TradeMode.SCALP

    def test_trend_ride_with_high_score(self):
        """Score: adx≥30=1, bos=1, net_dir≥0.60=1, ema=1, low_vol=1 → 5 ≥ 3"""
        regime = make_regime(adx=35, ema_up=True, vol_pct=0.5)
        ict = make_ict(bos_bullish=True)
        bundle = make_bundle(net_dir=0.7)
        result = self.detector.detect(regime, ict, bundle, 0.0001, 0.001, 1)
        assert result.mode == TradeMode.TREND_RIDE
        assert result.score >= 3

    def test_funding_risk_penalizes(self):
        """High funding rate subtracts 1 from score."""
        regime = make_regime(adx=35, ema_up=True, vol_pct=0.5)
        ict = make_ict(bos_bullish=True)
        bundle = make_bundle(net_dir=0.7)

        low_fund = self.detector.detect(regime, ict, bundle, 0.0001, 0.001, 1)
        high_fund = self.detector.detect(regime, ict, bundle, 0.001, 0.001, 1)

        assert high_fund.score < low_fund.score

    def test_wide_spread_penalizes(self):
        """Wide spread subtracts 1 from score."""
        regime = make_regime(adx=35, ema_up=True, vol_pct=0.5)
        ict = make_ict(bos_bullish=True)
        bundle = make_bundle(net_dir=0.7)

        narrow = self.detector.detect(regime, ict, bundle, 0.0001, 0.001, 1)
        wide = self.detector.detect(regime, ict, bundle, 0.0001, 0.003, 1)

        assert wide.score < narrow.score

    def test_score_details_contains_expected_keys(self):
        regime = make_regime()
        ict = make_ict()
        bundle = make_bundle()
        result = self.detector.detect(regime, ict, bundle, 0.0, 0.001, 1)
        expected_keys = {
            "adx_strong", "oi_aligned", "bos_aligned", "strong_net_dir",
            "low_vol", "ema_aligned", "funding_risk", "wide_spread"
        }
        assert expected_keys == set(result.score_details.keys())

    def test_short_direction_bos_check(self):
        """BOS bearish with direction=-1 should add +1."""
        regime = make_regime(adx=35, ema_down=True, vol_pct=0.5)
        ict = make_ict(bos_bearish=True)
        bundle = make_bundle(net_dir=-0.7)
        result = self.detector.detect(regime, ict, bundle, 0.0001, 0.001, -1)
        assert result.score_details["bos_aligned"] == 1
        assert result.score_details["ema_aligned"] == 1

    def test_get_params_scalp(self):
        params = self.detector.get_params(TradeMode.SCALP)
        assert isinstance(params, ScalpModeParams)
        assert params.stop_atr == self.settings.SCALP_STOP_ATR
        assert params.tp1_atr == self.settings.SCALP_TP1_ATR
        assert params.tp2_atr == self.settings.SCALP_TP2_ATR
        assert params.max_bars == self.settings.SCALP_MAX_BARS
        assert params.partial_exit_pct == self.settings.SCALP_PARTIAL_EXIT_PCT

    def test_get_params_trend_ride(self):
        params = self.detector.get_params(TradeMode.TREND_RIDE)
        assert params.stop_atr == self.settings.TREND_STOP_ATR
        assert params.tp1_atr == self.settings.TREND_TP1_ATR
        assert params.tp2_atr == self.settings.TREND_TP2_ATR
        assert params.max_bars == self.settings.TREND_MAX_BARS
        assert params.partial_exit_pct == self.settings.TREND_PARTIAL_EXIT_PCT

    def test_high_vol_prevents_trend_ride(self):
        """vol_percentile ≥ 0.70 should not get +1 for low_vol."""
        regime = make_regime(adx=35, ema_up=True, vol_pct=0.85)
        ict = make_ict(bos_bullish=True)
        bundle = make_bundle(net_dir=0.7)
        result = self.detector.detect(regime, ict, bundle, 0.0001, 0.001, 1)
        assert result.score_details["low_vol"] == 0
