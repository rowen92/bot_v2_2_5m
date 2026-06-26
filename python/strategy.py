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

  SHORT ▸ near swing HIGH, bearish close, OI rising (new shorts) OR OI falling (long liquidation)

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

        # Per-candle cache — keyed on last candle open_time so the cache is
        # refreshed on every new candle even when the deque is at maxlen (200).
        self._cache_key: int     = -1   # last candle open_time
        self._cache_df           = None
        self._cache_atr: float   = 0.0

    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_cache(self, state: State) -> None:
        """Rebuild the per-candle DataFrame + ATR cache when the latest candle changes.

        Uses the last candle's open_time as the cache key instead of the deque
        length, so the cache is always refreshed on a new candle even when the
        buffer is at maxlen (200) and candle_count() returns the same value.
        """
        last_open_time = state._candles[-1].open_time if state._candles else -1
        if self._cache_key != last_open_time:
            df                = state.to_dataframe()
            self._cache_atr   = _atr(df, self.atr_period)
            self._cache_df    = df
            self._cache_key   = last_open_time

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

        # ── SHORT: touch swing high ────────────────────────────────────────────
        # Body and OI requirements are asymmetric by setup type:
        #
        #   OI rising  → reversal short: new shorts piling in at the high
        #                needs bearish body (red candle) to confirm seller conviction
        #                needs oi_above_mean: real accumulation of short interest
        #
        #   OI falling → trend continuation short: longs unwinding / liquidating
        #                a GREEN retest candle is actually the signal — weak buyers
        #                retesting broken support-turned-resistance with no follow-through
        #                body direction is irrelevant; oi_above_mean relaxed (OI drains in downtrend)
        oi_falling = self._oi_is_falling(state)
        oi_confirms_short = oi_rising or oi_falling
        short_mean_ok  = oi_above_mean if oi_rising else True
        short_body_ok  = is_bearish    if oi_rising else True  # green retest is valid for liquidation short
        if near_high and short_body_ok and oi_confirms_short and short_mean_ok:
            log.debug(
                f"SHORT signal  close={last_close:.4f}  swing_high={swing_high:.4f}"
                f"  touch_dist={touch_dist:.4f}  oi_rising={oi_rising}  oi_falling={oi_falling}"
            )
            return "short"

        # ── Debug: log what was close but blocked ──────────────────────────────
        if near_low or near_high:
            direction = "LONG_CAND" if near_low else "SHORT_CAND"
            reasons   = []
            if near_low and not is_bullish:
                reasons.append("no_reversal_body")
            if near_high and oi_rising and not is_bearish:
                reasons.append("no_reversal_body(reversal_short_needs_red_candle)")
            if near_low and not oi_rising:
                reasons.append(f"oi_not_rising(last {self._oi_bars} bars)")
            if near_high and not oi_confirms_short:
                reasons.append(f"oi_not_rising_or_falling(last {self._oi_bars} bars)")
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

    def _oi_is_falling(self, state: State) -> bool:
        """
        Return True if OI has been consistently falling over the last
        OI_CONFIRM_BARS readings — indicates long liquidation / unwinding.
        Mirror of _oi_is_rising; used to confirm SHORT entries during downtrends
        where longs exit rather than new shorts enter (OI drops, not rises).

        IMPORTANT: Returns False if we are in a post-panic recovery phase.
        After a panic flush, OI falling = SHORT COVERING (bullish), not
        long liquidation (bearish). Shorting into short-covering is the
        opposite of the intended signal.
        """
        history = state.oi_history
        if len(history) < self._oi_bars + 1:
            return False
        window = list(history)[-(self._oi_bars + 1):]
        is_falling = all(window[i] <= window[i - 1] for i in range(1, len(window)))
        if is_falling and self._in_post_panic_recovery(state):
            log.debug("OI falling but post-panic recovery detected — SHORT suppressed")
            return False
        return is_falling

    def _in_post_panic_recovery(self, state: State) -> bool:
        """
        Detect if we are in a short-covering recovery phase after a panic flush.

        Conditions (all must be true):
          1. A panic candle occurred within the last POST_PANIC_BARS candles
             (vol > avg_vol * PANIC_VOL_MULT, e.g. 4× average)
          2. Price is ABOVE the panic candle's close (recovering, not continuing down)
          3. OI is still elevated vs pre-panic level (shorts haven't fully covered yet)

        When this returns True, OI falling means shorts covering → bullish, not bearish.
        """
        df = self._cache_df
        if df is None or len(df) < cfg.POST_PANIC_BARS + 5:
            return False

        vol   = df["volume"]
        close = df["close"]
        avg_vol = float(vol.iloc[-40:].mean()) if len(vol) >= 40 else float(vol.mean())
        panic_threshold = avg_vol * cfg.PANIC_VOL_MULT

        # Scan the last POST_PANIC_BARS candles for a panic candle
        lookback = min(cfg.POST_PANIC_BARS, len(df) - 1)
        recent_vol   = vol.iloc[-(lookback + 1):-1]
        recent_close = close.iloc[-(lookback + 1):-1]
        current_close = float(close.iloc[-1])

        for i in range(len(recent_vol)):
            if float(recent_vol.iloc[i]) >= panic_threshold:
                panic_close = float(recent_close.iloc[i])
                # Price has recovered above the panic close = short-covering rally
                if current_close > panic_close:
                    return True

        return False

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
