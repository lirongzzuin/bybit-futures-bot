"""
Order Manager — idempotent order lifecycle management.

Order type selection:
  - Default: Limit (PostOnly) for maker rebate
  - Switch to Market if:
    * Spread > threshold
    * Previous limit attempt timed out
    * Signal urgency is very high (configurable)

Idempotency:
  - Each trade intent generates a deterministic client_order_id
  - Duplicate detection prevents double-sends on restart
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Set

from src.api.bybit_client import BybitClient, BybitAPIError, CircuitOpenError
from src.api.slack import SlackNotifier
from src.config import Settings
from src.logger import get_logger
from src.risk.engine import RiskParameters

log = get_logger(__name__)


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class ManagedOrder:
    client_id: str
    exchange_id: Optional[str] = None
    symbol: str = ""
    side: str = ""
    qty: str = ""
    price: Optional[str] = None
    order_type: str = "Limit"
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_price: float = 0.0
    attempts: int = 0
    created_ts: float = field(default_factory=time.time)
    position_idx: int = 0    # 0=One-Way, 1=Hedge Long, 2=Hedge Short


class OrderManager:
    def __init__(
        self,
        client: BybitClient,
        settings: Settings,
        slack: SlackNotifier,
    ):
        self._client = client
        self._s = settings
        self._slack = slack
        self._orders: Dict[str, ManagedOrder] = {}
        self._sent_ids: Set[str] = set()

    # ── Entry Order ───────────────────────────────────────────────────────────

    def open_position(
        self,
        symbol: str,
        direction: int,         # +1 long, -1 short
        risk_params: RiskParameters,
        spread_pct: float,
        signal_strength: float,
        current_price: float,
        symbol_info: dict,
    ) -> Optional[ManagedOrder]:
        """
        Place an entry order. Returns ManagedOrder on success, None on failure.
        """
        side = "Buy" if direction == 1 else "Sell"
        qty_str = self._client.round_qty(risk_params.qty_base, symbol_info)
        client_id = self._make_client_id(symbol, side, "entry")

        # Idempotency: skip if already sent
        if client_id in self._sent_ids:
            log.warning("Duplicate entry order prevented for %s %s", symbol, side)
            return self._orders.get(client_id)

        # ── Set position mode + leverage ──────────────────────────────────────
        if not self._s.is_dry_run():
            try:
                self._client.set_position_mode(symbol, hedge_mode=self._s.BYBIT_HEDGE_MODE)
                self._client.set_leverage(symbol, risk_params.leverage)
            except Exception as e:
                log.error("Failed to init position mode/leverage for %s: %s", symbol, e)
                return None

        # ── positionIdx for hedge mode ─────────────────────────────────────────
        # Bybit hedge mode: 1 = long side, 2 = short side, 0 = one-way (default)
        if self._s.BYBIT_HEDGE_MODE:
            position_idx = 1 if direction == 1 else 2
        else:
            position_idx = 0

        # ── Choose order type ─────────────────────────────────────────────────
        use_market = (
            spread_pct > self._s.SPREAD_TAKER_THRESHOLD or
            signal_strength > 0.90  # very urgent signal
        )

        managed = ManagedOrder(
            client_id=client_id,
            symbol=symbol,
            side=side,
            qty=qty_str,
            order_type="Market" if use_market else "Limit",
            position_idx=position_idx,
        )

        if use_market:
            success = self._place_market(managed, current_price, symbol_info)
        else:
            success = self._place_limit_with_retry(
                managed, current_price, symbol_info, direction
            )

        if success:
            # Attach SL/TP after fill if not set at order level
            self._set_sl_tp(
                managed, risk_params, symbol_info
            )
            self._sent_ids.add(client_id)
            self._orders[client_id] = managed
            return managed

        log.warning("Entry order failed for %s %s — all attempts exhausted", symbol, side)
        return None

    # ── Exit Order ────────────────────────────────────────────────────────────

    def close_position(
        self,
        symbol: str,
        direction: int,     # existing position direction
        qty: float,
        current_price: float,
        symbol_info: dict,
        reason: str = "",
    ) -> Optional[ManagedOrder]:
        """
        Close an existing position.
        Uses Market order for stops (speed > cost).
        Uses Limit for TP exits (cost > speed).
        """
        side = "Sell" if direction == 1 else "Buy"  # opposite to close
        qty_str = self._client.round_qty(qty, symbol_info)
        client_id = self._make_client_id(symbol, side, "exit")

        if client_id in self._sent_ids:
            log.warning("Duplicate exit order prevented for %s", symbol)
            return self._orders.get(client_id)

        # STOP_LOSS, GUARD*, LIQUIDATION* → Market (즉각 실행 필요)
        # TRAILING_STOP, TAKE_PROFIT, TIME_EXIT, REGIME_CHANGE, SIGNAL_FLIP → Limit (Maker fee)
        # 주의: "STOP" substring 체크는 "TRAILING_STOP"도 잡아버리므로 exact match 사용
        use_market = (
            reason.upper() == "STOP_LOSS"
            or "GUARD" in reason.upper()
            or "LIQUIDATION" in reason.upper()
        )

        # In hedge mode, closing a long uses positionIdx=1, closing a short uses positionIdx=2
        # (same as opening — the positionIdx selects WHICH position to reduce, not direction)
        if self._s.BYBIT_HEDGE_MODE:
            position_idx = 1 if direction == 1 else 2
        else:
            position_idx = 0

        managed = ManagedOrder(
            client_id=client_id,
            symbol=symbol,
            side=side,
            qty=qty_str,
            order_type="Market" if use_market else "Limit",
            position_idx=position_idx,
        )

        if use_market:
            success = self._place_market(managed, current_price, symbol_info, is_close=True)
        else:
            success = self._place_limit_with_retry(
                managed, current_price, symbol_info, -direction, is_close=True  # opposite to get filled
            )

        if success:
            self._sent_ids.add(client_id)
            self._orders[client_id] = managed
            return managed

        log.warning("Close order failed for %s %s reason=%s", symbol, side, reason)
        return None

    # ── Internal placers ──────────────────────────────────────────────────────

    def _place_market(
        self, managed: ManagedOrder, current_price: float, symbol_info: dict,
        is_close: bool = False,
    ) -> bool:
        if self._s.is_dry_run():
            managed.status = OrderStatus.FILLED
            managed.avg_price = current_price
            managed.filled_qty = float(managed.qty)
            managed.price = str(current_price)
            log.info("[DRY-RUN] Market %s %s qty=%s @ %.4f",
                     managed.side, managed.symbol, managed.qty, current_price)
            return True

        try:
            result = self._client.place_order(
                symbol=managed.symbol,
                side=managed.side,
                qty=managed.qty,
                order_type="Market",
                client_order_id=managed.client_id,
                position_idx=managed.position_idx,
            )
            managed.exchange_id = result.get("orderId")
            managed.status = OrderStatus.FILLED   # market = instant
            managed.avg_price = current_price
            managed.price = str(current_price)
            return True
        except BybitAPIError as e:
            if e.code == 110072:
                # 동일 linkId 주문이 이미 존재 — 실제 체결 여부 검증 필수
                # ※ GTC 주문이 취소된 후 Market 폴백 시에도 110072 발생 가능
                #   이 경우 취소된 주문을 체결로 오판하면 '유령 포지션' 발생!
                existing = self._client.get_order_by_link_id(
                    managed.symbol, managed.client_id
                )
                if existing:
                    order_status = existing.get("orderStatus", "")
                    if order_status == "Filled":
                        log.info(
                            "Market order %s already filled (110072) — treating as success",
                            managed.client_id,
                        )
                        managed.exchange_id = existing.get("orderId")
                        managed.status = OrderStatus.FILLED
                        managed.avg_price = float(existing.get("avgPrice", current_price) or current_price)
                        managed.price = str(managed.avg_price)
                        return True
                    else:
                        # 취소/거부된 이전 주문과 linkId 충돌 — 유령 포지션 방지
                        log.warning(
                            "Order %s exists with status=%s (not Filled) — NOT treating as position open",
                            managed.client_id, order_status,
                        )
                        managed.status = OrderStatus.FAILED
                        return False
                else:
                    log.warning("110072 but no existing order found for %s — FAILED", managed.client_id)
                    managed.status = OrderStatus.FAILED
                    return False
            if e.code == 110017 and is_close:
                # "current position is zero" — position already closed/liquidated on exchange.
                # Treat as success so the state machine can move to FLAT.
                log.warning(
                    "110017: position already zero for %s — treating close as success",
                    managed.symbol,
                )
                managed.status = OrderStatus.FILLED
                managed.avg_price = current_price
                managed.price = str(current_price)
                return True
            log.error("Market order failed: %s", e)
            managed.status = OrderStatus.FAILED
            return False
        except CircuitOpenError as e:
            # Circuit breaker is open — cannot place order right now.
            # Propagate so the caller knows this wasn't a logic failure.
            log.error("Market order skipped (circuit open): %s %s", managed.symbol, managed.side)
            raise
        except Exception as e:
            log.error("Market order unexpected error: %s", e)
            managed.status = OrderStatus.FAILED
            return False

    def _place_limit_with_retry(
        self,
        managed: ManagedOrder,
        current_price: float,
        symbol_info: dict,
        direction: int,
        is_close: bool = False,
    ) -> bool:
        timeout = self._s.LIMIT_ORDER_TIMEOUT_SECONDS
        max_retries = self._s.LIMIT_ORDER_MAX_RETRIES

        for attempt in range(1, max_retries + 1):
            managed.attempts = attempt
            # Quote at current best
            offset = symbol_info.get("price_step", 0.01)
            if direction == 1:
                limit_price = current_price - offset   # slightly below for Buy
            else:
                limit_price = current_price + offset   # slightly above for Sell

            price_str = self._client.round_price(limit_price, symbol_info)
            managed.price = price_str

            if self._s.is_dry_run():
                managed.status = OrderStatus.FILLED
                managed.avg_price = float(price_str)
                managed.filled_qty = float(managed.qty)
                log.info("[DRY-RUN] Limit %s %s qty=%s @ %s",
                         managed.side, managed.symbol, managed.qty, price_str)
                return True

            try:
                result = self._client.place_order(
                    symbol=managed.symbol,
                    side=managed.side,
                    qty=managed.qty,
                    order_type="Limit",
                    price=price_str,
                    time_in_force="GTC",  # PostOnly 대신 GTC: 즉시 체결 허용(Taker 포함)
                    # PostOnly는 즉시 체결 시 거부 → Cancelled → 동일 client_id로 Market 시도
                    # → 110072 오류 → 취소된 주문을 체결로 오판 → 유령 포지션 발생
                    # GTC는 항상 체결됨(메이커 or 테이커), 폴백 시 새 client_id 사용
                    client_order_id=managed.client_id,
                    position_idx=managed.position_idx,
                )
                managed.exchange_id = result.get("orderId")
                managed.status = OrderStatus.PENDING
            except BybitAPIError as e:
                if e.code == 110072:
                    # orderLinkId already used — the previous attempt succeeded on
                    # Bybit's side but we didn't receive the confirmation.  Look up
                    # the existing order and continue waiting for fill.
                    log.info(
                        "Duplicate orderLinkId %s — fetching existing order from exchange",
                        managed.client_id,
                    )
                    existing = self._client.get_order_by_link_id(
                        managed.symbol, managed.client_id
                    )
                    if existing:
                        managed.exchange_id = existing.get("orderId")
                        managed.status = OrderStatus.PENDING
                        # Fall through to _wait_for_fill below (no continue)
                    else:
                        log.warning(
                            "Could not find order with linkId %s — giving up this attempt",
                            managed.client_id,
                        )
                        if attempt == max_retries:
                            log.info("Falling back to market order for %s", managed.symbol)
                            managed.client_id = self._make_client_id(
                                managed.symbol, managed.side, f"mkt-{int(time.time())}"
                            )
                            return self._place_market(managed, current_price, symbol_info, is_close=is_close)
                        continue
                elif e.code == 110017 and is_close:
                    # "current position is zero" — position already closed/liquidated.
                    log.warning(
                        "110017: position already zero for %s — treating close as success",
                        managed.symbol,
                    )
                    managed.status = OrderStatus.FILLED
                    managed.avg_price = current_price
                    managed.price = str(current_price)
                    return True
                else:
                    log.warning("Limit order attempt %d failed: %s", attempt, e)
                    if attempt == max_retries:
                        log.info("Falling back to market order for %s", managed.symbol)
                        managed.client_id = self._make_client_id(
                            managed.symbol, managed.side, f"mkt-{int(time.time())}"
                        )
                        return self._place_market(managed, current_price, symbol_info, is_close=is_close)
                    continue

            # Wait for fill or timeout
            fill_result = self._wait_for_fill(managed, timeout)
            if fill_result:
                return True

            # Not filled — cancel and re-quote
            if managed.exchange_id:
                try:
                    self._client.cancel_order(managed.symbol, managed.exchange_id)
                    log.info(
                        "Limit order timed out, re-quoting %s (attempt %d/%d)",
                        managed.symbol, attempt, max_retries,
                    )
                except Exception:
                    pass

            if attempt == max_retries:
                # Market fallback: 새 client_id 생성하여 GTC 취소된 ID 재사용 방지
                managed.client_id = self._make_client_id(
                    managed.symbol, managed.side, f"mkt-{int(time.time())}"
                )
                return self._place_market(managed, current_price, symbol_info, is_close=is_close)

        return False

    def _wait_for_fill(self, managed: ManagedOrder, timeout: float) -> bool:
        """Poll order status until filled or timeout."""
        if self._s.is_dry_run():
            return True

        start = time.time()
        while time.time() - start < timeout:
            try:
                order = self._client.get_order(managed.symbol, managed.exchange_id)
                if order:
                    status = order.get("orderStatus", "")
                    if status == "Filled":
                        managed.status = OrderStatus.FILLED
                        managed.avg_price = float(order.get("avgPrice", 0))
                        managed.filled_qty = float(order.get("cumExecQty", 0))
                        return True
                    elif status == "PartiallyFilled":
                        managed.status = OrderStatus.PARTIAL
                        managed.filled_qty = float(order.get("cumExecQty", 0))
                        log.debug(
                            "Partial fill %s %s/%s",
                            managed.symbol, managed.filled_qty, managed.qty,
                        )
                    elif status in ("Cancelled", "Rejected"):
                        managed.status = OrderStatus.CANCELLED
                        return False
            except Exception as e:
                log.warning("Order status check failed: %s", e)
            time.sleep(2)

        return False

    def _set_sl_tp(
        self,
        managed: ManagedOrder,
        risk_params: RiskParameters,
        symbol_info: dict,
    ) -> None:
        """
        Attach exchange-side SL/TP to the filled position via trading-stop API.
        This ensures positions are protected even if the bot crashes or loses
        connectivity — the exchange will enforce the stop independently.

        Order type policy (tpSlMode=Full):
          SL → Market: 손절은 즉각 실행이 최우선
          TP → Market: tpSlMode=Full 시 Bybit API 요구사항 (Limit 사용 시 10001 오류)
          TP2를 거래소 TP로 설정 — 봇이 TP1 부분청산을 먼저 처리하고,
          봇 크래시 시 거래소 TP(Market)가 최후 안전망 역할
        """
        if self._s.is_dry_run():
            return

        if risk_params.stop_price <= 0:
            log.warning("_set_sl_tp: invalid stop_price for %s — skipping", managed.symbol)
            return

        sl_price = self._client.round_price(risk_params.stop_price, symbol_info)

        # 거래소 TP는 TP2(최종 목표가)로 설정 — TP1 부분청산은 봇 5분 사이클에서 처리
        tp2 = risk_params.tp2_price if risk_params.tp2_price > 0 else risk_params.take_profit_price
        tp_price = self._client.round_price(tp2, symbol_info) if tp2 > 0 else None

        try:
            self._client.set_trading_stop(
                symbol=managed.symbol,
                stop_loss=sl_price,
                take_profit=tp_price,
                position_idx=managed.position_idx,
                sl_order_type="Market",   # SL: 시장가 (긴급)
                tp_order_type="Market",   # TP: 시장가 (tpSlMode=Full 요구사항)
            )
            log.info(
                "Exchange SL/TP set | %s sl=%s(Mkt) tp=%s(Mkt@TP2) posIdx=%d",
                managed.symbol, sl_price, tp_price, managed.position_idx,
            )
        except Exception as e:
            # Never block a trade over SL/TP attachment failure — log and continue
            log.error("Failed to set exchange SL/TP for %s: %s", managed.symbol, e)

    # ── Client Order ID ───────────────────────────────────────────────────────

    def _make_client_id(self, symbol: str, side: str, tag: str) -> str:
        """
        Deterministic client order ID based on symbol+side+tag+minute.
        This ensures idempotency within the same minute-window.
        """
        minute = int(time.time()) // 60
        raw = f"{symbol}-{side}-{tag}-{minute}"
        h = hashlib.md5(raw.encode()).hexdigest()[:16]
        return f"bot-{h}"

    def get_order(self, client_id: str) -> Optional[ManagedOrder]:
        return self._orders.get(client_id)
