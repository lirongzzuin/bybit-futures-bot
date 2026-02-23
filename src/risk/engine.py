"""
Risk Engine — dynamic leverage and position sizing.

Design principles:
  1. 1-trade max loss is always bounded: equity × MAX_RISK_PER_TRADE_PCT
  2. Leverage is a derived quantity, not an input
  3. Multiple dampening factors prevent over-leveraging in adverse conditions
  4. Hard caps prevent edge-case blow-ups

Additional sizing methods (both used as soft caps):
  - AQR Volatility Targeting: scale position so daily P&L vol ≈ target
  - Quarter-Kelly: f* × 0.25 fraction of equity as stop-distance sizing cap

References:
  - "Volatility Targeting" (Moreira & Muir 2017, JF)
  - AQR "Managed Futures" research (publicly available)
  - Van K. Tharp "Trade Your Way to Financial Freedom" (Quarter-Kelly)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from src.config import Settings
from src.logger import get_logger

log = get_logger(__name__)


@dataclass
class RiskParameters:
    """Output of the risk engine for a single trade opportunity."""
    leverage: int              # final leverage to set on exchange
    position_size_usdt: float  # notional value of the position
    margin_usdt: float         # required margin = notional / leverage
    qty_base: float            # quantity in base coin (position_size_usdt / price)
    stop_price: float          # recommended stop-loss price
    take_profit_price: float   # recommended take-profit price (TP2 after TP1 hit)
    tp1_price: float = 0.0     # first partial take-profit target
    tp2_price: float = 0.0     # second take-profit target (full close)
    trailing_stop_atr: float = 0.0  # ATR for trailing stop
    max_loss_usdt: float = 0.0      # max expected loss on this trade
    risk_ok: bool = True            # False if risk checks failed (no trade)
    reason: str = ""           # why risk_ok is False
    trade_mode: str = "SCALP"  # SCALP or TREND_RIDE
    partial_exit_pct: float = 0.60
    mode_max_bars: int = 15
    vol_target_size: float = 0.0   # AQR vol-targeting position size (informational)
    kelly_size: float = 0.0        # Quarter-Kelly position size cap (informational)


class RiskEngine:
    def __init__(self, settings: Settings):
        self._s = settings

    def calculate(
        self,
        equity: float,
        entry_price: float,
        side: str,              # "Buy" | "Sell"
        atr: float,
        signal_strength: float,
        spread_pct: float,
        funding_rate: float,
        vol_percentile: float,  # 0–1
        drawdown_pct: float,    # current drawdown as fraction
        consecutive_losses: int,
        symbol_max_leverage: int,
        symbol_info: dict,
        current_positions: int,
        trade_mode: Optional[str] = None,  # "SCALP" or "TREND_RIDE"
        recent_close: Optional[pd.Series] = None,  # for AQR vol-targeting
        win_stats: Optional[dict] = None,           # for quarter-Kelly cap
        bear_mode: bool = False,  # True = BEAR/SIDEWAYS → BEAR_MAX_LEVERAGE_CAP 사용
    ) -> RiskParameters:

        # ── Basic feasibility checks ─────────────────────────────────────────
        if equity <= 0:
            return self._no_trade("Equity <= 0")

        if current_positions >= self._s.MAX_CONCURRENT_POSITIONS:
            return self._no_trade(f"Max concurrent positions reached ({current_positions})")

        if atr <= 0 or entry_price <= 0:
            return self._no_trade("Invalid ATR or entry price")

        # ── Resolve trade mode and ATR multipliers ────────────────────────────
        resolved_mode = trade_mode or "SCALP"
        if resolved_mode == "TREND_RIDE":
            stop_atr_mult = self._s.TREND_STOP_ATR
            tp1_atr_mult = self._s.TREND_TP1_ATR
            tp2_atr_mult = self._s.TREND_TP2_ATR
            partial_exit_pct = self._s.TREND_PARTIAL_EXIT_PCT
            mode_max_bars = self._s.TREND_MAX_BARS
        else:
            stop_atr_mult = self._s.SCALP_STOP_ATR
            tp1_atr_mult = self._s.SCALP_TP1_ATR
            tp2_atr_mult = self._s.SCALP_TP2_ATR
            partial_exit_pct = self._s.SCALP_PARTIAL_EXIT_PCT
            mode_max_bars = self._s.SCALP_MAX_BARS

        # ── Signal-driven leverage escalation ─────────────────────────────────
        # 핵심 원리: base_leverage = trade_risk_pct / stop_distance_pct
        #   → 강한 시그널: 타이트 스탑(stop_atr_scale↓) + 높은 리스크%(trade_risk↑)
        #   → 두 효과가 곱해져 50× 레버리지 자연 달성
        #
        # 예시 (BTC, ATR=0.5%, SCALP_STOP=1.5×):
        #   strength=0.85: stop=0.25×1.5×0.5%=0.1875%, risk=10% → lev=10%/0.1875%=53× ✓
        #   strength=0.80: stop=0.40×1.5×0.5%=0.3000%, risk= 6% → lev= 6%/0.3000%=20×
        #   strength=0.75: stop=0.60×1.5×0.5%=0.4500%, risk= 3% → lev= 3%/0.4500%= 7×
        #   strength=0.65: stop=0.80×1.5×0.5%=0.6000%, risk=2.5%→ lev=2.5%/0.6000%= 4×
        #   default:       stop=1.00×1.5×0.5%=0.7500%, risk=1.5%→ lev=1.5%/0.7500%= 2×
        target_risk = self._s.MAX_RISK_PER_TRADE_PCT
        if signal_strength >= 0.85:
            stop_atr_scale = 0.35           # 타이트하되 노이즈 고려 (0.25→0.35)
            trade_risk_pct = min(0.13, target_risk * 9.0)   # 최대 13%
        elif signal_strength >= 0.80:
            stop_atr_scale = 0.50
            trade_risk_pct = min(0.07, target_risk * 5.0)   # 최대 7%
        elif signal_strength >= 0.75:
            stop_atr_scale = 0.65
            trade_risk_pct = min(0.035, target_risk * 2.3)  # 최대 3.5%
        elif signal_strength >= 0.65:
            stop_atr_scale = 0.85
            trade_risk_pct = min(0.025, target_risk * 1.7)  # 최대 2.5%
        else:
            stop_atr_scale = 1.00
            trade_risk_pct = target_risk                     # 기본값

        # ── Step 1: Stop distance (signal-scaled) ─────────────────────────────
        # TP prices는 FULL ATR 배수 사용 → tight stop + large TP = 우수한 R:R
        # 예: strength=0.85 → stop=0.375× ATR, TP1=2.5× ATR → R:R = 6.7:1
        stop_distance = atr * stop_atr_mult * stop_atr_scale
        stop_distance_pct = stop_distance / entry_price

        if stop_distance_pct <= 0:
            return self._no_trade("Stop distance zero")

        # ── Step 2: Base leverage ─────────────────────────────────────────────
        # leverage = trade_risk_pct / stop_distance_pct
        # → 강한 시그널: 높은 trade_risk + 타이트 스탑 → 50× 자연 달성
        base_leverage = trade_risk_pct / stop_distance_pct
        base_leverage = max(1.0, base_leverage)

        # ── Step 3: Dampening factors ─────────────────────────────────────────

        # (a) Volatility regime: reduce leverage when vol is high
        if vol_percentile > 0.80:
            vol_mod = 0.55
        elif vol_percentile > 0.65:
            vol_mod = 0.78
        elif vol_percentile > 0.50:
            vol_mod = 0.90
        else:
            vol_mod = 1.00

        # (b) Spread / liquidity: wide spread = poor execution
        if spread_pct > 0.005:
            liq_mod = 0.30
        elif spread_pct > 0.003:
            liq_mod = 0.60
        elif spread_pct > 0.001:
            liq_mod = 0.85
        else:
            liq_mod = 1.00

        # (c) Funding cost: high absolute funding → reduce leverage
        abs_funding = abs(funding_rate)
        if abs_funding > 0.001:
            fund_mod = 0.60
        elif abs_funding > 0.0005:
            fund_mod = 0.80
        else:
            fund_mod = 1.00

        # (d) Signal quality gate — 약한 시그널 약간 감쇠, 강한 시그널 boost 없음
        # (주요 레버리지 제어는 risk_escalation이 담당, sig_mod는 보조)
        sig_mod = 0.65 + 0.35 * min(signal_strength, 1.0)
        # 0.55→0.84, 0.65→0.88, 0.75→0.91, 0.85→0.95, 1.0→1.00

        # (e) Account health (drawdown + consecutive losses)
        dd_mod = max(0.25, 1.0 - drawdown_pct * 3.0)
        consec_mod = max(0.25, 0.90 ** consecutive_losses)
        health_mod = min(dd_mod, consec_mod)

        # ── Step 4: Combined leverage ─────────────────────────────────────────
        raw_leverage = base_leverage * vol_mod * liq_mod * fund_mod * sig_mod * health_mod

        # Hard caps
        # Bear/Sideways 모드: BTC+ETH 고배율 스캘핑 허용 (최대 50×)
        risk_engine_cap = self._s.BEAR_MAX_LEVERAGE_CAP if bear_mode else self._s.MAX_LEVERAGE_CAP
        exchange_cap = min(symbol_max_leverage, 100)

        # Additional safety: 드로우다운 비례 캡 (BEAR 50× 모드에서도 작동)
        # >20% DD: 캡의 25% (BULL 15× → 5×, BEAR 50× → 12×)
        # >12% DD: 캡의 50% (BULL 15× → 7×, BEAR 50× → 25×)
        if drawdown_pct > 0.20:
            risk_engine_cap = min(risk_engine_cap, max(5, risk_engine_cap // 4))
        elif drawdown_pct > 0.12:
            risk_engine_cap = min(risk_engine_cap, max(10, risk_engine_cap // 2))

        final_leverage = max(1, min(
            math.floor(raw_leverage),
            risk_engine_cap,
            exchange_cap,
        ))

        # ── Step 5: Position sizing ───────────────────────────────────────────
        # qty × stop_distance = equity × trade_risk_pct
        # → 강한 시그널: 더 많은 자본 배분, 하지만 max_notional이 실제 손실 제한
        qty_base = equity * trade_risk_pct / stop_distance

        # ── AQR Volatility Targeting (Moreira & Muir 2017) ────────────────────
        # Scale position so daily P&L vol ≈ VOLATILITY_TARGET_PCT / sqrt(252)
        vol_target_qty: float = 0.0
        if recent_close is not None and len(recent_close) >= 20:
            log_rets = np.log(recent_close / recent_close.shift(1)).dropna()
            if len(log_rets) >= 20:
                daily_vol = float(log_rets.rolling(20).std().iloc[-1])
                if not pd.isna(daily_vol) and daily_vol > 0:
                    daily_target = self._s.VOLATILITY_TARGET_PCT / math.sqrt(252)
                    # qty × price × daily_vol ≈ equity × daily_target
                    vol_target_qty = equity * daily_target / (entry_price * daily_vol)
                    if vol_target_qty > 0:
                        qty_base = min(qty_base, vol_target_qty)

        # ── Quarter-Kelly position size cap (Van K. Tharp) ───────────────────
        kelly_qty: float = 0.0
        if win_stats is not None and win_stats.get("n_trades", 0) >= 30:
            wr = float(win_stats.get("win_rate", 0.5))
            avg_win = float(win_stats.get("avg_win", 1.0))
            avg_loss = float(win_stats.get("avg_loss", 1.0))
            if avg_loss > 0 and avg_win > 0:
                r_ratio = avg_win / avg_loss
                kelly_f = wr - (1.0 - wr) / r_ratio
                kelly_f = max(0.0, kelly_f)
                # Quarter-Kelly risk amount, converted to qty via stop distance
                quarter_kelly_risk = equity * kelly_f * self._s.USE_KELLY_FRACTION
                kelly_qty = quarter_kelly_risk / stop_distance
                if kelly_qty > 0:
                    qty_base = min(qty_base, kelly_qty)

        # Clamp to total exposure limit
        max_notional_per_trade = equity * self._s.MAX_SINGLE_EXPOSURE_PCT * final_leverage
        notional = qty_base * entry_price
        if notional > max_notional_per_trade:
            qty_base = max_notional_per_trade / entry_price
            notional = max_notional_per_trade

        margin = notional / final_leverage
        max_loss = qty_base * stop_distance  # = equity × risk_pct (by design)

        # ── Step 6: Stop / TP prices ──────────────────────────────────────────
        if side == "Buy":
            stop_price = entry_price - stop_distance
            tp1_price = entry_price + atr * tp1_atr_mult
            tp_price = entry_price + atr * tp2_atr_mult   # TP2 = full close target
        else:
            stop_price = entry_price + stop_distance
            tp1_price = entry_price - atr * tp1_atr_mult
            tp_price = entry_price - atr * tp2_atr_mult

        # ── Step 7: Minimum notional check ────────────────────────────────────
        min_notional = symbol_info.get("min_notional", 5.0)
        if notional < min_notional:
            return self._no_trade(
                f"Position notional {notional:.2f} < minimum {min_notional:.2f}"
            )

        # ── Step 7b: 수수료 손익분기점 체크 ──────────────────────────────────
        # TP1 도달 시 예상 수익이 수수료(진입 + 청산)의 2배 이상이어야 진입.
        # round-trip: 진입 MAKER(0.020%) + TP1 청산 MAKER(0.020%) + 슬리피지×2
        # GTC 주문은 대부분 Maker 체결이지만 fee_safety_multiplier=2.0으로 여유 확보.
        round_trip_fee_pct = (
            self._s.BACKTEST_FEE_MAKER * 2 + self._s.BACKTEST_SLIPPAGE_PCT * 2
        )
        tp1_move_pct = (atr * tp1_atr_mult) / entry_price
        fee_safety_multiplier = 2.0  # TP1 수익이 수수료의 최소 2배 (강화)
        # SCALP: ATR=0.107% → TP1=0.161% > fee×2.0=0.160% ✓ (최소 통과선 ATR 0.107%)
        # TREND: ATR=0.054% → TP1=0.161% > fee×2.0=0.160% ✓ (더 여유 있음)
        if tp1_move_pct < round_trip_fee_pct * fee_safety_multiplier:
            return self._no_trade(
                f"TP1 예상수익 {tp1_move_pct:.4%} < 수수료×{fee_safety_multiplier:.1f} "
                f"{round_trip_fee_pct * fee_safety_multiplier:.4%} — 수수료 손익분기 미달"
            )

        # ── Step 8: Liquidation distance check ────────────────────────────────
        # Require liquidation price to be at least 3× the stop distance away.
        # Bybit USDT-perpetual maintenance margin rate ≈ 0.5% for most symbols.
        maintenance_margin_rate = 0.005
        if side == "Buy":
            liq_price = entry_price * (1.0 - 1.0 / final_leverage + maintenance_margin_rate)
            liq_distance = entry_price - liq_price
        else:
            liq_price = entry_price * (1.0 + 1.0 / final_leverage - maintenance_margin_rate)
            liq_distance = liq_price - entry_price

        # 고배율(≥25×): 1.5× 버퍼 허용 (타이트 스탑 + 넓은 TP R:R 설계)
        # 저배율(<25×): 3× 버퍼 유지 (안전 우선)
        liq_buffer_mult = 1.5 if final_leverage >= 25 else 3.0
        min_liq_buffer = stop_distance * liq_buffer_mult
        if liq_distance < min_liq_buffer:
            return self._no_trade(
                f"Liquidation too close: liq_dist={liq_distance:.4f} "
                f"< {liq_buffer_mult}×stop={min_liq_buffer:.4f} (lev={final_leverage}× liq={liq_price:.4f})"
            )

        log.debug(
            "Risk calc | lev=%d× qty_base=%.4f notional=%.2f margin=%.2f "
            "stop=%.4f tp1=%.4f tp2=%.4f max_loss=%.2f mode=%s",
            final_leverage, qty_base, notional, margin,
            stop_price, tp1_price, tp_price, max_loss, resolved_mode,
        )

        return RiskParameters(
            leverage=final_leverage,
            position_size_usdt=notional,
            margin_usdt=margin,
            qty_base=qty_base,
            stop_price=stop_price,
            take_profit_price=tp_price,
            tp1_price=tp1_price,
            tp2_price=tp_price,
            trailing_stop_atr=atr * self._s.TRAILING_STOP_ATR_MULTIPLIER,
            max_loss_usdt=max_loss,
            risk_ok=True,
            trade_mode=resolved_mode,
            partial_exit_pct=partial_exit_pct,
            mode_max_bars=mode_max_bars,
            vol_target_size=vol_target_qty * entry_price if vol_target_qty > 0 else 0.0,
            kelly_size=kelly_qty * entry_price if kelly_qty > 0 else 0.0,
        )

    @staticmethod
    def _no_trade(reason: str) -> RiskParameters:
        return RiskParameters(
            leverage=1,
            position_size_usdt=0,
            margin_usdt=0,
            qty_base=0,
            stop_price=0,
            take_profit_price=0,
            tp1_price=0,
            tp2_price=0,
            trailing_stop_atr=0,
            max_loss_usdt=0,
            risk_ok=False,
            reason=reason,
            vol_target_size=0.0,
            kelly_size=0.0,
        )
