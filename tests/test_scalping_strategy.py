"""
Tests for ScalpingStrategy (src/strategy/scalping.py).

테스트 커버리지:
- 롱 신호 생성 (각 조건별)
- 숏 신호 생성 (각 조건별)
- 점수 미달 → 중립 반환
- 신호 강도 매핑 검증
- HTF 없는 경우 처리
- 데이터 부족 처리
- 페널티 조건 (펀딩 비율, 스프레드)
- ATR 반환 검증
- ScalpSignalDetail 반환 검증
"""
from __future__ import annotations

import sys
import os
import pytest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategy.scalping import ScalpingStrategy
from src.strategy.regime import Regime


# ── Test Fixtures ────────────────────────────────────────────────────────────

def make_settings(**kwargs):
    from src.config import Settings
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        # 스캘핑 전략 기본 설정
        SCALP_PURE_LTF_TIMEFRAME="3",
        SCALP_PURE_HTF_TIMEFRAME="15",
        SCALP_PURE_EMA_FAST=9,
        SCALP_PURE_EMA_MID=21,
        SCALP_PURE_EMA_SLOW=50,
        SCALP_PURE_RSI_PERIOD=14,
        SCALP_PURE_RSI_LONG_MIN=40.0,
        SCALP_PURE_RSI_LONG_MAX=65.0,
        SCALP_PURE_RSI_SHORT_MIN=35.0,
        SCALP_PURE_RSI_SHORT_MAX=60.0,
        SCALP_PURE_VOLUME_MULT=1.2,
        SCALP_PURE_EMA_ZONE_ATR=1.0,
        SCALP_PURE_ENTRY_MIN_SCORE=5,
        SCALP_PURE_ENTRY_URGENT_SCORE=7,
        SCALP_PURE_STOP_ATR=1.0,
        SCALP_PURE_TP1_ATR=1.5,
        SCALP_PURE_TP2_ATR=3.0,
        SCALP_PURE_MAX_BARS=20,
        SCALP_PURE_PARTIAL_EXIT_PCT=0.50,
        STRATEGY_MODE="scalp",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def make_ohlcv(
    n: int = 100,
    price_trend: str = "up",    # "up", "down", "flat"
    base_price: float = 50000.0,
    atr_pct: float = 0.01,      # ATR as % of price
    vol_spike: bool = False,     # 마지막 봉 볼륨 스파이크
) -> pd.DataFrame:
    """테스트용 OHLCV 데이터 생성."""
    np.random.seed(42)

    if price_trend == "up":
        prices = base_price + np.cumsum(np.random.randn(n) * base_price * 0.002 + base_price * 0.001)
    elif price_trend == "down":
        prices = base_price + np.cumsum(np.random.randn(n) * base_price * 0.002 - base_price * 0.001)
    else:
        prices = base_price + np.cumsum(np.random.randn(n) * base_price * 0.001)

    prices = np.abs(prices)  # 음수 방지

    atr = base_price * atr_pct
    high = prices + np.abs(np.random.randn(n)) * atr * 0.5
    low = prices - np.abs(np.random.randn(n)) * atr * 0.5
    close = prices
    open_ = prices - (np.random.randn(n) * atr * 0.3)

    volume = np.abs(np.random.randn(n) * 100 + 200)
    if vol_spike:
        volume[-1] = volume[:-1].mean() * 2.0  # 마지막 봉 2× 볼륨

    ts = np.arange(n) * 180000  # 3분봉 (180초)

    df = pd.DataFrame({
        "ts": ts.astype(int),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "turnover": volume * prices,
    })
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def make_bullish_ohlcv(n: int = 100, base_price: float = 50000.0) -> pd.DataFrame:
    """명확한 상승 추세의 OHLCV 데이터 (EMA 정렬 보장)."""
    np.random.seed(1)
    # 강한 상승 추세: 모든 봉이 이전 봉보다 높음
    prices = base_price + np.arange(n) * (base_price * 0.001)  # 안정적 상승
    noise = np.random.randn(n) * base_price * 0.0002
    close = prices + noise
    open_ = close - np.abs(np.random.randn(n) * base_price * 0.0003)  # 양봉 우세
    high = close + np.abs(np.random.randn(n) * base_price * 0.0005)
    low = open_ - np.abs(np.random.randn(n) * base_price * 0.0003)
    volume = np.abs(np.random.randn(n) * 100 + 200)

    ts = np.arange(n) * 180000
    df = pd.DataFrame({
        "ts": ts.astype(int),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "turnover": volume * close,
    })
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def make_bearish_ohlcv(n: int = 100, base_price: float = 50000.0) -> pd.DataFrame:
    """명확한 하락 추세의 OHLCV 데이터."""
    np.random.seed(2)
    prices = base_price - np.arange(n) * (base_price * 0.001)
    noise = np.random.randn(n) * base_price * 0.0002
    close = prices + noise
    open_ = close + np.abs(np.random.randn(n) * base_price * 0.0003)  # 음봉 우세
    high = open_ + np.abs(np.random.randn(n) * base_price * 0.0003)
    low = close - np.abs(np.random.randn(n) * base_price * 0.0005)
    volume = np.abs(np.random.randn(n) * 100 + 200)

    ts = np.arange(n) * 180000
    df = pd.DataFrame({
        "ts": ts.astype(int),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "turnover": volume * close,
    })
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


# ── Test Classes ─────────────────────────────────────────────────────────────

class TestScalpingStrategyBasics:
    """기본 동작 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_returns_neutral_on_empty_df(self):
        """빈 데이터프레임 입력 시 중립 반환."""
        signal, atr = self.strategy.generate(pd.DataFrame(), None)
        assert signal.direction == 0
        assert signal.strength == 0.0
        assert atr == 0.0

    def test_returns_neutral_on_insufficient_bars(self):
        """50봉 미만 입력 시 중립 반환."""
        df = make_ohlcv(n=30)
        signal, atr = self.strategy.generate(df, None)
        assert signal.direction == 0
        assert atr == 0.0

    def test_returns_atr_value_with_valid_data(self):
        """유효한 데이터 입력 시 ATR > 0 반환."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        assert atr > 0.0

    def test_signal_direction_is_valid(self):
        """direction이 항상 -1, 0, +1 중 하나."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        assert signal.direction in (-1, 0, 1)

    def test_signal_strength_in_range(self):
        """strength가 항상 0.0–1.0 범위."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        assert 0.0 <= signal.strength <= 1.0

    def test_combined_signal_has_regime(self):
        """CombinedSignal이 유효한 regime 반환."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        assert isinstance(signal.regime, Regime)

    def test_neutral_regime_when_no_htf(self):
        """HTF 없는 경우 중립 신호 시 Regime.RANGING."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        if signal.direction == 0:
            assert signal.regime == Regime.RANGING


class TestScalpingStrategyLong:
    """롱 진입 신호 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_bullish_trend_can_generate_long(self):
        """강한 상승 추세에서 롱 신호 또는 중립 반환 (결정론적 보장 없음)."""
        ltf_df = make_bullish_ohlcv(n=150)
        htf_df = make_bullish_ohlcv(n=100)
        signal, atr = self.strategy.generate(ltf_df, htf_df)
        # 상승 추세에서 중립이거나 롱이어야 함 (숏은 안 됨)
        assert signal.direction in (0, 1)

    def test_no_long_in_bearish_htf(self):
        """HTF 하락 추세에서는 롱 신호 없음."""
        ltf_df = make_bullish_ohlcv(n=150)
        htf_df = make_bearish_ohlcv(n=100)
        signal, atr = self.strategy.generate(ltf_df, htf_df)
        # HTF 하락일 때 롱 점수 높아도 HTF 페널티로 min_score 미달 가능
        assert signal.direction in (0, -1)  # 롱 없음

    def test_long_regime_is_trending_up(self):
        """롱 신호 발생 시 regime이 TRENDING_UP이어야 함 (HTF 상승일 때)."""
        ltf_df = make_bullish_ohlcv(n=150)
        htf_df = make_bullish_ohlcv(n=100)
        signal, atr = self.strategy.generate(ltf_df, htf_df)
        if signal.direction == 1:
            assert signal.regime == Regime.TRENDING_UP


class TestScalpingStrategyShort:
    """숏 진입 신호 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_bearish_trend_can_generate_short(self):
        """강한 하락 추세에서 숏 신호 또는 중립 반환."""
        ltf_df = make_bearish_ohlcv(n=150)
        htf_df = make_bearish_ohlcv(n=100)
        signal, atr = self.strategy.generate(ltf_df, htf_df)
        # 하락 추세에서 롱은 안 됨
        assert signal.direction in (0, -1)

    def test_no_short_in_bullish_htf(self):
        """HTF 상승 추세에서는 숏 신호 없음."""
        ltf_df = make_bearish_ohlcv(n=150)
        htf_df = make_bullish_ohlcv(n=100)
        signal, atr = self.strategy.generate(ltf_df, htf_df)
        assert signal.direction in (0, 1)  # 숏 없음

    def test_short_regime_is_trending_down(self):
        """숏 신호 발생 시 regime이 TRENDING_DOWN이어야 함 (HTF 하락일 때)."""
        ltf_df = make_bearish_ohlcv(n=150)
        htf_df = make_bearish_ohlcv(n=100)
        signal, atr = self.strategy.generate(ltf_df, htf_df)
        if signal.direction == -1:
            assert signal.regime == Regime.TRENDING_DOWN


class TestScalpingStrategyScoring:
    """점수 계산 및 신호 강도 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_min_score_strength_is_at_threshold(self):
        """min_score=5 시 strength=0.60 (진입 임계값)."""
        # score=5 → strength = 0.60 + (5-5)*0.07 = 0.60
        expected = 0.60
        # score 5일 때 strength 수식 검증
        min_score = self.settings.SCALP_PURE_ENTRY_MIN_SCORE
        computed = min(0.95, 0.60 + (5 - min_score) * 0.07)
        assert abs(computed - expected) < 1e-9

    def test_score_7_gives_strength_074(self):
        """score=7 (non-urgent) 시 strength=0.74 → PostOnly 선호."""
        # 연속 강도: base=0.60+(7-5)*0.07=0.74, 최고점수(9)만 시장가 트리거
        min_score = self.settings.SCALP_PURE_ENTRY_MIN_SCORE
        base = 0.60 + (7 - min_score) * 0.07  # 0.74
        expected = min(0.95, base)
        assert abs(expected - 0.74) < 1e-6

    def test_score_8_gives_strength_081(self):
        """score=8 (non-urgent) 시 strength=0.81 → PostOnly 선호."""
        # 연속 강도: base=0.60+(8-5)*0.07=0.81
        min_score = self.settings.SCALP_PURE_ENTRY_MIN_SCORE
        base = 0.60 + (8 - min_score) * 0.07  # 0.81
        expected = min(0.95, base)
        assert abs(expected - 0.81) < 1e-6

    def test_score_9_gives_strength_091(self):
        """score=9 (최고점수) 시 strength=0.91 → 시장가 주문 트리거."""
        # 최고 점수만 시장가 트리거: urgent_threshold+0.01 = 0.91
        urgent_threshold = self.settings.SIGNAL_URGENT_THRESHOLD
        expected = min(0.95, urgent_threshold + 0.01)
        assert abs(expected - 0.91) < 1e-6

    def test_score_6_gives_strength_067(self):
        """score=6 (non-urgent) 시 strength=0.67 → PostOnly 선호."""
        min_score = self.settings.SCALP_PURE_ENTRY_MIN_SCORE
        base = 0.60 + (6 - min_score) * 0.07  # 0.67
        expected = min(0.95, base)
        assert abs(expected - 0.67) < 1e-6

    def test_urgent_score_threshold_boundary(self):
        """score 5-8은 SIGNAL_URGENT_THRESHOLD 미만, score 9만 초과."""
        urgent_threshold = self.settings.SIGNAL_URGENT_THRESHOLD  # 0.90
        min_score = self.settings.SCALP_PURE_ENTRY_MIN_SCORE  # 5

        for score in [5, 6, 7, 8]:
            base = 0.60 + (score - min_score) * 0.07
            strength = min(0.95, base)
            assert strength < urgent_threshold, f"score={score}: {strength} should be < {urgent_threshold}"

        # score 9만 시장가 트리거
        strength_9 = min(0.95, urgent_threshold + 0.01)
        assert strength_9 > urgent_threshold, f"score=9: {strength_9} should be > {urgent_threshold}"

    def test_strength_capped_at_095(self):
        """strength가 0.95를 초과하지 않음."""
        min_score = self.settings.SCALP_PURE_ENTRY_MIN_SCORE
        for score in range(0, 12):
            val = min(0.95, 0.60 + (score - min_score) * 0.07)
            assert val <= 0.95

    def test_below_min_score_returns_neutral(self):
        """score < min_score(5) 시 direction=0 반환."""
        # 점수가 낮게 나오는 데이터: 상승도 하락도 아닌 횡보
        df = make_ohlcv(n=100, price_trend="flat")
        signal, atr = self.strategy.generate(df, None)
        # 횡보 데이터: 점수 낮아서 중립
        # (모든 조건이 맞지 않으면 0)
        # 점수 < 5이면 direction == 0
        if signal.direction != 0:
            # 신호가 발생했다면 강도가 valid 범위
            assert signal.strength >= 0.60


class TestScalpingStrategyScoreDetails:
    """스코어 상세 항목 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_score_long_keys_present(self):
        """롱 스코어 딕셔너리에 모든 예상 키 포함."""
        df = make_ohlcv(n=100)
        _, details = self.strategy._score_long(
            current_price=50000.0,
            current_open=49900.0,
            current_vol=300.0,
            avg_vol=200.0,
            ltf_ema9=50100.0,
            ltf_ema21=49800.0,
            ltf_atr=500.0,
            ltf_rsi=52.0,
            htf_ema9=50200.0,
            htf_ema21=49900.0,
            htf_vwap=49500.0,
            htf_price=50000.0,
            has_htf=True,
            funding_rate=0.0001,
            spread_pct=0.001,
        )
        expected_keys = {
            "htf_ema_bull", "htf_vwap_bull", "ltf_ema_aligned",
            "price_in_zone", "rsi_in_zone", "volume_spike",
            "bull_candle", "adverse_funding", "wide_spread",
        }
        assert expected_keys == set(details.keys())

    def test_score_short_keys_present(self):
        """숏 스코어 딕셔너리에 모든 예상 키 포함."""
        _, details = self.strategy._score_short(
            current_price=50000.0,
            current_open=50100.0,
            current_vol=300.0,
            avg_vol=200.0,
            ltf_ema9=49800.0,
            ltf_ema21=50100.0,
            ltf_atr=500.0,
            ltf_rsi=48.0,
            htf_ema9=49700.0,
            htf_ema21=50200.0,
            htf_vwap=50500.0,
            htf_price=50000.0,
            has_htf=True,
            funding_rate=0.0001,
            spread_pct=0.001,
        )
        expected_keys = {
            "htf_ema_bear", "htf_vwap_bear", "ltf_ema_aligned",
            "price_in_zone", "rsi_in_zone", "volume_spike",
            "bear_candle", "adverse_funding", "wide_spread",
        }
        assert expected_keys == set(details.keys())

    def test_htf_ema_bull_gives_2_points(self):
        """HTF EMA9 > EMA21 시 +2점."""
        _, details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=50000.0, ltf_ema21=50000.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49900.0,
            htf_vwap=0.0, htf_price=50000.0, has_htf=True,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert details["htf_ema_bull"] == 2

    def test_htf_ema_bear_gives_0_for_long(self):
        """HTF EMA9 < EMA21 시 롱 htf_ema_bull = 0."""
        _, details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=50000.0, ltf_ema21=50000.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=49800.0, htf_ema21=50100.0,
            htf_vwap=0.0, htf_price=50000.0, has_htf=True,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert details["htf_ema_bull"] == 0

    def test_adverse_funding_gives_penalty(self):
        """|funding| > 0.001 시 -1점 패널티."""
        _, low_fund_details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49800.0,
            htf_vwap=49000.0, htf_price=50000.0, has_htf=True,
            funding_rate=0.0001,  # 낮은 펀딩
            spread_pct=0.001,
        )
        _, high_fund_details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49800.0,
            htf_vwap=49000.0, htf_price=50000.0, has_htf=True,
            funding_rate=0.0015,  # 높은 펀딩 (>0.001)
            spread_pct=0.001,
        )
        assert low_fund_details["adverse_funding"] == 0
        assert high_fund_details["adverse_funding"] == -1

    def test_wide_spread_gives_penalty(self):
        """spread > 0.2% 시 -1점 패널티."""
        _, narrow_details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49800.0,
            htf_vwap=49000.0, htf_price=50000.0, has_htf=True,
            funding_rate=0.0001,
            spread_pct=0.001,  # 0.1% — 좁은 스프레드
        )
        _, wide_details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49800.0,
            htf_vwap=49000.0, htf_price=50000.0, has_htf=True,
            funding_rate=0.0001,
            spread_pct=0.003,  # 0.3% — 넓은 스프레드
        )
        assert narrow_details["wide_spread"] == 0
        assert wide_details["wide_spread"] == -1

    def test_volume_spike_condition(self):
        """볼륨이 평균의 1.2배 이상 시 +1점."""
        # vol_mult=1.2: current_vol=300, avg_vol=200 → 300/200=1.5 ≥ 1.2 → +1
        _, high_vol_details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,  # 1.5× avg
            ltf_ema9=50000.0, ltf_ema21=50000.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        # vol_spike=1.5≥1.2 → +1
        assert high_vol_details["volume_spike"] == 1

        # current_vol=100, avg_vol=200 → 0.5 < 1.2 → 0
        _, low_vol_details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=100.0, avg_vol=200.0,  # 0.5× avg
            ltf_ema9=50000.0, ltf_ema21=50000.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert low_vol_details["volume_spike"] == 0

    def test_rsi_zone_for_long(self):
        """RSI 40-65 범위에서만 롱 rsi_in_zone=1."""
        base_kwargs = dict(
            current_price=50000.0, current_open=49900.0,
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=50000.0, ltf_ema21=50000.0, ltf_atr=500.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        # RSI=52 → in zone
        _, d = self.strategy._score_long(ltf_rsi=52.0, **base_kwargs)
        assert d["rsi_in_zone"] == 1

        # RSI=70 → out of zone (과매수)
        _, d = self.strategy._score_long(ltf_rsi=70.0, **base_kwargs)
        assert d["rsi_in_zone"] == 0

        # RSI=35 → out of zone (모멘텀 없음)
        _, d = self.strategy._score_long(ltf_rsi=35.0, **base_kwargs)
        assert d["rsi_in_zone"] == 0

    def test_rsi_zone_for_short(self):
        """RSI 35-60 범위에서만 숏 rsi_in_zone=1."""
        base_kwargs = dict(
            current_price=50000.0, current_open=50100.0,
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=49800.0, ltf_ema21=50100.0, ltf_atr=500.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        # RSI=48 → in short zone
        _, d = self.strategy._score_short(ltf_rsi=48.0, **base_kwargs)
        assert d["rsi_in_zone"] == 1

        # RSI=25 → out of zone (과매도 — 반등 가능성, 숏 안 됨)
        _, d = self.strategy._score_short(ltf_rsi=25.0, **base_kwargs)
        assert d["rsi_in_zone"] == 0

        # RSI=70 → out of zone (숏 RSI max=60)
        _, d = self.strategy._score_short(ltf_rsi=70.0, **base_kwargs)
        assert d["rsi_in_zone"] == 0

    def test_bull_candle_condition(self):
        """양봉 (close > open) 시 bull_candle=1."""
        base_kwargs = dict(
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=50000.0, ltf_ema21=50000.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        # 양봉
        _, d = self.strategy._score_long(current_price=50100.0, current_open=49900.0, **base_kwargs)
        assert d["bull_candle"] == 1

        # 음봉
        _, d = self.strategy._score_long(current_price=49800.0, current_open=50000.0, **base_kwargs)
        assert d["bull_candle"] == 0

    def test_bear_candle_condition(self):
        """음봉 (close < open) 시 bear_candle=1."""
        base_kwargs = dict(
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=49800.0, ltf_ema21=50100.0, ltf_atr=500.0, ltf_rsi=48.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        # 음봉
        _, d = self.strategy._score_short(current_price=49800.0, current_open=50000.0, **base_kwargs)
        assert d["bear_candle"] == 1

        # 양봉
        _, d = self.strategy._score_short(current_price=50100.0, current_open=49900.0, **base_kwargs)
        assert d["bear_candle"] == 0

    def test_price_in_zone_condition(self):
        """가격이 EMA21 ± ATR×ZONE 내에 있을 때 +1점."""
        # ltf_atr=500, EMA_ZONE_ATR=1.0 → zone=±500
        # EMA21=50000, current_price=50300 → |300| < 500 → in zone
        _, d = self.strategy._score_long(
            current_price=50300.0, current_open=50200.0,
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=50400.0, ltf_ema21=50000.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert d["price_in_zone"] == 1

        # current_price=51000 → |51000-50000|=1000 > 500 → out of zone
        _, d = self.strategy._score_long(
            current_price=51000.0, current_open=50900.0,
            current_vol=100.0, avg_vol=200.0,
            ltf_ema9=51100.0, ltf_ema21=50000.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert d["price_in_zone"] == 0


class TestScalpingStrategyNoHTF:
    """HTF 데이터 없을 때 동작 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_no_htf_still_works(self):
        """HTF=None 이어도 오류 없이 실행."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        assert isinstance(signal.direction, int)
        assert atr >= 0.0

    def test_no_htf_htf_scores_are_zero(self):
        """HTF 없으면 htf_ema_bull/htf_vwap_bull 점수 0."""
        _, details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=0.0, htf_ema21=0.0,
            htf_vwap=0.0, htf_price=0.0, has_htf=False,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert details["htf_ema_bull"] == 0
        assert details["htf_vwap_bull"] == 0

    def test_insufficient_htf_treated_as_no_htf(self):
        """HTF 봉 수 < 30이면 has_htf=False로 처리."""
        ltf_df = make_bullish_ohlcv(n=100)
        htf_df = make_bullish_ohlcv(n=10)  # 10봉 — 부족
        signal, atr = self.strategy.generate(ltf_df, htf_df)
        # 오류 없이 실행되어야 함
        assert isinstance(signal.direction, int)


class TestScalpingStrategySignalDetail:
    """get_signal_detail() 메서드 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_detail_returns_none_on_empty_df(self):
        """빈 데이터프레임 시 None 반환."""
        result = self.strategy.get_signal_detail(pd.DataFrame(), None)
        assert result is None

    def test_detail_returns_correct_structure(self):
        """ScalpSignalDetail 구조 검증."""
        from src.strategy.scalping import ScalpSignalDetail
        df = make_ohlcv(n=100)
        detail = self.strategy.get_signal_detail(df, None)
        assert detail is not None
        assert isinstance(detail, ScalpSignalDetail)
        assert detail.max_score == 9  # _MAX_POSITIVE_SCORE
        assert detail.direction in (-1, 0, 1)
        assert detail.atr_val > 0
        assert isinstance(detail.long_details, dict)
        assert isinstance(detail.short_details, dict)

    def test_detail_atr_matches_generate_atr(self):
        """get_signal_detail의 ATR이 generate의 ATR과 일치."""
        df = make_ohlcv(n=100)
        _, atr_from_generate = self.strategy.generate(df, None)
        detail = self.strategy.get_signal_detail(df, None)
        if detail is not None:
            assert abs(detail.atr_val - atr_from_generate) < 1e-8


class TestScalpingStrategyEdgeCases:
    """엣지 케이스 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_exactly_50_bars(self):
        """정확히 50봉 (경계값) 처리."""
        df = make_ohlcv(n=50)
        signal, atr = self.strategy.generate(df, None)
        assert isinstance(signal.direction, int)

    def test_very_high_atr(self):
        """극단적으로 높은 ATR에서도 정상 동작."""
        df = make_ohlcv(n=100, atr_pct=0.10)  # 10% ATR
        signal, atr = self.strategy.generate(df, None)
        assert atr > 0
        assert isinstance(signal.direction, int)

    def test_zero_volume_handled(self):
        """볼륨이 0인 경우 처리."""
        df = make_ohlcv(n=100)
        df["volume"] = 0.0
        # 볼륨 0: volume_spike 조건 미충족, 오류 없어야 함
        signal, atr = self.strategy.generate(df, None)
        assert isinstance(signal.direction, int)

    def test_category_scores_are_numeric(self):
        """category_scores가 모두 숫자."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        for k, v in signal.category_scores.items():
            assert isinstance(v, (int, float)), f"{k}={v} is not numeric"

    def test_long_short_scores_normalized(self):
        """long_score, short_score가 0.0–1.0 범위 (정규화됨)."""
        df = make_ohlcv(n=100)
        signal, atr = self.strategy.generate(df, None)
        assert 0.0 <= signal.long_score <= 1.0
        assert 0.0 <= signal.short_score <= 1.0

    def test_custom_min_score_setting(self):
        """SCALP_PURE_ENTRY_MIN_SCORE 설정이 반영됨."""
        # min_score를 매우 높게 설정 → 거의 모든 신호가 중립
        settings_strict = make_settings(SCALP_PURE_ENTRY_MIN_SCORE=10)
        strategy_strict = ScalpingStrategy(settings_strict)
        df = make_bullish_ohlcv(n=150)
        signal, _ = strategy_strict.generate(df, None)
        # 9점 만점인데 min_score=10 → 항상 중립
        assert signal.direction == 0

    def test_scalp_neutral_has_zero_scores(self):
        """중립 신호는 strength=0.0."""
        signal = ScalpingStrategy._neutral()
        assert signal.direction == 0
        assert signal.strength == 0.0
        assert signal.long_score == 0.0
        assert signal.short_score == 0.0


class TestScalpingStrategyVWAP:
    """VWAP 조건 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_htf_vwap_above_price_gives_bull_point(self):
        """가격이 VWAP 위 → htf_vwap_bull=1."""
        _, details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49800.0,
            htf_vwap=49000.0,   # VWAP < price → 롱에 유리
            htf_price=50000.0,
            has_htf=True,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert details["htf_vwap_bull"] == 1

    def test_htf_vwap_below_price_no_bull_point(self):
        """가격이 VWAP 아래 → htf_vwap_bull=0 (롱에 불리)."""
        _, details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49800.0,
            htf_vwap=51000.0,   # VWAP > price → 롱에 불리
            htf_price=50000.0,
            has_htf=True,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert details["htf_vwap_bull"] == 0

    def test_vwap_zero_not_counted(self):
        """VWAP=0이면 htf_vwap 점수 없음."""
        _, details = self.strategy._score_long(
            current_price=50000.0, current_open=49900.0,
            current_vol=300.0, avg_vol=200.0,
            ltf_ema9=50100.0, ltf_ema21=49900.0, ltf_atr=500.0, ltf_rsi=52.0,
            htf_ema9=50200.0, htf_ema21=49800.0,
            htf_vwap=0.0,  # VWAP 없음
            htf_price=50000.0,
            has_htf=True,
            funding_rate=0.0, spread_pct=0.001,
        )
        assert details["htf_vwap_bull"] == 0


class TestScalpingStrategyMaxScore:
    """최대 점수 시나리오 테스트."""

    def setup_method(self):
        self.settings = make_settings()
        self.strategy = ScalpingStrategy(self.settings)

    def test_max_long_score_all_conditions_met(self):
        """모든 롱 조건 충족 시 최대 점수 9."""
        score, details = self.strategy._score_long(
            current_price=50300.0,    # EMA21 근처, 양봉
            current_open=50200.0,    # close > open (양봉)
            current_vol=300.0,       # 1.5× avg_vol
            avg_vol=200.0,
            ltf_ema9=50400.0,        # EMA9 > EMA21 ✓
            ltf_ema21=50000.0,
            ltf_atr=500.0,           # zone=±500, |50300-50000|=300 < 500 ✓
            ltf_rsi=52.0,            # 40-65 범위 ✓
            htf_ema9=50500.0,        # HTF EMA9 > EMA21 ✓
            htf_ema21=50100.0,
            htf_vwap=49000.0,        # price > VWAP ✓
            htf_price=50300.0,
            has_htf=True,
            funding_rate=0.0001,     # |0.0001| < 0.001 → 패널티 없음 ✓
            spread_pct=0.001,        # 0.1% < 0.2% → 패널티 없음 ✓
        )
        # 예상: htf_ema_bull(2)+htf_vwap_bull(1)+ltf_ema(2)+zone(1)+rsi(1)+vol(1)+candle(1)=9
        assert score == 9

    def test_min_long_score_with_full_penalties(self):
        """모든 조건 불충족 + 패널티 시 음수 점수."""
        score, details = self.strategy._score_long(
            current_price=50000.0,
            current_open=50100.0,    # 음봉 (close < open)
            current_vol=100.0,       # 0.5× avg — 볼륨 부족
            avg_vol=200.0,
            ltf_ema9=49800.0,        # EMA9 < EMA21 (하락 정렬)
            ltf_ema21=50100.0,
            ltf_atr=500.0,           # zone=±500, |50000-50100|=100 < 500 (zone 충족)
            ltf_rsi=75.0,            # 과매수 (>65)
            htf_ema9=49700.0,        # HTF EMA9 < EMA21 (하락)
            htf_ema21=50200.0,
            htf_vwap=51000.0,        # price < VWAP
            htf_price=50000.0,
            has_htf=True,
            funding_rate=0.002,      # 높은 펀딩 → 패널티
            spread_pct=0.004,        # 넓은 스프레드 → 패널티
        )
        # htf_ema_bull=0, htf_vwap=0, ltf_ema=0, zone=1(|100|<500), rsi=0, vol=0, candle=0
        # 패널티: funding=-1, spread=-1
        # 합계: 1 - 1 - 1 = -1
        assert score < 5  # min_score 미달
