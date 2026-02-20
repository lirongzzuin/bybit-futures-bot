"""
Pure-function technical indicator library.
All functions operate on pandas Series / DataFrame and return pandas objects.
No side effects, no external state.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ── Trend / Momentum ──────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": histogram}
    )


# ── Volatility ────────────────────────────────────────────────────────────────

def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger_bands(
    close: pd.Series, period: int = 20, std_dev: float = 2.0
) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame(
        {"upper": upper, "mid": mid, "lower": lower, "pct_b": pct_b}
    )


def realized_volatility(close: pd.Series, period: int = 20) -> pd.Series:
    """Log-return realized volatility (annualized)."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(period).std() * np.sqrt(252 * 24 * 4)  # 15-min bars


def volatility_percentile(close: pd.Series, short: int = 20, long: int = 100) -> pd.Series:
    """Rolling percentile of short vol vs long vol window (0–1)."""
    rv = realized_volatility(close, short)
    return rv.rolling(long).rank(pct=True)


# ── ADX / Directional Movement ────────────────────────────────────────────────

def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.DataFrame:
    """
    Returns DataFrame: adx, plus_di, minus_di
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm_s = pd.Series(plus_dm, index=close.index)
    minus_dm_s = pd.Series(minus_dm, index=close.index)

    tr = atr(high, low, close, period)  # reuse ATR (smoothed TR)

    plus_di = 100 * plus_dm_s.ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0, np.nan)
    minus_di = 100 * minus_dm_s.ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0, np.nan)

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_val = dx.ewm(alpha=1 / period, adjust=False).mean()

    return pd.DataFrame({"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di})


# ── Volume ────────────────────────────────────────────────────────────────────

def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    return volume.rolling(period).mean()


def volume_ratio(volume: pd.Series, short: int = 5, long: int = 20) -> pd.Series:
    """Ratio of short-term avg volume to long-term avg volume."""
    return volume.rolling(short).mean() / volume.rolling(long).mean().replace(0, np.nan)


def volume_zscore(volume: pd.Series, period: int = 20) -> pd.Series:
    """Z-score of volume relative to rolling window."""
    avg = volume.rolling(period).mean()
    std = volume.rolling(period).std()
    return (volume - avg) / std.replace(0, np.nan)


# ── VWAP ──────────────────────────────────────────────────────────────────────

def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 96,
) -> pd.Series:
    """
    Rolling Volume-Weighted Average Price.
    Uses a rolling window of `period` bars rather than session reset.
    Returns NaN for the first `period-1` bars.
    """
    typical_price = (high + low + close) / 3.0
    tp_vol = typical_price * volume
    rolling_tp_vol = tp_vol.rolling(period).sum()
    rolling_vol = volume.rolling(period).sum()
    return rolling_tp_vol / rolling_vol.replace(0, np.nan)


# ── Efficiency Ratio (Kaufman) ────────────────────────────────────────────────

def efficiency_ratio(series: pd.Series, period: int = 10) -> pd.Series:
    """
    Kaufman Efficiency Ratio (ER).
    ER = abs(net_change_over_period) / sum(abs(bar_changes))
    ER ≈ 1.0 → clean directional trend
    ER ≈ 0.0 → choppy, noisy, random movement
    Reference: Perry Kaufman "Trading Systems and Methods"
    """
    net_change = (series - series.shift(period)).abs()
    path_length = series.diff().abs().rolling(period).sum()
    return net_change / path_length.replace(0, np.nan)


# ── Hurst Exponent (Peters 1991) ──────────────────────────────────────────────

def hurst_exponent(series: pd.Series, max_lags: int = 20) -> float:
    """
    Hurst Exponent via Rescaled Range (R/S) analysis (Peters 1991).
    H > 0.55 → trending / persistent momentum
    H ≈ 0.50 → random walk (efficient market)
    H < 0.45 → mean-reverting / anti-persistent
    Returns a scalar for the provided series.
    Reference: Edgar Peters "Fractal Market Analysis" (1994)
    """
    if len(series) < max_lags * 4:
        return 0.5

    log_returns = np.log(series / series.shift(1)).dropna().values
    n = len(log_returns)
    if n < max_lags * 2:
        return 0.5

    rs_vals = []
    lag_vals = []

    for lag in range(2, min(max_lags, n // 2)):
        n_chunks = n // lag
        if n_chunks < 2:
            continue

        rs_chunk = []
        for i in range(n_chunks):
            chunk = log_returns[i * lag: (i + 1) * lag]
            mean_chunk = chunk.mean()
            deviation = np.cumsum(chunk - mean_chunk)
            r = deviation.max() - deviation.min()
            s = chunk.std(ddof=1)
            if s > 0:
                rs_chunk.append(r / s)

        if rs_chunk:
            rs_vals.append(np.log(np.mean(rs_chunk)))
            lag_vals.append(np.log(lag))

    if len(rs_vals) < 4:
        return 0.5

    hurst, _ = np.polyfit(lag_vals, rs_vals, 1)
    return float(np.clip(hurst, 0.0, 1.0))


# ── Hull Moving Average (Alan Hull, 2005) ─────────────────────────────────────

def hull_ma(series: pd.Series, period: int = 16) -> pd.Series:
    """
    Hull Moving Average.
    HMA = WMA(2 × WMA(n/2) − WMA(n), sqrt(n))
    Faster and smoother than EMA with significantly less lag.
    Fast vectorized implementation using numpy convolution (O(n) per call).
    Reference: Alan Hull (2005), alanhull.com
    """
    half = max(2, period // 2)
    sqrt_n = max(2, int(round(period ** 0.5)))

    def _wma(s: pd.Series, w: int) -> pd.Series:
        vals = s.values.astype(float)
        n = len(vals)
        if n < w:
            return pd.Series(np.full(n, np.nan), index=s.index)
        weights = np.arange(1, w + 1, dtype=float)
        weights /= weights.sum()
        # numpy convolve 'valid': output length = n - w + 1
        conv = np.convolve(vals, weights[::-1], mode="valid")
        result = np.full(n, np.nan)
        result[w - 1:] = conv
        return pd.Series(result, index=s.index)

    wma_half = _wma(series, half)
    wma_full = _wma(series, period)
    raw = 2.0 * wma_half - wma_full
    return _wma(raw, sqrt_n)


# ── Keltner Channels ──────────────────────────────────────────────────────────

def keltner_channel(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
    atr_mult: float = 1.5,
) -> pd.DataFrame:
    """
    Keltner Channels (Chester Keltner original / Linda Raschke ATR variant).
    Mid   = EMA(close, period)
    Upper = Mid + atr_mult × ATR(period)
    Lower = Mid − atr_mult × ATR(period)
    pct_k = (close − lower) / (upper − lower)   [0..1, >1 = breakout above]
    """
    mid = ema(close, period)
    atr_vals = atr(high, low, close, period)
    upper = mid + atr_mult * atr_vals
    lower = mid - atr_mult * atr_vals
    pct_k = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame({"upper": upper, "mid": mid, "lower": lower, "pct_k": pct_k})


# ── Session VWAP ──────────────────────────────────────────────────────────────

def session_vwap(df: pd.DataFrame, session_start_utc_hour: int = 0) -> pd.Series:
    """
    True session VWAP that resets at session_start_utc_hour each day (UTC).
    df must have columns: high, low, close, volume.
    If df.index is a DatetimeIndex, a proper daily reset is performed.
    If not (e.g. integer index), falls back to the rolling VWAP.
    Standard in institutional and prop-trading floor practice.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        return vwap(df["high"], df["low"], df["close"], df["volume"])

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = typical * df["volume"]

    idx_utc = df.index
    if idx_utc.tz is None:
        idx_utc = idx_utc.tz_localize("UTC")
    shifted = idx_utc - pd.Timedelta(hours=session_start_utc_hour)
    session_key = shifted.normalize()

    cumtp_vol = tp_vol.groupby(session_key).cumsum()
    cumvol = df["volume"].groupby(session_key).cumsum()
    return cumtp_vol / cumvol.replace(0, np.nan)
