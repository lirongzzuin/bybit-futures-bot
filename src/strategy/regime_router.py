"""
Dynamic Regime Router
=====================
매 사이클 multi-timeframe 장세를 감지하고 최적 전략 + 리스크 파라미터를 자동 선택한다.

장세 분류 (7개):
  STRONG_BULL  : EMA21>50>200 + ADX > ADX_STRONG(27)   → trend, LONG only, TREND_RIDE
  BULL         : EMA21>50>200 + ADX ≥ ADX_MODERATE(18) → trend, LONG only, SCALP
  SIDEWAYS     : 혼합 EMA 또는 모멘텀 없음              → scalp, neutral, PURE_SCALP
  BEAR         : EMA21<50<200 + ADX ≥ ADX_MODERATE(18) → trend, SHORT only, SCALP
  STRONG_BEAR  : EMA21<50<200 + ADX > ADX_STRONG(27)   → trend, SHORT only, TREND_RIDE
  VOLATILE     : ATR 백분위 > ATR_VOLATILE(0.82)        → scalp, neutral, PURE_SCALP (사이즈 50%)
  TRANSITION   : 레짐 전환 확정 중                      → 진입 없음 (skip)

확정 로직 (디바운스):
  - 같은 그룹 전환 (BULL↔STRONG_BULL, BEAR↔STRONG_BEAR): 즉시 확정
  - 다른 그룹 전환 (예: BULL→BEAR): CONFIRM_BARS 연속 일치 후 확정
  - 확정 전 사이클: TRANSITION 반환 → 진입 없음

사용법:
    router = RegimeRouter(settings)
    result = router.detect("BTCUSDT", mtf_df_15m, macro_bear=False)
    if result.strategy != "skip":
        # route to trend or scalp strategy
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from src.config import Settings
from src.logger import get_logger
from src.strategy.indicators import adx as calc_adx, atr as calc_atr, ema, volatility_percentile

log = get_logger(__name__)


class DynamicRegime(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL        = "BULL"
    SIDEWAYS    = "SIDEWAYS"
    BEAR        = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"
    VOLATILE    = "VOLATILE"
    TRANSITION  = "TRANSITION"


def _regime_group(regime: DynamicRegime) -> str:
    """레짐 그룹 반환 (같은 그룹 내 전환은 즉시 확정)."""
    if regime in (DynamicRegime.STRONG_BULL, DynamicRegime.BULL):
        return "bull"
    if regime in (DynamicRegime.STRONG_BEAR, DynamicRegime.BEAR):
        return "bear"
    if regime == DynamicRegime.VOLATILE:
        return "volatile"
    return "neutral"  # SIDEWAYS, TRANSITION


@dataclass
class RouterResult:
    """RegimeRouter.detect() 반환값."""
    regime: DynamicRegime
    strategy: str           # "trend" | "scalp" | "skip"
    trade_mode: str         # "TREND_RIDE" | "SCALP" | "PURE_SCALP" | ""
    direction_bias: int     # +1=LONG only, -1=SHORT only, 0=both
    risk_multiplier: float  # qty_base에 적용할 스케일 (0.0–1.0)
    leverage_cap: int       # 레짐별 레버리지 상한
    confidence: float       # 0.0–1.0
    reason: str             # 분류 근거 (로그용)


# ── 레짐 → 전략 라우팅 테이블 ────────────────────────────────────────────────────
# (strategy, trade_mode, direction_bias, risk_multiplier, leverage_cap)
_ROUTING_TABLE: Dict[DynamicRegime, Tuple[str, str, int, float, int]] = {
    DynamicRegime.STRONG_BULL: ("trend", "TREND_RIDE", +1, 1.00, 20),
    DynamicRegime.BULL:        ("trend", "SCALP",       +1, 0.90, 15),
    DynamicRegime.SIDEWAYS:    ("scalp", "PURE_SCALP",   0, 0.80, 10),
    DynamicRegime.BEAR:        ("trend", "SCALP",       -1, 0.90, 15),
    DynamicRegime.STRONG_BEAR: ("trend", "TREND_RIDE",  -1, 1.00, 20),
    DynamicRegime.VOLATILE:    ("scalp", "PURE_SCALP",   0, 0.50,  8),
    DynamicRegime.TRANSITION:  ("skip",  "",              0, 0.00,  0),
}


@dataclass
class _SymbolState:
    """심볼별 레짐 상태 캐시."""
    confirmed_regime: DynamicRegime = DynamicRegime.SIDEWAYS
    candidate_regime: Optional[DynamicRegime] = None
    candidate_count: int = 0


class RegimeRouter:
    """
    Stateful 심볼별 레짐 감지 + 전략 라우터.

    - 15m MTF bars를 60m으로 리샘플링해 EMA21/50/200 + ADX + ATR 백분위 계산
    - 레짐 분류 → 확정 로직 적용 → RouterResult 반환
    - macro_bear=True 시 BULL→SIDEWAYS, STRONG_BULL→BEAR 강제 변환
    """

    def __init__(self, settings: Settings):
        self._s = settings
        self._state: Dict[str, _SymbolState] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def detect(
        self,
        symbol: str,
        mtf_df: Optional[pd.DataFrame],
        macro_bear: bool = False,
    ) -> RouterResult:
        """
        장세를 감지하고 라우팅 결정을 반환한다.

        Args:
            symbol:     거래 심볼 (예: "BTCUSDT") — 상태 관리용
            mtf_df:     15m OHLCV DataFrame (columns: open, high, low, close, volume).
                        EMA200 안정화에 최소 210행 필요.
            macro_bear: True = macro regime이 BEAR (BTC daily 기준).
                        BULL 분류를 억제하고 SHORT 방향으로 바이어스.

        Returns:
            RouterResult — strategy="skip" 이면 해당 사이클 진입 없음.
        """
        if symbol not in self._state:
            self._state[symbol] = _SymbolState()
        state = self._state[symbol]

        # 데이터 부족 처리
        if mtf_df is None or len(mtf_df) < 210:
            log.warning(
                "RegimeRouter %s: 데이터 부족 (%d행) — SIDEWAYS fallback",
                symbol,
                0 if mtf_df is None else len(mtf_df),
            )
            return self._build_result(DynamicRegime.SIDEWAYS, confidence=0.3,
                                      reason="insufficient_data")

        # 15m → 60m 리샘플링
        df_60 = self._resample_to_60m(mtf_df)
        if len(df_60) < 55:
            # 리샘플 후 데이터 부족 → 원본 15m 사용
            df_60 = mtf_df

        # 지표 계산 및 장세 분류
        raw_regime, confidence, reason = self._classify(df_60)

        # macro_bear 오버라이드
        if macro_bear:
            if raw_regime == DynamicRegime.STRONG_BULL:
                raw_regime = DynamicRegime.BEAR
                reason += "+macro_bear"
            elif raw_regime == DynamicRegime.BULL:
                raw_regime = DynamicRegime.SIDEWAYS
                reason += "+macro_bear"

        # 확정 로직 (디바운스)
        confirmed = self._update_confirmation(state, raw_regime)

        log.debug(
            "RegimeRouter %s raw=%s confirmed=%s (confidence=%.2f) reason=%s",
            symbol, raw_regime.value, confirmed.value, confidence, reason,
        )

        return self._build_result(confirmed, confidence, reason)

    def get_confirmed_regime(self, symbol: str) -> DynamicRegime:
        """현재 확정된 레짐 조회 (Slack 알림 등 외부 참조용)."""
        return self._state.get(symbol, _SymbolState()).confirmed_regime

    # ── Private helpers ───────────────────────────────────────────────────────

    def _resample_to_60m(self, df: pd.DataFrame) -> pd.DataFrame:
        """15m OHLCV를 60m으로 리샘플링. DatetimeIndex 없는 경우 합성 인덱스 사용."""
        df = df.copy()

        # DatetimeIndex 없으면 합성 생성
        if not isinstance(df.index, pd.DatetimeIndex):
            if "ts" in df.columns:
                df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
            else:
                df.index = pd.date_range(
                    end=pd.Timestamp.now(tz="UTC"),
                    periods=len(df),
                    freq="15min",
                )

        # 숫자 컬럼만 추출
        cols = ["open", "high", "low", "close", "volume"]
        ohlcv = df[[c for c in cols if c in df.columns]].copy()
        for col in ohlcv.columns:
            ohlcv[col] = pd.to_numeric(ohlcv[col], errors="coerce")

        agg = {}
        if "open"   in ohlcv: agg["open"]   = "first"
        if "high"   in ohlcv: agg["high"]   = "max"
        if "low"    in ohlcv: agg["low"]    = "min"
        if "close"  in ohlcv: agg["close"]  = "last"
        if "volume" in ohlcv: agg["volume"] = "sum"

        return ohlcv.resample("60min").agg(agg).dropna(subset=["close"])

    def _classify(
        self, df: pd.DataFrame
    ) -> Tuple[DynamicRegime, float, str]:
        """
        60m 봉에서 EMA/ADX/ATR 기반 장세 분류.
        Returns (regime, confidence, reason)
        """
        close = df["close"]
        high  = df["high"]  if "high"  in df.columns else close
        low   = df["low"]   if "low"   in df.columns else close

        # EMA
        ema21_s  = ema(close, 21)
        ema50_s  = ema(close, 50)
        ema200_s = ema(close, 200)

        c21  = float(ema21_s.iloc[-1])
        c50  = float(ema50_s.iloc[-1])
        c200 = float(ema200_s.iloc[-1])

        # ADX
        try:
            adx_df  = calc_adx(high, low, close, 14)
            adx_val = float(adx_df["adx"].iloc[-1])
        except Exception:
            adx_val = 20.0

        # ATR 백분위 (변동성 레짐 판단)
        try:
            vol_pct_s = volatility_percentile(close, short=20, long=100)
            vol_arr   = vol_pct_s.dropna()
            vol_pct   = float(vol_arr.iloc[-1]) if len(vol_arr) > 0 else 0.5
        except Exception:
            vol_pct = 0.5

        price = float(close.iloc[-1])

        adx_strong   = self._s.REGIME_ROUTER_ADX_STRONG
        adx_moderate = self._s.REGIME_ROUTER_ADX_MODERATE
        atr_volatile = self._s.REGIME_ROUTER_ATR_VOLATILE_PCT

        # 1) 변동성 우선 체크
        if vol_pct > atr_volatile:
            return DynamicRegime.VOLATILE, min(1.0, vol_pct), \
                   f"vol_pct={vol_pct:.2f}>threshold={atr_volatile}"

        # EMA 정렬 여부
        ema_bull = (c21 > c50 > c200)
        ema_bear = (c21 < c50 < c200)

        # EMA 갭 → confidence 계산
        ema_gap = abs(c21 - c50) / (price + 1e-9)
        confidence = min(1.0, 0.5 + ema_gap * 100)

        # 2) 상승 정렬
        if ema_bull:
            if adx_val > adx_strong:
                return DynamicRegime.STRONG_BULL, confidence, \
                       f"EMA↑ ADX={adx_val:.1f}>{adx_strong}"
            elif adx_val >= adx_moderate:
                return DynamicRegime.BULL, confidence * 0.85, \
                       f"EMA↑ ADX={adx_val:.1f}>={adx_moderate}"
            else:
                # EMA 정렬됐지만 모멘텀 없음 → SIDEWAYS
                return DynamicRegime.SIDEWAYS, 0.6, \
                       f"EMA↑ no_momentum ADX={adx_val:.1f}<{adx_moderate}"

        # 3) 하락 정렬
        if ema_bear:
            if adx_val > adx_strong:
                return DynamicRegime.STRONG_BEAR, confidence, \
                       f"EMA↓ ADX={adx_val:.1f}>{adx_strong}"
            elif adx_val >= adx_moderate:
                return DynamicRegime.BEAR, confidence * 0.85, \
                       f"EMA↓ ADX={adx_val:.1f}>={adx_moderate}"
            else:
                return DynamicRegime.SIDEWAYS, 0.6, \
                       f"EMA↓ no_momentum ADX={adx_val:.1f}<{adx_moderate}"

        # 4) 혼합 EMA → SIDEWAYS
        return DynamicRegime.SIDEWAYS, 0.5, f"EMA_mixed ADX={adx_val:.1f}"

    def _update_confirmation(
        self,
        state: _SymbolState,
        raw: DynamicRegime,
    ) -> DynamicRegime:
        """
        레짐 디바운스 로직.

        같은 그룹 내 전환 (BULL↔STRONG_BULL 등): 즉시 확정.
        다른 그룹 전환: CONFIRM_BARS 연속 일치 후 확정.
        확정 전: TRANSITION 반환.
        """
        confirmed = state.confirmed_regime
        confirm_bars = self._s.REGIME_ROUTER_CONFIRM_BARS

        # 같은 그룹 → 즉시 확정
        if _regime_group(raw) == _regime_group(confirmed):
            state.confirmed_regime = raw
            state.candidate_regime = None
            state.candidate_count  = 0
            return raw

        # 다른 그룹 → 후보 카운터 관리
        if raw == state.candidate_regime:
            state.candidate_count += 1
        else:
            state.candidate_regime = raw
            state.candidate_count  = 1

        if state.candidate_count >= confirm_bars:
            # 확정 전환
            state.confirmed_regime = raw
            state.candidate_regime = None
            state.candidate_count  = 0
            return raw
        else:
            return DynamicRegime.TRANSITION

    def _build_result(
        self,
        regime: DynamicRegime,
        confidence: float,
        reason: str,
    ) -> RouterResult:
        strategy, trade_mode, direction_bias, risk_mult, lev_cap = _ROUTING_TABLE[regime]
        return RouterResult(
            regime=regime,
            strategy=strategy,
            trade_mode=trade_mode,
            direction_bias=direction_bias,
            risk_multiplier=risk_mult,
            leverage_cap=lev_cap,
            confidence=confidence,
            reason=reason,
        )
