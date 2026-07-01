"""
strategy.py – Trend-Following Strategy

Entry logic:
  - EMA crossover (fast crosses above/below slow) to detect trend direction immediately
  - ADX > threshold to confirm real momentum (filters out choppy sideways markets)
  - Volume above rolling average to confirm participation
  - ATR computed for SL/TP sizing in risk_manager / order_manager
  - EMA 100 as long-term bias filter — only long above it, only short below it
  - Trend continuation re-entry — re-enter in trend direction after SL/cooldown
    when EMA alignment + ADX still strong, no fresh cross required

Signal:
  - 'long'  — cross=bull + ADX>30 + vol + no spike + EMA8>EMA21 + close>EMA100
           OR — continuation: EMA8>EMA21 + ADX>45 + vol + no spike + close>EMA100
  - 'short' — cross=bear + ADX>30 + vol + no spike + EMA8<EMA21 + close<EMA100
           OR — continuation: EMA8<EMA21 + ADX>45 + vol + no spike + close<EMA100
  - 'none'  otherwise

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
EMA_FAST      = 8      # fast EMA — momentum detection
EMA_SLOW      = 21     # slow EMA — medium-term trend reference (Fib 21)
EMA_TREND     = 100    # long-term bias — only long above, only short below
ADX_PERIOD    = 10     # shorter ADX window — reacts faster on 1m
ADX_MIN       = 30.0   # minimum ADX for crossover entries
ADX_TREND_MIN = 45.0   # higher ADX required for trend-continuation entries
VOL_MA        = 10     # volume average window
VOL_MULT      = 0.6    # volume must be at least 60% of average
SPIKE_ATR_MULT = 1.5   # skip signal if candle range > 1.5× ATR (exhaustion spike)

# Warm-up: need at least this many closed candles before any signal
_MIN_BARS = max(EMA_SLOW, ADX_PERIOD, VOL_MA) + 5


class ScalpingStrategy:
    """Trend-following strategy using EMA crossover + ADX + volume + EMA100 bias."""

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
            "ema_fast":  round(row["ema_fast"],  cfg.PRICE_PRECISION),
            "ema_slow":  round(row["ema_slow"],  cfg.PRICE_PRECISION),
            "ema_trend": round(row["ema_trend"], cfg.PRICE_PRECISION),
            "adx":       round(row["adx"],       2),
            "atr":       round(row["atr"],       cfg.PRICE_PRECISION + 1),
            "volume":    round(row["volume"],    2),
            "vol_avg":   round(row["vol_avg"],   2),
            "cross":     row["cross"],   # 'bull' | 'bear' | 'none'
            "close":     round(row["close"], cfg.PRICE_PRECISION),
        }

    def get_signal(self, state: State) -> str:
        """Return 'long', 'short', or 'none'. Reuses cached DataFrame."""
        df = self._cached_df
        if df is None or df.empty:
            return "none"

        row = df.iloc[-1]

        adx_ok       = row["adx"]          >= ADX_MIN
        adx_trend_ok = row["adx"]          >= ADX_TREND_MIN
        vol_ok       = row["volume"]       >= row["vol_avg"] * VOL_MULT
        spike_ok     = row["candle_range"] <= row["atr"] * SPIKE_ATR_MULT
        cross        = row["cross"]

        ema_fast  = row["ema_fast"]
        ema_slow  = row["ema_slow"]
        ema_trend = row["ema_trend"]
        close     = row["close"]

        in_uptrend   = ema_fast > ema_slow
        in_downtrend = ema_fast < ema_slow

        # EMA100 bias: only long above, only short below
        # Conflict zone (e.g. price > EMA100 but EMA8 < EMA21) → no trade
        bias_long  = close > ema_trend
        bias_short = close < ema_trend

        if not spike_ok:
            log.debug(
                f"SPIKE filtered  range={row['candle_range']:.5f}"
                f"  atr_limit={row['atr'] * SPIKE_ATR_MULT:.5f}"
            )

        # ── 1. Crossover entries (fresh cross signal) ─────────────────────────
        if cross == "bull" and adx_ok and vol_ok and spike_ok and in_uptrend and bias_long:
            log.info(
                f"SIGNAL long  |  cross=bull  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
            )
            return "long"

        if cross == "bear" and adx_ok and vol_ok and spike_ok and in_downtrend and bias_short:
            log.info(
                f"SIGNAL short |  cross=bear  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
            )
            return "short"

        # ── 2. Trend continuation entries (no fresh cross needed) ─────────────
        # Re-enters after SL+cooldown when trend is still strongly intact.
        # Requires ADX >= 45 (vs 30 for crossover) to avoid choppy re-entries.
        # EMA100 bias still enforced — never trade against long-term trend.
        if in_uptrend and adx_trend_ok and vol_ok and spike_ok and bias_long:
            log.info(
                f"SIGNAL long  |  continuation  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
            )
            return "long"

        if in_downtrend and adx_trend_ok and vol_ok and spike_ok and bias_short:
            log.info(
                f"SIGNAL short |  continuation  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
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
    df["ema_trend"]    = _ema(df["close"], EMA_TREND)
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