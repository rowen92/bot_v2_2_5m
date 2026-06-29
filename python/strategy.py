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


def _trimmed_vol_mean(vol_series: pd.Series, window: int = 20, trim_pct: float = 0.10) -> float:
    """
    Volume baseline using a trimmed mean — drops the top `trim_pct` fraction
    of the window before averaging.

    Prevents a single spike candle from inflating the baseline and silencing
    the volume filter for the next `window` bars.

    e.g. window=20, trim_pct=0.10 → drops the 2 highest readings, averages the rest.
    """
    data = vol_series.iloc[-window:] if len(vol_series) >= window else vol_series
    sorted_vals = sorted(data)
    cut = max(1, int(len(sorted_vals) * trim_pct))   # at least 1 dropped
    trimmed = sorted_vals[:-cut]                       # drop top cut values
    return float(sum(trimmed) / len(trimmed)) if trimmed else float(data.mean())


def _find_broken_resistance(
    df: pd.DataFrame, swing_bars: int, break_lookback: int, atr: float = 0.0
) -> Optional[float]:
    """
    Scan the last `swing_bars` closed candles (excluding current) for a breakout candle
    — i.e. a candle whose CLOSE exceeded the swing high of the `break_lookback` bars
    that preceded it.

    Step 6 — Break strength filter:
    Requires the close to exceed the level by at least 0.3 × ATR so micro-breaks
    (1-tick pokes) are ignored. Falls back to any close > level when ATR is 0.

    Returns the broken resistance level (old swing high) if found, else None.
    The returned level is what price should now retest from above (as support).
    """
    n = len(df)
    if n < break_lookback + swing_bars + 1:
        return None

    min_break = atr * 0.3   # minimum penetration required (0 when ATR unavailable)

    for offset in range(1, swing_bars + 1):
        break_idx = n - 1 - offset
        if break_idx < break_lookback:
            break

        candidate_close = float(df["close"].iloc[break_idx])
        pre_break_high  = float(df["high"].iloc[break_idx - break_lookback: break_idx].max())

        if candidate_close > pre_break_high + min_break:
            return pre_break_high  # the broken resistance = retest support

    return None


def _find_broken_support(
    df: pd.DataFrame, swing_bars: int, break_lookback: int, atr: float = 0.0
) -> Optional[float]:
    """
    Mirror of _find_broken_resistance for SHORT setups.

    Step 6 — Break strength filter:
    Requires the close to exceed the level by at least 0.3 × ATR so micro-breaks
    are rejected. Falls back to any close < level when ATR is 0.

    Returns the broken support level (old swing low) if found — this is the
    level price should retest from below (as resistance). Returns None otherwise.
    """
    n = len(df)
    if n < break_lookback + swing_bars + 1:
        return None

    min_break = atr * 0.3

    for offset in range(1, swing_bars + 1):
        break_idx = n - 1 - offset
        if break_idx < break_lookback:
            break

        candidate_close = float(df["close"].iloc[break_idx])
        pre_break_low   = float(df["low"].iloc[break_idx - break_lookback: break_idx].min())

        if candidate_close < pre_break_low - min_break:
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
        self.atr_max       = cfg.ATR_MAX_MULT       # used as fallback ceiling only
        self.vol_min       = cfg.VOLUME_MIN_MULT

        # How many recent bars to scan for the breakout candle
        self._swing_bars    = cfg.SWING_LOOKBACK        # e.g. 10
        # How many bars before the breakout to measure the prior swing level
        self._break_bars    = cfg.BREAK_LOOKBACK        # e.g. 20
        # Base retest touch multiplier — overridden dynamically per candle (Step 7)
        self._touch_mult    = cfg.STRUCTURE_TOUCH_ATR   # e.g. 0.5
        # How many consecutive OI readings must be rising/falling to confirm
        self._oi_bars       = cfg.OI_CONFIRM_BARS       # e.g. 3
        # Rolling window for OI mean filter
        self._oi_mean_bars  = cfg.OI_MEAN_BARS          # e.g. 20

        # Per-candle cache — refreshed when open_time changes
        self._cache_key: int   = -1
        self._cache_df         = None
        self._cache_atr: float = 0.0

    # ── Dynamic regime helpers ─────────────────────────────────────────────────

    def _atr_percentile_rank(self, df: pd.DataFrame, lookback: int = 50) -> float:
        """
        Return the percentile rank (0.0–1.0) of the current ATR relative to
        the last `lookback` ATR values.

        0.0 = lowest ATR seen in lookback (very slow/quiet market)
        1.0 = highest ATR seen in lookback (very fast/volatile market)

        Used by Steps 7 & 8 to detect the current volatility regime.
        """
        if len(df) < lookback + self.atr_period:
            return 0.5  # not enough data — assume mid-range
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        prev_c = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_c).abs(),
            (low  - prev_c).abs(),
        ], axis=1).max(axis=1)
        atr_series = tr.ewm(com=self.atr_period - 1, adjust=False).mean()
        window     = atr_series.iloc[-(lookback + 1):-1]   # exclude current bar
        current    = float(atr_series.iloc[-1])
        if window.empty or window.max() == window.min():
            return 0.5
        rank = float((window < current).sum()) / len(window)
        return rank

    def _dynamic_touch_mult(self, df: pd.DataFrame) -> float:
        """
        Step 7 — Dynamic retest touch zone width.

        Scales STRUCTURE_TOUCH_ATR based on the current ATR percentile rank:
          Top 20% (fast market)    → tighter zone (0.35×) — only precise retests
          Bottom 20% (slow market) → wider zone  (0.70×) — price moves less, needs tolerance
          Middle 60%               → base config (0.50×)

        Rationale: in a fast market price snaps through levels cleanly; in a slow
        market it oscillates around the level before committing.
        """
        rank = self._atr_percentile_rank(df)
        if rank >= 0.80:
            mult = 0.35   # fast regime — tight zone
        elif rank <= 0.20:
            mult = 0.70   # slow regime — wide zone
        else:
            mult = self._touch_mult   # normal — use config value
        log.debug(f"touch_mult={mult:.2f}  atr_rank={rank:.2f}")
        return mult

    def _dynamic_atr_max(self, df: pd.DataFrame) -> float:
        """
        Step 8 — Dynamic spike filter multiplier.

        Uses the 80th percentile of the last 50 bars' (candle_range / ATR) ratio
        as the spike threshold instead of a fixed ATR_MAX_MULT.

        On a trending day with naturally big candles this threshold rises
        automatically, so valid breakout candles aren't rejected as spikes.
        On a quiet day it tightens, catching genuine anomalies.

        Clamped between 1.3× (minimum — always reject extreme outliers) and
        cfg.ATR_MAX_MULT × 1.5 (ceiling — never become too permissive).
        """
        if len(df) < 55:
            return self.atr_max   # fallback to config until enough data
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        prev_c = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_c).abs(),
            (low  - prev_c).abs(),
        ], axis=1).max(axis=1)
        atr_s  = tr.ewm(com=self.atr_period - 1, adjust=False).mean()
        # ratio of each candle's range to the ATR at that time
        ratios = ((high - low) / atr_s).iloc[-50:]
        ratios = ratios.replace([float("inf"), float("-inf")], float("nan")).dropna()
        if ratios.empty:
            return self.atr_max
        p80    = float(ratios.quantile(0.80))
        result = max(1.3, min(self.atr_max * 1.5, p80))
        log.debug(f"dynamic_atr_max={result:.2f}  p80_range_ratio={p80:.2f}")
        return result

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

        # Step 8 — Dynamic spike filter: 80th-percentile range/ATR ratio of
        # last 50 bars instead of fixed ATR_MAX_MULT.
        # Step 4 — Trimmed volume mean: drop top 10% to ignore spike candles.
        dynamic_atr_max = self._dynamic_atr_max(df)
        is_spike        = last_range > atr * dynamic_atr_max
        avg_vol         = _trimmed_vol_mean(vol, window=20, trim_pct=0.10)
        enough_volume   = float(vol.iloc[-1]) >= avg_vol * self.vol_min

        if is_spike or not enough_volume:
            if is_spike:
                log.debug(f"SPIKE filtered  range={last_range:.5f}  atr_limit={atr*dynamic_atr_max:.5f}  mult={dynamic_atr_max:.2f}")
            if not enough_volume:
                log.debug(f"LOW_VOL filtered  vol={float(vol.iloc[-1]):.1f}  need={avg_vol*self.vol_min:.1f}")
            return "none"

        # ── OI checks ──────────────────────────────────────────────────────────
        oi_rising     = self._oi_is_rising(state)
        oi_falling    = self._oi_is_falling(state)
        oi_above_mean = self._oi_above_mean(state)

        # Step 7 — Dynamic touch zone: tighter on fast days, wider on slow days.
        dynamic_touch = self._dynamic_touch_mult(df)
        touch_dist    = atr * dynamic_touch

        # Step 5 — Dynamic candle body strength: require body >= 0.3 × ATR so
        # doji / 1-tick closes don't count as bullish/bearish confirmation.
        min_body   = atr * 0.3
        body_size  = abs(last_close - last_open)
        is_bullish = last_close > last_open and body_size >= min_body
        is_bearish = last_close < last_open and body_size >= min_body

        # ── LONG: Break & Retest of a prior resistance ─────────────────────────
        # 1. A breakout candle closed above the prior swing high by > 0.3 × ATR (Step 6)
        # 2. Current candle retests that old high from above (within touch_dist)
        # 3. Bullish confirmation body >= 0.3 × ATR (Step 5)
        # 4. OI rising + above mean

        broken_resistance = _find_broken_resistance(df, self._swing_bars, self._break_bars, atr=atr)
        if broken_resistance is not None:
            at_retest_long = (
                last_low  <= broken_resistance + touch_dist and
                last_close >= broken_resistance - touch_dist
            )
            if at_retest_long and is_bullish and oi_rising and oi_above_mean:
                log.debug(
                    f"LONG B&R  close={last_close:.4f}  broken_res={broken_resistance:.4f}"
                    f"  touch_dist={touch_dist:.4f}  body={body_size:.5f}  min_body={min_body:.5f}"
                )
                return "long"
            elif at_retest_long:
                reasons = []
                if not is_bullish:      reasons.append(f"weak_body({body_size:.5f}<{min_body:.5f})" if body_size < min_body else "no_bullish_body")
                if not oi_rising:       reasons.append(f"oi_not_rising({self._oi_bars}bars)")
                if not oi_above_mean:   reasons.append(f"oi_below_mean({self._oi_mean_bars}bar)")
                log.debug(f"BLOCKED LONG_B&R  broken_res={broken_resistance:.4f}  " + "  ".join(reasons))

        # ── SHORT: Break & Retest of a prior support ───────────────────────────
        # 1. A breakdown candle closed below the prior swing low by > 0.3 × ATR (Step 6)
        # 2. Current candle bounces up to retest that old low from below (within touch_dist)
        # 3. Bearish confirmation body >= 0.3 × ATR (Step 5, if OI rising)
        # 4. OI confirms (rising or falling)

        broken_support = _find_broken_support(df, self._swing_bars, self._break_bars, atr=atr)
        if broken_support is not None:
            at_retest_short = (
                last_high  >= broken_support - touch_dist and
                last_close <= broken_support + touch_dist
            )
            oi_confirms_short = oi_rising or oi_falling
            short_mean_ok     = oi_above_mean if oi_rising else True
            short_body_ok     = is_bearish    if oi_rising else True   # body check already includes min_body

            if at_retest_short and short_body_ok and oi_confirms_short and short_mean_ok:
                log.debug(
                    f"SHORT B&R  close={last_close:.4f}  broken_sup={broken_support:.4f}"
                    f"  touch_dist={touch_dist:.4f}  body={body_size:.5f}  min_body={min_body:.5f}"
                )
                return "short"
            elif at_retest_short:
                reasons = []
                if oi_rising and not is_bearish:    reasons.append(f"weak_body({body_size:.5f}<{min_body:.5f})" if body_size < min_body else "no_bearish_body(reversal_short)")
                if not oi_confirms_short:            reasons.append(f"oi_not_rising_or_falling({self._oi_bars}bars)")
                if oi_rising and not oi_above_mean:  reasons.append(f"oi_below_mean({self._oi_mean_bars}bar)")
                log.debug(f"BLOCKED SHORT_B&R  broken_sup={broken_support:.4f}  " + "  ".join(reasons))

        return "none"

    # ──────────────────────────────────────────────────────────────────────────

    # ── Dynamic OI helpers ────────────────────────────────────────────────────

    def _oi_dynamic_threshold(self, history: list) -> float:
        """
        Compute a dynamic OI move threshold based on recent OI volatility.

        Uses the std-dev of the last OI_MEAN_BARS readings.  When OI is
        bouncing around a lot (choppy day) the threshold rises so we only
        fire on *real* moves.  On a trending day std-dev is low and the
        threshold stays near the floor.

        Returns a minimum % change (as a fraction, e.g. 0.0003 = 0.03%)
        that each OI reading must exceed its predecessor.
        """
        if len(history) < self._oi_mean_bars:
            return 0.0  # not enough data — fall back to direction-only check
        window = history[-(self._oi_mean_bars):]
        mean   = sum(window) / len(window)
        if mean == 0:
            return 0.0
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std_dev  = variance ** 0.5
        # Normalise: std as % of mean, then halve it so threshold isn't too strict
        dynamic_pct = (std_dev / mean) * 0.5
        # Floor at 0.01% (always need some move), cap at 0.10% (don't over-filter)
        return max(0.0001, min(0.0010, dynamic_pct))

    def _oi_is_rising(self, state: State) -> bool:
        """
        Return True if OI has risen over the last OI_CONFIRM_BARS readings
        by more than a dynamic threshold based on session OI volatility.

        On choppy/flat days the threshold rises so random OI oscillations
        don't trigger entries.  On trending days the threshold stays low.
        """
        history = list(state.oi_history)
        if len(history) < self._oi_bars + 1:
            log.debug("OI history too short — skipping trade")
            return False
        threshold = self._oi_dynamic_threshold(history)
        window = history[-(self._oi_bars + 1):]
        return all(
            window[i] >= window[i - 1] * (1 + threshold)
            for i in range(1, len(window))
        )

    def _oi_above_mean(self, state: State) -> bool:
        """
        Return True if the latest OI reading is *meaningfully* above the
        rolling OI_MEAN_BARS mean — by at least 1× the dynamic threshold.

        On a choppy day, OI sitting barely above the mean is noise; this
        filter requires a statistically significant excess.
        """
        history = list(state.oi_history)
        if len(history) < self._oi_mean_bars:
            log.debug(
                f"OI mean filter: not enough data "
                f"({len(history)}/{self._oi_mean_bars}) — blocking trade"
            )
            return False
        window    = history[-(self._oi_mean_bars):]
        mean_oi   = sum(window) / len(window)
        threshold = self._oi_dynamic_threshold(history)
        # Require OI to be above mean by at least threshold % of mean
        return history[-1] >= mean_oi * (1 + threshold)

    def _oi_is_falling(self, state: State) -> bool:
        """
        Return True if OI has been consistently falling over the last
        OI_CONFIRM_BARS readings by more than the dynamic threshold —
        indicates real long liquidation / unwinding, not just noise.

        Returns False during a post-panic short-covering recovery so we don't
        mistake shorts covering (bullish) for longs liquidating (bearish).
        """
        history = list(state.oi_history)
        if len(history) < self._oi_bars + 1:
            return False
        threshold = self._oi_dynamic_threshold(history)
        window = history[-(self._oi_bars + 1):]
        is_falling = all(
            window[i] <= window[i - 1] * (1 - threshold)
            for i in range(1, len(window))
        )
        if is_falling and self._in_post_panic_recovery():
            log.debug("OI falling but post-panic recovery detected — SHORT suppressed")
            return False
        return is_falling

    def _dynamic_post_panic_bars(self, panic_vol: float, avg_vol: float) -> int:
        """
        Step 9 — Dynamic panic suppression window.

        Scales the suppression lookback based on how extreme the panic candle was:
            window = 10 + (panic_vol / avg_vol) × 3   capped at 30 bars

        A borderline panic (4× avg) → 10 + 12 = 22 bars
        A severe panic    (8× avg) → 10 + 24 = 30 bars (capped)

        This prevents over-suppressing after mild flushes and ensures severe
        ones are suppressed long enough for the short-covering to complete.
        """
        ratio  = panic_vol / avg_vol if avg_vol > 0 else cfg.PANIC_VOL_MULT
        window = int(10 + ratio * 3)
        window = max(cfg.POST_PANIC_BARS // 2, min(30, window))   # clamp 10–30
        log.debug(f"post_panic_bars={window}  panic_ratio={ratio:.1f}")
        return window

    def _in_post_panic_recovery(self) -> bool:
        """
        Detect a short-covering rally after a panic flush.

        Conditions (all must hold):
          1. A panic candle (vol > avg_vol * PANIC_VOL_MULT) within the last
             dynamic suppression window (Step 9 — scales with panic severity)
          2. Current price is ABOVE that panic candle's close (recovering, not continuing down)

        When True, OI falling = shorts covering → bullish signal, not a SHORT trigger.
        """
        df = self._cache_df
        if df is None or len(df) < 15:
            return False

        vol   = df["volume"]
        close = df["close"]
        avg_vol         = _trimmed_vol_mean(vol, window=40, trim_pct=0.10)
        panic_threshold = avg_vol * cfg.PANIC_VOL_MULT
        current_close   = float(close.iloc[-1])

        # Scan backwards for the most recent panic candle, then use its
        # severity to compute the dynamic suppression window for that candle.
        max_lookback = 30   # never look back more than 30 bars
        recent_vol   = vol.iloc[-(max_lookback + 1):-1]
        recent_close = close.iloc[-(max_lookback + 1):-1]

        for i in range(len(recent_vol) - 1, -1, -1):   # newest first
            candle_vol = float(recent_vol.iloc[i])
            if candle_vol >= panic_threshold:
                # Compute dynamic window for this specific panic candle
                dynamic_window = self._dynamic_post_panic_bars(candle_vol, avg_vol)
                # Check if this candle is still within its suppression window
                bars_ago = len(recent_vol) - 1 - i
                if bars_ago <= dynamic_window:
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
        avg_vol = _trimmed_vol_mean(vol, window=20, trim_pct=0.10)

        broken_res = _find_broken_resistance(df, self._swing_bars, self._break_bars, atr=atr)
        broken_sup = _find_broken_support(df, self._swing_bars, self._break_bars, atr=atr)
        latest_oi  = state.oi_history[-1] if state.oi_history else 0.0
        oi_mean    = (
            sum(list(state.oi_history)[-self._oi_mean_bars:]) / self._oi_mean_bars
            if len(state.oi_history) >= self._oi_mean_bars else 0.0
        )

        return {
            "mark_price":        state.mark_price,
            "atr":               round(atr, 5),
            "broken_resistance": round(broken_res, 4) if broken_res else None,
            "broken_support":    round(broken_sup, 4) if broken_sup else None,
            "touch_dist":        round(atr * self._touch_mult, 5),
            "oi_latest":         round(latest_oi, 2),
            "oi_mean":           round(oi_mean, 2),
            "oi_rising":         self._oi_is_rising(state),
            "oi_falling":        self._oi_is_falling(state),
            "vol_ratio":         round(float(vol.iloc[-1]) / avg_vol, 2) if avg_vol else 0,
            "min_body":          round(atr * 0.3, 5),
            "touch_mult":        round(self._dynamic_touch_mult(df), 2),
            "atr_max_mult":      round(self._dynamic_atr_max(df), 2),
            "atr_rank":          round(self._atr_percentile_rank(df), 2),
            "candles":           state.candle_count(),
            "oi_readings":       len(state.oi_history),
        }
