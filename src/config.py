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
    MAX_RISK_PER_TRADE_PCT: float = 0.015      # 1.5% equity risk per trade
    MAX_DAILY_LOSS_PCT: float = 0.05           # 5% → halt trading for today
    MAX_WEEKLY_LOSS_PCT: float = 0.10          # 10% → halt for the week
    MAX_DRAWDOWN_PCT: float = 0.25             # 25% → switch to conservative
    MAX_LEVERAGE_CAP: int = 20                 # risk-engine ceiling (not exchange)
    MAX_CONCURRENT_POSITIONS: int = 3
    MAX_TOTAL_EXPOSURE_PCT: float = 0.60       # 60% of equity exposed
    MAX_SINGLE_EXPOSURE_PCT: float = 0.25      # 25% per symbol

    # ── Signal Settings ───────────────────────────────────────
    SIGNAL_ENTRY_THRESHOLD: float = 0.48       # combined strength to enter
    SIGNAL_EXIT_THRESHOLD: float = 0.40
    SIGNAL_TIMEFRAME: str = "5"                # minutes; Bybit interval string (scalp default)
    TREND_TIMEFRAME: str = "60"                # higher timeframe for regime detection
    UNIVERSE_REFRESH_INTERVAL_HOURS: int = 4
    TOP_N_COINS: int = 5

    # ── Scalping Parameters ───────────────────────────────────────
    VWAP_SESSION_BARS: int = 96                # bars per session for VWAP
    SCALP_STOP_ATR: float = 1.0               # tight stop for scalp mode
    SCALP_TP1_ATR: float = 1.5                # first TP level
    SCALP_TP2_ATR: float = 2.8                # second TP level (after breakeven)
    SCALP_MAX_BARS: int = 15                   # max bars to hold in scalp mode
    SCALP_PARTIAL_EXIT_PCT: float = 0.60       # close 60% at TP1

    # ── Trend Ride Parameters ─────────────────────────────────────
    TREND_STOP_ATR: float = 1.5               # wider stop for trend ride
    TREND_TP1_ATR: float = 2.0                # first TP for trend ride
    TREND_TP2_ATR: float = 4.0                # second TP for trend ride
    TREND_MAX_BARS: int = 120                  # max bars in trend ride
    TREND_PARTIAL_EXIT_PCT: float = 0.50       # close 50% at TP1
    TREND_RIDE_SCORE_THRESHOLD: int = 2        # score needed to switch to TREND_RIDE

    # ── Signal Quality Filters (Kaufman / Peters) ─────────────────
    SIGNAL_FILTER_ER_THRESHOLD: float = 0.08    # 50-bar ER min for 5-min crypto (0.10-0.30 typical)
    SIGNAL_FILTER_HURST_MIN: float = 0.45       # Hurst exponent min for momentum
    SIGNAL_FILTER_MIN_ATR_PCT: float = 0.0004   # Min ATR as % of price (dead market)

    # ── Advanced Risk Management ──────────────────────────────────
    VOLATILITY_TARGET_PCT: float = 0.15         # AQR vol-targeting (15% annualized)
    USE_KELLY_FRACTION: float = 0.25            # Quarter-Kelly position size cap

    # ── Indicators ────────────────────────────────────────────
    ATR_PERIOD: int = 14
    STOP_ATR_MULTIPLIER: float = 2.0
    TP_ATR_MULTIPLIER: float = 3.0
    TRAILING_STOP_ATR_MULTIPLIER: float = 1.5
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

    # ── ICT Settings ─────────────────────────────────────────
    ICT_SWING_PERIOD: int = 5              # bars each side for swing pivot
    ICT_FVG_LOOKBACK: int = 50
    ICT_RANGE_PERIOD: int = 20             # bars for premium/discount range

    # ── Order Execution ───────────────────────────────────────
    LIMIT_ORDER_TIMEOUT_SECONDS: int = 30
    LIMIT_ORDER_MAX_RETRIES: int = 3
    REENTRY_COOLDOWN_SECONDS: int = 300
    SPREAD_TAKER_THRESHOLD: float = 0.003  # 0.3% spread → use market order

    # ── Backtest ─────────────────────────────────────────────
    BACKTEST_START: str = ""               # ISO date; empty = 5 years ago
    BACKTEST_END: str = ""                 # ISO date; empty = today
    BACKTEST_FEE_TAKER: float = 0.00055    # Bybit taker fee
    BACKTEST_FEE_MAKER: float = 0.00020    # Bybit maker fee
    BACKTEST_SLIPPAGE_PCT: float = 0.0002  # realistic slippage for BTC perp (0.02% per side)
    BACKTEST_INITIAL_CAPITAL: float = 10000.0
    BACKTEST_FUNDING_RATE_AVG: float = 0.0001  # avg funding rate per 8h interval

    # ── Quality Gate ─────────────────────────────────────────
    QUALITY_GATE_SHARPE: float = 1.0
    QUALITY_GATE_MDD: float = 0.30
    QUALITY_GATE_WIN_RATE: float = 0.45
    QUALITY_GATE_PROFIT_FACTOR: float = 1.5
    QUALITY_GATE_MIN_TRADES: int = 100
    QUALITY_GATE_PASSED: bool = False      # flipped only by backtest runner

    # ── Profit Protection ─────────────────────────────────────
    PROFIT_PROTECT_ENABLED: bool = True
    # Transfer to Funding Account when profit exceeds this % of initial capital
    PROFIT_PROTECT_THRESHOLD_PCT: float = 0.20    # 20% profit → trigger
    # What fraction of the profit to move to Funding Account
    PROFIT_PROTECT_TRANSFER_PCT: float = 0.50     # move 50% of profits
    # Always keep at least this multiple of initial capital in trading account
    PROFIT_PROTECT_MIN_CAPITAL_PCT: float = 1.10  # keep ≥ 110% of initial capital
    # How often to check (hours)
    PROFIT_PROTECT_CHECK_HOURS: int = 24

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
