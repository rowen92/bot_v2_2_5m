"""
strategy.py – Trend-Following Strategy

Entry logic:
  - EMA crossover (fast crosses above/below slow) to detect trend direction immediately
  - ADX > threshold to confirm real momentum (filters out choppy sideways markets)
  - Volume above rolling average to confirm participation
  - ATR computed for SL/TP sizing in risk_manager / order_manager

Signal:
  - 'long'  when fast EMA crosses above slow EMA + ADX confirms
  - 'short' when fast EMA crosses below slow EMA + ADX confirms
  - 'none'  otherwise (choppy / no crossover)

The trailing stop in order_manager rides the position as far as the trend goes.
"""

from __future__ import annotations

import logging
import pandas as pd
from typing import Optional

from config import cfg
from state import State

log = logging.getLogger("strategy")

# ── Tuneable parameters ───────────────────────────────────────────────────────
EMA_FAST   = 8     # wider fast EMA — requires ~8 bars of momentum, avoids single-candle flips
EMA_SLOW   = 21    # wider slow EMA — genuine medium-term trend reference (Fib 21)
ADX_PERIOD = 10    # shorter ADX window — reacts faster on 1m
ADX_MIN    = 30.0  # minimum trend strength — blocks choppy low-momentum entries
VOL_MA        = 10    # faster volume average for 1m context
VOL_MULT      = 0.6   # more permissive — low-vol 1m bars are normal
SPIKE_ATR_MULT = 1.5  # skip signal if candle range > 1.5× ATR (exhaustion spike)

# Warm-up: need at least this many closed candles before any signal
_MIN_BARS = max(EMA_SLOW, ADX_PERIOD, VOL_MA) + 5


class ScalpingStrategy:
    """Trend-following strategy using EMA crossover + ADX + volume filter."""

    _last_df_hash: Optional[int] = None
    _cached_df: Optional[pd.DataFrame] = None

    def indicator_snapshot(self, state: State) -> Optional[dict]:
        """
        Compute all indicators and return them as a dict.
        Returns None (falsy) while still warming up.
        """
        if state.candle_count() < _MIN_BARS:
            return None

        df = self._compute(state)
        if df is None or df.empty:
            return None

        row = df.iloc[-1]
        return {
            "ema_fast": round(row["ema_fast"], cfg.PRICE_PRECISION),
            "ema_slow": round(row["ema_slow"], cfg.PRICE_PRECISION),
            "adx":      round(row["adx"],      2),
            "atr":      round(row["atr"],      cfg.PRICE_PRECISION + 1),
            "volume":   round(row["volume"],   2),
            "vol_avg":  round(row["vol_avg"],  2),
            "cross":    row["cross"],   # 'bull' | 'bear' | 'none'
            "close":    round(row["close"], cfg.PRICE_PRECISION),
        }

    def get_signal(self, state: State) -> str:
        """Return 'long', 'short', or 'none'. Reuses cached DataFrame."""
        df = self._cached_df
        if df is None or df.empty:
            return "none"

        row = df.iloc[-1]

        adx_ok   = row["adx"]          >= ADX_MIN
        vol_ok   = row["volume"]       >= row["vol_avg"] * VOL_MULT
        spike_ok = row["candle_range"] <= row["atr"] * SPIKE_ATR_MULT
        cross    = row["cross"]

        ema_fast = row["ema_fast"]
        ema_slow = row["ema_slow"]

        # Trend filter: only trade with the prevailing EMA trend.
        # LONG only when fast EMA is above slow EMA (uptrend).
        # SHORT only when fast EMA is below slow EMA (downtrend).
        # This blocks counter-trend trades that were causing repeated SL hits.
        in_uptrend   = ema_fast > ema_slow
        in_downtrend = ema_fast < ema_slow

        if cross == "bull" and adx_ok and vol_ok and spike_ok and in_uptrend:
            log.info(
                f"SIGNAL long  |  cross=bull  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  vol_avg={row['vol_avg']:.0f}"
            )
            return "long"

        if not spike_ok:
            log.debug(
                f"SPIKE filtered  range={row['candle_range']:.5f}"
                f"  atr_limit={row['atr'] * SPIKE_ATR_MULT:.5f}"
            )

        if cross == "bear" and adx_ok and vol_ok and spike_ok and in_downtrend:
            log.info(
                f"SIGNAL short |  cross=bear  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  vol_avg={row['vol_avg']:.0f}"
            )
            return "short"

        return "none"

    def _compute(self, state: State) -> Optional[pd.DataFrame]:
        """Build indicator DataFrame from closed candles.
        Cached by (row_count, last_open_time) — using len alone caused a freeze
        once the deque hit maxlen: len stayed constant while candles rolled,
        so the cache was never invalidated and indicators stopped updating.
        """
        df = state.to_dataframe()
        if df.empty:
            return None

        last_open_time = int(df["open_time"].iloc[-1]) if not df.empty else 0
        h = (len(df), last_open_time)
        if h == self._last_df_hash and self._cached_df is not None:
            return self._cached_df

        df = _add_indicators(df)
        self._last_df_hash = h
        self._cached_df = df
        return df


# ── Pure indicator functions ──────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder-smoothed ADX (no external deps)."""
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)

    up   = high - high.shift(1)
    down = low.shift(1) - low

    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    smoothed_tr       = tr.ewm(span=period, adjust=False).mean()
    smoothed_plus_dm  = plus_dm.ewm(span=period,  adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(span=period, adjust=False).mean()

    plus_di  = 100 * smoothed_plus_dm  / smoothed_tr.replace(0, float("nan"))
    minus_di = 100 * smoothed_minus_dm / smoothed_tr.replace(0, float("nan"))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    return dx.ewm(span=period, adjust=False).mean().fillna(0)


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema_fast"]     = _ema(df["close"], EMA_FAST)
    df["ema_slow"]     = _ema(df["close"], EMA_SLOW)
    df["atr"]          = _atr(df, cfg.ATR_PERIOD)
    df["adx"]          = _adx(df, ADX_PERIOD)
    df["vol_avg"]      = df["volume"].rolling(VOL_MA, min_periods=1).mean()
    df["candle_range"] = df["high"] - df["low"]

    prev_fast = df["ema_fast"].shift(1)
    prev_slow = df["ema_slow"].shift(1)

    bull = (df["ema_fast"] > df["ema_slow"]) & (prev_fast <= prev_slow)
    bear = (df["ema_fast"] < df["ema_slow"]) & (prev_fast >= prev_slow)

    df["cross"] = "none"
    df.loc[bull, "cross"] = "bull"
    df.loc[bear, "cross"] = "bear"

    return df