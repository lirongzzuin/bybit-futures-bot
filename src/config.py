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

    # ── Strategy Selection ────────────────────────────────────
    # "swing": 현재 15분봉 기반 스윙/추세추종 전략 (기본값)
    # "scalp": 3분봉 기반 EMA Ribbon+VWAP+RSI 순수 스캘핑 전략
    STRATEGY_MODE: Literal["swing", "scalp"] = "swing"

    # ── Bybit API ─────────────────────────────────────────────
    BYBIT_API_KEY: str = ""
    BYBIT_API_SECRET: str = ""
    BYBIT_TESTNET: bool = True
    BYBIT_RECV_WINDOW: int = 5000
    BYBIT_HEDGE_MODE: bool = True   # True = hedge mode (long+short simultaneously)

    # ── Capital & Risk ────────────────────────────────────────
    PAPER_CAPITAL_USDT: float = 10000.0
    MAX_RISK_PER_TRADE_PCT: float = 0.02       # 2% equity risk per trade (aggressive compounding)
    MAX_DAILY_LOSS_PCT: float = 0.08           # 8% → Slack 알림 (거래 중지 없음)
    MAX_WEEKLY_LOSS_PCT: float = 0.15          # 15% → Slack 알림 (거래 중지 없음)
    MAX_DRAWDOWN_PCT: float = 0.30             # 30% → REDUCE 트리거 (포지션 비중 축소 시작)
    PAUSE_DRAWDOWN_PCT: float = 0.42           # 42% → PAUSE (신규 진입 중단, 기존 포지션 관리)
    HALT_DRAWDOWN_PCT: float = 0.50            # 50% → HALT (긴급 전면 중단 — 계좌 보호 핵심)
    MAX_LEVERAGE_CAP: int = 20                 # 20× max leverage (백테스트: PF=1.001 최적값)
    MAX_CONCURRENT_POSITIONS: int = 2
    MAX_TOTAL_EXPOSURE_PCT: float = 0.75       # 75% of equity exposed
    MAX_SINGLE_EXPOSURE_PCT: float = 0.30      # 30% per symbol

    # ── Signal Settings ───────────────────────────────────────
    SIGNAL_ENTRY_THRESHOLD: float = 0.60       # MTF 정렬 기반 — 최고품질 진입만 허용 (0.60 상향)
    SIGNAL_EXIT_THRESHOLD: float = 0.38
    SIGNAL_TIMEFRAME: str = "15"               # 15분봉 — 5분봉 대비 노이즈 3× 감소
    TREND_TIMEFRAME: str = "1440"              # 일봉 — 200일 이동평균 기반 불/베어마켓 구분
    UNIVERSE_REFRESH_INTERVAL_HOURS: int = 4
    TOP_N_COINS: int = 10                      # more symbols = more opportunities

    # ── Scalping Parameters (15분봉 기준) ──────────────────────────
    VWAP_SESSION_BARS: int = 96                # bars per session for VWAP
    SCALP_STOP_ATR: float = 1.2               # 타이트 SL → 높은 R:R 달성
    SCALP_TP1_ATR: float = 2.5                # TP1 R:R 2.08:1 (TP1 후 +1.0 ATR 트레일링)
    SCALP_TP2_ATR: float = 5.5                # TP2 R:R 4.58:1 (풀 트렌드 수익)
    SCALP_MAX_BARS: int = 48                   # 15분봉 × 48 = 12시간 최대
    SCALP_PARTIAL_EXIT_PCT: float = 0.55       # 55% TP1에서 청산, 45% 추세 유지

    # ── Trend Ride Parameters (15분봉 기준) ──────────────────────────
    TREND_STOP_ATR: float = 2.0               # 일봉 추세 동행 — 여유 있는 SL
    TREND_TP1_ATR: float = 4.0                # TP1 R:R 2.0:1 (TP1 후 +2.5ATR 트레일링)
    TREND_TP2_ATR: float = 10.0               # TP2 R:R 5.0:1 — 풀 트렌드 수익 극대화
    TREND_MAX_BARS: int = 240                  # 15분봉 × 240 = 60시간 최대
    TREND_PARTIAL_EXIT_PCT: float = 0.45       # close 45% at TP1, keep 55% for full run
    TREND_RIDE_SCORE_THRESHOLD: int = 1        # allow trend ride more often

    # ── Pure Scalping Strategy Parameters (STRATEGY_MODE="scalp") ──────────
    # LTF (진입 신호용) / HTF (추세 바이어스용) 타임프레임
    SCALP_PURE_LTF_TIMEFRAME: str = "3"       # 3분봉 — 스캘핑 진입 신호
    SCALP_PURE_HTF_TIMEFRAME: str = "15"      # 15분봉 — HTF 추세 바이어스 필터
    # EMA 파라미터 (LTF에서 사용)
    SCALP_PURE_EMA_FAST: int = 9
    SCALP_PURE_EMA_MID: int = 21
    SCALP_PURE_EMA_SLOW: int = 50
    # RSI 진입 구간
    SCALP_PURE_RSI_PERIOD: int = 14
    SCALP_PURE_RSI_LONG_MIN: float = 42.0     # 롱: RSI 42 이상 (모멘텀 형성, 과매도 회피)
    SCALP_PURE_RSI_LONG_MAX: float = 62.0     # 롱: RSI 62 이하 (과매수 회피, 타이트)
    SCALP_PURE_RSI_SHORT_MIN: float = 38.0    # 숏: RSI 38 이상 (과매도 반등 회피)
    SCALP_PURE_RSI_SHORT_MAX: float = 58.0    # 숏: RSI 58 이하 (타이트, 과매수 회피)
    # 진입 조건
    SCALP_PURE_VOLUME_MULT: float = 1.3       # 볼륨 확인: 평균 대비 1.3배 이상 (품질 강화)
    SCALP_PURE_EMA_ZONE_ATR: float = 1.0      # EMA 존 범위: EMA20 ± 1.0×ATR
    SCALP_PURE_ENTRY_MIN_SCORE: int = 6       # 진입 최소 점수 (9점 만점, HTF+LTF EMA 정렬 보장)
    SCALP_PURE_ENTRY_URGENT_SCORE: int = 9    # 긴급 진입 점수 → 시장가 선호 (최고 점수만)
    # 리스크 파라미터 (3분봉 ATR 기준, 15분봉보다 타이트)
    SCALP_PURE_STOP_ATR: float = 1.0          # SL: 1.0× ATR (타이트 — 빠른 손절)
    SCALP_PURE_TP1_ATR: float = 2.0           # TP1: 2.0× ATR (R:R 2.0:1 — 개선)
    SCALP_PURE_TP2_ATR: float = 4.5           # TP2: 4.5× ATR (R:R 4.5:1 — 개선, 더 큰 수익 극대화)
    SCALP_PURE_MAX_BARS: int = 30             # 최대 보유: 3분봉×30 = 90분 (TP2 도달 시간 충분히)
    SCALP_PURE_PARTIAL_EXIT_PCT: float = 0.50 # TP1에서 50% 청산, 50% 유지

    # ── Signal Quality Filters (15분봉 기준 최적화) ─────────────────
    SIGNAL_FILTER_ER_THRESHOLD: float = 0.06   # 추세성 확인 (완화로 기회 확대)
    SIGNAL_FILTER_HURST_MIN: float = 0.42      # 평균회귀 구간 진입 회피
    SIGNAL_FILTER_MIN_ATR_PCT: float = 0.0012  # 최소 0.12% ATR (수수료 대비 여유)

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
    TRAILING_STOP_ATR_MULTIPLIER: float = 1.0  # 1.0× ATR trailing — SCALP profit lock=1.5ATR, TREND lock=3.0ATR
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
    LIMIT_ORDER_TIMEOUT_SECONDS: int = 15   # 15s/시도 (스캘핑: 빠른 진입이 중요)
    LIMIT_ORDER_MAX_RETRIES: int = 2        # 2회 후 시장가 폴백 (총 최대 ~30s)
    REENTRY_COOLDOWN_SECONDS: int = 600        # 10 min — 스캘핑 모드 재진입 대기 (3m × 3~4봉)
    SPREAD_TAKER_THRESHOLD: float = 0.003  # 0.3% spread → use market order
    # 신호 강도가 이 값 초과 시 지정가 대신 시장가(즉시 체결 우선)
    SIGNAL_URGENT_THRESHOLD: float = 0.90  # > 0.90 → market order (urgency)

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
    BEAR_MAX_LEVERAGE_CAP: int = 20
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
