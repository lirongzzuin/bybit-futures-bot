"""
Bybit v5 API client.
Wraps pybit HTTP session with:
 - Automatic rate-limit retry (exponential back-off, max 10 retries)
 - Circuit breaker (fail-fast after 5 consecutive errors, 60 s cool-down)
 - All required market-data + trading endpoints
"""
from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pybit.unified_trading import HTTP
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from src.config import Settings
from src.logger import get_logger

log = get_logger(__name__)

# ── Exceptions ────────────────────────────────────────────────────────────────

class BybitAPIError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code

# Error codes that are expected / benign — should NOT trip the circuit breaker
_BENIGN_ERROR_CODES = frozenset({
    110001,  # Order already filled or expired (cancel on stale order)
    110043,  # Leverage already set to requested value
    110025,  # Position mode already set
    110021,  # Position mode already set (alt code)
    110072,  # Duplicate orderLinkId — order was already placed (idempotency signal)
})


def _parse_api_error_code(exc: Exception) -> Optional[int]:
    """Extract Bybit API error code from a pybit library exception string.

    pybit sometimes raises its own exceptions instead of returning a retCode
    dict, e.g. InvalidRequestError containing '(ErrCode: 110001)'.  We parse
    the code so we can apply the same benign/circuit-breaker logic.
    """
    m = re.search(r"ErrCode:\s*(\d+)", str(exc))
    return int(m.group(1)) if m else None

class CircuitOpenError(Exception):
    """Raised when circuit breaker is OPEN."""


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.time() - self._opened_at > self._recovery_timeout:
                # half-open: allow one attempt
                self._opened_at = None
                self._failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = time.time()
                log.error(
                    "Circuit breaker OPENED after %d failures", self._failures
                )


# ── Bybit Client ──────────────────────────────────────────────────────────────

class BybitClient:
    """Thin wrapper around pybit HTTP with retry and circuit breaker."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._circuit = CircuitBreaker()
        self._session = HTTP(
            api_key=settings.BYBIT_API_KEY,
            api_secret=settings.BYBIT_API_SECRET,
            testnet=settings.BYBIT_TESTNET,
            recv_window=settings.BYBIT_RECV_WINDOW,
        )
        log.info(
            "BybitClient initialised | testnet=%s mode=%s",
            settings.BYBIT_TESTNET,
            settings.TRADING_MODE,
        )

    # ── Internal call wrapper ─────────────────────────────────────────────

    def _call(self, fn_name: str, **kwargs: Any) -> Dict:
        if self._circuit.is_open:
            raise CircuitOpenError("Circuit breaker is OPEN — skipping API call")

        fn = getattr(self._session, fn_name)

        for attempt in range(1, 11):  # max 10 retries
            try:
                resp = fn(**kwargs)
                if resp.get("retCode", -1) != 0:
                    code = resp.get("retCode", -1)
                    msg = resp.get("retMsg", "unknown")
                    # 10006 = rate limit exceeded → back off
                    if code == 10006:
                        wait = min(2 ** attempt, 60)
                        log.warning("Rate limit hit, waiting %ss (attempt %d)", wait, attempt)
                        time.sleep(wait)
                        continue
                    # Benign codes don't count as real failures
                    if code not in _BENIGN_ERROR_CODES:
                        self._circuit.record_failure()
                    raise BybitAPIError(code, msg)
                self._circuit.record_success()
                return resp.get("result", {})
            except BybitAPIError:
                raise
            except CircuitOpenError:
                raise
            except Exception as exc:
                # pybit library may raise its own exception types instead of
                # returning a retCode dict.  Parse the embedded error code so
                # we can apply the same benign / circuit-breaker logic.
                api_code = _parse_api_error_code(exc)
                if api_code is not None:
                    if api_code not in _BENIGN_ERROR_CODES:
                        self._circuit.record_failure()
                    raise BybitAPIError(api_code, str(exc))
                # Pure network / connection error — retry with back-off
                self._circuit.record_failure()
                wait = min(2 ** attempt, 60)
                log.warning("API error (attempt %d/%d): %s — retry in %ss", attempt, 10, exc, wait)
                time.sleep(wait)

        raise BybitAPIError(-1, "Max retries exceeded")

    # ── Market Data ──────────────────────────────────────────────────────────

    def get_tickers(self) -> List[Dict]:
        """All USDT-linear ticker snapshots (24h stats)."""
        result = self._call("get_tickers", category="linear")
        return result.get("list", [])

    def get_kline(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[List]:
        """
        OHLCV candles.
        Returns raw list: [startTime, open, high, low, close, volume, turnover]
        """
        params: Dict[str, Any] = dict(
            category="linear", symbol=symbol, interval=interval, limit=limit
        )
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        result = self._call("get_kline", **params)
        return result.get("list", [])

    def get_orderbook(self, symbol: str, limit: int = 25) -> Dict:
        """Order book snapshot."""
        return self._call("get_orderbook", category="linear", symbol=symbol, limit=limit)

    def get_funding_rate_history(self, symbol: str, limit: int = 200) -> List[Dict]:
        """Historical funding rates."""
        result = self._call(
            "get_funding_rate_history",
            category="linear",
            symbol=symbol,
            limit=limit,
        )
        return result.get("list", [])

    def get_open_interest(
        self, symbol: str, interval_time: str = "15min", limit: int = 200
    ) -> List[Dict]:
        """Open interest history."""
        result = self._call(
            "get_open_interest",
            category="linear",
            symbol=symbol,
            intervalTime=interval_time,
            limit=limit,
        )
        return result.get("list", [])

    def get_instruments_info(self, symbol: str) -> Dict:
        """Symbol metadata including leverage limits and tick sizes."""
        result = self._call(
            "get_instruments_info", category="linear", symbol=symbol
        )
        lst = result.get("list", [])
        return lst[0] if lst else {}

    # ── Account & Position ───────────────────────────────────────────────────

    def get_wallet_balance(self) -> Dict:
        """Unified account wallet balance."""
        result = self._call("get_wallet_balance", accountType="UNIFIED")
        accounts = result.get("list", [])
        return accounts[0] if accounts else {}

    def get_usdt_equity(self) -> float:
        """Quick helper — total equity in USDT."""
        wallet = self.get_wallet_balance()
        coins = wallet.get("coin", [])
        for c in coins:
            if c.get("coin") == "USDT":
                return float(c.get("equity", 0))
        return 0.0

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """All open positions (or for a specific symbol)."""
        params: Dict[str, Any] = dict(category="linear", settleCoin="USDT")
        if symbol:
            params["symbol"] = symbol
        result = self._call("get_positions", **params)
        return result.get("list", [])

    def get_position(self, symbol: str, side: Optional[str] = None) -> Optional[Dict]:
        """
        Single symbol position, or None if flat.

        side: "Buy" (long) | "Sell" (short) — required in hedge mode to distinguish
              which position you want. None returns the first non-zero position.
        """
        positions = self.get_positions(symbol)
        for p in positions:
            if float(p.get("size", 0)) <= 0:
                continue
            if side is None:
                return p
            # In hedge mode Bybit returns side as "Buy" or "Sell"
            if p.get("side", "") == side:
                return p
        return None

    def get_positions_hedge(self, symbol: str) -> Dict[str, Optional[Dict]]:
        """
        Returns {"long": pos_or_none, "short": pos_or_none} for hedge mode.
        Useful for checking both sides at once.
        """
        positions = self.get_positions(symbol)
        result: Dict[str, Optional[Dict]] = {"long": None, "short": None}
        for p in positions:
            if float(p.get("size", 0)) <= 0:
                continue
            if p.get("side") == "Buy":
                result["long"] = p
            elif p.get("side") == "Sell":
                result["short"] = p
        return result

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set buy and sell leverage (one-way mode)."""
        lev_str = str(leverage)
        try:
            self._call(
                "set_leverage",
                category="linear",
                symbol=symbol,
                buyLeverage=lev_str,
                sellLeverage=lev_str,
            )
            log.info("Leverage set to %s× for %s", leverage, symbol)
        except BybitAPIError as e:
            if e.code == 110043:
                log.debug("Leverage already set to %s for %s", leverage, symbol)
            else:
                raise

    def set_position_mode(self, symbol: str, hedge_mode: bool = True) -> None:
        """
        Set position mode for a symbol.
        hedge_mode=True  → mode=3 (Both-Side / Hedge) — long and short simultaneously.
        hedge_mode=False → mode=0 (One-Way)
        """
        mode = 3 if hedge_mode else 0
        mode_name = "Hedge" if hedge_mode else "One-Way"
        try:
            self._call(
                "switch_position_mode",
                category="linear",
                symbol=symbol,
                mode=mode,
            )
            log.info("Position mode set to %s for %s", mode_name, symbol)
        except BybitAPIError as e:
            if e.code in (110025, 110021):
                log.debug("Position mode already %s for %s", mode_name, symbol)
            else:
                raise

    # ── Order Management ─────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: str,          # "Buy" | "Sell"
        qty: str,
        order_type: str = "Limit",  # "Limit" | "Market"
        price: Optional[str] = None,
        time_in_force: str = "PostOnly",
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        sl_trigger_by: str = "LastPrice",
        tp_trigger_by: str = "LastPrice",
        position_idx: int = 0,  # 0=One-Way, 1=Hedge Long, 2=Hedge Short
    ) -> Dict:
        """
        Place a linear perpetual order.

        position_idx:
          0 = One-Way mode (default)
          1 = Hedge mode, Long position (Buy to open long / Sell to close long)
          2 = Hedge mode, Short position (Sell to open short / Buy to close short)

        Returns full order object.
        """
        params: Dict[str, Any] = dict(
            category="linear",
            symbol=symbol,
            side=side,
            orderType=order_type,
            qty=qty,
            reduceOnly=reduce_only,
            positionIdx=position_idx,
        )
        if order_type == "Limit" and price:
            params["price"] = price
            params["timeInForce"] = time_in_force
        else:
            params["timeInForce"] = "IOC"  # market orders
        if client_order_id:
            params["orderLinkId"] = client_order_id
        if stop_loss:
            params["stopLoss"] = stop_loss
            params["slTriggerBy"] = sl_trigger_by
        if take_profit:
            params["takeProfit"] = take_profit
            params["tpTriggerBy"] = tp_trigger_by

        result = self._call("place_order", **params)
        log.info(
            "Order placed | %s %s %s qty=%s price=%s posIdx=%d orderId=%s",
            symbol, side, order_type, qty, price, position_idx, result.get("orderId"),
        )
        return result

    def cancel_order(self, symbol: str, order_id: str) -> Dict:
        try:
            return self._call(
                "cancel_order", category="linear", symbol=symbol, orderId=order_id
            )
        except BybitAPIError as e:
            if e.code == 110001:
                # Order already filled or expired — not a real error
                log.debug("cancel_order %s: order %s already filled/expired", symbol, order_id)
                return {}
            raise

    def cancel_all_orders(self, symbol: str, side: Optional[str] = None) -> int:
        """
        심볼의 모든 미체결 지정가 주문을 취소한다.
        side: "Buy" | "Sell" | None (both sides)
        Returns: 취소된 주문 수
        """
        try:
            open_orders = self.get_open_orders(symbol)
        except Exception as e:
            log.warning("cancel_all_orders: get_open_orders failed for %s: %s", symbol, e)
            return 0

        cancelled = 0
        for order in open_orders:
            if side and order.get("side") != side:
                continue
            order_id = order.get("orderId")
            if not order_id:
                continue
            try:
                self.cancel_order(symbol, order_id)
                cancelled += 1
                log.debug("Cancelled stale order %s for %s", order_id, symbol)
            except Exception as e:
                log.warning("Failed to cancel order %s for %s: %s", order_id, symbol, e)
        if cancelled:
            log.info("Cancelled %d stale order(s) for %s (side=%s)", cancelled, symbol, side or "both")
        return cancelled

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        params: Dict[str, Any] = dict(category="linear")
        if symbol:
            params["symbol"] = symbol
        result = self._call("get_open_orders", **params)
        return result.get("list", [])

    def get_order(self, symbol: str, order_id: str) -> Optional[Dict]:
        result = self._call(
            "get_order_history",
            category="linear",
            symbol=symbol,
            orderId=order_id,
            limit=1,
        )
        lst = result.get("list", [])
        return lst[0] if lst else None

    def get_order_by_link_id(self, symbol: str, order_link_id: str) -> Optional[Dict]:
        """Look up an order by its client orderLinkId.

        Checks open orders first (fast path), then order history.
        Returns the order dict or None if not found.
        """
        # 1. Check open orders
        try:
            result = self._call(
                "get_open_orders",
                category="linear",
                symbol=symbol,
                orderLinkId=order_link_id,
            )
            lst = result.get("list", [])
            if lst:
                return lst[0]
        except Exception:
            pass
        # 2. Check recent history (may already be filled)
        try:
            result = self._call(
                "get_order_history",
                category="linear",
                symbol=symbol,
                orderLinkId=order_link_id,
                limit=1,
            )
            lst = result.get("list", [])
            if lst:
                return lst[0]
        except Exception:
            pass
        return None

    # ── Asset Transfers ───────────────────────────────────────────────────────

    def transfer_to_funding(self, amount: float, coin: str = "USDT") -> Dict:
        """
        Transfer funds from Unified Trading Account → Funding Account.
        Used by ProfitProtector to lock in profits safely.
        """
        import uuid
        transfer_id = str(uuid.uuid4())
        result = self._call(
            "create_internal_transfer",
            transferId=transfer_id,
            coin=coin,
            amount=str(round(amount, 2)),
            fromAccountType="UNIFIED",
            toAccountType="FUND",
        )
        log.info("Transferred %.2f %s to Funding Account | txId=%s", amount, coin, result.get("transferId"))
        return result

    def get_funding_account_balance(self, coin: str = "USDT") -> float:
        """Get USDT balance in Funding Account (safe storage)."""
        try:
            result = self._call("get_wallet_balance", accountType="FUND")
            accounts = result.get("list", [])
            if not accounts:
                return 0.0
            for c in accounts[0].get("coin", []):
                if c.get("coin") == coin:
                    return float(c.get("walletBalance", 0))
        except Exception as e:
            log.warning("Failed to get funding balance: %s", e)
        return 0.0

    def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        position_idx: int = 0,
        sl_trigger_by: str = "LastPrice",
        tp_trigger_by: str = "LastPrice",
        sl_order_type: str = "Market",
        tp_order_type: str = "Market",   # tpSlMode=Full 시 반드시 Market (Bybit 10001 오류 방지)
    ) -> Dict:
        """
        Set SL/TP on an open position via Bybit trading-stop API.
        Called after fill to ensure exchange-side protection even if bot crashes.
        position_idx: 0=One-Way, 1=Hedge Long, 2=Hedge Short

        Order type policy:
          SL → Market: 손절은 즉각 실행이 최우선 (Taker fee 감수)
          TP → Market: tpSlMode=Full 시 Bybit API 요구사항 (Limit 불가 → 10001 오류)
                       TP1 부분청산은 봇의 5분 사이클에서 처리
                       거래소 TP(Market)는 봇 크래시 시 최후 안전망
        """
        params: Dict[str, Any] = dict(
            category="linear",
            symbol=symbol,
            tpslMode="Full",
            positionIdx=position_idx,
        )
        if stop_loss:
            params["stopLoss"] = stop_loss
            params["slTriggerBy"] = sl_trigger_by
            params["slOrderType"] = sl_order_type
        if take_profit:
            params["takeProfit"] = take_profit
            params["tpTriggerBy"] = tp_trigger_by
            params["tpOrderType"] = tp_order_type   # "Market" — tpSlMode=Full 요구사항
        result = self._call("set_trading_stop", **params)
        log.info(
            "TradingStop set | %s posIdx=%d sl=%s(Market) tp=%s(Market)",
            symbol, position_idx, stop_loss, take_profit,
        )
        return result

    def amend_order(
        self, symbol: str, order_id: str, price: str, qty: Optional[str] = None
    ) -> Dict:
        params: Dict[str, Any] = dict(
            category="linear", symbol=symbol, orderId=order_id, price=price
        )
        if qty:
            params["qty"] = qty
        return self._call("amend_order", **params)

    # ── Execution Helpers ────────────────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> Dict:
        """
        Returns convenient symbol constraints dict:
          min_qty, qty_step, price_step, min_notional, max_leverage
        """
        info = self.get_instruments_info(symbol)
        if not info:
            return {}
        lot = info.get("lotSizeFilter", {})
        price_f = info.get("priceFilter", {})
        lev = info.get("leverageFilter", {})
        return {
            "min_qty": float(lot.get("minOrderQty", 0.001)),
            "qty_step": float(lot.get("qtyStep", 0.001)),
            "price_step": float(price_f.get("tickSize", 0.01)),
            "min_notional": float(lot.get("minNotionalValue", 5)),
            "max_leverage": int(float(lev.get("maxLeverage", 100))),
        }

    def round_qty(self, qty: float, symbol_info: Dict) -> str:
        step = symbol_info.get("qty_step", 0.001)
        rounded = round(qty // step * step, 10)
        rounded = max(rounded, symbol_info.get("min_qty", step))
        # Determine decimal places from step
        decimals = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
        return f"{rounded:.{decimals}f}"

    def round_price(self, price: float, symbol_info: Dict) -> str:
        step = symbol_info.get("price_step", 0.01)
        rounded = round(price // step * step, 10)
        decimals = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 2
        return f"{rounded:.{decimals}f}"
