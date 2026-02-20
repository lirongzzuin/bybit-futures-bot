"""
Main bot entry point.

Running modes:
  dry-run : no real orders, all logic simulated
  paper   : paper trading (fake capital, real signals)
  live    : real orders on Bybit (requires quality gate)

Usage:
  python -m src.main                        # uses TRADING_MODE from .env
  TRADING_MODE=paper python -m src.main
  TRADING_MODE=live  python -m src.main     # only if QUALITY_GATE_PASSED=true
"""
from __future__ import annotations

import signal
import sys
import time
from typing import Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.api.bybit_client import BybitClient, CircuitOpenError
from src.api.slack import SlackNotifier
from src.config import settings
from src.data.collector import DataCollector
from src.data.universe import UniverseManager
from src.db.database import Database
from src.execution.order_manager import OrderManager
from src.execution.state_machine import StateMachine
from src.logger import get_logger
from src.reporting.daily_briefing import DailyBriefing
from src.risk.engine import RiskEngine
from src.risk.guards import GuardAction, RiskGuards
from src.risk.profit_protector import ProfitProtector
from src.strategy.combiner import SignalCombiner
from src.strategy.ict import ICTAnalyzer
from src.strategy.indicators import volatility_percentile
from src.strategy.regime import RegimeDetector, Regime
from src.strategy.scalp import TradeModeDetector
from src.strategy.signals import SignalGenerator

log = get_logger(__name__)

# ── Bot Core ──────────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self):
        # Safety: check quality gate before live trading
        settings.check_live_allowed()

        # Components
        self._client = BybitClient(settings)
        self._slack = SlackNotifier(settings.SLACK_WEBHOOK_URL)
        self._db = Database(settings)
        self._universe = UniverseManager(self._client, settings)
        self._collector = DataCollector(self._client, settings)
        self._regime = RegimeDetector(settings)
        self._ict = ICTAnalyzer(settings)
        self._sig_gen = SignalGenerator(settings)
        self._combiner = SignalCombiner(settings)
        self._mode_detector = TradeModeDetector(settings)
        self._risk_engine = RiskEngine(settings)
        self._guards = RiskGuards(settings)
        self._state_machine = StateMachine(settings)
        self._order_mgr = OrderManager(self._client, settings, self._slack)
        self._briefing = DailyBriefing(
            settings, self._client, self._db, self._slack, self._state_machine
        )
        self._profit_protector = ProfitProtector(
            client=self._client,
            settings=settings,
            slack=self._slack,
            initial_capital=settings.PAPER_CAPITAL_USDT,
        )

        # Paper capital tracking
        self._paper_equity = settings.PAPER_CAPITAL_USDT

        # Dashboard tracking
        self._start_ts: float = time.time()
        self._last_cycle_ts: float = 0.0

        log.info(
            "TradingBot ready | mode=%s testnet=%s",
            settings.TRADING_MODE,
            settings.BYBIT_TESTNET,
        )

    # ── Main cycle ────────────────────────────────────────────────────────────

    def run_cycle(self) -> None:
        """Called every N minutes by scheduler."""
        try:
            self._cycle()
            self._last_cycle_ts = time.time()
        except CircuitOpenError:
            log.error("Circuit breaker open — skipping cycle")
        except Exception as e:
            log.exception("Unexpected error in cycle: %s", e)

    def _cycle(self) -> None:
        # ── 1. Refresh universe ───────────────────────────────────────────────
        symbols = self._universe.get_or_refresh()
        if not symbols:
            log.warning("Empty universe — skipping cycle")
            return

        if self._universe.needs_refresh():
            log.info("Universe refreshed: %s", symbols)

        # ── 2. Get account state ──────────────────────────────────────────────
        equity = self._get_equity()
        daily_pnl = self._db.get_today_pnl()
        weekly_pnl = self._db.get_weekly_pnl()
        peak_equity = self._db.get_peak_equity(settings.TRADING_MODE) or equity

        # ── 3. Sync positions from exchange every cycle to detect liquidations ──
        if not settings.is_dry_run():
            self._sync_positions(symbols)

        # ── 4. Process each symbol ────────────────────────────────────────────
        for symbol in symbols:
            try:
                self._process_symbol(
                    symbol=symbol,
                    equity=equity,
                    daily_pnl=daily_pnl,
                    weekly_pnl=weekly_pnl,
                    peak_equity=peak_equity,
                )
            except Exception as e:
                log.error("Error processing %s: %s", symbol, e)
                continue

        # ── 5. Profit protection check ────────────────────────────────────────
        try:
            transferred = self._profit_protector.check_and_protect(equity)
            if transferred:
                log.info("Profit protection: %.2f USDT moved to Funding Account", transferred)
                # Adjust paper equity to reflect transfer
                if settings.is_paper() or settings.is_dry_run():
                    self._paper_equity -= transferred
        except Exception as e:
            log.error("Profit protector error: %s", e)

        # ── 6. Record equity ──────────────────────────────────────────────────
        self._db.record_equity(equity, settings.TRADING_MODE)

    def _process_symbol(
        self,
        symbol: str,
        equity: float,
        daily_pnl: float,
        weekly_pnl: float,
        peak_equity: float,
    ) -> None:
        # Collect market data
        snap = self._collector.collect(symbol, limit=250)
        if snap.ohlcv.empty or len(snap.ohlcv) < 100:
            log.warning("Insufficient data for %s (%d bars)", symbol, len(snap.ohlcv))
            return

        df = snap.ohlcv
        current_price = snap.last_price

        # ── Run strategy ──────────────────────────────────────────────────────
        # Use trend TF bars for regime detection if available
        regime_df = snap.trend_ohlcv if (
            snap.trend_ohlcv is not None and len(snap.trend_ohlcv) >= 60
        ) else df
        regime_r = self._regime.detect(regime_df, oi_change_pct=snap.oi_change_pct)
        ict_r = self._ict.analyze(df)
        sig_bundle = self._sig_gen.generate(
            df, ict_r,
            funding_rate=snap.funding_rate,
            oi_change_pct=snap.oi_change_pct,
            spread_pct=snap.orderbook.spread_pct,
            vwap=snap.vwap,
            vwap_distance_pct=snap.vwap_distance_pct,
            orderbook_imbalance=snap.orderbook.imbalance,
        )
        combined = self._combiner.combine(sig_bundle, regime_r)

        # Vol percentile for guards
        close = df["close"]
        vol_pct_s = volatility_percentile(close)
        vol_pct = float(vol_pct_s.iloc[-1]) if not vol_pct_s.empty else 0.5

        # ── Global risk guard ─────────────────────────────────────────────────
        guard = self._guards.check(
            equity=equity,
            peak_equity=peak_equity,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            initial_capital=settings.PAPER_CAPITAL_USDT,
            spread_pct=snap.orderbook.spread_pct,
            vol_percentile=vol_pct,
        )

        if guard.action == GuardAction.HALT:
            log.warning("Guard HALT: %s — closing all positions", guard.reason)
            self._emergency_close_all(symbols=[symbol])
            return

        # ── Check existing position ───────────────────────────────────────────
        pos = self._state_machine.get_position(symbol)

        if not pos.is_flat:
            # Calculate bars in trade
            bars_in_trade = int((time.time() - pos.entry_ts) /
                                (int(settings.SIGNAL_TIMEFRAME) * 60))

            # Detect regime flip (adversely against position)
            regime_flip = (
                (pos.is_long and regime_r.regime == Regime.TRENDING_DOWN) or
                (pos.is_short and regime_r.regime == Regime.TRENDING_UP)
            )

            exit_check = self._state_machine.check_exit(
                symbol=symbol,
                current_price=current_price,
                current_atr=sig_bundle.atr_val,
                signal_direction=combined.direction,
                regime_flip=regime_flip,
                bars_in_trade=bars_in_trade,
                signal_strength=combined.strength,
                exit_threshold=settings.SIGNAL_EXIT_THRESHOLD,
            )

            if exit_check.should_exit:
                if exit_check.partial:
                    self._execute_partial_exit(symbol, pos, current_price, exit_check, snap)
                else:
                    self._execute_exit(symbol, pos, current_price, exit_check.reason.value, snap)
            return  # one action per symbol per cycle

        # ── Entry logic ───────────────────────────────────────────────────────
        if guard.action in (GuardAction.PAUSE, GuardAction.REDUCE):
            return  # no new entries

        if combined.direction == 0:
            return

        if not self._state_machine.can_enter(symbol):
            return

        # Get symbol info for position sizing
        sym_info = self._client.get_symbol_info(symbol)
        if not sym_info:
            return

        # Get consecutive losses for risk dampening
        consec = self._db.get_consecutive_losses(symbol)

        # Detect trade mode (SCALP vs TREND_RIDE)
        mode_result = self._mode_detector.detect(
            regime=regime_r,
            ict_result=ict_r,
            bundle=sig_bundle,
            funding_rate=snap.funding_rate,
            spread_pct=snap.orderbook.spread_pct,
            direction=combined.direction,
        )

        # Calculate risk parameters
        risk_params = self._risk_engine.calculate(
            equity=equity,
            entry_price=current_price,
            side="Buy" if combined.direction == 1 else "Sell",
            atr=sig_bundle.atr_val,
            signal_strength=combined.strength,
            spread_pct=snap.orderbook.spread_pct,
            funding_rate=snap.funding_rate,
            vol_percentile=vol_pct,
            drawdown_pct=(peak_equity - equity) / peak_equity if peak_equity > 0 else 0,
            consecutive_losses=consec,
            symbol_max_leverage=sym_info.get("max_leverage", 100),
            symbol_info=sym_info,
            current_positions=self._state_machine.open_count(),
            trade_mode=mode_result.mode.value,
        )

        if not risk_params.risk_ok:
            log.debug("Risk check failed for %s: %s", symbol, risk_params.reason)
            return

        log.info(
            "Signal %s %s strength=%.2f regime=%s",
            symbol,
            "Buy" if combined.direction == 1 else "Sell",
            combined.strength,
            regime_r.regime.value,
        )

        # Execute entry
        self._execute_entry(symbol, combined.direction, risk_params, snap, sym_info, combined.strength)

    # ── Order Execution ───────────────────────────────────────────────────────

    def _execute_entry(
        self, symbol, direction, risk_params, snap, sym_info, signal_strength: float = 0.7
    ) -> None:
        order = self._order_mgr.open_position(
            symbol=symbol,
            direction=direction,
            risk_params=risk_params,
            spread_pct=snap.orderbook.spread_pct,
            signal_strength=signal_strength,
            current_price=snap.last_price,
            symbol_info=sym_info,
        )

        if order and order.status.value in ("FILLED", "PENDING"):
            entry_price = order.avg_price or snap.last_price
            trade_id = self._db.insert_trade_open(
                symbol=symbol,
                side="Buy" if direction == 1 else "Sell",
                entry_price=entry_price,
                qty=float(order.qty),
                leverage=risk_params.leverage,
                mode=settings.TRADING_MODE,
            )
            self._state_machine.enter(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                qty=float(order.qty),
                leverage=risk_params.leverage,
                stop_price=risk_params.stop_price,
                take_profit_price=risk_params.take_profit_price,
                trailing_distance=risk_params.trailing_stop_atr,
                order_id=order.client_id,
                reason=f"signal_strength={signal_strength:.2f}",
                tp1_price=risk_params.tp1_price,
                tp2_price=risk_params.tp2_price,
                trade_mode=risk_params.trade_mode,
                mode_max_bars=risk_params.mode_max_bars,
                partial_exit_pct=risk_params.partial_exit_pct,
            )
            # Store trade_id in state for later close update
            self._db.set_state(f"trade_id:{symbol}", trade_id)

            # Slack: 포지션 진입 알림
            self._slack.send_position_opened(
                symbol=symbol,
                side="Buy" if direction == 1 else "Sell",
                entry_price=entry_price,
                qty=float(order.qty),
                leverage=risk_params.leverage,
                stop_price=risk_params.stop_price,
                take_profit_price=risk_params.take_profit_price,
                signal_strength=signal_strength,
                order_type=order.order_type,
            )

    def _execute_partial_exit(self, symbol, pos, current_price, exit_signal, snap) -> None:
        """Close partial_exit_pct of the position at TP1; move stop to breakeven."""
        qty_to_close = pos.qty * pos.partial_exit_pct
        sym_info = self._client.get_symbol_info(symbol)
        direction = 1 if pos.is_long else -1

        order = self._order_mgr.close_position(
            symbol=symbol,
            direction=direction,
            qty=qty_to_close,
            current_price=current_price,
            symbol_info=sym_info or {},
            reason="PARTIAL_TP1",
        )

        if order:
            exit_price = order.avg_price or current_price
            pnl = self._state_machine.partial_exit(symbol, qty_to_close, exit_price)

            notional = qty_to_close * exit_price
            fee = notional * settings.BACKTEST_FEE_TAKER

            trade_id = self._db.get_state(f"trade_id:{symbol}")
            if trade_id:
                # Record partial close as a separate row
                partial_trade_id = self._db.insert_trade_open(
                    symbol=symbol,
                    side="Buy" if direction == 1 else "Sell",
                    entry_price=pos.entry_price,
                    qty=qty_to_close,
                    leverage=pos.leverage,
                    mode=settings.TRADING_MODE,
                )
                self._db.update_trade_close(
                    trade_id=partial_trade_id,
                    exit_price=exit_price,
                    pnl=pnl,
                    fee=fee,
                    reason="PARTIAL_TP1",
                )

            log.info(
                "Partial TP1 exit %s | closed=%.4f pnl=%.4f stop→breakeven",
                symbol, qty_to_close, pnl,
            )

    def _execute_exit(self, symbol, pos, current_price, reason, snap) -> None:
        sym_info = self._client.get_symbol_info(symbol)
        direction = 1 if pos.is_long else -1
        order = self._order_mgr.close_position(
            symbol=symbol,
            direction=direction,
            qty=pos.qty,
            current_price=current_price,
            symbol_info=sym_info or {},
            reason=reason,
        )

        if order:
            exit_price = order.avg_price or current_price
            pnl = self._state_machine.exit(symbol, exit_price)

            # Estimate fee
            notional = pos.qty * exit_price
            fee = notional * settings.BACKTEST_FEE_TAKER

            trade_id = self._db.get_state(f"trade_id:{symbol}")
            if trade_id:
                self._db.update_trade_close(
                    trade_id=trade_id,
                    exit_price=exit_price,
                    pnl=pnl,
                    fee=fee,
                    reason=reason,
                )

            side_str = "Buy" if direction == 1 else "Sell"
            entry = pos.entry_price
            pnl_pct = pnl / (entry * pos.qty) if entry > 0 and pos.qty > 0 else 0
            self._slack.send_position_closed(
                symbol=symbol,
                side=side_str,
                reason=reason,
                pnl=pnl,
                pnl_pct=pnl_pct,
            )

    def _emergency_close_all(self, symbols: List[str]) -> None:
        for symbol in symbols:
            pos = self._state_machine.get_position(symbol)
            if pos.is_flat:
                continue
            snap = self._collector.collect(symbol, limit=5)
            price = snap.last_price or pos.entry_price
            sym_info = self._client.get_symbol_info(symbol)
            self._execute_exit(symbol, pos, price, "RISK_GUARD_HALT", snap)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def get_dashboard_data(self) -> Dict:
        """Return a snapshot dict consumed by both send_dashboard() and the Slack bot."""
        equity = self._get_equity()
        initial_capital = settings.PAPER_CAPITAL_USDT
        daily_pnl = self._db.get_today_pnl()
        weekly_pnl = self._db.get_weekly_pnl()
        total_pnl = self._db.get_total_pnl(mode=settings.TRADING_MODE)
        trades_today = self._db.get_trades_count_today()
        win_rate = self._db.get_win_rate(days=30)

        positions: List[Dict] = []
        if settings.is_live():
            try:
                for p in self._client.get_positions():
                    if float(p.get("size", 0)) <= 0:
                        continue
                    positions.append({
                        "symbol": p.get("symbol", ""),
                        "side": p.get("side", "Buy"),
                        "qty": float(p.get("size", 0)),
                        "entry_price": float(p.get("avgPrice", 0)),
                        "mark_price": float(p.get("markPrice", 0)),
                        "unrealised_pnl": float(p.get("unrealisedPnl", 0)),
                        "leverage": int(float(p.get("leverage", 1))),
                    })
            except Exception as e:
                log.error("Dashboard: failed to fetch positions: %s", e)
        else:
            for symbol in (self._universe.get_or_refresh() or []):
                pos = self._state_machine.get_position(symbol)
                if pos.is_flat:
                    continue
                positions.append({
                    "symbol": symbol,
                    "side": "Buy" if pos.is_long else "Sell",
                    "qty": pos.qty,
                    "entry_price": pos.entry_price,
                    "mark_price": pos.entry_price,
                    "unrealised_pnl": pos.unrealised_pnl,
                    "leverage": pos.leverage,
                })

        return {
            "equity": equity,
            "initial_capital": initial_capital,
            "daily_pnl": daily_pnl,
            "weekly_pnl": weekly_pnl,
            "total_pnl": total_pnl,
            "positions": positions,
            "mode": settings.TRADING_MODE,
            "trades_today": trades_today,
            "win_rate": win_rate,
            "uptime_seconds": time.time() - self._start_ts,
            "last_cycle_ts": self._last_cycle_ts,
            "circuit_breaker_open": self._client._circuit.is_open,
        }

    def send_dashboard(self) -> None:
        """Scheduled push: collect data and post to Slack webhook."""
        try:
            data = self.get_dashboard_data()
            self._slack.send_dashboard(
                equity=data["equity"],
                initial_capital=data["initial_capital"],
                daily_pnl=data["daily_pnl"],
                total_pnl=data["total_pnl"],
                positions=data["positions"],
                mode=data["mode"],
            )
        except Exception as e:
            log.error("send_dashboard failed: %s", e)

    # ── Equity ────────────────────────────────────────────────────────────────

    def _get_equity(self) -> float:
        if settings.is_dry_run() or settings.is_paper():
            total_pnl = self._db.get_total_pnl(mode=settings.TRADING_MODE)
            return settings.PAPER_CAPITAL_USDT + total_pnl
        try:
            return self._client.get_usdt_equity()
        except Exception as e:
            log.error("Failed to get equity: %s", e)
            return settings.PAPER_CAPITAL_USDT

    # ── Position Sync ─────────────────────────────────────────────────────────

    def _sync_positions(self, symbols: List[str]) -> None:
        """Sync local state with exchange every cycle to detect liquidations."""
        try:
            exchange_positions = self._client.get_positions()
            exchange_by_symbol = {p.get("symbol"): p for p in exchange_positions if float(p.get("size", 0)) > 0}

            for symbol in symbols:
                local_pos = self._state_machine.get_position(symbol)
                sym_pos = [p for p in exchange_positions if p.get("symbol") == symbol]
                self._state_machine.sync_from_exchange(symbol, sym_pos)

                # Detect liquidation: local state says open, but exchange says flat
                if not local_pos.is_flat and symbol not in exchange_by_symbol:
                    log.error(
                        "LIQUIDATION DETECTED for %s — local=%s but exchange has no position",
                        symbol, local_pos.state.value,
                    )
                    # Record as liquidated trade
                    trade_id = self._db.get_state(f"trade_id:{symbol}")
                    if trade_id:
                        liq_price = local_pos.stop_price or local_pos.entry_price
                        pnl = (liq_price - local_pos.entry_price) * local_pos.qty
                        if local_pos.is_short:
                            pnl = (local_pos.entry_price - liq_price) * local_pos.qty
                        self._db.update_trade_close(
                            trade_id=trade_id,
                            exit_price=liq_price,
                            pnl=pnl,
                            fee=local_pos.qty * liq_price * settings.BACKTEST_FEE_TAKER,
                            reason="LIQUIDATION",
                        )
                    # Force local state to FLAT
                    self._state_machine.force_flat(symbol)
                    self._slack.send_risk_alert(
                        f"🚨 LIQUIDATION: `{symbol}` position was liquidated by exchange!"
                    )
        except Exception as e:
            log.warning("Position sync failed: %s", e)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("  Bybit Futures Bot — Starting")
    log.info("  Mode: %s | Testnet: %s", settings.TRADING_MODE, settings.BYBIT_TESTNET)
    log.info("=" * 60)

    bot = TradingBot()
    slack = SlackNotifier(settings.SLACK_WEBHOOK_URL)
    slack.send_startup(settings.TRADING_MODE)

    # ── Slack Bot (Socket Mode) ────────────────────────────────────────────────
    if settings.SLACK_BOT_TOKEN and settings.SLACK_APP_TOKEN:
        from src.api.slack_bot import SlackBotHandler
        slack_bot = SlackBotHandler(
            bot_token=settings.SLACK_BOT_TOKEN,
            app_token=settings.SLACK_APP_TOKEN,
            data_provider=bot.get_dashboard_data,
        )
        slack_bot.start()
    else:
        log.info(
            "Slack Bot disabled — set SLACK_BOT_TOKEN and SLACK_APP_TOKEN to enable slash commands"
        )

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # Bot cycle: every 5 minutes
    cycle_interval = int(settings.SIGNAL_TIMEFRAME) if int(settings.SIGNAL_TIMEFRAME) <= 60 else 5
    scheduler.add_job(
        bot.run_cycle,
        "interval",
        minutes=cycle_interval,
        id="bot_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    # Universe refresh: every 4 hours
    scheduler.add_job(
        bot._universe.refresh,
        "interval",
        hours=settings.UNIVERSE_REFRESH_INTERVAL_HOURS,
        id="universe_refresh",
    )

    # Daily briefing: 9 AM KST
    scheduler.add_job(
        bot._briefing.send,
        CronTrigger(hour=settings.SLACK_DAILY_REPORT_HOUR, minute=0, timezone="Asia/Seoul"),
        id="daily_briefing",
    )

    # Real-time dashboard: every N minutes (0 = disabled)
    if settings.SLACK_DASHBOARD_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            bot.send_dashboard,
            "interval",
            minutes=settings.SLACK_DASHBOARD_INTERVAL_MINUTES,
            id="dashboard",
            max_instances=1,
            coalesce=True,
        )
        log.info(
            "Dashboard enabled: every %d min via Slack",
            settings.SLACK_DASHBOARD_INTERVAL_MINUTES,
        )

    scheduler.start()
    log.info("Scheduler started. Running first cycle immediately...")

    # Run first cycle immediately, then send initial dashboard snapshot
    try:
        bot.run_cycle()
    except Exception as e:
        log.exception("First cycle failed: %s", e)

    try:
        bot.send_dashboard()
    except Exception as e:
        log.exception("Initial dashboard failed: %s", e)

    # Graceful shutdown
    def _shutdown(signum, frame):
        log.info("Shutdown signal received")
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        except Exception:
            pass
        slack.send_shutdown("signal")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Bot running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        # Signal handler (_shutdown) already ran — just exit cleanly
        pass


if __name__ == "__main__":
    main()
