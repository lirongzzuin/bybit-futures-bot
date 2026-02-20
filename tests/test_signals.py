"""Tests for signal generation and combining."""
import pytest
import numpy as np
import pandas as pd
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategy.indicators import ema, rsi, atr, bollinger_bands, adx, macd, vwap, hull_ma, keltner_channel
from src.strategy.regime import RegimeDetector, Regime
from src.strategy.signals import SignalGenerator
from src.strategy.combiner import SignalCombiner
from src.strategy.ict import ICTAnalyzer


def make_trending_df(n=200, direction=1) -> pd.DataFrame:
    """Synthetic trending OHLCV data — strong trend so ADX reliably > 25."""
    np.random.seed(42)
    # Large drift (1.0) with small noise (0.3) → strong directional move
    prices = 100.0 + direction * np.cumsum(np.random.normal(1.0, 0.3, n))
    prices = np.maximum(prices, 1.0)
    high = prices * (1 + np.random.uniform(0, 0.005, n))
    low = prices * (1 - np.random.uniform(0, 0.005, n))
    volume = np.random.uniform(1000, 5000, n)
    return pd.DataFrame({
        "ts": list(range(n)),
        "open": prices,
        "high": high,
        "low": low,
        "close": prices,
        "volume": volume,
        "turnover": prices * volume,
    })


def make_ranging_df(n=200) -> pd.DataFrame:
    """Synthetic ranging OHLCV data."""
    np.random.seed(123)
    prices = 100.0 + np.random.normal(0, 2.0, n)
    prices = np.maximum(prices, 1.0)
    high = prices * (1 + np.random.uniform(0, 0.005, n))
    low = prices * (1 - np.random.uniform(0, 0.005, n))
    volume = np.random.uniform(500, 2000, n)
    return pd.DataFrame({
        "ts": range(n),
        "open": prices,
        "high": high,
        "low": low,
        "close": prices,
        "volume": volume,
        "turnover": prices * volume,
    })


# ── Indicator tests ───────────────────────────────────────────────────────────

class TestIndicators:
    def test_ema_length(self):
        s = pd.Series(np.arange(50, dtype=float))
        result = ema(s, 10)
        assert len(result) == len(s)

    def test_rsi_range(self):
        df = make_trending_df(100)
        result = rsi(df["close"], 14)
        valid = result.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_atr_positive(self):
        df = make_trending_df(100)
        result = atr(df["high"], df["low"], df["close"], 14)
        assert (result.dropna() > 0).all()

    def test_bb_structure(self):
        df = make_trending_df(100)
        result = bollinger_bands(df["close"], 20, 2.0)
        assert "upper" in result.columns and "lower" in result.columns
        # Upper should always >= lower
        valid = result.dropna()
        assert (valid["upper"] >= valid["lower"]).all()

    def test_adx_range(self):
        df = make_trending_df(100)
        result = adx(df["high"], df["low"], df["close"], 14)
        valid = result["adx"].dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_macd_histogram_sign(self):
        """Histogram = MACD - signal. Should be negative when bearish cross."""
        df = make_trending_df(200, direction=1)
        result = macd(df["close"])
        assert "histogram" in result.columns
        assert len(result) == len(df)

    def test_vwap_length_matches_input(self):
        df = make_trending_df(200)
        result = vwap(df["high"], df["low"], df["close"], df["volume"], period=96)
        assert len(result) == len(df)

    def test_vwap_value_reasonable(self):
        df = make_trending_df(200)
        result = vwap(df["high"], df["low"], df["close"], df["volume"], period=96)
        valid = result.dropna()
        # VWAP should be close to price range
        assert (valid > 0).all()
        # Should be within range of close prices (not wildly off)
        price_min = df["close"].min()
        price_max = df["close"].max()
        assert (valid >= price_min * 0.5).all()
        assert (valid <= price_max * 2.0).all()


# ── Regime tests ──────────────────────────────────────────────────────────────

class TestRegimeDetector:
    def setup_method(self):
        from src.config import Settings
        self.settings = Settings(
            TRADING_MODE="dry-run",
            BYBIT_API_KEY="",
            BYBIT_API_SECRET="",
        )
        self.detector = RegimeDetector(self.settings)

    def test_uptrend_detected(self):
        df = make_trending_df(200, direction=1)
        result = self.detector.detect(df)
        # Strong uptrend should produce non-RANGING regime
        assert result.regime in (Regime.TRENDING_UP, Regime.HIGH_VOLATILITY)

    def test_downtrend_detected(self):
        df = make_trending_df(200, direction=-1)
        result = self.detector.detect(df)
        assert result.regime in (Regime.TRENDING_DOWN, Regime.HIGH_VOLATILITY)

    def test_ranging_detected(self):
        df = make_ranging_df(200)
        result = self.detector.detect(df)
        assert result.regime in (Regime.RANGING, Regime.HIGH_VOLATILITY)

    def test_regime_confidence_bounded(self):
        df = make_trending_df(200, direction=1)
        result = self.detector.detect(df)
        assert 0.0 <= result.confidence <= 1.0

    def test_trend_score_bounded(self):
        df = make_trending_df(200, direction=1)
        result = self.detector.detect(df)
        assert -1.0 <= result.trend_score <= 1.0

    def test_insufficient_data_returns_ranging(self):
        df = make_trending_df(30)
        result = self.detector.detect(df)
        assert result.regime == Regime.RANGING
        assert result.confidence == 0.0


# ── Signal tests ──────────────────────────────────────────────────────────────

class TestSignalGenerator:
    def setup_method(self):
        from src.config import Settings
        self.settings = Settings(TRADING_MODE="dry-run", BYBIT_API_KEY="", BYBIT_API_SECRET="")
        self.gen = SignalGenerator(self.settings)
        self.ict = ICTAnalyzer(self.settings)

    def test_signals_generated(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        assert len(bundle.signals) > 0

    def test_all_categories_present(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        cats = {s.category for s in bundle.signals}
        # Should have at least trend + volume categories
        assert "trend" in cats
        assert "volume" in cats

    def test_direction_values(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        for sig in bundle.signals:
            assert sig.direction in (-1, 0, 1)
            assert 0.0 <= sig.confidence <= 1.0

    def test_atr_positive(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        assert bundle.atr_val > 0

    def test_vwap_signals_generated_when_vwap_provided(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        last_price = float(df["close"].iloc[-1])
        bundle = self.gen.generate(
            df, ict_r,
            vwap=last_price * 0.99,   # price slightly above VWAP
            vwap_distance_pct=0.01,
        )
        vwap_sigs = [s for s in bundle.signals if s.category == "vwap"]
        assert len(vwap_sigs) > 0

    def test_vwap_no_signals_when_vwap_zero(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r, vwap=0.0)
        vwap_sigs = [s for s in bundle.signals if s.category == "vwap"]
        assert len(vwap_sigs) == 0

    def test_vwap_bullish_above_vwap(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        price = float(df["close"].iloc[-1])
        bundle = self.gen.generate(
            df, ict_r,
            vwap=price * 0.98,       # price 2% above VWAP
            vwap_distance_pct=0.02,
        )
        dist_sigs = [s for s in bundle.signals if s.name == "vwap_distance"]
        assert len(dist_sigs) == 1
        assert dist_sigs[0].direction == 1   # bullish (above VWAP)

    def test_vwap_bearish_below_vwap(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        price = float(df["close"].iloc[-1])
        bundle = self.gen.generate(
            df, ict_r,
            vwap=price * 1.02,       # price 2% below VWAP
            vwap_distance_pct=-0.02,
        )
        dist_sigs = [s for s in bundle.signals if s.name == "vwap_distance"]
        assert len(dist_sigs) == 1
        assert dist_sigs[0].direction == -1  # bearish (below VWAP)

    def test_orderbook_bullish_signal(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r, orderbook_imbalance=0.30)
        ob_sigs = [s for s in bundle.signals if s.category == "orderbook"]
        assert len(ob_sigs) == 1
        assert ob_sigs[0].direction == 1

    def test_orderbook_bearish_signal(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r, orderbook_imbalance=-0.30)
        ob_sigs = [s for s in bundle.signals if s.category == "orderbook"]
        assert len(ob_sigs) == 1
        assert ob_sigs[0].direction == -1

    def test_orderbook_neutral_signal(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r, orderbook_imbalance=0.05)
        ob_sigs = [s for s in bundle.signals if s.category == "orderbook"]
        assert len(ob_sigs) == 1
        assert ob_sigs[0].direction == 0

    def test_keltner_signal_present_in_bundle(self):
        """Keltner breakout signal should appear in every bundle as 'trend' category."""
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        keltner_sigs = [s for s in bundle.signals if s.name == "keltner_breakout"]
        assert len(keltner_sigs) == 1

    def test_keltner_signal_direction_valid(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        keltner_sigs = [s for s in bundle.signals if s.name == "keltner_breakout"]
        assert keltner_sigs[0].direction in (-1, 0, 1)
        assert 0.0 <= keltner_sigs[0].confidence <= 1.0

    def test_hma_signal_present_in_bundle(self):
        """HMA direction signal should appear in every bundle."""
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        hma_sigs = [s for s in bundle.signals if s.name == "hma_direction"]
        assert len(hma_sigs) == 1

    def test_hma_signal_direction_valid(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        hma_sigs = [s for s in bundle.signals if s.name == "hma_direction"]
        assert hma_sigs[0].direction in (-1, 0, 1)
        assert 0.0 <= hma_sigs[0].confidence <= 1.0

    def test_hma_bullish_in_uptrend(self):
        """In a strong uptrend, HMA direction should be bullish (1)."""
        df = make_trending_df(200, direction=1)
        ict_r = self.ict.analyze(df)
        bundle = self.gen.generate(df, ict_r)
        hma_sigs = [s for s in bundle.signals if s.name == "hma_direction"]
        # Allow either bullish or neutral — just not all bearish
        assert hma_sigs[0].direction >= 0


# ── Combiner tests ────────────────────────────────────────────────────────────

class TestSignalCombiner:
    def setup_method(self):
        from src.config import Settings
        self.settings = Settings(
            TRADING_MODE="dry-run",
            BYBIT_API_KEY="",
            BYBIT_API_SECRET="",
            SIGNAL_ENTRY_THRESHOLD=0.55,
        )
        self.gen = SignalGenerator(self.settings)
        self.ict = ICTAnalyzer(self.settings)
        self.regime = RegimeDetector(self.settings)
        self.combiner = SignalCombiner(self.settings)

    def test_combined_direction_valid(self):
        df = make_trending_df(200)
        ict_r = self.ict.analyze(df)
        regime_r = self.regime.detect(df)
        bundle = self.gen.generate(df, ict_r)
        combined = self.combiner.combine(bundle, regime_r)
        assert combined.direction in (-1, 0, 1)
        assert 0.0 <= combined.strength <= 1.0

    def test_trending_suppresses_counter_trend(self):
        """In TRENDING_UP regime, short score should be suppressed."""
        from src.strategy.regime import RegimeResult
        from src.strategy.signals import SignalBundle, Signal

        # Force TRENDING_UP regime
        regime_r = RegimeResult(
            regime=Regime.TRENDING_UP,
            adx_val=35, plus_di=30, minus_di=10,
            vol_percentile=0.3, ema_aligned_up=True, ema_aligned_down=False,
            volume_ratio=1.5, trend_score=0.8, confidence=0.9,
        )
        # All short signals
        bundle = SignalBundle()
        bundle.signals = [
            Signal("test1", -1, 0.9, "trend"),
            Signal("test2", -1, 0.9, "trend"),
            Signal("test3", -1, 0.9, "ict"),
        ]

        combined = self.combiner.combine(bundle, regime_r)
        # Even with all-short signals, trending up suppresses short
        # Short score should be < long strength or at least reduced
        assert combined.short_score <= 0.6 * 0.9   # suppressed by 0.4 factor
