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
  - 'long'  — cross=bull + ADX>=40 + vol + no spike + EMA8>EMA21 + close>EMA100 + EMA100 rising
           OR — continuation: EMA8>EMA21 + ADX>=50 + vol + no spike + close>EMA100 + EMA100 rising
  - 'short' — cross=bear + ADX>=40 + vol + no spike + EMA8<EMA21 + close<EMA100 + EMA100 falling
           OR — continuation: EMA8<EMA21 + ADX>=50 + vol + no spike + close<EMA100 + EMA100 falling
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
ADX_MIN       = 40.0   # minimum ADX for crossover entries (raised from 30 — filters marginal momentum)
ADX_TREND_MIN = 50.0   # higher ADX required for trend-continuation entries (raised from 45)
ADX_SLOPE_BARS = 2     # ADX must be rising over this many bars (tightened from 3 — catches momentum exhaustion faster)
ADX_STRONG     = 50.0  # above this level, skip slope check — aligns with ADX_TREND_MIN; strong trends absorb 1-bar dips
EMA_TREND_SLOPE_BARS = 5  # EMA100 must be moving in trade direction over this many bars
VOL_MA        = 10     # volume average window
VOL_MULT      = 0.6    # volume must be at least 60% of average
SPIKE_ATR_MULT     = 1.5  # skip signal if candle range > 1.5× ATR (exhaustion spike)
SPIKE_LOCKOUT_BARS = 2    # candles to block entries after a massive volume spike
SPIKE_VOL_MULT     = 4.0  # spike is "massive" if volume > 4× average

# Warm-up: need at least this many closed candles before any signal
_MIN_BARS = max(EMA_SLOW, ADX_PERIOD, VOL_MA) + 5


class ScalpingStrategy:
    """Trend-following strategy using EMA crossover + ADX + volume + EMA100 bias."""

    def __init__(self) -> None:
        self._last_df_hash: Optional[int] = None
        self._cached_df: Optional[pd.DataFrame] = None
        self._spike_lockout_remaining: int = 0  # candles left in post-spike lockout
        self._spike_direction: str = "none"      # 'down' | 'up' — direction of the spike that triggered lockout
        self._last_signal_was_continuation: bool = False  # set by get_signal()
        self._cross_window_remaining: int = 0   # candles left to treat last cross as active
        self._cross_window_direction: str = "none"  # 'bull' | 'bear' | 'none'

    def was_continuation(self) -> bool:
        """True if the last non-'none' signal was a continuation (no fresh cross).
        Used by bot.py to block the very first post-restart trade when it is
        a continuation — we have no prior position history to know the trend age.
        """
        return self._last_signal_was_continuation

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

    def market_regime(self, state: State) -> str:
        """
        Classify current market condition using EMA + ADX.
        Returns: 'STRONG_TREND' | 'TREND' | 'CHOP'

        Used by risk_manager to select dynamic SL / TP / trail multipliers.

        STRONG_TREND — ADX >= 50 and EMA8 is at least 1.5×ATR away from EMA100
                       → wide SL to breathe, trail activates sooner, tight callback
        TREND        — ADX 45-49 and EMA8/21 clearly aligned
                       → normal SL and trail settings
        CHOP         — ADX < 45 (marginal momentum — crossover entries at ADX 40-44
                       still pass get_signal but get tighter SL/TP to reflect the
                       weaker momentum confirmation)
                       → tighter SL, trail activates later, smaller callback

        Boundary alignment with get_signal:
          ADX_MIN=40 (crossover entry floor) → entries at ADX 40-44 → CHOP params
          ADX 45-49                          → entries here        → TREND params
          ADX >= 50 (continuation floor)     → entries here        → STRONG_TREND (if ema_sep ok)
        """
        df = self._cached_df
        if df is None or df.empty:
            return "CHOP"  # safe default while warming up

        row       = df.iloc[-1]
        adx       = row["adx"]
        atr       = row["atr"]
        ema_fast  = row["ema_fast"]
        ema_slow  = row["ema_slow"]   # used for ema_aligned (mirrors get_signal's ema_gap_ok)
        ema_trend = row["ema_trend"]

        close          = row["close"]
        ema_separation = abs(ema_fast - ema_trend)        # EMA8 distance from EMA100
        # Mirror the same gap check used in get_signal: price must be >0.5×ATR
        # from EMA21 — if it isn't, the market is at equilibrium (choppy).
        ema_aligned = abs(close - ema_slow) > 0.5 * atr

        if adx >= 50 and ema_separation >= 1.5 * atr:
            regime = "STRONG_TREND"
        elif adx >= 45 and ema_aligned:
            regime = "TREND"
        else:
            # ADX 40-44: valid crossover signal but marginal momentum → tighter params
            # ADX < 40: no signal fires anyway, but safe default while warming up
            regime = "CHOP"

        log.debug(
            f"market_regime={regime}  adx={adx:.1f}  "
            f"ema_sep={ema_separation:.5f}  atr={atr:.5f}  ema_aligned={ema_aligned}"
        )
        return regime

    def get_signal(self, state: State) -> str:
        """Return 'long', 'short', or 'none'. Reuses cached DataFrame."""
        df = self._cached_df
        if df is None or df.empty:
            return "none"

        row = df.iloc[-1]

        # ADX slope: require ADX to be rising over last ADX_SLOPE_BARS candles.
        # Prevents entries when momentum is exhausting (high but falling ADX).
        # Exception: when ADX is already above ADX_STRONG (45), a post-spike dip
        # in ADX is noise — the trend is clearly intact so skip the slope check.
        if len(df) > ADX_SLOPE_BARS:
            adx_prev   = df["adx"].iloc[-1 - ADX_SLOPE_BARS]
            adx_rising = (row["adx"] >= ADX_STRONG) or (row["adx"] > adx_prev)
        else:
            adx_rising = True  # not enough history — don't block

        if not adx_rising:
            log.debug(
                f"ADX SLOPE filtered  adx={row['adx']:.1f}"
                f"  adx_{ADX_SLOPE_BARS}bars_ago={df['adx'].iloc[-1 - ADX_SLOPE_BARS]:.1f}"
            )

        adx_ok       = (row["adx"] >= ADX_MIN)       and adx_rising
        adx_trend_ok = (row["adx"] >= ADX_TREND_MIN) and adx_rising
        vol_ok       = row["volume"]       >= row["vol_avg"] * VOL_MULT
        spike_ok     = row["candle_range"] <= row["atr"] * SPIKE_ATR_MULT
        cross        = row["cross"]

        # ── Cumulative move guard: block entries at the top/bottom of a staircase pump/dump ──
        # Per-candle spike filter is blind when each candle looks normal but the
        # last N candles together represent an exhausted move (trade #6: 3
        # consecutive bull candles, each sub-threshold, cumulative +2.5× ATR).
        # Only applied to continuation signals — those are most vulnerable to chasing.
        _CUMULATIVE_BARS     = 4
        _CUMULATIVE_ATR_MULT = 2.0
        if len(df) >= _CUMULATIVE_BARS + 1:
            _ref_close     = df["close"].iloc[-1 - _CUMULATIVE_BARS]
            _net_move      = row["close"] - _ref_close
            _atr_limit     = row["atr"] * _CUMULATIVE_ATR_MULT
            _pump_extended = _net_move >  _atr_limit
            _dump_extended = _net_move < -_atr_limit
        else:
            _net_move = _atr_limit = 0.0
            _pump_extended = _dump_extended = False

        # ── Cross window: keep a fresh cross "active" for N candles ──────────
        # A bull/bear cross fires on exactly one candle. If volume or ADX aren't
        # ready that bar, the signal is permanently missed. The window counter
        # lets the confirmation catch up on the next 1–3 bars.
        # Reset the window if EMAs flip direction (cross is no longer valid).
        # In CHOP (ADX 40-44) the cross window is collapsed to 1 — the market has
        # marginal momentum and a 3-bar-old cross is too stale to act on safely.
        # Trade #7: bear cross fired at 18:23, entry fired on 3rd extension at
        # 18:26 with price already recovering — stale signal in a weak trend.
        _adx_for_window = row["adx"]
        _CROSS_WINDOW = 1 if _adx_for_window < 45 else 3
        ema_fast_now = row["ema_fast"]
        ema_slow_now = row["ema_slow"]
        if cross in ("bull", "bear"):
            self._cross_window_remaining = _CROSS_WINDOW
            self._cross_window_direction = cross
        elif self._cross_window_remaining > 0:
            if (self._cross_window_direction == "bull" and ema_fast_now > ema_slow_now) or \
               (self._cross_window_direction == "bear" and ema_fast_now < ema_slow_now):
                cross = self._cross_window_direction  # extend cross signal
                self._cross_window_remaining -= 1
                log.debug(
                    f"CROSS WINDOW extended  dir={cross}  "
                    f"remaining={self._cross_window_remaining}"
                )
            else:
                # EMAs flipped — cross is invalidated, kill the window
                self._cross_window_remaining = 0
                self._cross_window_direction = "none"

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

        # EMA21 distance guard: require price to be at least 0.5× ATR away from
        # EMA_SLOW before entering. Entries right at EMA21 are equilibrium — the
        # market has not yet committed to a direction and SL hits are frequent.
        atr_val = row["atr"]
        ema_gap_ok_long  = (close - ema_slow) >  0.5 * atr_val
        ema_gap_ok_short = (ema_slow - close) >  0.5 * atr_val

        # EMA100 slope filter: require EMA100 to be moving in trade direction.
        # A still-rising EMA100 means the long-term trend is bullish — a short
        # entry against it is a counter-trend fade with high SL risk (e.g.
        # trades 7+8 that fired after a STRONG_TREND long rally).
        # Falls back to True if not enough history — don't block on warmup.
        if len(df) > EMA_TREND_SLOPE_BARS:
            ema_trend_prev = df["ema_trend"].iloc[-1 - EMA_TREND_SLOPE_BARS]
            ema100_rising  = ema_trend > ema_trend_prev
            ema100_falling = ema_trend < ema_trend_prev
            # flat (exact equality) → both are False → blocks all directions.
            # Log only when EMA100 is flat (blocks both longs and shorts).
            if not ema100_rising and not ema100_falling:
                log.debug(
                    f"EMA100 SLOPE  rising={ema100_rising}  falling={ema100_falling}"
                    f"  ema100={ema_trend:.5f}  prev={ema_trend_prev:.5f}"
                )
        else:
            ema100_rising  = True
            ema100_falling = True

        if not spike_ok:
            log.debug(
                f"SPIKE filtered  range={row['candle_range']:.5f}"
                f"  atr_limit={row['atr'] * SPIKE_ATR_MULT:.5f}"
            )

        # Massive volume spike lockout: if a candle had vol > 4× average,
        # block new entries for SPIKE_LOCKOUT_BARS candles — direction is
        # unreliable immediately after an extreme spike.
        # Also record spike direction: a down-spike exhausts sellers, so the
        # continuation short is already over by the time lockout clears.
        vol_avg = row["vol_avg"] if row["vol_avg"] > 0 else 1
        if row["volume"] > vol_avg * SPIKE_VOL_MULT:
            self._spike_lockout_remaining = SPIKE_LOCKOUT_BARS
            prev_close = df["close"].iloc[-2] if len(df) >= 2 else close
            self._spike_direction = "down" if close < prev_close else "up"
            log.debug(
                f"SPIKE LOCKOUT set  vol={row['volume']:.0f}"
                f"  avg={vol_avg:.0f}  lockout={SPIKE_LOCKOUT_BARS} bars"
                f"  spike_dir={self._spike_direction}"
            )
            # Block on the spike candle itself — the move is already extracted.
            # Without this, the bull/bear cross or continuation on this same
            # candle fires before the lockout takes effect (trade #9 pattern).
            return "none"
        elif self._spike_lockout_remaining > 0:
            self._spike_lockout_remaining -= 1
            log.debug(f"SPIKE LOCKOUT active  remaining={self._spike_lockout_remaining}")
            return "none"
        elif self._spike_direction != "none":
            # First candle after lockout: block continuation in spike direction.
            # The spike already extracted that move — entries in the same
            # direction now are chasing an exhausted leg (trade #10 pattern).
            if self._spike_direction == "down" and in_downtrend:
                log.debug(
                    f"POST-SPIKE DIR filtered  spike_dir=down  signal=short"
                    f"  close={close:.4f}  ema21={ema_slow:.4f}"
                )
                self._spike_direction = "none"
                return "none"
            if self._spike_direction == "up" and in_uptrend:
                log.debug(
                    f"POST-SPIKE DIR filtered  spike_dir=up  signal=long"
                    f"  close={close:.4f}  ema21={ema_slow:.4f}"
                )
                self._spike_direction = "none"
                return "none"
            self._spike_direction = "none"  # opposite direction — clear and proceed

        # ── 1. Crossover entries (fresh cross signal) ─────────────────────────
        if cross == "bull" and adx_ok and vol_ok and spike_ok and in_uptrend and bias_long and ema_gap_ok_long and ema100_rising:
            log.info(
                f"SIGNAL long  |  cross=bull  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
            )
            self._last_signal_was_continuation = False
            return "long"

        if cross == "bear" and adx_ok and vol_ok and spike_ok and in_downtrend and bias_short and ema_gap_ok_short and ema100_falling:
            log.info(
                f"SIGNAL short |  cross=bear  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
            )
            self._last_signal_was_continuation = False
            return "short"

        # ── 2. Trend continuation entries (no fresh cross needed) ─────────────
        # Re-enters after SL+cooldown when trend is still strongly intact.
        # Requires ADX >= 50 (vs 40 for crossover) to avoid choppy re-entries.
        # EMA100 bias still enforced — never trade against long-term trend.
        if in_uptrend and adx_trend_ok and vol_ok and spike_ok and bias_long and ema_gap_ok_long and ema100_rising:
            if _pump_extended:
                log.debug(
                    f"CUMULATIVE MOVE filtered (long)  net={_net_move:.5f}  limit={_atr_limit:.5f}"
                )
            else:
                log.info(
                    f"SIGNAL long  |  continuation  adx={row['adx']:.1f}  "
                    f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
                )
                self._last_signal_was_continuation = True
                return "long"

        if in_downtrend and adx_trend_ok and vol_ok and spike_ok and bias_short and ema_gap_ok_short and ema100_falling:
            if _dump_extended:
                log.debug(
                    f"CUMULATIVE MOVE filtered (short)  net={_net_move:.5f}  limit={-_atr_limit:.5f}"
                )
            else:
                log.info(
                    f"SIGNAL short |  continuation  adx={row['adx']:.1f}  "
                    f"vol={row['volume']:.0f}  ema100={ema_trend:.4f}  close={close:.4f}"
                )
                self._last_signal_was_continuation = True
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
    df["vol_avg"]      = df["volume"].rolling(VOL_MA, min_periods=1).median()
    df["candle_range"] = df["high"] - df["low"]

    prev_fast = df["ema_fast"].shift(1)
    prev_slow = df["ema_slow"].shift(1)

    bull = (df["ema_fast"] > df["ema_slow"]) & (prev_fast <= prev_slow)
    bear = (df["ema_fast"] < df["ema_slow"]) & (prev_fast >= prev_slow)

    df["cross"] = "none"
    df.loc[bull, "cross"] = "bull"
    df.loc[bear, "cross"] = "bear"

    return df