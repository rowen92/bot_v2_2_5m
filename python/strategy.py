"""
strategy.py – Break & Retest scalping signal generator.

Pattern:
  ── Price breaks (closes) above a prior SWING HIGH  → the old resistance becomes support
  ── Price pulls back and retests that broken level (within STRUCTURE_TOUCH_ATR * ATR)
  ── A bullish confirmation candle (close > open) forms at the retest zone
  ── OI is rising (real money entering, not just liquidations) and above its mean
  ── No volatility spike (range < ATR * ATR_MAX_MULT)
  ── Volume is not abnormally low (dead-market filter)

  LONG  ▸ A candle within the last SWING_LOOKBACK bars CLOSED above the prior BREAK_LOOKBACK swing high
         ▸ Current price has pulled back to within STRUCTURE_TOUCH_ATR * ATR of that broken high
         ▸ Current candle is a bullish close (bounce off the retested level)
         ▸ OI has increased over the last OI_CONFIRM_BARS candles
         ▸ OI absolute level is above its rolling mean (OI_MEAN_BARS)
         ▸ Candle range < ATR * ATR_MAX_MULT    (spike filter)
         ▸ Volume ≥ avg_volume * VOLUME_MIN_MULT (dead-market filter)

  SHORT ▸ A candle within the last SWING_LOOKBACK bars CLOSED below the prior BREAK_LOOKBACK swing low
         ▸ Current price has pulled back (up) to within STRUCTURE_TOUCH_ATR * ATR of that broken low
         ▸ Current candle is bearish (OI rising) OR any body (OI falling = long liquidation)
         ▸ OI confirms (rising = new shorts entering; falling = longs liquidating)

  Returns 'long' | 'short' | 'none'
"""

from __future__ import annotations

import logging
from typing import Optional

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


def _find_broken_resistance(df: pd.DataFrame, swing_bars: int, break_lookback: int) -> Optional[float]:
    """
    Scan the last `swing_bars` closed candles (excluding current) for a breakout candle
    — i.e. a candle whose CLOSE exceeded the swing high of the `break_lookback` bars
    that preceded it.

    Returns the broken resistance level (old swing high) if found, else None.

    The returned level is what price should now retest from above (as support).
    """
    n = len(df)
    # We need at least break_lookback + swing_bars + 1 rows
    if n < break_lookback + swing_bars + 1:
        return None

    # Scan the last swing_bars candles (excluding the current candle at -1)
    for offset in range(1, swing_bars + 1):
        # Index of the candidate breakout candle
        break_idx = n - 1 - offset
        if break_idx < break_lookback:
            break

        candidate_close = float(df["close"].iloc[break_idx])
        # Swing high of the bars BEFORE the breakout candle
        pre_break_high = float(df["high"].iloc[break_idx - break_lookback: break_idx].max())

        if candidate_close > pre_break_high:
            return pre_break_high  # the broken resistance = retest support

    return None


def _find_broken_support(df: pd.DataFrame, swing_bars: int, break_lookback: int) -> Optional[float]:
    """
    Mirror of _find_broken_resistance for SHORT setups.

    Scans the last `swing_bars` candles for one whose CLOSE broke below the
    swing low of the preceding `break_lookback` bars.

    Returns the broken support level (old swing low) if found — this is the
    level price should retest from below (as resistance). Returns None otherwise.
    """
    n = len(df)
    if n < break_lookback + swing_bars + 1:
        return None

    for offset in range(1, swing_bars + 1):
        break_idx = n - 1 - offset
        if break_idx < break_lookback:
            break

        candidate_close = float(df["close"].iloc[break_idx])
        pre_break_low   = float(df["low"].iloc[break_idx - break_lookback: break_idx].min())

        if candidate_close < pre_break_low:
            return pre_break_low  # the broken support = retest resistance

    return None


# ── Strategy ───────────────────────────────────────────────────────────────────

class ScalpingStrategy:
    """
    Break & Retest scalper using price action + Open Interest confirmation.

    Entry logic:
      LONG  — a recent candle broke (closed above) a swing high.
              Price has now pulled back to retest that old level from above.
              A bullish confirmation candle fires the entry.

      SHORT — a recent candle broke (closed below) a swing low.
              Price has now pulled back (bounced up) to retest that old level from below.
              A bearish (or any, if OI is falling) candle fires the entry.
    """

    def __init__(self):
        self.atr_period    = cfg.ATR_PERIOD
        self.atr_max       = cfg.ATR_MAX_MULT
        self.vol_min       = cfg.VOLUME_MIN_MULT

        # How many recent bars to scan for the breakout candle
        self._swing_bars    = cfg.SWING_LOOKBACK        # e.g. 10
        # How many bars before the breakout to measure the prior swing level
        self._break_bars    = cfg.BREAK_LOOKBACK        # e.g. 20
        # How close (in ATR units) the retest must be to the broken level
        self._touch_mult    = cfg.STRUCTURE_TOUCH_ATR   # e.g. 0.5
        # How many consecutive OI readings must be rising/falling to confirm
        self._oi_bars       = cfg.OI_CONFIRM_BARS       # e.g. 3
        # Rolling window for OI mean filter
        self._oi_mean_bars  = cfg.OI_MEAN_BARS          # e.g. 20

        # Per-candle cache — refreshed when open_time changes
        self._cache_key: int   = -1
        self._cache_df         = None
        self._cache_atr: float = 0.0

    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_cache(self, state: State) -> None:
        """Rebuild DataFrame + ATR cache on each new closed candle."""
        last_open_time = state._candles[-1].open_time if state._candles else -1
        if self._cache_key != last_open_time:
            df              = state.to_dataframe()
            self._cache_atr = _atr(df, self.atr_period)
            self._cache_df  = df
            self._cache_key = last_open_time

    def get_signal(self, state: State) -> str:
        """
        Evaluate the current closed candle for a Break & Retest setup.
        Returns 'long', 'short', or 'none'.
        """
        min_bars = self._break_bars + self._swing_bars + self.atr_period + 5
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
        last_high  = float(high.iloc[-1])
        last_low   = float(low.iloc[-1])
        last_range = last_high - last_low

        # ── Shared filters ─────────────────────────────────────────────────────
        is_spike = last_range > atr * self.atr_max
        avg_vol       = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else float(vol.mean())
        enough_volume = float(vol.iloc[-1]) >= avg_vol * self.vol_min

        if is_spike or not enough_volume:
            if is_spike:
                log.debug(f"SPIKE filtered  range={last_range:.5f}  atr_limit={atr*self.atr_max:.5f}")
            if not enough_volume:
                log.debug(f"LOW_VOL filtered  vol={float(vol.iloc[-1]):.1f}  need={avg_vol*self.vol_min:.1f}")
            return "none"

        # ── OI checks ──────────────────────────────────────────────────────────
        oi_rising     = self._oi_is_rising(state)
        oi_falling    = self._oi_is_falling(state)
        oi_above_mean = self._oi_above_mean(state)

        touch_dist = atr * self._touch_mult
        is_bullish = last_close > last_open
        is_bearish = last_close < last_open

        # ── LONG: Break & Retest of a prior resistance ─────────────────────────
        # 1. A breakout candle closed above the prior swing high within SWING_LOOKBACK bars
        # 2. Current candle retests that old high from above (within touch_dist)
        # 3. Bullish confirmation body (green candle bouncing off the level)
        # 4. OI rising + above mean

        broken_resistance = _find_broken_resistance(df, self._swing_bars, self._break_bars)
        if broken_resistance is not None:
            # Retest: current low dips to within touch_dist above the broken level
            # and current close stays above it (confirmed hold)
            at_retest_long = (
                last_low  <= broken_resistance + touch_dist and
                last_close >= broken_resistance - touch_dist
            )
            if at_retest_long and is_bullish and oi_rising and oi_above_mean:
                log.debug(
                    f"LONG B&R  close={last_close:.4f}  broken_res={broken_resistance:.4f}"
                    f"  touch_dist={touch_dist:.4f}  oi_rising={oi_rising}"
                )
                return "long"
            elif at_retest_long:
                reasons = []
                if not is_bullish:      reasons.append("no_bullish_body")
                if not oi_rising:       reasons.append(f"oi_not_rising({self._oi_bars}bars)")
                if not oi_above_mean:   reasons.append(f"oi_below_mean({self._oi_mean_bars}bar)")
                log.debug(f"BLOCKED LONG_B&R  broken_res={broken_resistance:.4f}  " + "  ".join(reasons))

        # ── SHORT: Break & Retest of a prior support ───────────────────────────
        # 1. A breakdown candle closed below the prior swing low within SWING_LOOKBACK bars
        # 2. Current candle bounces up to retest that old low from below (within touch_dist)
        # 3. Bearish confirmation (if OI rising = new shorts) OR any body (if OI falling = long liq)
        # 4. OI confirms (rising or falling)

        broken_support = _find_broken_support(df, self._swing_bars, self._break_bars)
        if broken_support is not None:
            # Retest from below: current high pushes up to within touch_dist of broken level
            # and current close stays below it (confirmed rejection)
            at_retest_short = (
                last_high  >= broken_support - touch_dist and
                last_close <= broken_support + touch_dist
            )
            oi_confirms_short = oi_rising or oi_falling
            short_mean_ok     = oi_above_mean if oi_rising else True
            short_body_ok     = is_bearish    if oi_rising else True

            if at_retest_short and short_body_ok and oi_confirms_short and short_mean_ok:
                log.debug(
                    f"SHORT B&R  close={last_close:.4f}  broken_sup={broken_support:.4f}"
                    f"  touch_dist={touch_dist:.4f}  oi_rising={oi_rising}  oi_falling={oi_falling}"
                )
                return "short"
            elif at_retest_short:
                reasons = []
                if oi_rising and not is_bearish:    reasons.append("no_bearish_body(reversal_short)")
                if not oi_confirms_short:            reasons.append(f"oi_not_rising_or_falling({self._oi_bars}bars)")
                if oi_rising and not oi_above_mean:  reasons.append(f"oi_below_mean({self._oi_mean_bars}bar)")
                log.debug(f"BLOCKED SHORT_B&R  broken_sup={broken_support:.4f}  " + "  ".join(reasons))

        return "none"

    # ──────────────────────────────────────────────────────────────────────────

    def _oi_is_rising(self, state: State) -> bool:
        """Return True if OI has risen consecutively over the last OI_CONFIRM_BARS readings."""
        history = state.oi_history
        if len(history) < self._oi_bars + 1:
            log.debug("OI history too short — skipping trade")
            return False
        window = list(history)[-(self._oi_bars + 1):]
        return all(window[i] >= window[i - 1] for i in range(1, len(window)))

    def _oi_above_mean(self, state: State) -> bool:
        """Return True if the latest OI reading is above the rolling OI_MEAN_BARS mean."""
        history = state.oi_history
        if len(history) < self._oi_mean_bars:
            log.debug(
                f"OI mean filter: not enough data "
                f"({len(history)}/{self._oi_mean_bars}) — blocking trade"
            )
            return False
        window   = list(history)[-(self._oi_mean_bars):]
        mean_oi  = sum(window) / len(window)
        return history[-1] >= mean_oi

    def _oi_is_falling(self, state: State) -> bool:
        """
        Return True if OI has been consistently falling over the last
        OI_CONFIRM_BARS readings — indicates long liquidation / unwinding.

        Returns False during a post-panic short-covering recovery so we don't
        mistake shorts covering (bullish) for longs liquidating (bearish).
        """
        history = state.oi_history
        if len(history) < self._oi_bars + 1:
            return False
        window = list(history)[-(self._oi_bars + 1):]
        is_falling = all(window[i] <= window[i - 1] for i in range(1, len(window)))
        if is_falling and self._in_post_panic_recovery():
            log.debug("OI falling but post-panic recovery detected — SHORT suppressed")
            return False
        return is_falling

    def _in_post_panic_recovery(self) -> bool:
        """
        Detect a short-covering rally after a panic flush.

        Conditions (all must hold):
          1. A panic candle (vol > avg_vol * PANIC_VOL_MULT) within the last POST_PANIC_BARS bars
          2. Current price is ABOVE that panic candle's close (recovering, not continuing down)

        When True, OI falling = shorts covering → bullish signal, not a SHORT trigger.
        """
        df = self._cache_df
        if df is None or len(df) < cfg.POST_PANIC_BARS + 5:
            return False

        vol   = df["volume"]
        close = df["close"]
        avg_vol         = float(vol.iloc[-40:].mean()) if len(vol) >= 40 else float(vol.mean())
        panic_threshold = avg_vol * cfg.PANIC_VOL_MULT

        lookback      = min(cfg.POST_PANIC_BARS, len(df) - 1)
        recent_vol    = vol.iloc[-(lookback + 1):-1]
        recent_close  = close.iloc[-(lookback + 1):-1]
        current_close = float(close.iloc[-1])

        for i in range(len(recent_vol)):
            if float(recent_vol.iloc[i]) >= panic_threshold:
                if current_close > float(recent_close.iloc[i]):
                    return True
        return False

    # ──────────────────────────────────────────────────────────────────────────

    def indicator_snapshot(self, state: State) -> dict:
        """Return a dict of current indicator values (for logging/debugging).

        Populates the per-candle cache so the subsequent get_signal() call
        reuses it without rebuilding the DataFrame a second time.
        """
        min_bars = self._break_bars + self._swing_bars + self.atr_period + 5
        if state.candle_count() < min_bars:
            return {}

        self._refresh_cache(state)

        df  = self._cache_df
        atr = self._cache_atr
        vol = df["volume"]
        avg_vol = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else float(vol.mean())

        broken_res = _find_broken_resistance(df, self._swing_bars, self._break_bars)
        broken_sup = _find_broken_support(df, self._swing_bars, self._break_bars)
        latest_oi  = state.oi_history[-1] if state.oi_history else 0.0
        oi_mean    = (
            sum(list(state.oi_history)[-self._oi_mean_bars:]) / self._oi_mean_bars
            if len(state.oi_history) >= self._oi_mean_bars else 0.0
        )

        return {
            "mark_price":       state.mark_price,
            "atr":              round(atr, 5),
            "broken_resistance": round(broken_res, 4) if broken_res else None,
            "broken_support":    round(broken_sup, 4) if broken_sup else None,
            "touch_dist":       round(atr * self._touch_mult, 5),
            "oi_latest":        round(latest_oi, 2),
            "oi_mean":          round(oi_mean, 2),
            "oi_rising":        self._oi_is_rising(state),
            "oi_falling":       self._oi_is_falling(state),
            "vol_ratio":        round(float(vol.iloc[-1]) / avg_vol, 2) if avg_vol else 0,
            "candles":          state.candle_count(),
            "oi_readings":      len(state.oi_history),
        }
