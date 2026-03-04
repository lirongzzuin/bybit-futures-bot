"""
MarketData collector — fetches and packages OHLCV, orderbook,
funding rate and open interest into a single MarketSnapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from src.api.bybit_client import BybitClient
from src.config import Settings
from src.logger import get_logger
from src.strategy.indicators import vwap as compute_vwap_series

log = get_logger(__name__)

_INTERVAL_MAP = {
    "1": "1",
    "3": "3",
    "5": "5",
    "15": "15",
    "30": "30",
    "60": "60",
    "120": "120",
    "240": "240",
    "720": "720",
    "1440": "D",   # 일봉 (1440분 = 24시간 = 1 day)
    "D": "D",
    "W": "W",
}


@dataclass
class OrderBookSnapshot:
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    spread_pct: float = 0.0
    bid_depth_top5: float = 0.0    # USDT value of top-5 bids
    ask_depth_top5: float = 0.0    # USDT value of top-5 asks
    imbalance: float = 0.0         # (bid - ask) / (bid + ask)


@dataclass
class MarketSnapshot:
    symbol: str
    ohlcv: pd.DataFrame = field(default_factory=pd.DataFrame)
    # Derived columns added by collector: atr, ema_fast, ema_mid, ema_slow
    trend_ohlcv: Optional[pd.DataFrame] = None   # higher TF bars for regime detection
    orderbook: OrderBookSnapshot = field(default_factory=OrderBookSnapshot)
    funding_rate: float = 0.0       # latest funding rate (e.g. 0.0001)
    next_funding_ts: int = 0
    open_interest: float = 0.0      # in USDT
    oi_prev: float = 0.0
    oi_change_pct: float = 0.0
    last_price: float = 0.0
    vwap: float = 0.0               # latest VWAP value
    vwap_distance_pct: float = 0.0  # (price - vwap) / vwap


class DataCollector:
    def __init__(self, client: BybitClient, settings: Settings):
        self._client = client
        self._settings = settings

    # ── Primary public method ────────────────────────────────────────────────

    def collect(self, symbol: str, limit: int = 250) -> MarketSnapshot:
        snap = MarketSnapshot(symbol=symbol)

        try:
            snap.ohlcv = self._fetch_ohlcv(symbol, limit)
        except Exception as e:
            log.error("OHLCV fetch failed for %s: %s", symbol, e)
            return snap

        try:
            snap.orderbook = self._fetch_orderbook(symbol)
        except Exception as e:
            log.warning("Orderbook fetch failed for %s: %s", symbol, e)

        try:
            funding = self._client.get_funding_rate_history(symbol, limit=5)
            if funding:
                snap.funding_rate = float(funding[0].get("fundingRate", 0))
                snap.next_funding_ts = int(funding[0].get("fundingRateTimestamp", 0))
        except Exception as e:
            log.warning("Funding fetch failed for %s: %s", symbol, e)

        try:
            oi_records = self._client.get_open_interest(
                symbol,
                interval_time=_oi_interval(self._settings.SIGNAL_TIMEFRAME),
                limit=5,
            )
            if len(oi_records) >= 2:
                snap.open_interest = float(oi_records[0].get("openInterest", 0))
                snap.oi_prev = float(oi_records[1].get("openInterest", 0))
                if snap.oi_prev > 0:
                    snap.oi_change_pct = (snap.open_interest - snap.oi_prev) / snap.oi_prev
        except Exception as e:
            log.warning("OI fetch failed for %s: %s", symbol, e)

        if not snap.ohlcv.empty:
            snap.last_price = float(snap.ohlcv["close"].iloc[-1])

        # ── Trend OHLCV (higher timeframe) ───────────────────────────────────
        try:
            snap.trend_ohlcv = self._fetch_trend_ohlcv(symbol)
        except Exception as e:
            log.warning("Trend OHLCV fetch failed for %s: %s", symbol, e)

        # ── VWAP ─────────────────────────────────────────────────────────────
        if not snap.ohlcv.empty:
            snap.vwap = self._compute_vwap(snap.ohlcv, self._settings.VWAP_SESSION_BARS)
            if snap.vwap > 0 and snap.last_price > 0:
                snap.vwap_distance_pct = (snap.last_price - snap.vwap) / snap.vwap

        return snap

    def collect_for_scalp(self, symbol: str, limit: int = 500) -> MarketSnapshot:
        """
        순수 스캘핑 전략용 데이터 수집.

        STRATEGY_MODE="scalp" 일 때 호출:
          - snap.ohlcv      = LTF (3m) 봉 — ScalpingStrategy 진입 신호용
          - snap.trend_ohlcv = HTF (15m) 봉 — 추세 바이어스 필터용

        일반 collect()와 동일한 MarketSnapshot 반환,
        단 타임프레임이 설정의 SCALP_PURE_LTF_TIMEFRAME / SCALP_PURE_HTF_TIMEFRAME 사용.
        """
        snap = MarketSnapshot(symbol=symbol)

        # LTF OHLCV (3m 기본)
        try:
            ltf_interval = _INTERVAL_MAP.get(self._settings.SCALP_PURE_LTF_TIMEFRAME, "3")
            raw = self._client.get_kline(symbol, interval=ltf_interval, limit=limit)
            if raw:
                df = pd.DataFrame(
                    raw,
                    columns=["ts", "open", "high", "low", "close", "volume", "turnover"],
                )
                df = df.astype({
                    "ts": "int64", "open": "float64", "high": "float64",
                    "low": "float64", "close": "float64", "volume": "float64",
                    "turnover": "float64",
                })
                df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                df.sort_values("ts", inplace=True)
                df.reset_index(drop=True, inplace=True)
                snap.ohlcv = df
        except Exception as e:
            log.error("Scalp LTF OHLCV fetch failed for %s: %s", symbol, e)
            return snap

        # HTF OHLCV (15m 기본) — 추세 바이어스
        try:
            htf_interval = _INTERVAL_MAP.get(self._settings.SCALP_PURE_HTF_TIMEFRAME, "15")
            raw_htf = self._client.get_kline(symbol, interval=htf_interval, limit=300)
            if raw_htf:
                df_htf = pd.DataFrame(
                    raw_htf,
                    columns=["ts", "open", "high", "low", "close", "volume", "turnover"],
                )
                df_htf = df_htf.astype({
                    "ts": "int64", "open": "float64", "high": "float64",
                    "low": "float64", "close": "float64", "volume": "float64",
                    "turnover": "float64",
                })
                df_htf["datetime"] = pd.to_datetime(df_htf["ts"], unit="ms", utc=True)
                df_htf.sort_values("ts", inplace=True)
                df_htf.reset_index(drop=True, inplace=True)
                snap.trend_ohlcv = df_htf
        except Exception as e:
            log.warning("Scalp HTF OHLCV fetch failed for %s: %s", symbol, e)

        # 오더북
        try:
            snap.orderbook = self._fetch_orderbook(symbol)
        except Exception as e:
            log.warning("Orderbook fetch failed for %s: %s", symbol, e)

        # 펀딩 비율
        try:
            funding = self._client.get_funding_rate_history(symbol, limit=5)
            if funding:
                snap.funding_rate = float(funding[0].get("fundingRate", 0))
                snap.next_funding_ts = int(funding[0].get("fundingRateTimestamp", 0))
        except Exception as e:
            log.warning("Funding fetch failed for %s: %s", symbol, e)

        # OI (LTF 타임프레임 기준)
        try:
            oi_records = self._client.get_open_interest(
                symbol,
                interval_time=_oi_interval(self._settings.SCALP_PURE_LTF_TIMEFRAME),
                limit=5,
            )
            if len(oi_records) >= 2:
                snap.open_interest = float(oi_records[0].get("openInterest", 0))
                snap.oi_prev = float(oi_records[1].get("openInterest", 0))
                if snap.oi_prev > 0:
                    snap.oi_change_pct = (snap.open_interest - snap.oi_prev) / snap.oi_prev
        except Exception as e:
            log.warning("OI fetch failed for %s: %s", symbol, e)

        if not snap.ohlcv.empty:
            snap.last_price = float(snap.ohlcv["close"].iloc[-1])

        # VWAP (LTF 데이터 기준)
        if not snap.ohlcv.empty:
            snap.vwap = self._compute_vwap(snap.ohlcv, self._settings.VWAP_SESSION_BARS)
            if snap.vwap > 0 and snap.last_price > 0:
                snap.vwap_distance_pct = (snap.last_price - snap.vwap) / snap.vwap

        return snap

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _fetch_ohlcv(self, symbol: str, limit: int) -> pd.DataFrame:
        interval = _INTERVAL_MAP.get(self._settings.SIGNAL_TIMEFRAME, "15")
        raw = self._client.get_kline(symbol, interval=interval, limit=limit)
        if not raw:
            return pd.DataFrame()

        df = pd.DataFrame(
            raw,
            columns=["ts", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.astype(
            {
                "ts": "int64",
                "open": "float64",
                "high": "float64",
                "low": "float64",
                "close": "float64",
                "volume": "float64",
                "turnover": "float64",
            }
        )
        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def _fetch_trend_ohlcv(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        """Fetch higher-timeframe (TREND_TIMEFRAME) OHLCV bars."""
        interval = _INTERVAL_MAP.get(self._settings.TREND_TIMEFRAME, "60")
        raw = self._client.get_kline(symbol, interval=interval, limit=limit)
        if not raw:
            return pd.DataFrame()

        df = pd.DataFrame(
            raw,
            columns=["ts", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.astype(
            {
                "ts": "int64",
                "open": "float64",
                "high": "float64",
                "low": "float64",
                "close": "float64",
                "volume": "float64",
                "turnover": "float64",
            }
        )
        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def _compute_vwap(self, ohlcv: pd.DataFrame, period: int) -> float:
        """Compute rolling VWAP and return the last bar's value."""
        if ohlcv.empty or len(ohlcv) < period:
            return 0.0
        vwap_series = compute_vwap_series(
            ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"], period
        )
        val = vwap_series.iloc[-1]
        return float(val) if not (val != val) else 0.0  # NaN check

    def _fetch_orderbook(self, symbol: str) -> OrderBookSnapshot:
        book = self._client.get_orderbook(symbol, limit=25)
        bids = book.get("b", [])
        asks = book.get("a", [])

        if not bids or not asks:
            return OrderBookSnapshot()

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2
        spread_pct = spread / mid if mid > 0 else 0

        bid_depth = sum(float(b[0]) * float(b[1]) for b in bids[:5])
        ask_depth = sum(float(a[0]) * float(a[1]) for a in asks[:5])
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total > 0 else 0

        return OrderBookSnapshot(
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_pct=spread_pct,
            bid_depth_top5=bid_depth,
            ask_depth_top5=ask_depth,
            imbalance=imbalance,
        )


def _oi_interval(timeframe: str) -> str:
    """Map OHLCV interval to OI interval string."""
    mapping = {
        "1": "5min",
        "3": "5min",
        "5": "5min",
        "15": "15min",
        "30": "30min",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "D": "1d",
    }
    return mapping.get(timeframe, "15min")
