"""
TrendEMA Strategy Backtest
==========================
Bar-by-bar simulation of TrendEMAStrategy on synthetic BTC data.

Architecture:
  LTF : 5-min bars  (entry timing)
  HTF : 60-min bars (phase detection — higher SNR than 15-min)

Trade parameters (phase-dependent):
  Trend (BULL/BEAR): stop 1.0×ATR, TP 2.5×ATR, max_bars 24 (2h)
  Range (SIDEWAYS):  stop 0.8×ATR, TP 1.2×ATR, max_bars 12 (1h)

Cooldown: 24 bars (2 hours) between entries.

Usage:
    python -m src.backtest.trend_ema_backtest
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from src.backtest.synthetic import SyntheticBTCData
from src.config import Settings
from src.strategy.regime import Regime
from src.strategy.trend_ema import TrendEMAStrategy

# ── Quality gate ───────────────────────────────────────────────────────────────
SHARPE_MIN = 1.2
WIN_RATE_MIN = 0.55
MDD_MAX = 0.25
PF_MIN = 1.5

# ── Timeframes ─────────────────────────────────────────────────────────────────
LTF_MIN = 5          # 5-min LTF bars
HTF_MIN = 60         # 60-min HTF bars (higher SNR for EMA200 phase detection)
HTF_FACTOR = HTF_MIN // LTF_MIN  # 12

# ── Simulation parameters ──────────────────────────────────────────────────────
INITIAL_CAPITAL  = 10_000.0
WARMUP_LTF       = HTF_FACTOR * 220   # need 200+ HTF bars for EMA200 (2,640 LTF bars)
COOLDOWN_LTF     = 24                 # 2 hours between entries
MAKER_FEE        = 0.0002
TAKER_FEE        = 0.00055
SLIPPAGE         = 0.0003

# Trend entries: wider RR, ride the trend
TREND_SL_MULT    = 1.0
TREND_TP_MULT    = 2.5
TREND_MAX_BARS   = 24   # 2 hours

# Sideways entries: mean reversion, tighter target
RANGE_SL_MULT    = 0.8
RANGE_TP_MULT    = 1.2
RANGE_MAX_BARS   = 12   # 1 hour

# Leverage table (strength → leverage, risk_pct)
_LEV_TABLE = [
    (0.85, 12.0, 0.03),
    (0.77, 6.0,  0.02),
    (0.65, 3.0,  0.01),
    (0.0,  2.0,  0.005),
]


@dataclass
class Trade:
    side: int
    is_range: bool
    entry_bar: int
    entry_price: float
    stop_price: float
    tp_price: float
    max_bar: int
    qty: float
    leverage: float
    exit_bar: int = 0
    exit_price: float = 0.0
    pnl_net: float = 0.0
    exit_reason: str = ""


def _resample_to_htf(df: pd.DataFrame, factor: int) -> pd.DataFrame:
    rows = []
    for start in range(0, len(df) - factor + 1, factor):
        chunk = df.iloc[start: start + factor]
        rows.append({
            "ts":     int(chunk["ts"].iloc[0]),
            "open":   float(chunk["open"].iloc[0]),
            "high":   float(chunk["high"].max()),
            "low":    float(chunk["low"].min()),
            "close":  float(chunk["close"].iloc[-1]),
            "volume": float(chunk["volume"].sum()),
        })
    return pd.DataFrame(rows)


def _get_lev_risk(strength: float):
    for s_min, lev, risk in _LEV_TABLE:
        if strength >= s_min:
            return lev, risk
    return 2.0, 0.005


def run_backtest(n_days: int = 365, seed: int = 42, verbose: bool = True) -> dict:
    s = Settings(TRADING_MODE="dry-run", BYBIT_API_KEY="", BYBIT_API_SECRET="")
    strategy = TrendEMAStrategy(s)

    if verbose:
        print(f"\n[TrendEMA Backtest] {n_days}d synthetic BTC | LTF=5m HTF=60m | seed={seed}")
    ltf_df = SyntheticBTCData().generate(n_days=n_days, interval_min=LTF_MIN, seed=seed)
    htf_df = _resample_to_htf(ltf_df, HTF_FACTOR)

    ltf = ltf_df.reset_index(drop=True)
    htf = htf_df.reset_index(drop=True)

    equity = float(INITIAL_CAPITAL)
    peak   = equity
    mdd    = 0.0

    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    last_entry_bar = -COOLDOWN_LTF * 2  # allow entry from start

    equity_snap: List[float] = []

    for i in range(WARMUP_LTF, len(ltf)):
        close = float(ltf["close"].iloc[i])
        high  = float(ltf["high"].iloc[i])
        low   = float(ltf["low"].iloc[i])

        equity_snap.append(equity)

        # ── Exit check ───────────────────────────────────────────────────────
        if open_trade is not None:
            t = open_trade
            bars_held = i - t.entry_bar

            hit_sl   = (t.side ==  1 and low  <= t.stop_price) or \
                       (t.side == -1 and high >= t.stop_price)
            hit_tp   = (t.side ==  1 and high >= t.tp_price) or \
                       (t.side == -1 and low  <= t.tp_price)
            time_out = i >= t.max_bar  # t.max_bar is absolute bar index

            if hit_tp:
                ep, fee_rate = t.tp_price, MAKER_FEE + MAKER_FEE
                t.exit_reason = "TP"
            elif hit_sl:
                ep, fee_rate = t.stop_price, MAKER_FEE + TAKER_FEE
                t.exit_reason = "SL"
            elif time_out:
                ep, fee_rate = close, MAKER_FEE + TAKER_FEE
                t.exit_reason = "TIME"
            else:
                continue

            gross = t.side * (ep - t.entry_price) * t.qty
            fee   = (t.entry_price + ep) * t.qty * (fee_rate + SLIPPAGE * 2)
            t.pnl_net    = gross - fee
            t.exit_price = ep
            t.exit_bar   = i

            equity += t.pnl_net
            peak    = max(peak, equity)
            dd      = (peak - equity) / peak if peak > 0 else 0
            mdd     = max(mdd, dd)
            trades.append(t)
            open_trade = None

            if equity <= 0:
                break

        if open_trade is not None:
            continue  # still in trade

        # ── Cooldown check ───────────────────────────────────────────────────
        if i - last_entry_bar < COOLDOWN_LTF:
            continue

        # ── Max drawdown halt ────────────────────────────────────────────────
        dd_now = (peak - equity) / peak if peak > 0 else 0
        if dd_now >= 0.30:
            continue

        # ── Build windows ────────────────────────────────────────────────────
        ltf_window = ltf.iloc[max(0, i - 299): i + 1]
        if len(ltf_window) < 50:
            continue

        htf_bar = (i // HTF_FACTOR) - 1
        if htf_bar < 210:  # need 200+ bars for EMA200 + buffer
            continue
        htf_start  = max(0, htf_bar - 299)
        htf_window = htf.iloc[htf_start: htf_bar + 1]

        try:
            sig, atr_val = strategy.generate(
                ltf_df=ltf_window,
                htf_df=htf_window,
                vwap_val=0.0,
                funding_rate=0.0,
                spread_pct=0.0,
            )
        except Exception:
            continue

        if sig.direction == 0 or atr_val <= 0:
            continue

        is_range = (sig.regime == Regime.RANGING)
        sl_mult  = RANGE_SL_MULT if is_range else TREND_SL_MULT
        tp_mult  = RANGE_TP_MULT if is_range else TREND_TP_MULT
        max_bars = RANGE_MAX_BARS if is_range else TREND_MAX_BARS

        lev, risk_pct = _get_lev_risk(sig.strength)
        risk_amt = equity * risk_pct
        stop_dist = atr_val * sl_mult
        qty = risk_amt / stop_dist if stop_dist > 0 else 0.0
        if qty <= 0:
            continue

        d = sig.direction
        ep     = close * (1 + SLIPPAGE * d)
        stop_p = ep - d * stop_dist
        tp_p   = ep + d * atr_val * tp_mult

        open_trade    = Trade(
            side=d, is_range=is_range,
            entry_bar=i, entry_price=ep,
            stop_price=stop_p, tp_price=tp_p,
            max_bar=i + max_bars,
            qty=qty, leverage=lev,
        )
        last_entry_bar = i

    # ── Metrics ───────────────────────────────────────────────────────────────
    n = len(trades)
    if n == 0:
        if verbose:
            print("  No trades generated.")
        return {"passed": False, "n_trades": 0}

    wins   = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    win_rate = len(wins) / n
    gp = sum(t.pnl_net for t in wins)
    gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    pf = gp / gl

    total_ret = (equity - INITIAL_CAPITAL) / INITIAL_CAPITAL
    pnls = np.array([t.pnl_net / INITIAL_CAPITAL for t in trades])
    sharpe = float(pnls.mean() / (pnls.std() + 1e-9)) * np.sqrt(365 * 24 * 60 / LTF_MIN / n * n)
    # Simpler annualised Sharpe: scale by sqrt(252) assuming ~daily returns
    sharpe = float(pnls.mean() / (pnls.std() + 1e-9)) * np.sqrt(252)

    exit_cnt = {}
    for t in trades:
        exit_cnt[t.exit_reason] = exit_cnt.get(t.exit_reason, 0) + 1

    passed = sharpe >= SHARPE_MIN and win_rate >= WIN_RATE_MIN and mdd <= MDD_MAX and pf >= PF_MIN

    results = dict(
        passed=passed, n_trades=n,
        win_rate=win_rate, sharpe=sharpe,
        mdd=mdd, profit_factor=pf,
        total_return=total_ret, final_equity=equity,
        exit_reasons=exit_cnt, trades=trades,
    )

    if verbose:
        _print(results)
    return results


def _print(r: dict) -> None:
    sep = "─" * 52
    print(f"\n{sep}")
    print("  TrendEMA Backtest Results (LTF=5m HTF=60m)")
    print(sep)
    print(f"  Trades       : {r['n_trades']}")
    print(f"  Win Rate     : {r['win_rate']:.1%}   (target ≥ 55%)")
    print(f"  Sharpe       : {r['sharpe']:.2f}    (target ≥ 1.20)")
    print(f"  Max DD       : {r['mdd']:.1%}   (target ≤ 25%)")
    print(f"  Profit Factor: {r['profit_factor']:.2f}    (target ≥ 1.50)")
    print(f"  Total Return : {r['total_return']:+.1%}")
    print(f"  Final Equity : ${r['final_equity']:,.0f}")
    print(f"  Exit Reasons : {r['exit_reasons']}")
    status = "PASSED" if r["passed"] else "FAILED"
    print(f"\n  Quality Gate : {status}")
    thresholds = [
        ("Sharpe",        r["sharpe"],        ">=", SHARPE_MIN),
        ("Win Rate",      r["win_rate"],       ">=", WIN_RATE_MIN),
        ("Max Drawdown",  r["mdd"],            "<=", MDD_MAX),
        ("Profit Factor", r["profit_factor"],  ">=", PF_MIN),
    ]
    for name, val, op, thr in thresholds:
        ok = (val >= thr) if op == ">=" else (val <= thr)
        print(f"    [{'OK  ' if ok else 'FAIL'}] {name}: {val:.3f} {op} {thr}")
    print(sep)


if __name__ == "__main__":
    run_backtest(n_days=365, seed=42, verbose=True)
