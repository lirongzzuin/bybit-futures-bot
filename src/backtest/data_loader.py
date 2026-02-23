"""
Historical data loader for backtesting.

Downloads up to 5 years of OHLCV data from Bybit (max 1000 bars per call)
and caches locally as CSV.

Bybit limits:
  - Max 1000 bars per kline call
  - 15-min bars: 5 years = ~175,000 bars → ~175 API calls per symbol
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
from pybit.unified_trading import HTTP

from src.logger import get_logger

log = get_logger(__name__)

_CACHE_DIR = Path("data_cache")
_CACHE_DIR.mkdir(exist_ok=True)


def load_ohlcv(
    symbol: str,
    interval: str = "15",
    years: int = 5,
    api_key: str = "",
    api_secret: str = "",
    testnet: bool = False,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Load OHLCV for `symbol` covering the last `years` years.
    Uses cache if available and not older than 1 hour.
    """
    cache_file = _CACHE_DIR / f"{symbol}_{interval}.csv"

    # Calculate requested start timestamp
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    requested_start_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=365 * years)).timestamp() * 1000
    )

    # Check cache
    if not force_refresh and cache_file.exists():
        age_h = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_h < 1.0:
            log.info("Loading %s from cache (%s)", symbol, cache_file)
            df = pd.read_csv(cache_file, parse_dates=["datetime"])
            df = df.sort_values("ts").reset_index(drop=True)
            # Filter to requested time window (years parameter)
            df = df[df["ts"] >= requested_start_ms].reset_index(drop=True)
            if not df.empty:
                log.info("Cache filtered to %d bars (last %d years)", len(df), years)
                return df

    log.info("Downloading %s history (interval=%s, years=%d)...", symbol, interval, years)

    session = HTTP(api_key=api_key, api_secret=api_secret, testnet=testnet)

    start_ms = requested_start_ms

    interval_ms = _interval_to_ms(interval)
    all_bars = []
    current_end = end_ms

    while current_end > start_ms:
        try:
            resp = session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=1000,
                end=current_end,
            )
            bars = resp.get("result", {}).get("list", [])
            if not bars:
                break

            all_bars.extend(bars)
            oldest_ts = int(bars[-1][0])
            current_end = oldest_ts - interval_ms

            if oldest_ts <= start_ms:
                break

            time.sleep(0.05)  # rate limit friendly

        except Exception as e:
            log.warning("Download error for %s at %d: %s", symbol, current_end, e)
            time.sleep(2)
            continue

    if not all_bars:
        log.error("No data downloaded for %s", symbol)
        return pd.DataFrame()

    df = _bars_to_df(all_bars)
    df = df[df["ts"] >= start_ms]
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df.drop_duplicates(subset=["ts"], inplace=True)

    # Save cache
    df.to_csv(cache_file, index=False)
    log.info(
        "Downloaded %d bars for %s | %s → %s",
        len(df),
        symbol,
        df["datetime"].iloc[0].date() if not df.empty else "?",
        df["datetime"].iloc[-1].date() if not df.empty else "?",
    )
    return df


def load_multi(
    symbols: list,
    interval: str = "15",
    years: int = 5,
    api_key: str = "",
    api_secret: str = "",
    testnet: bool = False,
) -> Dict[str, pd.DataFrame]:
    """Load OHLCV for multiple symbols."""
    result = {}
    for sym in symbols:
        df = load_ohlcv(sym, interval, years, api_key, api_secret, testnet)
        if not df.empty:
            result[sym] = df
        else:
            log.warning("No data for %s — excluded from backtest", sym)
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bars_to_df(raw_bars: list) -> pd.DataFrame:
    """Convert raw Bybit kline list to DataFrame."""
    df = pd.DataFrame(
        raw_bars,
        columns=["ts", "open", "high", "low", "close", "volume", "turnover"],
    )
    df = df.astype({
        "ts": "int64",
        "open": "float64",
        "high": "float64",
        "low": "float64",
        "close": "float64",
        "volume": "float64",
        "turnover": "float64",
    })
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def _interval_to_ms(interval: str) -> int:
    """Convert Bybit interval string to milliseconds."""
    mapping = {
        "1": 60_000,
        "3": 180_000,
        "5": 300_000,
        "15": 900_000,
        "30": 1_800_000,
        "60": 3_600_000,
        "120": 7_200_000,
        "240": 14_400_000,
        "D": 86_400_000,
        "W": 604_800_000,
    }
    return mapping.get(interval, 900_000)
