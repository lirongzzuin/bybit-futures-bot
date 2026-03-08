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
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

_KST = timezone(timedelta(hours=9))

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
from src.strategy.macro_regime import MacroRegimeDetector, MacroRegime
from src.strategy.regime import RegimeDetector, Regime
from src.strategy.scalp import TradeModeDetector
from src.strategy.scalping import ScalpingStrategy
from src.strategy.trend_ema import TrendEMAStrategy
from src.strategy.signals import SignalGenerator
from src.strategy.regime_router import RegimeRouter, DynamicRegime

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
        self._scalp_strategy = ScalpingStrategy(settings)
        self._trend_strategy = TrendEMAStrategy(settings)
        self._regime_router  = RegimeRouter(settings)
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

        # ── Macro Regime ───────────────────────────────────────────────────────
        self._macro_detector = MacroRegimeDetector(self._client, settings)
        # 현재 확정된 매크로 레짐: "BULL" | "BEAR_SIDEWAYS"
        self._macro_mode: str = "BEAR_SIDEWAYS"  # 기본값: 보수적 (BTC/ETH only)
        # 연속 신호 카운터 (확정 전환을 위한 디바운스)
        self._macro_bull_streak: int = 0    # 연속 BULL 신호 횟수
        self._macro_bear_streak: int = 0    # 연속 non-BULL 신호 횟수

        # Auto regime router per-symbol cache (STRATEGY_MODE=auto)
        self._auto_risk_multiplier: Dict[str, float] = {}
        self._auto_leverage_cap: Dict[str, int] = {}
        self._auto_direction_bias: Dict[str, int] = {}

        # Paper capital tracking
        self._paper_equity = settings.PAPER_CAPITAL_USDT

        # Dashboard tracking
        self._start_ts: float = time.time()
        self._last_cycle_ts: float = 0.0

        # Guard alert 디바운스: KST 날짜별로 한 번만 Slack 알림 전송
        self._daily_loss_alert_sent_date: str = ""

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
        # ── 1. Refresh universe + 매크로 레짐 필터링 ──────────────────────────
        symbols = self._universe.get_or_refresh()
        if not symbols:
            log.warning("Empty universe — skipping cycle")
            return

        if self._universe.needs_refresh():
            log.info("Universe refreshed: %s", symbols)

        # 매크로 레짐에 따라 거래 심볼 필터링
        symbols = self._filter_symbols_by_macro_regime(symbols)

        # ── 2. Get account state ──────────────────────────────────────────────
        equity = self._get_equity()
        daily_pnl = self._db.get_today_pnl()
        weekly_pnl = self._db.get_weekly_pnl()
        peak_equity = self._db.get_peak_equity(settings.TRADING_MODE) or equity

        # ── 3. Sync positions from exchange every cycle to detect liquidations ──
        # Paper 모드는 거래소에 실제 포지션이 없으므로 sync 시 모두 청산으로 오감지됨
        # → live 모드에서만 sync 실행
        if settings.is_live():
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
        # ── 전략별 데이터 수집 및 신호 생성 ──────────────────────────────────
        if settings.STRATEGY_MODE == "scalp":
            result = self._collect_and_signal_scalp(symbol)
        elif settings.STRATEGY_MODE == "trend":
            result = self._collect_and_signal_trend(symbol)
        elif settings.STRATEGY_MODE == "auto":
            result = self._collect_and_signal_auto(symbol)
        else:
            result = self._collect_and_signal_swing(symbol)

        if result is None:
            return

        snap, combined, atr_val, regime_r, trade_mode_str = result
        if snap.ohlcv.empty:
            return

        df = snap.ohlcv
        current_price = snap.last_price

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

        # ── Daily loss Slack 알림 (하루 한 번)
        if guard.reason and "Daily loss" in guard.reason:
            today_kst = datetime.now(tz=_KST).date().isoformat()
            if self._daily_loss_alert_sent_date != today_kst:
                self._daily_loss_alert_sent_date = today_kst
                try:
                    loss_pct = float(guard.reason.split("%")[0].split()[-1]) / 100
                except Exception:
                    loss_pct = settings.MAX_DAILY_LOSS_PCT
                self._slack.send_daily_loss_limit(loss_pct)
                log.info("Daily loss 알림 전송 — 거래는 계속 진행 (복리 모드)")

        # ── Check existing position ───────────────────────────────────────────
        pos = self._state_machine.get_position(symbol)

        if not pos.is_flat:
            # 보유 봉 수 계산 (전략별 타임프레임 기준)
            bar_minutes = int(settings.SCALP_PURE_LTF_TIMEFRAME) if settings.STRATEGY_MODE in ("scalp", "auto") \
                else int(settings.SIGNAL_TIMEFRAME)
            bars_in_trade = int((time.time() - pos.entry_ts) / (bar_minutes * 60))

            # Detect regime flip (adversely against position)
            regime_flip = (
                (pos.is_long and regime_r.regime == Regime.TRENDING_DOWN) or
                (pos.is_short and regime_r.regime == Regime.TRENDING_UP)
            )

            exit_check = self._state_machine.check_exit(
                symbol=symbol,
                current_price=current_price,
                current_atr=atr_val,
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
        if guard.action == GuardAction.PAUSE:
            log.debug("Guard PAUSE: %s — 신규 진입 건너뜀", guard.reason)
            return

        # ── Recovery mode: REDUCE 구간에서 저품질 신호 거부 ─────────────────
        # heavy reduce (>60%): score 7+ (strength ≥ 0.77) 신호만 허용
        # 드로우다운 회복 중에는 최고품질 신호에만 소규모 진입
        if guard.action == GuardAction.REDUCE and guard.reduce_pct > 0.60:
            if combined.strength < 0.77:
                log.debug(
                    "Recovery REDUCE mode: skipping weak signal "
                    "(strength=%.2f reduce_pct=%.2f)",
                    combined.strength, guard.reduce_pct,
                )
                return

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

        # Calculate risk parameters
        is_bear_mode = (self._macro_mode == "BEAR_SIDEWAYS")
        risk_params = self._risk_engine.calculate(
            equity=equity,
            entry_price=current_price,
            side="Buy" if combined.direction == 1 else "Sell",
            atr=atr_val,
            signal_strength=combined.strength,
            spread_pct=snap.orderbook.spread_pct,
            funding_rate=snap.funding_rate,
            vol_percentile=vol_pct,
            drawdown_pct=(peak_equity - equity) / peak_equity if peak_equity > 0 else 0,
            consecutive_losses=consec,
            symbol_max_leverage=sym_info.get("max_leverage", 100),
            symbol_info=sym_info,
            current_positions=self._state_machine.open_count(),
            trade_mode=trade_mode_str,
            bear_mode=is_bear_mode,
        )

        if not risk_params.risk_ok:
            log.debug("Risk check failed for %s: %s", symbol, risk_params.reason)
            return

        # ── Auto mode: 레짐 라우터 리스크 스케일 적용 ────────────────────────
        # direction_bias 필터 (이미 _collect_and_signal_auto에서 1차 필터링하지만
        # 여기서도 방어적으로 체크)
        if settings.STRATEGY_MODE == "auto":
            auto_bias = self._auto_direction_bias.get(symbol, 0)
            if auto_bias == +1 and combined.direction == -1:
                log.debug("Auto regime: SHORT 신호 차단 (LONG-only 레짐) %s", symbol)
                return
            elif auto_bias == -1 and combined.direction == +1:
                log.debug("Auto regime: LONG 신호 차단 (SHORT-only 레짐) %s", symbol)
                return

            rm = self._auto_risk_multiplier.get(symbol, 1.0)
            lc = self._auto_leverage_cap.get(symbol, int(settings.MAX_LEVERAGE_CAP))

            if rm < 1.0:
                risk_params.qty_base           *= rm
                risk_params.position_size_usdt *= rm
                risk_params.margin_usdt        *= rm
                risk_params.max_loss_usdt      *= rm

            if lc < risk_params.leverage:
                risk_params.leverage = lc

        # ── Recovery mode: 포지션 비중 축소 적용 ────────────────────────────
        # REDUCE 가드 발동 시 계산된 포지션 사이즈를 비례 축소
        # risk_engine은 정상 사이즈로 계산 → 여기서 실제 비중 조정
        if guard.action == GuardAction.REDUCE and guard.reduce_pct > 0:
            scale = max(0.10, 1.0 - guard.reduce_pct)  # 최소 10% 유지
            risk_params.qty_base *= scale
            risk_params.position_size_usdt *= scale
            risk_params.margin_usdt *= scale
            risk_params.max_loss_usdt *= scale
            log.info(
                "Recovery REDUCE mode: %s position scaled to %.0f%% "
                "(dd_reduce_pct=%.2f)",
                symbol, scale * 100, guard.reduce_pct,
            )

        log.info(
            "Signal %s %s strength=%.2f regime=%s strategy=%s",
            symbol,
            "Buy" if combined.direction == 1 else "Sell",
            combined.strength,
            regime_r.regime.value,
            settings.STRATEGY_MODE,
        )

        # Execute entry
        self._execute_entry(symbol, combined.direction, risk_params, snap, sym_info, combined.strength)

    # ── Strategy Signal Methods ────────────────────────────────────────────────

    def _collect_and_signal_swing(self, symbol: str):
        """
        스윙/추세추종 전략: 15분봉 데이터 수집 + 복합 신호 생성.
        Returns (snap, combined, atr_val, regime_r, trade_mode_str) or None
        """
        snap = self._collector.collect(symbol, limit=250)
        if snap.ohlcv.empty or len(snap.ohlcv) < 100:
            log.warning("Insufficient data for %s (%d bars)", symbol, len(snap.ohlcv))
            return None

        df = snap.ohlcv

        # Regime detection
        regime_df = snap.trend_ohlcv if (
            snap.trend_ohlcv is not None and len(snap.trend_ohlcv) >= 60
        ) else df
        regime_r = self._regime.detect(regime_df, oi_change_pct=snap.oi_change_pct)

        # ICT + signal generation
        ict_r = self._ict.analyze(df)
        sig_bundle = self._sig_gen.generate(
            df, ict_r,
            funding_rate=snap.funding_rate,
            oi_change_pct=snap.oi_change_pct,
            spread_pct=snap.orderbook.spread_pct,
            vwap=snap.vwap,
            vwap_distance_pct=snap.vwap_distance_pct,
            orderbook_imbalance=snap.orderbook.imbalance,
            htf_df=snap.trend_ohlcv,
        )
        combined = self._combiner.combine(sig_bundle, regime_r)

        # Trade mode (SCALP or TREND_RIDE) detection
        mode_result = self._mode_detector.detect(
            regime=regime_r,
            ict_result=ict_r,
            bundle=sig_bundle,
            funding_rate=snap.funding_rate,
            spread_pct=snap.orderbook.spread_pct,
            direction=combined.direction,
        )
        trade_mode_str = mode_result.mode.value

        return snap, combined, sig_bundle.atr_val, regime_r, trade_mode_str

    def _collect_and_signal_scalp(self, symbol: str):
        """
        순수 스캘핑 전략: 3분봉(LTF) + 15분봉(HTF) 데이터 수집 + 스캘핑 신호 생성.
        Returns (snap, combined, atr_val, regime_r, trade_mode_str) or None
        """
        from src.strategy.regime import RegimeResult

        snap = self._collector.collect_for_scalp(symbol, limit=500)
        if snap.ohlcv.empty or len(snap.ohlcv) < 50:
            log.warning("Scalp: Insufficient LTF data for %s (%d bars)", symbol, len(snap.ohlcv))
            return None

        # 스캘핑 신호 생성
        combined, atr_val = self._scalp_strategy.generate(
            ltf_df=snap.ohlcv,
            htf_df=snap.trend_ohlcv,
            vwap_val=snap.vwap,
            funding_rate=snap.funding_rate,
            spread_pct=snap.orderbook.spread_pct,
            oi_change_pct=snap.oi_change_pct,
        )

        # 스캘핑 모드에서 RegimeResult 합성 (check_exit의 regime_flip에 사용)
        regime_r = RegimeResult(
            regime=combined.regime,
            confidence=combined.strength,
            adx_val=0.0,
            plus_di=0.0,
            minus_di=0.0,
            ema_aligned_up=(combined.direction == 1),
            ema_aligned_down=(combined.direction == -1),
            vol_percentile=0.5,
            volume_ratio=1.0,
            trend_score=combined.long_score - combined.short_score,
        )

        # 스캘핑 전략은 항상 PURE_SCALP 모드 사용 (타이트한 ATR 파라미터)
        trade_mode_str = "PURE_SCALP"

        return snap, combined, atr_val, regime_r, trade_mode_str

    def _collect_and_signal_trend(self, symbol: str):
        """
        Trend EMA 전략: Phase-locked Direction System.
        3분봉(LTF) + 15분봉(HTF) 데이터로 장세 판단 후 방향 고정 진입.

        - BULL_TREND: LONG만 허용 (EMA21>50>200 + ADX 강세)
        - BEAR_TREND: SHORT만 허용 (EMA21<50<200 + ADX 강세)
        - SIDEWAYS: 볼린저밴드 극단 양방향 (더 높은 점수 요구)

        Returns (snap, combined, atr_val, regime_r, trade_mode_str) or None
        """
        from src.strategy.regime import RegimeResult

        snap = self._collector.collect_for_scalp(symbol, limit=500)
        if snap.ohlcv.empty or len(snap.ohlcv) < 50:
            log.warning("Trend: Insufficient LTF data for %s (%d bars)", symbol, len(snap.ohlcv))
            return None

        combined, atr_val = self._trend_strategy.generate(
            ltf_df=snap.ohlcv,
            htf_df=snap.trend_ohlcv,
            vwap_val=snap.vwap,
            funding_rate=snap.funding_rate,
            spread_pct=snap.orderbook.spread_pct,
            oi_change_pct=snap.oi_change_pct,
        )

        from src.strategy.regime import Regime
        regime_r = RegimeResult(
            regime=combined.regime,
            confidence=combined.strength,
            adx_val=0.0,
            plus_di=0.0,
            minus_di=0.0,
            ema_aligned_up=(combined.direction == 1),
            ema_aligned_down=(combined.direction == -1),
            vol_percentile=0.5,
            volume_ratio=1.0,
            trend_score=combined.long_score - combined.short_score,
        )

        # TRENDING 국면은 SCALP(TP1=2.5×ATR, stop=1.2×ATR, max=48봉)으로 추세 타기
        # RANGING 국면은 PURE_SCALP(TP1=1.5×ATR, stop=1.0×ATR)으로 타이트 mean reversion
        trade_mode_str = "SCALP" if combined.regime != Regime.RANGING else "PURE_SCALP"
        return snap, combined, atr_val, regime_r, trade_mode_str

    def _collect_and_signal_auto(self, symbol: str):
        """
        Dynamic Regime Router 모드 — 매 사이클 장세를 자동 감지하고 최적 전략을 선택.

        장세별 라우팅:
          STRONG_BULL → TrendEMA, LONG only, TREND_RIDE (넓은 TP로 추세 극대화)
          BULL        → TrendEMA, LONG only, SCALP (빠른 TP)
          SIDEWAYS    → Scalping, 양방향, PURE_SCALP (mean reversion)
          BEAR        → TrendEMA, SHORT only, SCALP
          STRONG_BEAR → TrendEMA, SHORT only, TREND_RIDE
          VOLATILE    → Scalping, 양방향, PURE_SCALP (사이즈 50% 축소)
          TRANSITION  → 진입 없음 (레짐 전환 확정 중)

        Returns (snap, combined, atr_val, regime_r, trade_mode_str) or None
        """
        from src.strategy.regime import RegimeResult, Regime

        snap = self._collector.collect_for_scalp(symbol, limit=500)
        if snap.ohlcv.empty or len(snap.ohlcv) < 50:
            log.warning("Auto: LTF 데이터 부족 %s (%d봉)", symbol, len(snap.ohlcv))
            return None

        # ── 레짐 감지 ────────────────────────────────────────────────────────
        is_macro_bear = (self._macro_mode == "BEAR_SIDEWAYS")
        router_result = self._regime_router.detect(
            symbol=symbol,
            mtf_df=snap.trend_ohlcv,
            macro_bear=is_macro_bear,
        )

        log.info(
            "Auto regime %s: %s → strategy=%s mode=%s bias=%+d "
            "risk_mult=%.2f lev_cap=%d (conf=%.2f | %s)",
            symbol, router_result.regime.value,
            router_result.strategy, router_result.trade_mode,
            router_result.direction_bias,
            router_result.risk_multiplier, router_result.leverage_cap,
            router_result.confidence, router_result.reason,
        )

        # TRANSITION: 레짐 전환 확정 중 → 진입 없음
        if router_result.strategy == "skip":
            return None

        # ── 심볼별 리스크 파라미터 저장 (_process_symbol에서 사용) ─────────────
        self._auto_risk_multiplier[symbol] = router_result.risk_multiplier
        self._auto_leverage_cap[symbol]    = router_result.leverage_cap
        self._auto_direction_bias[symbol]  = router_result.direction_bias

        # ── 전략 라우팅 ───────────────────────────────────────────────────────
        if router_result.strategy == "trend":
            combined, atr_val = self._trend_strategy.generate(
                ltf_df=snap.ohlcv,
                htf_df=snap.trend_ohlcv,
                vwap_val=snap.vwap,
                funding_rate=snap.funding_rate,
                spread_pct=snap.orderbook.spread_pct,
                oi_change_pct=snap.oi_change_pct,
            )
        else:
            # scalp
            combined, atr_val = self._scalp_strategy.generate(
                ltf_df=snap.ohlcv,
                htf_df=snap.trend_ohlcv,
                vwap_val=snap.vwap,
                funding_rate=snap.funding_rate,
                spread_pct=snap.orderbook.spread_pct,
                oi_change_pct=snap.oi_change_pct,
            )

        # ── 방향 바이어스 1차 필터 ────────────────────────────────────────────
        if router_result.direction_bias != 0 and combined.direction != 0:
            if combined.direction != router_result.direction_bias:
                log.debug(
                    "Auto: %s %s 신호 차단 (레짐=%s, bias=%+d)",
                    symbol,
                    "LONG" if combined.direction == 1 else "SHORT",
                    router_result.regime.value,
                    router_result.direction_bias,
                )
                return None

        # ── regime_r 생성 (check_exit 레짐플립 감지용) ───────────────────────
        if combined.direction == 1:
            regime_obj = Regime.TRENDING_UP
        elif combined.direction == -1:
            regime_obj = Regime.TRENDING_DOWN
        else:
            regime_obj = Regime.RANGING

        regime_r = RegimeResult(
            regime=regime_obj,
            confidence=router_result.confidence,
            adx_val=0.0,
            plus_di=0.0,
            minus_di=0.0,
            ema_aligned_up=(router_result.direction_bias == +1),
            ema_aligned_down=(router_result.direction_bias == -1),
            vol_percentile=0.5,
            volume_ratio=1.0,
            trend_score=float(router_result.direction_bias),
        )

        # trade_mode는 레짐 라우터 테이블에서 결정 (전략 자체가 아니라)
        return snap, combined, atr_val, regime_r, router_result.trade_mode

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
            qty = float(order.qty)
            # 진입 수수료: Market 주문 → Taker(0.055%), Limit/GTC → Maker(0.020%)
            entry_fee_rate = (
                settings.BACKTEST_FEE_TAKER
                if order.order_type == "Market"
                else settings.BACKTEST_FEE_MAKER
            )
            entry_fee = qty * entry_price * entry_fee_rate
            trade_id = self._db.insert_trade_open(
                symbol=symbol,
                side="Buy" if direction == 1 else "Sell",
                entry_price=entry_price,
                qty=qty,
                leverage=risk_params.leverage,
                mode=settings.TRADING_MODE,
            )
            self._state_machine.enter(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                qty=qty,
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
                entry_fee=entry_fee,
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
            partial_pct = pos.partial_exit_pct

            # 진입 수수료 pro-rata: partial_exit() 호출 전에 캡처
            # (partial_exit 내에서 pos.entry_fee가 감소하기 때문)
            entry_fee_for_partial = pos.entry_fee * partial_pct

            pnl = self._state_machine.partial_exit(
                symbol, qty_to_close, exit_price, partial_pct=partial_pct
            )

            notional = qty_to_close * exit_price
            # PARTIAL_TP1: Limit(Maker) 청산 수수료 + 진입 수수료 pro-rata
            partial_exit_fee = notional * settings.BACKTEST_FEE_MAKER
            fee = partial_exit_fee + entry_fee_for_partial
            pnl_net = pnl - fee

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
                    pnl=pnl_net,   # net PnL (수수료 차감 후)
                    fee=fee,       # 진입 pro-rata + 청산 수수료 합산
                    reason="PARTIAL_TP1",
                )

            log.info(
                "Partial TP1 exit %s | closed=%.4f gross_pnl=%.4f fee=%.4f "
                "net_pnl=%.4f stop→breakeven",
                symbol, qty_to_close, pnl, fee, pnl_net,
            )
            # TP1 부분청산은 Slack 알림 없음 (노이즈 감소 — /dashboard 로 확인)

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

            # 남은 포지션 정보 캡처 (exit() 호출 전에 — exit()는 레버리지/qty를 초기화함)
            remaining_qty = pos.qty
            entry = pos.entry_price
            pos_leverage = pos.leverage              # 레버리지 캡처 (exit 후 1로 초기화됨)
            remaining_entry_fee = pos.entry_fee  # partial_exit 후 남은 진입 수수료

            pnl = self._state_machine.exit(symbol, exit_price)

            # Fee rate: STOP_LOSS/GUARD*/LIQUIDATION* → 시장가(Taker, 0.055%)
            # TRAILING_STOP/TIME_EXIT/REGIME_CHANGE/SIGNAL_FLIP → 지정가(Maker, 0.020%)
            # 주의: "STOP" substring은 "TRAILING_STOP"도 매칭되므로 exact match 사용
            notional = remaining_qty * exit_price
            exit_fee_rate = (
                settings.BACKTEST_FEE_TAKER
                if (reason == "STOP_LOSS" or "GUARD" in reason or "LIQUIDATION" in reason)
                else settings.BACKTEST_FEE_MAKER
            )
            exit_fee = notional * exit_fee_rate
            # 진입 수수료(남은 비율) + 청산 수수료 합산 → 완전한 round-trip 수수료
            total_fee = remaining_entry_fee + exit_fee
            pnl_net = pnl - total_fee

            trade_id = self._db.get_state(f"trade_id:{symbol}")
            if trade_id:
                self._db.update_trade_close(
                    trade_id=trade_id,
                    exit_price=exit_price,
                    pnl=pnl_net,    # net PnL (수수료 차감 후)
                    fee=total_fee,  # 진입 + 청산 수수료 합산
                    reason=reason,
                )

            log.info(
                "Exit %s | reason=%s lev=%d× gross_pnl=%.4f fee=%.4f "
                "(entry_fee=%.4f exit_fee=%.4f) net_pnl=%.4f",
                symbol, reason, pos_leverage, pnl, total_fee,
                remaining_entry_fee, exit_fee, pnl_net,
            )

            side_str = "Buy" if direction == 1 else "Sell"
            # PnL%: 실제 투입 증거금(마진) 대비 수익률 계산
            # margin = notional / leverage → 실제 계좌에서 사용된 자본
            margin_used = (entry * remaining_qty) / pos_leverage if pos_leverage > 0 else (entry * remaining_qty)
            pnl_pct = pnl_net / margin_used if margin_used > 0 else 0
            self._slack.send_position_closed(
                symbol=symbol,
                side=side_str,
                reason=reason,
                pnl=pnl_net,
                pnl_pct=pnl_pct,
                entry_price=entry,
                exit_price=exit_price,
                leverage=pos_leverage,
            )

    # ── Macro Regime ──────────────────────────────────────────────────────────

    def _filter_symbols_by_macro_regime(self, symbols: List[str]) -> List[str]:
        """
        매크로 레짐에 따라 거래 심볼을 필터링한다.
        - BULL 모드: 전체 유니버스 심볼 허용 (멀티코인 스캘핑)
        - BEAR/SIDEWAYS 모드: BTC + ETH만 허용 (고배율 스캘핑)
        """
        if self._macro_mode == "BULL":
            # BULL 모드 심볼 설정이 있으면 그것만, 없으면 전체 유니버스
            if settings.BULL_MODE_SYMBOLS:
                allowed = [s.strip() for s in settings.BULL_MODE_SYMBOLS.split(",") if s.strip()]
                filtered = [s for s in symbols if s in allowed]
                return filtered if filtered else symbols
            return symbols
        else:
            # BEAR/SIDEWAYS 모드: BTC + ETH만
            allowed = [s.strip() for s in settings.BEAR_MODE_SYMBOLS.split(",") if s.strip()]
            # 현재 포지션 보유 심볼은 항상 포함 (청산 관리 위해)
            has_position = [s for s in symbols if not self._state_machine.get_position(s).is_flat]
            target = set(allowed) | set(has_position)
            # 유니버스에 포함된 심볼 우선, BTC/ETH는 유니버스 여부 무관하게 항상 포함
            result = [s for s in symbols if s in target]
            # allowed에 있지만 universe에 없는 심볼 추가 (BTC/ETH 보장)
            for s in allowed:
                if s not in result:
                    result.append(s)
            return result if result else allowed

    def update_macro_regime(self) -> None:
        """
        매크로 레짐을 감지하고, 확정 조건 충족 시 모드를 전환한다.
        스케줄러에 의해 MACRO_REGIME_CHECK_HOURS마다 호출됨.
        """
        try:
            result = self._macro_detector.detect()
            is_bull_signal = (result.regime == MacroRegime.BULL)

            prev_mode = self._macro_mode

            if is_bull_signal:
                self._macro_bull_streak += 1
                self._macro_bear_streak = 0
            else:
                self._macro_bear_streak += 1
                self._macro_bull_streak = 0

            # BULL 전환: 연속 N회 BULL 신호
            if (self._macro_bull_streak >= settings.MACRO_BULL_CONFIRM_COUNT
                    and self._macro_mode != "BULL"):
                self._macro_mode = "BULL"
                log.info(
                    "Macro regime → BULL (streak=%d, confidence=%.2f)",
                    self._macro_bull_streak, result.confidence,
                )
                lev_cap = settings.MAX_LEVERAGE_CAP
                allowed_syms = (
                    [s.strip() for s in settings.BULL_MODE_SYMBOLS.split(",") if s.strip()]
                    or ["(전체 유니버스)"]
                )
                self._slack.send_market_mode_changed(
                    from_mode=prev_mode,
                    to_mode="BULL",
                    reason=result.reason,
                    symbols=allowed_syms,
                    leverage_cap=lev_cap,
                )

            # BEAR/SIDEWAYS 전환: 연속 N회 non-BULL 신호
            elif (self._macro_bear_streak >= settings.MACRO_BEAR_CONFIRM_COUNT
                    and self._macro_mode != "BEAR_SIDEWAYS"):
                self._macro_mode = "BEAR_SIDEWAYS"
                log.info(
                    "Macro regime → BEAR_SIDEWAYS (streak=%d, regime=%s, confidence=%.2f)",
                    self._macro_bear_streak, result.regime.value, result.confidence,
                )
                bear_syms = [s.strip() for s in settings.BEAR_MODE_SYMBOLS.split(",") if s.strip()]
                self._slack.send_market_mode_changed(
                    from_mode=prev_mode,
                    to_mode=result.regime.value,  # "BEAR" 또는 "SIDEWAYS"
                    reason=result.reason,
                    symbols=bear_syms,
                    leverage_cap=settings.BEAR_MAX_LEVERAGE_CAP,
                )

            log.debug(
                "Macro regime check: %s (bull_streak=%d bear_streak=%d) current_mode=%s",
                result.regime.value,
                self._macro_bull_streak,
                self._macro_bear_streak,
                self._macro_mode,
            )
        except Exception as e:
            log.error("update_macro_regime failed: %s", e)

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

    # Bot cycle: 스캘핑/트렌드는 LTF 타임프레임, 스윙은 SIGNAL_TIMEFRAME 기준
    if settings.STRATEGY_MODE in ("scalp", "trend"):
        cycle_interval = int(settings.SCALP_PURE_LTF_TIMEFRAME)  # 3분 기본
    else:
        cycle_interval = int(settings.SIGNAL_TIMEFRAME) if int(settings.SIGNAL_TIMEFRAME) <= 60 else 5
    log.info(
        "Scheduler cycle: %d min (strategy=%s)",
        cycle_interval, settings.STRATEGY_MODE,
    )
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

    # Macro regime check: MACRO_REGIME_CHECK_HOURS마다 BTC 일봉 기반 시장 상황 업데이트
    scheduler.add_job(
        bot.update_macro_regime,
        "interval",
        hours=settings.MACRO_REGIME_CHECK_HOURS,
        id="macro_regime_check",
    )

    # Briefing: 09:00 KST + 21:00 KST (하루 2회)
    # /dashboard 명령어로 수시 확인 가능 → 자동 푸시 최소화
    scheduler.add_job(
        bot._briefing.send,
        CronTrigger(hour="9,21", minute=0, timezone="Asia/Seoul"),
        id="periodic_briefing",
    )

    scheduler.start()
    log.info("Scheduler started. Running first cycle immediately...")

    # 첫 번째 사이클 전에 매크로 레짐 초기 감지
    try:
        log.info("Initial macro regime detection...")
        bot.update_macro_regime()
    except Exception as e:
        log.exception("Initial macro regime detection failed: %s", e)

    # Run first cycle immediately
    try:
        bot.run_cycle()
    except Exception as e:
        log.exception("First cycle failed: %s", e)

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
