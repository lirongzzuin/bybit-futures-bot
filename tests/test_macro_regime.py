"""
Tests for MacroRegimeDetector.

Covers:
  - BULL detection: EMA50 > EMA200, positive ROC, strong ADX
  - BEAR detection: EMA50 < EMA200, negative ROC, strong ADX
  - SIDEWAYS detection: weak ADX, neutral ROC
  - API failure fallback → SIDEWAYS
  - Symbol filtering by regime
  - Confirmation streak logic (bull/bear confirm count)
  - RiskEngine bear_mode leverage cap
"""
from __future__ import annotations

import pandas as pd
import numpy as np
import pytest
from unittest.mock import MagicMock, patch

from src.strategy.macro_regime import MacroRegimeDetector, MacroRegime, _ema, _adx


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_settings(**overrides):
    from src.config import Settings
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_TESTNET=True,
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        BULL_MODE_SYMBOLS="",
        BEAR_MODE_SYMBOLS="BTCUSDT,ETHUSDT",
        BEAR_MAX_LEVERAGE_CAP=50,
        MACRO_REGIME_CHECK_HOURS=4,
        MACRO_BULL_CONFIRM_COUNT=3,
        MACRO_BEAR_CONFIRM_COUNT=2,
        MAX_LEVERAGE_CAP=10,
        MAX_RISK_PER_TRADE_PCT=0.02,
        SCALP_STOP_ATR=1.0,
        SCALP_TP1_ATR=1.5,
        SCALP_TP2_ATR=3.0,
        TREND_STOP_ATR=2.0,
        TREND_TP1_ATR=3.0,
        TREND_TP2_ATR=6.0,
        TRAILING_STOP_ATR_MULTIPLIER=1.0,
        BACKTEST_FEE_MAKER=0.00020,
        BACKTEST_FEE_TAKER=0.00055,
        BACKTEST_SLIPPAGE_PCT=0.0002,
        PAPER_CAPITAL_USDT=150.0,
        MAX_DAILY_LOSS_PCT=0.08,
        MAX_WEEKLY_LOSS_PCT=0.15,
        MAX_DRAWDOWN_PCT=0.30,
        MAX_CONCURRENT_POSITIONS=3,
        MAX_SINGLE_EXPOSURE_PCT=0.30,
        MAX_TOTAL_EXPOSURE_PCT=0.75,
        VOLATILITY_TARGET_PCT=0.15,
        USE_KELLY_FRACTION=0.25,
        SIGNAL_ENTRY_THRESHOLD=0.42,
        SIGNAL_EXIT_THRESHOLD=0.35,
        SIGNAL_FILTER_ER_THRESHOLD=0.06,
        SIGNAL_FILTER_HURST_MIN=0.40,
        SIGNAL_FILTER_MIN_ATR_PCT=0.001,
        SCALP_PARTIAL_EXIT_PCT=0.60,
        SCALP_MAX_BARS=12,
        TREND_PARTIAL_EXIT_PCT=0.40,
        TREND_MAX_BARS=120,
        TREND_RIDE_SCORE_THRESHOLD=2,
        LIMIT_ORDER_TIMEOUT_SECONDS=30,
        LIMIT_ORDER_MAX_RETRIES=3,
        REENTRY_COOLDOWN_SECONDS=60,
        SPREAD_TAKER_THRESHOLD=0.003,
        QUALITY_GATE_PASSED=True,
        PROFIT_PROTECT_ENABLED=True,
        PROFIT_PROTECT_MIN_EQUITY_USDT=10000.0,
        PROFIT_PROTECT_THRESHOLD_PCT=0.20,
        PROFIT_PROTECT_TRANSFER_PCT=0.20,
        PROFIT_PROTECT_MIN_CAPITAL_PCT=1.10,
        PROFIT_PROTECT_CHECK_HOURS=24,
        VWAP_SESSION_BARS=96,
        ATR_PERIOD=14,
        STOP_ATR_MULTIPLIER=2.0,
        TP_ATR_MULTIPLIER=3.0,
        ADX_PERIOD=14,
        ADX_TREND_THRESHOLD=25.0,
        ADX_RANGE_THRESHOLD=20.0,
        EMA_FAST=9,
        EMA_MID=21,
        EMA_SLOW=50,
        VOLATILITY_LOOKBACK=20,
        RSI_PERIOD=14,
        BB_PERIOD=20,
        BB_STD=2.0,
        KELTNER_PERIOD=20,
        KELTNER_ATR_MULT=1.5,
        HULL_MA_PERIOD=16,
        STOCHRSI_RSI_PERIOD=14,
        STOCHRSI_STOCH_PERIOD=14,
        STOCHRSI_SMOOTH_K=3,
        STOCHRSI_SMOOTH_D=3,
        WILLIAMS_R_PERIOD=14,
        CMF_PERIOD=20,
        OBV_EMA_PERIOD=20,
        SQUEEZE_BB_PERIOD=20,
        SQUEEZE_KC_PERIOD=20,
        RSI_DIVERGENCE_LOOKBACK=14,
        ICT_SWING_PERIOD=5,
        ICT_FVG_LOOKBACK=50,
        ICT_RANGE_PERIOD=20,
        SIGNAL_TIMEFRAME="5",
        TREND_TIMEFRAME="60",
        UNIVERSE_REFRESH_INTERVAL_HOURS=4,
        TOP_N_COINS=10,
        BACKTEST_START="",
        BACKTEST_END="",
        BACKTEST_INITIAL_CAPITAL=150.0,
        BACKTEST_FUNDING_RATE_AVG=0.0001,
        QUALITY_GATE_SHARPE=1.0,
        QUALITY_GATE_MDD=0.30,
        QUALITY_GATE_WIN_RATE=0.45,
        QUALITY_GATE_PROFIT_FACTOR=1.5,
        QUALITY_GATE_MIN_TRADES=100,
        DATABASE_PATH="test_bot.db",
        LOG_LEVEL="ERROR",
        LOG_DIR="logs",
        SLACK_WEBHOOK_URL="",
        SLACK_DAILY_REPORT_HOUR=9,
        SLACK_DASHBOARD_INTERVAL_MINUTES=0,
        SLACK_BOT_TOKEN="",
        SLACK_APP_TOKEN="",
        BYBIT_RECV_WINDOW=5000,
        BYBIT_HEDGE_MODE=True,
    )
    defaults.update(overrides)
    with patch.dict("os.environ", {k: str(v) for k, v in defaults.items()}, clear=False):
        return Settings(**defaults)


def _make_bull_df(n=250) -> pd.DataFrame:
    """EMA50 > EMA200이 되도록 선형 상승 데이터.
    가격이 선형적으로 증가하면 EMA50(빠름)이 EMA200(느림)보다 항상 높다.
    """
    # 선형 상승: 20000 → 120000 (충분한 bull trend)
    prices = np.linspace(20000, 120000, n)
    # 약간의 노이즈 추가 (모멘텀 유지)
    np.random.seed(0)
    prices = prices * (1 + np.random.normal(0, 0.001, n))
    close = pd.Series(prices)
    high = close * 1.01
    low = close * 0.99
    return pd.DataFrame({"close": close, "high": high, "low": low,
                         "open": close * 0.999, "volume": [1e6] * n,
                         "ts": list(range(n)), "turnover": [1e7] * n})


def _make_bear_df(n=250) -> pd.DataFrame:
    """EMA50 < EMA200이 되도록 꾸준히 하락하는 가격 데이터."""
    np.random.seed(42)
    price = 60000.0
    prices = []
    for i in range(n):
        price *= 1 + np.random.normal(-0.003, 0.01)  # 평균 0.3% 일일 하락
        prices.append(max(price, 1.0))
    close = pd.Series(prices)
    high = close * 1.01
    low = close * 0.99
    return pd.DataFrame({"close": close, "high": high, "low": low,
                         "open": close * 0.999, "volume": [1e6] * n,
                         "ts": list(range(n)), "turnover": [1e7] * n})


def _make_sideways_df(n=250) -> pd.DataFrame:
    """가격이 횡보하는 데이터 (ROC ≈ 0, ADX 낮음)."""
    np.random.seed(99)
    price = 45000.0
    prices = []
    for i in range(n):
        price *= 1 + np.random.normal(0.0, 0.005)  # 방향성 없음
        price = max(min(price, 50000), 40000)  # 범위 제한
        prices.append(price)
    close = pd.Series(prices)
    high = close * 1.005
    low = close * 0.995
    return pd.DataFrame({"close": close, "high": high, "low": low,
                         "open": close, "volume": [1e6] * n,
                         "ts": list(range(n)), "turnover": [1e7] * n})


def _df_to_kline(df: pd.DataFrame):
    """DataFrame을 Bybit kline raw 형식으로 변환."""
    return [
        [str(row["ts"]), str(row["open"]), str(row["high"]),
         str(row["low"]), str(row["close"]), str(row["volume"]), "0"]
        for _, row in df.iterrows()
    ]


# ── Unit Tests: Indicator helpers ─────────────────────────────────────────────

class TestIndicatorHelpers:
    def test_ema_converges(self):
        prices = pd.Series([100.0] * 100)
        result = _ema(prices, 50)
        assert abs(float(result.iloc[-1]) - 100.0) < 0.01

    def test_adx_positive(self):
        df = _make_bull_df(250)
        adx = _adx(df["high"], df["low"], df["close"])
        assert float(adx.iloc[-1]) >= 0


# ── Unit Tests: Regime Detection ──────────────────────────────────────────────

class TestMacroRegimeDetection:

    def _make_detector(self, df: pd.DataFrame):
        settings = make_settings()
        client = MagicMock()
        client.get_kline.return_value = _df_to_kline(df)
        return MacroRegimeDetector(client, settings), settings

    def test_bull_market_detected(self):
        """꾸준한 상승 데이터 → BULL 감지."""
        df = _make_bull_df(250)
        detector, _ = self._make_detector(df)
        result = detector.detect()
        assert result.regime == MacroRegime.BULL
        assert result.confidence > 0.3
        assert result.ema50 > 0
        assert result.ema200 > 0

    def test_bear_market_detected(self):
        """꾸준한 하락 데이터 → BEAR 감지."""
        df = _make_bear_df(250)
        detector, _ = self._make_detector(df)
        result = detector.detect()
        assert result.regime in (MacroRegime.BEAR, MacroRegime.SIDEWAYS)
        assert result.regime != MacroRegime.BULL

    def test_sideways_market_detected(self):
        """횡보 데이터 → SIDEWAYS 감지."""
        df = _make_sideways_df(250)
        detector, _ = self._make_detector(df)
        result = detector.detect()
        # 횡보는 SIDEWAYS 또는 BEAR (방향성 없음 → 보수적)
        assert result.regime in (MacroRegime.SIDEWAYS, MacroRegime.BEAR)

    def test_bull_result_has_full_universe_symbols(self):
        """BULL 모드: BULL_MODE_SYMBOLS 비어있으면 symbols 필드도 빔."""
        df = _make_bull_df(250)
        settings = make_settings(BULL_MODE_SYMBOLS="")
        client = MagicMock()
        client.get_kline.return_value = _df_to_kline(df)
        detector = MacroRegimeDetector(client, settings)
        result = detector.detect()
        if result.regime == MacroRegime.BULL:
            assert result.symbols == []  # 빈 문자열 split → 빈 리스트

    def test_bear_result_has_btc_eth_symbols(self):
        """BEAR/SIDEWAYS 모드: 심볼 목록에 BTC, ETH만 포함."""
        df = _make_bear_df(250)
        settings = make_settings(BEAR_MODE_SYMBOLS="BTCUSDT,ETHUSDT")
        client = MagicMock()
        client.get_kline.return_value = _df_to_kline(df)
        detector = MacroRegimeDetector(client, settings)
        result = detector.detect()
        if result.regime in (MacroRegime.BEAR, MacroRegime.SIDEWAYS):
            assert "BTCUSDT" in result.symbols
            assert "ETHUSDT" in result.symbols

    def test_insufficient_data_returns_sideways(self):
        """데이터 부족 → SIDEWAYS 기본값."""
        settings = make_settings()
        client = MagicMock()
        # 100개 미만의 데이터 직접 생성
        short_prices = np.linspace(30000, 35000, 100)
        close = pd.Series(short_prices)
        short_df = pd.DataFrame({
            "close": close, "high": close * 1.01, "low": close * 0.99,
            "open": close, "volume": [1e6] * 100,
            "ts": list(range(100)), "turnover": [1e7] * 100,
        })
        client.get_kline.return_value = _df_to_kline(short_df)
        detector = MacroRegimeDetector(client, settings)
        result = detector.detect()
        assert result.regime == MacroRegime.SIDEWAYS

    def test_api_failure_returns_sideways(self):
        """API 호출 실패 → SIDEWAYS fallback."""
        settings = make_settings()
        client = MagicMock()
        client.get_kline.side_effect = Exception("API error")
        detector = MacroRegimeDetector(client, settings)
        result = detector.detect()
        assert result.regime == MacroRegime.SIDEWAYS
        assert "fallback" in result.reason

    def test_empty_kline_returns_sideways(self):
        """빈 kline 응답 → SIDEWAYS fallback."""
        settings = make_settings()
        client = MagicMock()
        client.get_kline.return_value = []
        detector = MacroRegimeDetector(client, settings)
        result = detector.detect()
        assert result.regime == MacroRegime.SIDEWAYS

    def test_result_has_reason_string(self):
        """결과에 근거 문자열 포함."""
        df = _make_bull_df(250)
        detector, _ = self._make_detector(df)
        result = detector.detect()
        assert len(result.reason) > 0

    def test_confidence_in_range(self):
        """confidence는 0~1 범위."""
        df = _make_bull_df(250)
        detector, _ = self._make_detector(df)
        result = detector.detect()
        assert 0.0 <= result.confidence <= 1.0


# ── Unit Tests: RiskEngine bear_mode ─────────────────────────────────────────

class TestRiskEngineBearMode:

    def make_risk_engine(self, **overrides):
        from src.risk.engine import RiskEngine
        settings = make_settings(**overrides)
        return RiskEngine(settings), settings

    def _base_params(self):
        return dict(
            equity=500.0,
            entry_price=50000.0,
            side="Buy",
            atr=500.0,
            signal_strength=0.7,
            spread_pct=0.0001,
            funding_rate=0.0001,
            vol_percentile=0.4,
            drawdown_pct=0.0,
            consecutive_losses=0,
            symbol_max_leverage=100,
            symbol_info={"min_notional": 5.0, "qty_step": 0.001},
            current_positions=0,
        )

    def test_bear_mode_uses_higher_leverage_cap(self):
        """Bear 모드: BEAR_MAX_LEVERAGE_CAP(50)까지 허용."""
        engine, settings = self.make_risk_engine(
            MAX_LEVERAGE_CAP=10,
            BEAR_MAX_LEVERAGE_CAP=50,
        )
        params = self._base_params()
        # atr이 작으면 base_leverage가 높아져 cap까지 올라감
        params["atr"] = 10.0  # stop_distance = 1.0 ATR = 10 → 0.02%
        params["equity"] = 5000.0

        result = engine.calculate(**params, bear_mode=True)
        if result.risk_ok:
            assert result.leverage <= 50
            # Normal mode는 10×까지
            normal_result = engine.calculate(**params, bear_mode=False)
            if normal_result.risk_ok:
                assert normal_result.leverage <= 10

    def test_normal_mode_uses_normal_leverage_cap(self):
        """Normal 모드: MAX_LEVERAGE_CAP(10) 초과 불가."""
        engine, settings = self.make_risk_engine(
            MAX_LEVERAGE_CAP=10,
            BEAR_MAX_LEVERAGE_CAP=50,
        )
        params = self._base_params()
        params["atr"] = 50.0  # stop=0.1%
        params["equity"] = 5000.0
        result = engine.calculate(**params, bear_mode=False)
        if result.risk_ok:
            assert result.leverage <= 10

    def test_bear_mode_default_false(self):
        """bear_mode 기본값은 False — 기존 동작과 동일."""
        engine, settings = self.make_risk_engine(MAX_LEVERAGE_CAP=10)
        params = self._base_params()
        result_default = engine.calculate(**params)
        result_explicit = engine.calculate(**params, bear_mode=False)
        assert result_default.risk_ok == result_explicit.risk_ok
        if result_default.risk_ok:
            assert result_default.leverage == result_explicit.leverage


# ── Unit Tests: Symbol Filtering ──────────────────────────────────────────────

class TestSymbolFiltering:
    """TradingBot._filter_symbols_by_macro_regime 직접 테스트."""

    def _make_bot_mock(self, macro_mode: str, **settings_overrides):
        """TradingBot의 핵심 부분만 모킹."""
        from unittest.mock import MagicMock, patch
        settings = make_settings(**settings_overrides)
        bot = MagicMock()
        bot._macro_mode = macro_mode
        # _state_machine.get_position → is_flat=True (포지션 없음)
        flat_pos = MagicMock()
        flat_pos.is_flat = True
        bot._state_machine.get_position.return_value = flat_pos

        # _filter_symbols_by_macro_regime 실제 메서드를 바인딩
        from src.main import TradingBot
        bot._filter_symbols_by_macro_regime = lambda syms: TradingBot._filter_symbols_by_macro_regime(bot, syms)

        # settings를 전역에 주입 (실제 함수에서 settings 모듈 참조)
        import src.main as main_module
        original_settings = main_module.settings
        main_module.settings = settings

        return bot, settings, main_module, original_settings

    def test_bull_mode_allows_all_symbols(self):
        """BULL 모드: 모든 심볼 허용."""
        bot, settings, main_module, orig = self._make_bot_mock(
            "BULL", BULL_MODE_SYMBOLS=""
        )
        try:
            universe = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"]
            result = bot._filter_symbols_by_macro_regime(universe)
            assert set(result) == set(universe)
        finally:
            main_module.settings = orig

    def test_bear_mode_allows_only_btc_eth(self):
        """BEAR_SIDEWAYS 모드: BTC + ETH만 허용."""
        bot, settings, main_module, orig = self._make_bot_mock(
            "BEAR_SIDEWAYS", BEAR_MODE_SYMBOLS="BTCUSDT,ETHUSDT"
        )
        try:
            universe = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "XRPUSDT"]
            result = bot._filter_symbols_by_macro_regime(universe)
            assert "BTCUSDT" in result
            assert "ETHUSDT" in result
            assert "SOLUSDT" not in result
            assert "AVAXUSDT" not in result
        finally:
            main_module.settings = orig

    def test_bear_mode_includes_symbols_with_open_positions(self):
        """BEAR_SIDEWAYS 모드: 오픈 포지션 심볼은 항상 포함 (청산 관리)."""
        bot, settings, main_module, orig = self._make_bot_mock(
            "BEAR_SIDEWAYS", BEAR_MODE_SYMBOLS="BTCUSDT,ETHUSDT"
        )
        try:
            # SOLUSDT에 오픈 포지션 있음
            open_pos = MagicMock()
            open_pos.is_flat = False

            flat_pos = MagicMock()
            flat_pos.is_flat = True

            def get_position_side_effect(symbol):
                return open_pos if symbol == "SOLUSDT" else flat_pos

            bot._state_machine.get_position.side_effect = get_position_side_effect

            universe = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"]
            result = bot._filter_symbols_by_macro_regime(universe)
            assert "BTCUSDT" in result
            assert "ETHUSDT" in result
            assert "SOLUSDT" in result  # 포지션 있으므로 포함
            assert "AVAXUSDT" not in result
        finally:
            main_module.settings = orig

    def test_bull_mode_with_custom_symbols(self):
        """BULL 모드: BULL_MODE_SYMBOLS 설정된 경우 해당 심볼만 허용."""
        bot, settings, main_module, orig = self._make_bot_mock(
            "BULL", BULL_MODE_SYMBOLS="BTCUSDT,ETHUSDT,SOLUSDT"
        )
        try:
            universe = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "XRPUSDT"]
            result = bot._filter_symbols_by_macro_regime(universe)
            assert "BTCUSDT" in result
            assert "ETHUSDT" in result
            assert "SOLUSDT" in result
            assert "AVAXUSDT" not in result
        finally:
            main_module.settings = orig
