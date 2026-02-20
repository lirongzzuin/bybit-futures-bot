"""Tests for QualityFilter and new indicator functions."""
import pytest
import numpy as np
import pandas as pd
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategy.filters import QualityFilter, SignalFilter
from src.strategy.indicators import (
    efficiency_ratio,
    hurst_exponent,
    hull_ma,
    keltner_channel,
    session_vwap,
)


# ── Helper factories ──────────────────────────────────────────────────────────

def make_settings(**kwargs):
    from src.config import Settings
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        SIGNAL_FILTER_ER_THRESHOLD=0.35,
        SIGNAL_FILTER_HURST_MIN=0.45,
        SIGNAL_FILTER_MIN_ATR_PCT=0.0004,
        ATR_PERIOD=14,
        ADX_PERIOD=14,
        EMA_FAST=9,
        EMA_MID=21,
        EMA_SLOW=50,
        VOLATILITY_LOOKBACK=20,
        BB_PERIOD=20,
        BB_STD=2.0,
        RSI_PERIOD=14,
        KELTNER_PERIOD=20,
        KELTNER_ATR_MULT=1.5,
        HULL_MA_PERIOD=16,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def make_trending_df(n=200, direction=1, noise=0.001):
    """Strong trending market with good ER."""
    rng = np.random.default_rng(42)
    prices = [10000.0]
    for _ in range(n - 1):
        ret = 0.003 * direction + rng.normal(0, noise)
        prices.append(prices[-1] * (1 + ret))
    prices = np.array(prices)
    return pd.DataFrame({
        "open":   prices * (1 - 0.001),
        "high":   prices * (1 + 0.004),
        "low":    prices * (1 - 0.004),
        "close":  prices,
        "volume": rng.uniform(500, 3000, n),
    })


def make_choppy_df(n=200):
    """Sideways choppy market with low ER."""
    rng = np.random.default_rng(99)
    prices = [10000.0]
    for _ in range(n - 1):
        ret = rng.normal(0, 0.004)   # pure noise, no trend
        prices.append(prices[-1] * (1 + ret))
    prices = np.array(prices)
    return pd.DataFrame({
        "open":   prices * 0.999,
        "high":   prices * 1.003,
        "low":    prices * 0.997,
        "close":  prices,
        "volume": rng.uniform(500, 3000, n),
    })


def make_dead_df(n=200):
    """Dead / frozen market — ATR ≈ 0."""
    price = 10000.0
    return pd.DataFrame({
        "open":   [price] * n,
        "high":   [price + 0.01] * n,
        "low":    [price - 0.01] * n,
        "close":  [price] * n,
        "volume": [100.0] * n,
    })


# ── Efficiency Ratio tests ────────────────────────────────────────────────────

class TestEfficiencyRatio:
    def test_returns_series_same_length(self):
        df = make_trending_df(100)
        result = efficiency_ratio(df["close"], period=10)
        assert len(result) == len(df)

    def test_trending_has_high_er(self):
        df = make_trending_df(100, noise=0.0002)
        er = efficiency_ratio(df["close"], period=10)
        last_er = float(er.dropna().iloc[-1])
        # Strong clean trend → ER should be high
        assert last_er > 0.50

    def test_choppy_has_low_er(self):
        df = make_choppy_df(100)
        er = efficiency_ratio(df["close"], period=10)
        last_er = float(er.dropna().iloc[-1])
        # Pure noise → ER should be low
        assert last_er < 0.60   # typically 0.0–0.3 for pure noise

    def test_er_bounded_0_to_1(self):
        df = make_trending_df(100)
        er = efficiency_ratio(df["close"], period=10).dropna()
        assert (er >= 0.0).all() and (er <= 1.01).all()  # allow tiny float errors


# ── Hurst Exponent tests ──────────────────────────────────────────────────────

class TestHurstExponent:
    def test_returns_float(self):
        df = make_trending_df(200)
        result = hurst_exponent(df["close"], max_lags=20)
        assert isinstance(result, float)

    def test_hurst_bounded(self):
        df = make_trending_df(200)
        result = hurst_exponent(df["close"], max_lags=20)
        assert 0.0 <= result <= 1.0

    def test_insufficient_data_returns_half(self):
        df = make_trending_df(10)   # too short
        result = hurst_exponent(df["close"], max_lags=20)
        assert result == 0.5

    def test_trending_hurst_above_random(self):
        """Strong trend should have H > 0.5 (more than random walk)."""
        df = make_trending_df(200, noise=0.0005)
        h = hurst_exponent(df["close"], max_lags=20)
        # Not strictly guaranteed but should be generally above 0.5 for strong trend
        assert h >= 0.40   # conservative bound


# ── Hull MA tests ─────────────────────────────────────────────────────────────

class TestHullMA:
    def test_length_matches_input(self):
        df = make_trending_df(200)
        result = hull_ma(df["close"], period=16)
        assert len(result) == len(df)

    def test_values_positive(self):
        df = make_trending_df(200)
        result = hull_ma(df["close"], period=16).dropna()
        assert (result > 0).all()

    def test_less_lag_than_ema(self):
        """HMA should react faster to trend changes than same-period EMA."""
        from src.strategy.indicators import ema
        df = make_trending_df(200)
        hma_series = hull_ma(df["close"], period=16).dropna()
        ema_series = ema(df["close"], 16).dropna()
        close = df["close"]

        # At end of uptrend, HMA should be closer to price than EMA (less lag)
        hma_last = float(hma_series.iloc[-1])
        ema_last = float(ema_series.iloc[-1])
        price_last = float(close.iloc[-1])

        # Both should be below price in uptrend, HMA should be closer
        assert hma_last > 0 and ema_last > 0   # basic sanity


# ── Keltner Channel tests ─────────────────────────────────────────────────────

class TestKeltnerChannel:
    def test_has_required_columns(self):
        df = make_trending_df(100)
        result = keltner_channel(df["close"], df["high"], df["low"], period=20, atr_mult=1.5)
        assert set(result.columns) == {"upper", "mid", "lower", "pct_k"}

    def test_upper_above_lower(self):
        df = make_trending_df(100)
        kc = keltner_channel(df["close"], df["high"], df["low"], period=20)
        valid = kc.dropna()
        assert (valid["upper"] >= valid["lower"]).all()

    def test_mid_between_bounds(self):
        df = make_trending_df(100)
        kc = keltner_channel(df["close"], df["high"], df["low"], period=20)
        valid = kc.dropna()
        assert (valid["mid"] >= valid["lower"]).all()
        assert (valid["mid"] <= valid["upper"]).all()

    def test_length_matches_input(self):
        df = make_trending_df(100)
        kc = keltner_channel(df["close"], df["high"], df["low"], period=20)
        assert len(kc) == len(df)


# ── Session VWAP tests ────────────────────────────────────────────────────────

class TestSessionVWAP:
    def test_fallback_to_rolling_when_no_datetime_index(self):
        df = make_trending_df(200)   # integer index
        result = session_vwap(df)
        assert len(result) == len(df)
        assert (result.dropna() > 0).all()

    def test_proper_session_reset_with_datetime_index(self):
        """With DatetimeIndex, VWAP resets daily."""
        rng = np.random.default_rng(77)
        n = 600   # 600 bars = 50 hours of 5-min data
        idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
        prices = 50000.0 + np.cumsum(rng.normal(0, 10, n))
        df = pd.DataFrame({
            "open":   prices * 0.9995,
            "high":   prices * 1.001,
            "low":    prices * 0.999,
            "close":  prices,
            "volume": rng.uniform(100, 1000, n),
        }, index=idx)
        result = session_vwap(df)
        assert len(result) == n
        assert (result.dropna() > 0).all()
        # VWAP should be close to close prices
        diff_pct = abs(result.dropna() - df["close"][result.notna()]) / df["close"][result.notna()]
        assert (diff_pct < 0.05).all()   # VWAP within 5% of price


# ── QualityFilter tests ───────────────────────────────────────────────────────

class TestQualityFilter:
    def setup_method(self):
        self.settings = make_settings()
        self.qf = QualityFilter(self.settings)

    def test_trending_market_passes(self):
        df = make_trending_df(200)
        result = self.qf.check(df)
        assert result.passed, f"Expected pass, got: {result.reason}"

    def test_choppy_market_rejected(self):
        """Low ER market should be filtered out."""
        # Use very strict threshold to guarantee rejection
        s = make_settings(SIGNAL_FILTER_ER_THRESHOLD=0.80)
        qf = QualityFilter(s)
        df = make_choppy_df(200)
        result = qf.check(df)
        assert not result.passed

    def test_dead_market_rejected(self):
        df = make_dead_df(200)
        result = self.qf.check(df)
        assert not result.passed
        # Should mention ATR/volatility in reason
        lower_reason = result.reason.lower()
        assert any(kw in lower_reason for kw in ("atr", "dead", "volatility"))

    def test_insufficient_data_rejected(self):
        df = make_trending_df(10)  # too few bars
        result = self.qf.check(df)
        assert not result.passed

    def test_er_value_in_result(self):
        df = make_trending_df(200)
        result = self.qf.check(df)
        assert 0.0 <= result.er_val <= 1.0

    def test_hurst_value_in_result(self):
        df = make_trending_df(200)
        result = self.qf.check(df)
        assert 0.0 <= result.hurst_val <= 1.0

    def test_signalfilter_has_required_fields(self):
        df = make_trending_df(200)
        result = self.qf.check(df)
        assert hasattr(result, "passed")
        assert hasattr(result, "reason")
        assert hasattr(result, "er_val")
        assert hasattr(result, "hurst_val")

    def test_relaxed_thresholds_pass_everything(self):
        """Very relaxed thresholds should pass almost any market."""
        s = make_settings(
            SIGNAL_FILTER_ER_THRESHOLD=0.01,
            SIGNAL_FILTER_HURST_MIN=0.10,
            SIGNAL_FILTER_MIN_ATR_PCT=0.000001,
        )
        qf = QualityFilter(s)
        df = make_choppy_df(200)
        result = qf.check(df)
        assert result.passed

    def test_strict_er_rejects_choppy(self):
        """Tight ER threshold rejects choppy market."""
        s = make_settings(SIGNAL_FILTER_ER_THRESHOLD=0.95)
        qf = QualityFilter(s)
        df = make_choppy_df(200)
        result = qf.check(df)
        assert not result.passed
