"""
Central configuration via pydantic-settings.
All values sourced from environment variables / .env file.
"""
from __future__ import annotations

from typing import Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Trading Mode ──────────────────────────────────────────
    TRADING_MODE: Literal["dry-run", "paper", "live"] = "dry-run"

    # ── Bybit API ─────────────────────────────────────────────
    BYBIT_API_KEY: str = ""
    BYBIT_API_SECRET: str = ""
    BYBIT_TESTNET: bool = True
    BYBIT_RECV_WINDOW: int = 5000
    BYBIT_HEDGE_MODE: bool = True   # True = hedge mode (long+short simultaneously)

    # ── Capital & Risk ────────────────────────────────────────
    PAPER_CAPITAL_USDT: float = 10000.0
    MAX_RISK_PER_TRADE_PCT: float = 0.02       # 2% equity risk per trade (aggressive compounding)
    MAX_DAILY_LOSS_PCT: float = 0.08           # 8% → halt trading for today
    MAX_WEEKLY_LOSS_PCT: float = 0.15          # 15% → halt for the week
    MAX_DRAWDOWN_PCT: float = 0.30             # 30% → switch to conservative
    MAX_LEVERAGE_CAP: int = 10                 # 10× max leverage
    MAX_CONCURRENT_POSITIONS: int = 3
    MAX_TOTAL_EXPOSURE_PCT: float = 0.75       # 75% of equity exposed
    MAX_SINGLE_EXPOSURE_PCT: float = 0.30      # 30% per symbol

    # ── Signal Settings ───────────────────────────────────────
    SIGNAL_ENTRY_THRESHOLD: float = 0.52       # MTF 정렬 기반 — 고품질 진입만 허용
    SIGNAL_EXIT_THRESHOLD: float = 0.42
    SIGNAL_TIMEFRAME: str = "15"               # 15분봉 — 5분봉 대비 노이즈 3× 감소
    TREND_TIMEFRAME: str = "1440"              # 일봉 — 200일 이동평균 기반 불/베어마켓 구분
    UNIVERSE_REFRESH_INTERVAL_HOURS: int = 4
    TOP_N_COINS: int = 10                      # more symbols = more opportunities

    # ── Scalping Parameters (15분봉 기준) ──────────────────────────
    VWAP_SESSION_BARS: int = 96                # bars per session for VWAP
    SCALP_STOP_ATR: float = 1.5               # 15분봉 ATR 기반 — 노이즈 초과 SL
    SCALP_TP1_ATR: float = 2.5                # TP1 R:R 1.67:1 (승률 높은 구간)
    SCALP_TP2_ATR: float = 5.0                # TP2 R:R 3.33:1 (추세 연장 시)
    SCALP_MAX_BARS: int = 24                   # 15분봉 × 24 = 6시간 최대
    SCALP_PARTIAL_EXIT_PCT: float = 0.55       # 55% TP1에서 청산, 45% 추세 유지

    # ── Trend Ride Parameters (15분봉 기준) ──────────────────────────
    TREND_STOP_ATR: float = 2.5               # 4H 추세 동행 — 더 여유 있는 SL
    TREND_TP1_ATR: float = 4.0                # TP1 R:R 1.6:1
    TREND_TP2_ATR: float = 8.0                # TP2 R:R 3.2:1 — 풀 트렌드 수익
    TREND_MAX_BARS: int = 96                   # 15분봉 × 96 = 24시간 최대
    TREND_PARTIAL_EXIT_PCT: float = 0.40       # close 40% at TP1, keep 60% for full run
    TREND_RIDE_SCORE_THRESHOLD: int = 2        # allow trend ride more often

    # ── Signal Quality Filters (15분봉 기준 강화) ─────────────────
    SIGNAL_FILTER_ER_THRESHOLD: float = 0.10   # 추세성 확인 (0.15보다 약간 완화)
    SIGNAL_FILTER_HURST_MIN: float = 0.46      # 평균회귀 구간 진입 회피
    SIGNAL_FILTER_MIN_ATR_PCT: float = 0.002   # 최소 0.2% ATR (수수료 대비 여유)

    # ── Higher Timeframe (4H) Trend Filter ──────────────────────
    HTF_EMA_FAST: int = 9                      # 4H EMA fast
    HTF_EMA_MID: int = 21                      # 4H EMA mid
    HTF_EMA_SLOW: int = 50                     # 4H EMA slow (추세 방향 기준)
    HTF_EMA_TREND: int = 200                   # 일봉 EMA 200 (불/베어마켓 가장 중요한 기준선)
    HTF_ADX_THRESHOLD: float = 20.0            # 4H ADX 최소값 (추세 확인)
    HTF_RSI_BULL_MIN: float = 40.0             # 롱 진입 시 4H RSI 최소 (과매도 회피)
    HTF_RSI_BULL_MAX: float = 70.0             # 롱 진입 시 4H RSI 최대 (과매수 회피)
    HTF_RSI_BEAR_MIN: float = 30.0             # 숏 진입 시 4H RSI 최소
    HTF_RSI_BEAR_MAX: float = 60.0             # 숏 진입 시 4H RSI 최대

    # ── Advanced Risk Management ──────────────────────────────────
    VOLATILITY_TARGET_PCT: float = 0.15         # AQR vol-targeting (15% annualized)
    USE_KELLY_FRACTION: float = 0.25            # Quarter-Kelly position size cap

    # ── Indicators ────────────────────────────────────────────
    ATR_PERIOD: int = 14
    STOP_ATR_MULTIPLIER: float = 2.0
    TP_ATR_MULTIPLIER: float = 3.0
    TRAILING_STOP_ATR_MULTIPLIER: float = 1.0  # 1.0× ATR trailing — meaningful profit lock after TP1
    ADX_PERIOD: int = 14
    ADX_TREND_THRESHOLD: float = 25.0
    ADX_RANGE_THRESHOLD: float = 20.0
    EMA_FAST: int = 9
    EMA_MID: int = 21
    EMA_SLOW: int = 50
    VOLATILITY_LOOKBACK: int = 20
    RSI_PERIOD: int = 14
    BB_PERIOD: int = 20
    BB_STD: float = 2.0
    # Keltner channels (Chester Keltner / Linda Raschke)
    KELTNER_PERIOD: int = 20
    KELTNER_ATR_MULT: float = 1.5
    # Hull Moving Average (Alan Hull, 2005)
    HULL_MA_PERIOD: int = 16

    # ── Advanced Indicators ───────────────────────────────────
    STOCHRSI_RSI_PERIOD: int = 14          # RSI period for StochRSI
    STOCHRSI_STOCH_PERIOD: int = 14        # Stochastic period
    STOCHRSI_SMOOTH_K: int = 3             # K line smoothing
    STOCHRSI_SMOOTH_D: int = 3             # D signal line smoothing
    WILLIAMS_R_PERIOD: int = 14            # Williams %R period
    CMF_PERIOD: int = 20                   # Chaikin Money Flow period
    OBV_EMA_PERIOD: int = 20               # OBV trend EMA
    SQUEEZE_BB_PERIOD: int = 20            # Squeeze momentum BB period
    SQUEEZE_KC_PERIOD: int = 20            # Squeeze momentum KC period
    RSI_DIVERGENCE_LOOKBACK: int = 14      # RSI divergence lookback window

    # ── ICT Settings ─────────────────────────────────────────
    ICT_SWING_PERIOD: int = 5              # bars each side for swing pivot
    ICT_FVG_LOOKBACK: int = 50
    ICT_RANGE_PERIOD: int = 20             # bars for premium/discount range

    # ── Order Execution ───────────────────────────────────────
    LIMIT_ORDER_TIMEOUT_SECONDS: int = 30
    LIMIT_ORDER_MAX_RETRIES: int = 3
    REENTRY_COOLDOWN_SECONDS: int = 900        # 15 min = 1 full bar on 15m timeframe
    SPREAD_TAKER_THRESHOLD: float = 0.003  # 0.3% spread → use market order

    # ── Backtest ─────────────────────────────────────────────
    BACKTEST_START: str = ""               # ISO date; empty = 5 years ago
    BACKTEST_END: str = ""                 # ISO date; empty = today
    BACKTEST_FEE_TAKER: float = 0.00055    # Bybit taker fee
    BACKTEST_FEE_MAKER: float = 0.00020    # Bybit maker fee
    BACKTEST_SLIPPAGE_PCT: float = 0.0002  # realistic slippage for BTC perp (0.02% per side)
    BACKTEST_INITIAL_CAPITAL: float = 10000.0
    BACKTEST_FUNDING_RATE_AVG: float = 0.0001  # avg funding rate per 8h interval

    # ── Quality Gate (MTF 전략 기준 상향) ────────────────────────
    QUALITY_GATE_SHARPE: float = 1.2
    QUALITY_GATE_MDD: float = 0.25
    QUALITY_GATE_WIN_RATE: float = 0.60        # 60%+ 승률 목표
    QUALITY_GATE_PROFIT_FACTOR: float = 1.8    # PF 1.8+ 목표
    QUALITY_GATE_MIN_TRADES: int = 50          # MTF는 진입 횟수 줄어드므로 조정
    QUALITY_GATE_PASSED: bool = False      # flipped only by backtest runner

    # ── Profit Protection ─────────────────────────────────────
    PROFIT_PROTECT_ENABLED: bool = True
    # 최소 계좌 잔고: 이 금액 이상일 때만 이체 실행 (복리 성장 단계 보호)
    # $10,000 달성 전까지 이체 비활성화 → 복리의 마법 최대한 누리기
    PROFIT_PROTECT_MIN_EQUITY_USDT: float = 10000.0
    # Transfer to Funding Account when profit exceeds this % of initial capital
    PROFIT_PROTECT_THRESHOLD_PCT: float = 0.20    # 20% profit → trigger
    # What fraction of the profit to move to Funding Account
    # 복리 극대화: 수익의 20%만 이체 (80%는 재투자)
    PROFIT_PROTECT_TRANSFER_PCT: float = 0.20     # move 20% of profits (복리 극대화)
    # Always keep at least this multiple of initial capital in trading account
    PROFIT_PROTECT_MIN_CAPITAL_PCT: float = 1.10  # keep ≥ 110% of initial capital
    # How often to check (hours)
    PROFIT_PROTECT_CHECK_HOURS: int = 24

    # ── Macro Regime (시장 상황 자동 전환) ────────────────────────────────────
    # BULL  모드 심볼: 상승장 — 멀티코인 스캘핑 (빈 문자열 = TOP_N_COINS 자동 선택)
    BULL_MODE_SYMBOLS: str = ""
    # BEAR/SIDEWAYS 모드 심볼: 하락장/횡보장 — BTC + ETH만 거래
    BEAR_MODE_SYMBOLS: str = "BTCUSDT,ETHUSDT"
    # Bear/Sideways 모드에서 허용하는 최대 레버리지 (고배율 스캘핑)
    BEAR_MAX_LEVERAGE_CAP: int = 50
    # 매크로 레짐 체크 주기 (시간)
    MACRO_REGIME_CHECK_HOURS: int = 4
    # BULL 전환 확정에 필요한 연속 신호 횟수
    MACRO_BULL_CONFIRM_COUNT: int = 3
    # BEAR/SIDEWAYS 전환 확정에 필요한 연속 신호 횟수
    MACRO_BEAR_CONFIRM_COUNT: int = 2

    # ── Slack ─────────────────────────────────────────────────
    SLACK_WEBHOOK_URL: str = ""
    SLACK_DAILY_REPORT_HOUR: int = 9       # KST
    SLACK_DASHBOARD_INTERVAL_MINUTES: int = 120  # 0 = disabled

    # Slack Bot (Socket Mode) — for on-demand slash commands
    # SLACK_BOT_TOKEN  : xoxb-...  (Bot User OAuth Token)
    # SLACK_APP_TOKEN  : xapp-...  (App-Level Token, scope: connections:write)
    SLACK_BOT_TOKEN: str = ""
    SLACK_APP_TOKEN: str = ""

    # ── Database ─────────────────────────────────────────────
    DATABASE_PATH: str = "bot.db"

    # ── Logging ──────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"

    model_config = {
        "env_file": ".env",
        "case_sensitive": True,
        "extra": "ignore",
    }

    # ── Helpers ───────────────────────────────────────────────
    def is_live(self) -> bool:
        return self.TRADING_MODE == "live"

    def is_paper(self) -> bool:
        return self.TRADING_MODE == "paper"

    def is_dry_run(self) -> bool:
        return self.TRADING_MODE == "dry-run"

    def check_live_allowed(self) -> None:
        """Raise if LIVE mode is requested but quality gate not passed."""
        if self.is_live() and not self.QUALITY_GATE_PASSED:
            raise RuntimeError(
                "LIVE mode requires QUALITY_GATE_PASSED=true.\n"
                "Run the backtest first: python -m src.backtest.engine --run\n"
                "Then set QUALITY_GATE_PASSED=true in .env after reviewing the report."
            )

    @field_validator("TRADING_MODE")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        return v.lower()


settings = Settings()
