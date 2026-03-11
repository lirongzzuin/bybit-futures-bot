"""
Universe manager — maintains the top-N USDT perpetual contracts
by 24-hour turnover (USDT volume), refreshed periodically.

Excluded: stablecoins, leveraged tokens, BTC/USDT and ETH/USDT
(usually dominate but may be included; configurable).
"""
from __future__ import annotations

import time
from typing import List, Optional

from src.api.bybit_client import BybitClient
from src.config import Settings
from src.logger import get_logger

log = get_logger(__name__)

# Tokens to skip regardless of volume rank
_EXCLUDE_PATTERNS = ["USDC", "USDT", "BUSD", "DAI", "TUSD", "USDP",
                     "3L", "3S", "5L", "5S", "10L", "10S"]  # leveraged tokens


class UniverseManager:
    def __init__(self, client: BybitClient, settings: Settings):
        self._client = client
        self._settings = settings
        self._symbols: List[str] = []
        self._last_refresh: float = 0.0

    @property
    def symbols(self) -> List[str]:
        return list(self._symbols)

    def needs_refresh(self) -> bool:
        elapsed_h = (time.time() - self._last_refresh) / 3600
        return elapsed_h >= self._settings.UNIVERSE_REFRESH_INTERVAL_HOURS

    def refresh(self) -> List[str]:
        """Fetch latest tickers and pick top-N by 24h USDT turnover."""
        try:
            tickers = self._client.get_tickers()
        except Exception as exc:
            log.error("Universe refresh failed: %s", exc)
            return self._symbols  # keep old universe

        candidates = []
        for t in tickers:
            symbol: str = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            # Check base currency only (strip "USDT" suffix) to avoid
            # matching "USDT" inside every USDT-paired symbol (e.g. BTCUSDT)
            base = symbol[:-4]
            if any(pat in base for pat in _EXCLUDE_PATTERNS):
                continue
            # Only linear perpetuals (no expiry)
            turnover = float(t.get("turnover24h", 0) or 0)
            volume = float(t.get("volume24h", 0) or 0)
            min_turnover = self._settings.MIN_24H_TURNOVER_USDT
            if turnover < min_turnover:
                continue
            candidates.append((symbol, turnover))

        candidates.sort(key=lambda x: x[1], reverse=True)
        top_n = [sym for sym, _ in candidates[: self._settings.TOP_N_COINS]]

        if top_n != self._symbols:
            log.info("Universe updated: %s", top_n)
        else:
            log.debug("Universe unchanged: %s", top_n)

        self._symbols = top_n
        self._last_refresh = time.time()
        return top_n

    def get_or_refresh(self) -> List[str]:
        if self.needs_refresh() or not self._symbols:
            return self.refresh()
        return self._symbols
