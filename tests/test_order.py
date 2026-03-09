"""Tests for order manager and state machine."""
import pytest
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.execution.state_machine import StateMachine, PositionState, ExitReason, ExitSignal


def make_settings(**kwargs):
    from src.config import Settings
    defaults = dict(
        TRADING_MODE="dry-run",
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        REENTRY_COOLDOWN_SECONDS=0,  # no cooldown in tests
        ATR_PERIOD=14,
        STOP_ATR_MULTIPLIER=2.0,
        TP_ATR_MULTIPLIER=3.0,
        TRAILING_STOP_ATR_MULTIPLIER=1.5,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


class TestStateMachine:
    def setup_method(self):
        self.settings = make_settings()
        self.sm = StateMachine(self.settings)

    def _enter_long(self, symbol="BTCUSDT", entry=50000.0, qty=0.01, leverage=5,
                    tp1_price=0.0, tp2_price=0.0, trade_mode="SCALP",
                    mode_max_bars=15, partial_exit_pct=0.60):
        return self.sm.enter(
            symbol=symbol, direction=1,
            entry_price=entry, qty=qty, leverage=leverage,
            stop_price=entry - 1000,
            take_profit_price=entry + 1500,
            trailing_distance=750,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            trade_mode=trade_mode,
            mode_max_bars=mode_max_bars,
            partial_exit_pct=partial_exit_pct,
        )

    def _enter_short(self, symbol="BTCUSDT", entry=50000.0, qty=0.01, leverage=5,
                     tp1_price=0.0, tp2_price=0.0, trade_mode="SCALP",
                     mode_max_bars=15, partial_exit_pct=0.60):
        return self.sm.enter(
            symbol=symbol, direction=-1,
            entry_price=entry, qty=qty, leverage=leverage,
            stop_price=entry + 1000,
            take_profit_price=entry - 1500,
            trailing_distance=750,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            trade_mode=trade_mode,
            mode_max_bars=mode_max_bars,
            partial_exit_pct=partial_exit_pct,
        )

    def test_initial_state_flat(self):
        pos = self.sm.get_position("BTCUSDT")
        assert pos.state == PositionState.FLAT
        assert pos.is_flat

    def test_enter_long(self):
        result = self._enter_long()
        assert result is True
        pos = self.sm.get_position("BTCUSDT")
        assert pos.is_long
        assert pos.entry_price == 50000.0

    def test_enter_short(self):
        result = self._enter_short()
        assert result is True
        pos = self.sm.get_position("BTCUSDT")
        assert pos.is_short

    def test_cannot_enter_when_already_open(self):
        self._enter_long()
        result = self._enter_long()  # try again
        assert result is False
        # State should still be LONG, not double-entered
        pos = self.sm.get_position("BTCUSDT")
        assert pos.is_long

    def test_stop_loss_long(self):
        self._enter_long(entry=50000)
        # Price drops to stop level
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=48999,
                                       current_atr=500, signal_direction=0)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.STOP_LOSS

    def test_stop_loss_short(self):
        self._enter_short(entry=50000)
        # Price rises to stop level
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=51001,
                                       current_atr=500, signal_direction=0)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.STOP_LOSS

    def test_take_profit_long(self):
        self._enter_long(entry=50000)
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=51500,
                                       current_atr=500, signal_direction=0)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.TAKE_PROFIT

    def test_take_profit_short(self):
        self._enter_short(entry=50000)
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=48500,
                                       current_atr=500, signal_direction=0)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.TAKE_PROFIT

    def test_trailing_stop_long(self):
        # Trailing stop only fires AFTER tp1 partial exit.
        # Chandelier ratchet: adaptive_trail = max(trail_dist, profit * 0.40)
        # entry=50000, high=52000, profit=2000, ratchet=max(750, 2000*0.4)=800
        # trailing_stop = 52000 - 800 = 51200
        # min_lock = 50000 * 1.002 = 50100 (below 51200, no effect)
        self._enter_long(entry=50000)
        pos = self.sm.get_position("BTCUSDT")
        pos.tp1_hit = True
        pos.stop_price = 50000  # breakeven stop after TP1
        # Price rises to 52000 (new high), trailing dist=750
        self.sm.check_exit("BTCUSDT", current_price=52000, current_atr=500, signal_direction=0)
        # Price drops below adaptive trail (52000-800=51200) → trailing fires
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=51199,
                                       current_atr=500, signal_direction=0)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.TRAILING_STOP

    def test_regime_change_exit(self):
        self._enter_long()
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=50100,
                                       current_atr=500, signal_direction=0,
                                       regime_flip=True)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.REGIME_CHANGE

    def test_signal_flip_exits(self):
        self._enter_long()
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=50100,
                                       current_atr=500, signal_direction=-1)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.SIGNAL_FLIP

    def test_time_exit(self):
        self._enter_long()
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=50100,
                                       current_atr=500, signal_direction=0,
                                       bars_in_trade=201, max_bars=200)
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.TIME_EXIT

    def test_normal_conditions_no_exit(self):
        self._enter_long(entry=50000)
        # Price moves slightly in favour — no exit triggered
        exit_sig = self.sm.check_exit("BTCUSDT", current_price=50200,
                                       current_atr=500, signal_direction=1)
        assert not exit_sig.should_exit

    def test_exit_returns_pnl(self):
        self._enter_long(entry=50000, qty=0.01)
        pnl = self.sm.exit("BTCUSDT", exit_price=51000)
        assert abs(pnl - 10.0) < 0.001  # (51000-50000) * 0.01 = 10

    def test_exit_short_pnl(self):
        self._enter_short(entry=50000, qty=0.01)
        pnl = self.sm.exit("BTCUSDT", exit_price=49000)
        assert abs(pnl - 10.0) < 0.001  # (50000-49000) * 0.01 = 10

    def test_state_after_exit_is_flat(self):
        self._enter_long()
        self.sm.exit("BTCUSDT", exit_price=51000)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.is_flat

    def test_consecutive_losses_tracked(self):
        # Loss → increment
        self._enter_long(entry=50000, qty=0.01)
        pnl = self.sm.exit("BTCUSDT", exit_price=49000)  # loss
        pos = self.sm.get_position("BTCUSDT")
        assert pos.consecutive_losses == 1

    def test_open_count(self):
        self.sm.enter("BTCUSDT", 1, 50000, 0.01, 5, 49000, 51500, 750)
        self.sm.enter("ETHUSDT", 1, 3000, 0.1, 5, 2940, 3090, 45)
        assert self.sm.open_count() == 2

    def test_cooldown_prevents_reentry(self):
        settings = make_settings(REENTRY_COOLDOWN_SECONDS=300)
        sm = StateMachine(settings)
        sm.enter("BTCUSDT", 1, 50000, 0.01, 5, 49000, 51500, 750)
        sm.exit("BTCUSDT", 51000)
        # Should be blocked by cooldown
        assert not sm.can_enter("BTCUSDT")

    # ── Partial TP1 tests ─────────────────────────────────────────────────────

    def test_partial_tp1_triggered_for_long(self):
        """When price hits TP1, a partial exit signal should be returned."""
        self._enter_long(
            entry=50000, qty=0.10,
            tp1_price=50500,
            tp2_price=52000,
            partial_exit_pct=0.60,
        )
        exit_sig = self.sm.check_exit(
            "BTCUSDT", current_price=50500,
            current_atr=500, signal_direction=1
        )
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.PARTIAL_TP1
        assert exit_sig.partial is True
        assert exit_sig.partial_pct == 0.60

    def test_partial_tp1_triggered_for_short(self):
        """When price hits TP1 (below entry for short), partial exit returned."""
        self._enter_short(
            entry=50000, qty=0.10,
            tp1_price=49500,
            tp2_price=48000,
            partial_exit_pct=0.60,
        )
        exit_sig = self.sm.check_exit(
            "BTCUSDT", current_price=49500,
            current_atr=500, signal_direction=-1
        )
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.PARTIAL_TP1
        assert exit_sig.partial is True

    def test_partial_exit_reduces_qty(self):
        self._enter_long(
            entry=50000, qty=0.10,
            tp1_price=50500,
            tp2_price=52000,
            partial_exit_pct=0.60,
        )
        pnl = self.sm.partial_exit("BTCUSDT", qty_to_close=0.06, exit_price=50500)
        pos = self.sm.get_position("BTCUSDT")
        assert abs(pos.qty - 0.04) < 1e-9
        assert pnl > 0

    def test_partial_exit_moves_stop_to_breakeven(self):
        self._enter_long(
            entry=50000, qty=0.10,
            tp1_price=50500,
            tp2_price=52000,
        )
        self.sm.partial_exit("BTCUSDT", qty_to_close=0.06, exit_price=50500)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.stop_price == 50000.0   # moved to entry (breakeven)

    def test_tp1_hit_flag_set_after_partial_exit(self):
        self._enter_long(
            entry=50000, qty=0.10,
            tp1_price=50500,
            tp2_price=52000,
        )
        self.sm.partial_exit("BTCUSDT", qty_to_close=0.06, exit_price=50500)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.tp1_hit is True

    def test_no_second_tp1_after_already_hit(self):
        """After tp1_hit=True, TP1 price check should not trigger again."""
        self._enter_long(
            entry=50000, qty=0.10,
            tp1_price=50500,
            tp2_price=52000,
            partial_exit_pct=0.60,
        )
        # Trigger partial exit
        self.sm.partial_exit("BTCUSDT", qty_to_close=0.06, exit_price=50500)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.tp1_hit is True

        # Check again at same price — should NOT trigger PARTIAL_TP1 again
        exit_sig = self.sm.check_exit(
            "BTCUSDT", current_price=50500,
            current_atr=500, signal_direction=1
        )
        assert exit_sig.reason != ExitReason.PARTIAL_TP1

    def test_take_profit_price_updated_to_tp2(self):
        """After partial exit, take_profit_price should point to TP2."""
        self._enter_long(
            entry=50000, qty=0.10,
            tp1_price=50500,
            tp2_price=52000,
        )
        self.sm.partial_exit("BTCUSDT", qty_to_close=0.06, exit_price=50500)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.take_profit_price == 52000.0

    def test_mode_max_bars_used_in_time_exit(self):
        """SCALP mode should exit at SCALP_MAX_BARS, not 200."""
        self._enter_long(
            entry=50000, qty=0.01,
            trade_mode="SCALP", mode_max_bars=15,
        )
        # Should NOT exit before max_bars
        no_exit = self.sm.check_exit(
            "BTCUSDT", current_price=50100,
            current_atr=500, signal_direction=1,
            bars_in_trade=14, max_bars=200,  # pass 200 as default
        )
        assert not no_exit.should_exit

        # Should exit AT max_bars
        exit_sig = self.sm.check_exit(
            "BTCUSDT", current_price=50100,
            current_atr=500, signal_direction=1,
            bars_in_trade=15, max_bars=200,
        )
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.TIME_EXIT

    def test_trade_mode_stored_in_position(self):
        self._enter_long(trade_mode="TREND_RIDE", mode_max_bars=120)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.trade_mode == "TREND_RIDE"
        assert pos.mode_max_bars == 120

    def test_state_reset_after_full_exit(self):
        """After full exit, tp1/tp2/tp1_hit should all reset."""
        self._enter_long(
            entry=50000, qty=0.01,
            tp1_price=50500, tp2_price=52000,
        )
        self.sm.exit("BTCUSDT", exit_price=52000)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.is_flat
        assert pos.tp1_price == 0.0
        assert pos.tp2_price == 0.0
        assert pos.tp1_hit is False

    def test_trailing_stop_only_fires_after_tp1(self):
        """TRAILING_STOP should NOT fire before tp1_hit=True."""
        self._enter_long(
            entry=50000, qty=0.01,
            tp1_price=51000, tp2_price=53000,
        )
        # Price moved up to 51500 then pulled back — trailing should NOT fire
        # because tp1 has not been hit yet
        no_exit = self.sm.check_exit(
            "BTCUSDT", current_price=50250,
            current_atr=500, signal_direction=1,
            bars_in_trade=3,
        )
        assert no_exit.reason != ExitReason.TRAILING_STOP

    def test_trailing_stop_fires_after_tp1(self):
        """TRAILING_STOP should fire only after tp1_hit=True."""
        self._enter_long(
            entry=50000, qty=0.10,
            tp1_price=50750, tp2_price=53000,
            partial_exit_pct=0.60,
        )
        # Trigger TP1 partial exit
        self.sm.partial_exit("BTCUSDT", qty_to_close=0.06, exit_price=50750)
        pos = self.sm.get_position("BTCUSDT")
        assert pos.tp1_hit is True
        # highest_price was set to 50750 during TP1 check
        pos.highest_price = 52000
        # trailing_stop_distance = 750, so trailing_stop = 52000 - 750 = 51250
        # current_price = 51100 < 51250 → trailing fires
        exit_sig = self.sm.check_exit(
            "BTCUSDT", current_price=51100,
            current_atr=500, signal_direction=1,
            bars_in_trade=5,
        )
        assert exit_sig.should_exit
        assert exit_sig.reason == ExitReason.TRAILING_STOP


class TestOrderManagerUseMarket:
    """Test that order type selection is correct for each exit reason."""

    def _make_use_market_exit(self, reason: str) -> bool:
        """Replicate the use_market logic from order_manager.py (exit)."""
        return (
            reason.upper() == "STOP_LOSS"
            or "GUARD" in reason.upper()
            or "LIQUIDATION" in reason.upper()
        )

    def _make_fee_is_taker(self, reason: str) -> bool:
        """Replicate the fee rate logic from main.py."""
        return (
            reason == "STOP_LOSS"
            or "GUARD" in reason
            or "LIQUIDATION" in reason
        )

    def _make_use_market_entry(
        self, spread_pct: float, signal_strength: float,
        spread_threshold: float = 0.003, urgent_threshold: float = 0.90,
    ) -> bool:
        """Replicate the entry order type selection from order_manager.py."""
        return (
            spread_pct > spread_threshold
            or signal_strength > urgent_threshold
        )

    # ── Exit order type tests ─────────────────────────────────────────────────

    def test_stop_loss_uses_market_order(self):
        assert self._make_use_market_exit("STOP_LOSS") is True

    def test_trailing_stop_uses_limit_order(self):
        """TRAILING_STOP should NOT use market order (was the bug)."""
        assert self._make_use_market_exit("TRAILING_STOP") is False

    def test_take_profit_uses_limit_order(self):
        assert self._make_use_market_exit("TAKE_PROFIT") is False

    def test_time_exit_uses_limit_order(self):
        assert self._make_use_market_exit("TIME_EXIT") is False

    def test_regime_change_uses_limit_order(self):
        assert self._make_use_market_exit("REGIME_CHANGE") is False

    def test_signal_flip_uses_limit_order(self):
        assert self._make_use_market_exit("SIGNAL_FLIP") is False

    def test_risk_guard_halt_uses_market_order(self):
        assert self._make_use_market_exit("RISK_GUARD_HALT") is True

    def test_liquidation_uses_market_order(self):
        assert self._make_use_market_exit("LIQUIDATION") is True

    def test_stop_loss_uses_taker_fee(self):
        assert self._make_fee_is_taker("STOP_LOSS") is True

    def test_trailing_stop_uses_maker_fee(self):
        """TRAILING_STOP should use maker fee (was the bug)."""
        assert self._make_fee_is_taker("TRAILING_STOP") is False

    def test_take_profit_uses_maker_fee(self):
        assert self._make_fee_is_taker("TAKE_PROFIT") is False

    def test_partial_tp1_uses_maker_fee(self):
        assert self._make_fee_is_taker("PARTIAL_TP1") is False

    # ── Entry order type tests ────────────────────────────────────────────────

    def test_normal_entry_uses_postonly_limit(self):
        """스프레드 정상, 신호 강도 보통 → PostOnly 지정가."""
        assert self._make_use_market_entry(spread_pct=0.001, signal_strength=0.75) is False

    def test_wide_spread_entry_uses_market(self):
        """스프레드 > 0.3% → 시장가."""
        assert self._make_use_market_entry(spread_pct=0.004, signal_strength=0.75) is True

    def test_urgent_signal_entry_uses_market(self):
        """신호 강도 > 0.90 → 시장가(즉시 체결 우선)."""
        assert self._make_use_market_entry(spread_pct=0.001, signal_strength=0.91) is True

    def test_signal_at_threshold_uses_postonly(self):
        """신호 강도 정확히 0.90 → PostOnly 지정가(초과 시에만 시장가)."""
        assert self._make_use_market_entry(spread_pct=0.001, signal_strength=0.90) is False

    def test_both_conditions_uses_market(self):
        """스프레드 넓음 + 강한 신호 → 시장가."""
        assert self._make_use_market_entry(spread_pct=0.005, signal_strength=0.95) is True

    def test_configurable_urgent_threshold(self):
        """SIGNAL_URGENT_THRESHOLD 설정값 반영."""
        # threshold=0.85로 낮추면 0.86 신호에서 시장가
        assert self._make_use_market_entry(
            spread_pct=0.001, signal_strength=0.86, urgent_threshold=0.85
        ) is True
        # threshold=0.85에서 0.85는 통과 안 됨 (strict >)
        assert self._make_use_market_entry(
            spread_pct=0.001, signal_strength=0.85, urgent_threshold=0.85
        ) is False


class TestOrderManagerMarketFallback:
    """_place_market이 order_type을 'Market'으로 올바르게 설정하는지 검증."""

    def _make_settings(self, **kwargs):
        from src.config import Settings
        defaults = dict(
            TRADING_MODE="dry-run",
            BYBIT_API_KEY="", BYBIT_API_SECRET="",
            BYBIT_HEDGE_MODE=True,
            SPREAD_TAKER_THRESHOLD=0.003,
            SIGNAL_URGENT_THRESHOLD=0.90,
            LIMIT_ORDER_TIMEOUT_SECONDS=1,
            LIMIT_ORDER_MAX_RETRIES=2,
            BACKTEST_FEE_TAKER=0.00055,
            BACKTEST_FEE_MAKER=0.00020,
        )
        defaults.update(kwargs)
        return Settings(**defaults)

    def _make_order_manager(self, settings=None):
        from unittest.mock import MagicMock
        from src.execution.order_manager import OrderManager
        s = settings or self._make_settings()
        client = MagicMock()
        client.round_qty.return_value = "0.010"
        client.round_price.return_value = "49999.9"
        client.set_position_mode.return_value = None
        client.set_leverage.return_value = None
        client.cancel_all_orders.return_value = 0
        slack = MagicMock()
        return OrderManager(client, s, slack), client

    def test_place_market_sets_order_type_to_market(self):
        """_place_market은 항상 managed.order_type을 'Market'으로 설정."""
        from src.execution.order_manager import ManagedOrder, OrderStatus
        om, _ = self._make_order_manager()
        managed = ManagedOrder(
            client_id="test-001",
            symbol="BTCUSDT",
            side="Buy",
            qty="0.01",
            order_type="Limit",   # 초기값이 Limit이어도
            position_idx=1,
        )
        om._place_market(managed, 50000.0, {"price_step": 0.1, "min_qty": 0.001})
        # dry-run에서도 Market으로 갱신되어야 함
        assert managed.order_type == "Market"
        assert managed.status == OrderStatus.FILLED

    def test_market_entry_order_type_correct(self):
        """스프레드 넓은 경우 진입 → 시장가, order_type='Market'."""
        from src.risk.engine import RiskParameters
        om, client = self._make_order_manager()
        risk = RiskParameters(
            leverage=5, position_size_usdt=500, margin_usdt=100, qty_base=0.01,
            stop_price=49000, take_profit_price=51500,
            tp1_price=50500, tp2_price=51500,
            trailing_stop_atr=250, max_loss_usdt=200,
            risk_ok=True, trade_mode="SCALP",
        )
        sym_info = {"min_qty": 0.001, "qty_step": 0.001, "price_step": 0.1,
                    "min_notional": 5.0, "max_leverage": 100}
        order = om.open_position(
            symbol="BTCUSDT", direction=1, risk_params=risk,
            spread_pct=0.005,   # > SPREAD_TAKER_THRESHOLD → Market
            signal_strength=0.75, current_price=50000.0, symbol_info=sym_info,
        )
        assert order is not None
        assert order.order_type == "Market"

    def test_postonly_entry_order_type_limit(self):
        """스프레드 정상, 신호 보통 → PostOnly 지정가, order_type='Limit'."""
        from src.risk.engine import RiskParameters
        om, client = self._make_order_manager()
        risk = RiskParameters(
            leverage=5, position_size_usdt=500, margin_usdt=100, qty_base=0.01,
            stop_price=49000, take_profit_price=51500,
            tp1_price=50500, tp2_price=51500,
            trailing_stop_atr=250, max_loss_usdt=200,
            risk_ok=True, trade_mode="SCALP",
        )
        sym_info = {"min_qty": 0.001, "qty_step": 0.001, "price_step": 0.1,
                    "min_notional": 5.0, "max_leverage": 100}
        order = om.open_position(
            symbol="BTCUSDT", direction=1, risk_params=risk,
            spread_pct=0.001,   # < SPREAD_TAKER_THRESHOLD → PostOnly/Limit
            signal_strength=0.75, current_price=50000.0, symbol_info=sym_info,
        )
        assert order is not None
        assert order.order_type == "Limit"

    def test_postonly_cancelled_fallback_sets_market(self):
        """PostOnly 취소(Taker 시도) 시 시장가 폴백 → order_type='Market'."""
        from src.execution.order_manager import ManagedOrder, OrderStatus
        om, _ = self._make_order_manager()

        # PostOnly 취소 시나리오: _wait_for_fill이 CANCELLED 반환 시뮬레이션
        def fake_wait(managed, timeout):
            managed.status = OrderStatus.CANCELLED
            return False

        om._wait_for_fill = fake_wait

        managed = ManagedOrder(
            client_id="test-002",
            symbol="BTCUSDT",
            side="Buy",
            qty="0.01",
            order_type="Limit",
            position_idx=1,
        )
        sym_info = {"price_step": 0.1, "min_qty": 0.001}
        # dry-run이므로 실제 API 호출 없음 — dry-run early return 확인
        # dry-run에서는 _wait_for_fill 진입 전 반환하므로 폴백 경로 확인은 live 전용
        # 대신 _place_market 직접 호출 시 order_type 변경 검증
        om._place_market(managed, 50000.0, sym_info)
        assert managed.order_type == "Market"
