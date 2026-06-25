"""
strategy.py – Structure-reversal scalping signal generator.

Pattern observed on chart (the "squares"):
  ── Price forms a clear swing LOW or swing HIGH (structure level)
  ── Open Interest is RISING  → real money entering, not just liquidations
  ── The reversal candle closes in the direction of the trade (bullish/bearish body)
  ── No volatility spike (range < ATR * ATR_MAX_MULT)
  ── Volume is not abnormally low (dead-market filter)

  LONG  ▸ Current close is near (≤ STRUCTURE_TOUCH_ATR * ATR) above the N-bar swing low
         ▸ Current candle is a bullish close  (close > open)
         ▸ Open Interest has increased over the last OI_CONFIRM_BARS candles
         ▸ OI absolute level is above its rolling mean (OI_MEAN_BARS) → trend of accumulation
         ▸ Candle range < ATR * ATR_MAX_MULT    (spike filter)
         ▸ Volume ≥ avg_volume * VOLUME_MIN_MULT (dead-market filter)

  SHORT ▸ mirror — near swing HIGH, bearish close, OI rising

  Returns 'long' | 'short' | 'none'
"""

from __future__ import annotations

import logging

import pandas as pd

from config import cfg
from state import State

log = logging.getLogger("strategy")


# ── Technical helpers ──────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int) -> float:
    """Average True Range (Wilder) over `period` candles."""
    high       = df["high"]
    low        = df["low"]
    close      = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(com=period - 1, adjust=False).mean().iloc[-1])


def _swing_low(low: pd.Series, lookback: int) -> float:
    """Lowest low over the last `lookback` candles (excluding current bar)."""
    window = low.iloc[-(lookback + 1):-1]
    return float(window.min())


def _swing_high(high: pd.Series, lookback: int) -> float:
    """Highest high over the last `lookback` candles (excluding current bar)."""
    window = high.iloc[-(lookback + 1):-1]
    return float(window.max())


# ── Strategy ───────────────────────────────────────────────────────────────────

class ScalpingStrategy:
    """
    Reversal-at-structure scalper using Open Interest + price action.
    No EMA crossovers — entries happen at swing lows / swing highs
    when OI confirms that real money is stepping in.
    """

    def __init__(self):
        self.atr_period    = cfg.ATR_PERIOD
        self.atr_max       = cfg.ATR_MAX_MULT
        self.vol_min       = cfg.VOLUME_MIN_MULT

        # How many bars to look back when finding the swing level
        self._swing_bars   = cfg.SWING_LOOKBACK        # e.g. 10
        # How close (in ATR units) to the swing level counts as a "touch"
        self._touch_mult   = cfg.STRUCTURE_TOUCH_ATR   # e.g. 0.5
        # How many OI readings must be rising consecutively
        self._oi_bars      = cfg.OI_CONFIRM_BARS       # e.g. 3
        # Rolling window for OI mean filter
        self._oi_mean_bars = cfg.OI_MEAN_BARS          # e.g. 20

        # Per-candle cache
        self._cache_count: int   = -1
        self._cache_df           = None
        self._cache_atr: float   = 0.0

    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_cache(self, state: State) -> None:
        """Rebuild the per-candle DataFrame + ATR cache if the candle count changed."""
        candle_count = state.candle_count()
        if self._cache_count != candle_count:
            df                = state.to_dataframe()
            self._cache_atr   = _atr(df, self.atr_period)
            self._cache_df    = df
            self._cache_count = candle_count

    def get_signal(self, state: State) -> str:
        """
        Evaluate the current closed candle and OI history.
        Returns 'long', 'short', or 'none'.
        """
        min_bars = self._swing_bars + self.atr_period + 5
        if state.candle_count() < min_bars:
            return "none"

        self._refresh_cache(state)

        df  = self._cache_df
        atr = self._cache_atr

        close = df["close"]
        open_ = df["open"]
        high  = df["high"]
        low   = df["low"]
        vol   = df["volume"]

        last_close = float(close.iloc[-1])
        last_open  = float(open_.iloc[-1])
        last_range = float(high.iloc[-1] - low.iloc[-1])

        # ── Filters shared by both directions ──────────────────────────────────
        is_spike = last_range > atr * self.atr_max

        avg_vol       = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else float(vol.mean())
        enough_volume = float(vol.iloc[-1]) >= avg_vol * self.vol_min

        if is_spike or not enough_volume:
            if is_spike:
                log.debug(f"SPIKE filtered  range={last_range:.5f}  atr_limit={atr*self.atr_max:.5f}")
            if not enough_volume:
                log.debug(f"LOW_VOL filtered  vol={float(vol.iloc[-1]):.1f}  need={avg_vol*self.vol_min:.1f}")
            return "none"

        # ── Open Interest check ────────────────────────────────────────────────
        oi_rising  = self._oi_is_rising(state)
        oi_above_mean = self._oi_above_mean(state)

        # ── Swing structure levels ─────────────────────────────────────────────
        touch_dist  = atr * self._touch_mult
        swing_low   = _swing_low(low, self._swing_bars)
        swing_high  = _swing_high(high, self._swing_bars)

        # Near swing low: price dipped within touch_dist of the swing low
        near_low  = float(low.iloc[-1]) <= swing_low + touch_dist
        # Near swing high: price pushed within touch_dist of the swing high
        near_high = float(high.iloc[-1]) >= swing_high - touch_dist

        # If candle touches BOTH levels it swept the whole range — too chaotic, skip
        if near_low and near_high:
            log.debug(f"SWEEP candle — touches both swing_low={swing_low:.4f} and swing_high={swing_high:.4f}, skipping")
            return "none"

        # Candle body direction
        is_bullish = last_close > last_open  # green candle
        is_bearish = last_close < last_open  # red candle

        # ── LONG: touch swing low + bullish reversal candle + OI rising ───────
        if near_low and is_bullish and oi_rising and oi_above_mean:
            log.debug(
                f"LONG signal  close={last_close:.4f}  swing_low={swing_low:.4f}"
                f"  touch_dist={touch_dist:.4f}  oi_rising={oi_rising}"
            )
            return "long"

        # ── SHORT: touch swing high + bearish reversal candle + OI rising ─────
        if near_high and is_bearish and oi_rising and oi_above_mean:
            log.debug(
                f"SHORT signal  close={last_close:.4f}  swing_high={swing_high:.4f}"
                f"  touch_dist={touch_dist:.4f}  oi_rising={oi_rising}"
            )
            return "short"

        # ── Debug: log what was close but blocked ──────────────────────────────
        if near_low or near_high:
            direction  = "LONG_CAND" if near_low else "SHORT_CAND"
            body_ok    = is_bullish if near_low else is_bearish
            reasons = []
            if not body_ok:
                reasons.append("no_reversal_body")
            if not oi_rising:
                reasons.append(f"oi_not_rising(last {self._oi_bars} bars)")
            if not oi_above_mean:
                reasons.append(f"oi_below_mean({self._oi_mean_bars}bar avg)")
            if reasons:
                log.debug(f"BLOCKED {direction}  " + "  ".join(reasons))

        return "none"

    # ──────────────────────────────────────────────────────────────────────────

    def _oi_is_rising(self, state: State) -> bool:
        """
        Return True if OI has been consistently rising over the last
        OI_CONFIRM_BARS readings.
        Requires at least OI_CONFIRM_BARS + 1 readings in the buffer.
        """
        history = state.oi_history
        if len(history) < self._oi_bars + 1:
            log.debug("OI history too short — skipping trade")
            return False
        # Check last N consecutive readings are each >= previous
        window = list(history)[-( self._oi_bars + 1):]
        return all(window[i] >= window[i - 1] for i in range(1, len(window)))

    def _oi_above_mean(self, state: State) -> bool:
        """
        Return True if the latest OI reading is above the rolling mean
        of the last OI_MEAN_BARS readings (avoids trading into OI drain).
        Requires at least OI_MEAN_BARS readings — returns False (blocking)
        until enough data is collected, same conservative stance as _oi_is_rising.
        """
        history = state.oi_history
        if len(history) < self._oi_mean_bars:
            log.debug(
                f"OI mean filter: not enough data "
                f"({len(history)}/{self._oi_mean_bars}) — blocking trade"
            )
            return False  # conservative: don't trade without enough OI history
        window     = list(history)[-(self._oi_mean_bars):]
        mean_oi    = sum(window) / len(window)
        latest_oi  = history[-1]
        return latest_oi >= mean_oi

    # ──────────────────────────────────────────────────────────────────────────

    def indicator_snapshot(self, state: State) -> dict:
        """Return a dict of current indicator values (for logging/debugging).

        Populates the per-candle cache so the subsequent get_signal() call
        reuses it without rebuilding the DataFrame a second time.
        """
        min_bars = self._swing_bars + self.atr_period + 5
        if state.candle_count() < min_bars:
            return {}

        # _refresh_cache is also called by get_signal — whichever runs first wins.
        self._refresh_cache(state)

        df  = self._cache_df
        atr = self._cache_atr
        vol = df["volume"]
        avg_vol = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else float(vol.mean())

        swing_low  = _swing_low(df["low"], self._swing_bars)
        swing_high = _swing_high(df["high"], self._swing_bars)
        latest_oi  = state.oi_history[-1] if state.oi_history else 0.0
        oi_mean    = (
            sum(list(state.oi_history)[-self._oi_mean_bars:]) / self._oi_mean_bars
            if len(state.oi_history) >= self._oi_mean_bars else 0.0
        )

        return {
            "mark_price":  state.mark_price,
            "atr":         round(atr, 5),
            "swing_low":   round(swing_low, 4),
            "swing_high":  round(swing_high, 4),
            "touch_dist":  round(atr * self._touch_mult, 5),
            "oi_latest":   round(latest_oi, 2),
            "oi_mean":     round(oi_mean, 2),
            "oi_rising":   self._oi_is_rising(state),
            "vol_ratio":   round(float(vol.iloc[-1]) / avg_vol, 2) if avg_vol else 0,
            "candles":     state.candle_count(),
            "oi_readings": len(state.oi_history),
        }
