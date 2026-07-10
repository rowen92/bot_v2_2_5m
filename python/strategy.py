"""
strategy.py – Trend-Following Strategy

Entry logic:
  - EMA crossover (fast crosses above/below slow) to detect trend direction immediately
  - ADX > threshold to confirm real momentum (filters out choppy sideways markets)
  - Volume above rolling average to confirm participation
  - ATR computed for SL/TP sizing in risk_manager / order_manager
  - EMA 50 as long-term bias filter — only long above it, only short below it
  - Trend continuation re-entry — re-enter in trend direction after SL/cooldown
    when EMA alignment + ADX still strong, no fresh cross required

Signal:
  - 'long'  — cross=bull + ADX>=40 + vol + no spike + EMA8>EMA21 + close>EMA50 + EMA50 rising
           OR — continuation: EMA8>EMA21 + ADX>=50 + vol + no spike + close>EMA50 + EMA50 rising
  - 'short' — cross=bear + ADX>=40 + vol + no spike + EMA8<EMA21 + close<EMA50 + EMA50 falling
           OR — continuation: EMA8<EMA21 + ADX>=50 + vol + no spike + close<EMA50 + EMA50 falling
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
EMA_TREND     = 50     # long-term bias — only long above, only short below
ADX_PERIOD    = 14     # standard ADX window for 5m — balances reactivity and smoothness
ADX_MIN       = 25.0   # minimum ADX for crossover entries — lowered to catch early-trend entries
                       # where ADX is building (20→25→30→35) with a fresh EMA cross.
                       # The adx_rising (slope) check is the real guard — a rising ADX at 25
                       # with a fresh cross is a valid early entry. Requiring 40 means always
                       # entering late after the best price is gone.
ADX_TREND_MIN = 45.0   # higher ADX required for trend-continuation entries (lowered from 50: post-breakout ADX consolidates around 43-47 which is still valid trend)
ADX_SLOPE_BARS = 3     # ADX must be rising over this many bars (5m is smoother; 3 bars = 15 min confirmation)
ADX_STRONG     = 45.0  # above this level, skip ADX slope check — after a breakout ADX naturally pulls back
                       # from its peak while trend is still intact; requiring rising ADX during a healthy
                       # consolidation blocks all continuation entries in the 17:10-17:55 zone.
EMA_TREND_SLOPE_BARS = 3  # EMA50 must be moving in trade direction over this many bars
VOL_MA        = 10     # volume average window
VOL_MULT      = 0.6    # volume must be at least 60% of average
SPIKE_ATR_MULT          = 1.5  # skip signal if candle range > 1.5× ATR (exhaustion spike)
SPIKE_ATR_MULT_TREND    = 2.5  # relaxed spike limit for continuation entries in STRONG_TREND
                                # (a big breakout candle IS the momentum — blocking it costs the entry)
SPIKE_LOCKOUT_BARS = 2    # candles to block entries after a massive volume spike
SPIKE_VOL_MULT     = 4.0  # spike is "massive" if volume > 4× average

# Warm-up: need at least this many closed candles before any signal.
# Exhaustion entries (section 1b) bypass EMA50 bias/slope — those require
# EMA50 convergence (~35 bars) but exhaustion only needs:
#   - ADX stable (14 bars + small buffer)
#   - EMA8/21 cross reliable (21 bars)
#   - vol_avg median (10 bars)
# +4 gives ADX room to form its first peak/fall pattern (needs 4 ADX values).
_MIN_BARS = max(EMA_SLOW, ADX_PERIOD, VOL_MA) + 4  # → 25 candles (~2h05m)


class ScalpingStrategy:
    """Trend-following strategy using EMA crossover + ADX + volume + EMA50 bias."""

    def __init__(self) -> None:
        self._last_df_hash: Optional[int] = None
        self._cached_df: Optional[pd.DataFrame] = None
        self._spike_lockout_remaining: int = 0  # candles left in post-spike lockout
        self._spike_direction: str = "none"      # 'down' | 'up' — direction of the spike that triggered lockout
        self._last_signal_was_continuation: bool = False       # set by get_signal()
        self._last_signal_was_exhaustion_reversal: bool = False # set by get_signal()
        self._last_signal_was_di_snap: bool = False             # set by get_signal()
        self._di_snap_levels: dict = {}                         # {"sl": float, "tp": float}
        self._cross_window_remaining: int = 0   # candles left to treat last cross as active
        self._cross_window_direction: str = "none"  # 'bull' | 'bear' | 'none'
        # Exhaustion-armed state: set when ADX peak is detected in the current trend.
        # Entry fires only when the subsequent cross confirms the reversal.
        self._short_armed: bool = False          # uptrend exhausting → waiting for bear cross
        self._long_armed: bool = False           # downtrend exhausting → waiting for bull cross
        self._short_armed_remaining: int = 0    # candles before SHORT arm expires
        self._long_armed_remaining: int = 0     # candles before LONG arm expires

    def was_continuation(self) -> bool:
        """True if the last non-'none' signal was a continuation (no fresh cross).
        Used by bot.py to block the very first post-restart trade when it is
        a continuation — we have no prior position history to know the trend age.
        """
        return self._last_signal_was_continuation

    def was_exhaustion_reversal(self) -> bool:
        """True if the last non-'none' signal was an exhaustion reversal.
        Used by bot.py to bypass the ADX >= 40 flip guard — exhaustion reversals
        fire precisely when ADX is falling, so the guard would always suppress them.
        """
        return self._last_signal_was_exhaustion_reversal

    def was_di_snap(self) -> bool:
        """True if the last signal was a DI-snap exhaustion entry.
        Used by order_manager to apply the DI-snap specific SL/TP:
          SL = trigger candle high/low
          TP = EMA21 at signal time (fixed mean-reversion target)
        """
        return self._last_signal_was_di_snap

    def di_snap_levels(self) -> dict:
        """Return the SL and TP levels captured at the time of the DI-snap signal.
        sl  = trigger candle high (short) or low (long)
        tp  = EMA21 at signal time
        """
        return self._di_snap_levels

    def last_ema21(self) -> float:
        """Return EMA21 (ema_slow) from the most recently cached DataFrame.
        Used by order_manager to set TP2 for exhaustion-armed entries at open.
        Returns 0.0 if the DataFrame is not yet available.
        """
        if self._cached_df is None or self._cached_df.empty:
            return 0.0
        return float(self._cached_df["ema_slow"].iloc[-1])

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
            "plus_di":   round(row["plus_di"],   2),
            "minus_di":  round(row["minus_di"],  2),
            "atr":       round(row["atr"],       cfg.PRICE_PRECISION + 1),
            "volume":    round(row["volume"],    2),
            "vol_avg":   round(row["vol_avg"],   2),
            "cross":     row["cross"],   # 'bull' | 'bear' | 'none'
            "close":     round(row["close"], cfg.PRICE_PRECISION),
        }

    def market_regime(self, state: State) -> str:
        """
        Classify current market condition using EMA separation + ADX.
        Returns: 'STRONG_TREND' | 'TREND' | 'CHOP'

        Used by risk_manager to select dynamic SL / TP / trail multipliers.

        Calibrated from WLD 5m live data (24h, 300 candles):
          ADX on this asset lives in 20–67. The old ADX≥50 TREND threshold meant
          ~89% of candles were CHOP, even when EMA8 was visibly separated from EMA50
          and directional moves were clearly trending (ADX 35–49, ema_sep ≥ 1.0×ATR).

        STRONG_TREND — ADX ≥ 50 AND ema_sep ≥ 1.5×ATR
                       Strong momentum + wide EMA gap. ~11% of candles.
                       → widest SL (2.5×ATR), trail activates sooner

        TREND        — ADX ≥ 35 AND ema_sep ≥ 1.0×ATR
                       Real directional move at moderate momentum. ~35% of candles.
                       Captures the ADX 35–49 sessions (T12, T13, T17 all fired here
                       and had genuine trending room — were mislabelled CHOP before).
                       → normal SL (2.0×ATR)

        CHOP         — everything else (ADX < 35, or narrow ema_sep despite high ADX)
                       True low-momentum / equilibrium market. ~54% of candles.
                       → tight SL (1.5×ATR), flat TP exits
        """
        df = self._cached_df
        if df is None or df.empty:
            return "CHOP"  # safe default while warming up

        row       = df.iloc[-1]
        adx       = row["adx"]
        atr       = row["atr"]
        ema_fast  = row["ema_fast"]
        ema_slow  = row["ema_slow"]
        ema_trend = row["ema_trend"]

        close          = row["close"]
        ema_separation = abs(ema_fast - ema_trend)   # EMA8 distance from EMA50

        if adx >= 50 and ema_separation >= 1.5 * atr:
            regime = "STRONG_TREND"
        elif adx >= 35 and ema_separation >= 1.0 * atr:
            # ADX 35–49 with a meaningful EMA8/EMA50 gap = real trend, not chop.
            # ema_sep ≥ 1.0×ATR ensures the separation is genuine (not noise at ADX 35).
            regime = "TREND"
        else:
            regime = "CHOP"

        log.debug(
            f"market_regime={regime}  adx={adx:.1f}  "
            f"ema_sep={ema_separation:.5f}  ema_sep_atr={ema_separation/atr:.2f}x"
            f"  atr={atr:.5f}"
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
        # Exception: when ADX is already above ADX_STRONG (50), a post-spike dip
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
        # High-volume breakout candles are genuine momentum, not exhaustion spikes.
        # If volume is > 3× average, relax the spike limit to SPIKE_ATR_MULT_TREND
        # so a real institutional breakout candle is never blocked by the spike filter.
        is_volume_breakout = row["volume"] >= row["vol_avg"] * 3.0
        _spike_mult_cross = SPIKE_ATR_MULT_TREND if is_volume_breakout else SPIKE_ATR_MULT
        spike_ok     = row["candle_range"] <= row["atr"] * _spike_mult_cross
        # Continuation entries in STRONG_TREND use a relaxed spike limit:
        # a large breakout candle is momentum, not exhaustion.
        regime           = self.market_regime(state)
        _spike_mult_cont = SPIKE_ATR_MULT_TREND if (regime == "STRONG_TREND" or is_volume_breakout) else SPIKE_ATR_MULT
        spike_ok_cont    = row["candle_range"] <= row["atr"] * _spike_mult_cont
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
        # When ADX < 45 the window is 3 bars (raised from 1) to allow ADX time
        # to confirm a slow, gradual breakout. On smooth trends (e.g. SUI) ADX
        # lags the EMA cross by 3-4 bars — a 1-bar window permanently missed the
        # entry (SUI 10:50 cross, ADX reached 40 only at 10:52).
        # Other guards (adx_ok ≥ 40, ema_sep_ok, ema_gap_ok) still block stale
        # crosses — the longer window only keeps the signal alive, not relaxed.
        # Trade #7 risk (stale cross in weak trend): was adx < 30 at cross time,
        # adx_ok (≥ 40) would have blocked it regardless of window length.
        _adx_for_window = row["adx"]
        _CROSS_WINDOW = 3
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

        # EMA50 bias: only long above, only short below
        # Conflict zone (e.g. price > EMA50 but EMA8 < EMA21) → no trade
        bias_long  = close > ema_trend
        bias_short = close < ema_trend

        # Pullback entry guard: price must have pulled back CLOSE to EMA21 before
        # entering — not extended far away from it. Entering when price is already
        # 1.5+ ATR from EMA21 means chasing an exhausted leg (the move has already
        # happened). The sweet spot is: trend confirmed (EMA8 > EMA21 > EMA50 aligned,
        # ADX high) but price has retraced near EMA21, giving a low-risk entry with
        # room to run.
        # Minimum floor of 0.1×ATR kept: don't enter exactly at EMA21 (equilibrium).
        # Maximum ceiling of 1.5×ATR: don't chase price that is already extended.
        atr_val = row["atr"]
        PULLBACK_MIN_ATR = 0.1   # price must be at least this far above/below EMA21 (on correct side)
        PULLBACK_MAX_ATR = 1.5   # price must be no more than this far from EMA21
        ema_gap_ok_long  = PULLBACK_MIN_ATR * atr_val < (close - ema_slow) <= PULLBACK_MAX_ATR * atr_val
        ema_gap_ok_short = PULLBACK_MIN_ATR * atr_val < (ema_slow - close) <= PULLBACK_MAX_ATR * atr_val

        # EMA8-vs-EMA50 separation guard: EMA8 must be at least 0.5×ATR from
        # EMA50 before any entry. Near-zero separation means price is at
        # equilibrium with the long-term trend — cross/continuation signals here
        # are phantom entries inside a range, not genuine breakouts.
        # Covers: phantom crosses in TREND (trade #9 ema_sep=0.14×ATR),
        #         CHOP crosses with zero separation (trade #11 ema_sep=0.05×ATR),
        #         fading continuation entries (trade #5 ema_sep=0.26×ATR).
        _ema_sep = abs(ema_fast - ema_trend)
        ema_sep_ok       = _ema_sep >= 0.5 * atr_val   # continuations / DI-snap / armed
        ema_sep_ok_cross = _ema_sep >= 0.1 * atr_val   # fresh crossover — EMAs just met, naturally close
        if not ema_sep_ok_cross:
            log.debug(
                f"EMA SEP filtered  ema_sep={_ema_sep:.5f}"
                f"  threshold={0.1 * atr_val:.5f}"
            )

        # EMA50 slope filter: require EMA50 to be moving in trade direction
        # AND by a meaningful amount (at least 0.5×ATR over EMA_TREND_SLOPE_BARS).
        # Direction alone is not enough — a flat EMA50 that ticks 1 pip counts
        # as "falling" and would approve a SHORT into dead chop (e.g. DOGE T1:
        # EMA50 moved only 0.000026 over 3 bars = 0.33×ATR while visually flat).
        # Falls back to True if not enough history — don't block on warmup.
        ema50_slope_min = row["atr"] * 0.15  # minimum meaningful EMA50 movement — low enough for slow EMA50 to clear during real trends, high enough to block near-zero noise
        if len(df) > EMA_TREND_SLOPE_BARS:
            ema_trend_prev = df["ema_trend"].iloc[-1 - EMA_TREND_SLOPE_BARS]
            ema50_delta   = ema_trend - ema_trend_prev
            ema50_rising  = ema50_delta >  ema50_slope_min
            ema50_falling = ema50_delta < -ema50_slope_min
            # flat (below threshold in either direction) → both are False → blocks all.
            if not ema50_rising and not ema50_falling:
                log.debug(
                    f"EMA50 SLOPE flat  delta={ema50_delta:.6f}"
                    f"  min={ema50_slope_min:.6f}"
                    f"  ema50={ema_trend:.5f}  prev={ema_trend_prev:.5f}"
                )
        else:
            ema50_rising  = True
            ema50_falling = True

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
            # Exception: in STRONG_TREND (adx >= 50) a spike in the trend
            # direction is momentum, not exhaustion — the trend resumes after
            # the spike candle clears so blocking here costs a valid entry
            # (trades #1 02:02, #2 01:18, #3 02:02 were all blocked this way).
            # Mirror the market_regime STRONG_TREND criteria: ADX >= 50 AND
            # EMA8-vs-EMA50 separation >= 1.5×ATR. Both must hold — high ADX
            # alone can occur in volatile chop (e.g. trade #2: adx=67 but
            # ema_sep=0.78×ATR, price oscillating in a range). Only when both
            # conditions are met is a spike truly momentum rather than exhaustion.
            _ema_sep = abs(ema_fast - ema_trend)
            _is_strong_trend = row["adx"] >= 50 and _ema_sep >= 1.5 * atr_val
            if self._spike_direction == "down" and in_downtrend:
                if _is_strong_trend:
                    log.debug(
                        f"POST-SPIKE DIR skipped  spike_dir=down  adx={row['adx']:.1f}"
                        f"  ema_sep={_ema_sep:.5f}  (STRONG_TREND — spike is momentum)"
                    )
                else:
                    log.debug(
                        f"POST-SPIKE DIR filtered  spike_dir=down  signal=short"
                        f"  close={close:.4f}  ema21={ema_slow:.4f}"
                    )
                    self._spike_direction = "none"
                    return "none"
            if self._spike_direction == "up" and in_uptrend:
                if _is_strong_trend:
                    log.debug(
                        f"POST-SPIKE DIR skipped  spike_dir=up  adx={row['adx']:.1f}"
                        f"  ema_sep={_ema_sep:.5f}  (STRONG_TREND — spike is momentum)"
                    )
                else:
                    log.debug(
                        f"POST-SPIKE DIR filtered  spike_dir=up  signal=long"
                        f"  close={close:.4f}  ema21={ema_slow:.4f}"
                    )
                    self._spike_direction = "none"
                    return "none"
            self._spike_direction = "none"  # opposite direction — clear and proceed

        # ── 1. Crossover entries (fresh cross signal) ─────────────────────────
        # Standard cross entries (trend-following, no exhaustion arm needed).
        if cross == "bull" and adx_ok and vol_ok and spike_ok and in_uptrend and bias_long and ema_gap_ok_long and ema_sep_ok_cross:
            _pb_dist = (close - ema_slow) / atr_val
            log.info(
                f"SIGNAL long  |  cross=bull  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema50={ema_trend:.4f}  close={close:.4f}  "
                f"pullback={_pb_dist:.2f}x ATR from EMA21"
            )
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = False
            self._last_signal_was_di_snap = False
            self._long_armed = False   # consumed
            return "long"

        if cross == "bear" and adx_ok and vol_ok and spike_ok and in_downtrend and bias_short and ema_gap_ok_short and ema_sep_ok_cross:
            _pb_dist = (ema_slow - close) / atr_val
            log.info(
                f"SIGNAL short |  cross=bear  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema50={ema_trend:.4f}  close={close:.4f}  "
                f"pullback={_pb_dist:.2f}x ATR from EMA21"
            )
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = False
            self._last_signal_was_di_snap = False
            self._short_armed = False  # consumed
            return "short"

        # ── 1b. Exhaustion-armed entries (no cross required) ──────────────────
        # Fires when a prior exhaustion detection (ADX peak while still in trend)
        # armed the flag. Cross requirement removed: by the time EMA8 crosses EMA21
        # the best entry price is already gone, and ema_sep_ok is naturally tiny
        # at cross time (EMA8 just flipped through EMA21), making the cross guard
        # self-defeating.
        #
        # Guards used instead of cross:
        #   - ema_sep_ok: EMA8 must be >= 0.5×ATR from EMA50 — ensures price is
        #     not at equilibrium and has real room to mean-revert toward EMA21/EMA50
        #   - close < ema_slow (LONG) / close > ema_slow (SHORT): price must be
        #     stretched away from EMA21, confirming reversion room exists
        #   - in_downtrend / in_uptrend: EMA8/21 alignment still required
        #   - vol_ok: volume confirmation still required
        #
        # ADX slope and spike filter still skipped — same reasoning as before:
        #   ADX is naturally falling post-exhaustion; exhaustion candles are often larger than normal.
        # EMA50 slope IS now required: without it, armed signals fire into counter-trend
        # moves where EMA50 is actively running against us (T22: SHORT armed while EMA50
        # rising → −1.28; same pattern blocks bad LONGs during downtrends).
        # Note: ema50_rising for SHORT means the exhausted upmove had real momentum behind
        # it — EMA50 was genuinely rising before the ADX peak, making the reversal valid.
        # Block exhaustion SHORT if bulls are still clearly dominant (+DI >> -DI).
        # A +DI that is more than 2× -DI means the uptrend has not transferred
        # control to sellers yet — firing a short here is premature reversal.
        _plus_di  = row["plus_di"]
        _minus_di = row["minus_di"]
        _di_balanced_short = _plus_di < _minus_di * 2.0   # bears must be closing the gap

        # Bounce confirmation: require the trigger candle to close in the direction
        # of the reversal before entering. Without this, armed signals fire on the
        # first candle of the exhaustion move itself — price is still running hard
        # against us (e.g. a 174M-vol dump candle closes bearish → we enter LONG).
        # Candle-colour confirmation is only required during a deep strong-trend waterfall
        # (DI > 45 and ADX > 50). A blanket check blocks too many valid mid-recovery entries.
        _open                = row["open"]
        _bearish_candle      = close < _open   # confirming candle for SHORT reversal
        _bullish_candle      = close > _open   # confirming candle for LONG  reversal
        _deep_bear_waterfall = _plus_di  > 45 and row["adx"] > 50  # SHORT: buyers dominating hard
        _deep_bull_waterfall = _minus_di > 45 and row["adx"] > 50  # LONG:  sellers dominating hard
        _short_candle_ok     = (not _deep_bear_waterfall) or _bearish_candle
        _long_candle_ok      = (not _deep_bull_waterfall) or _bullish_candle

        if self._short_armed and vol_ok and in_uptrend and ema_sep_ok and close > ema_slow and ema50_rising and _di_balanced_short and _short_candle_ok:
            _pb_dist = (close - ema_slow) / atr_val
            log.info(
                f"SIGNAL short |  exhaustion armed  armed_remaining={self._short_armed_remaining}"
                f"  adx={row['adx']:.1f}  ema50={ema_trend:.4f}  close={close:.4f}"
                f"  stretch={_pb_dist:.2f}x ATR from EMA21"
            )
            self._short_armed = False
            self._short_armed_remaining = 0
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = False
            return "short"

        # Block exhaustion LONG if bears are still clearly dominant (-DI >> +DI).
        _di_balanced_long = _minus_di < _plus_di * 2.0   # bulls must be closing the gap

        if self._long_armed and vol_ok and in_downtrend and ema_sep_ok and close < ema_slow and ema50_falling and _di_balanced_long and _long_candle_ok:
            _pb_dist = (ema_slow - close) / atr_val
            log.info(
                f"SIGNAL long  |  exhaustion armed  armed_remaining={self._long_armed_remaining}"
                f"  adx={row['adx']:.1f}  ema50={ema_trend:.4f}  close={close:.4f}"
                f"  stretch={_pb_dist:.2f}x ATR from EMA21"
            )
            self._long_armed = False
            self._long_armed_remaining = 0
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = False
            return "long"

        # ── 1c. DI-snap exhaustion entries ────────────────────────────────────
        # Fires immediately when directional pressure hits an extreme and snaps:
        #   LONG:  -DI > 35 (extreme bearish pressure)
        #          AND -DI peaked and fell 2 consecutive bars (-DI[-3] > -DI[-2] > -DI[-1])
        #          AND close < ema_slow (price stretched below EMA21 = real downside stretch)
        #          AND adx > 20 (some real momentum behind the move, not flat noise)
        #          AND vol_ok (volume confirmed)
        #   SHORT: mirror with +DI
        # No EMA50 filter: these are mean-reversion snaps — EMA50 lags the reversal.
        # No spike filter: an outsized candle IS the exhaustion we're trading against.
        # TP target is EMA21 (handled by order_manager/risk_manager).
        _DI_EXTREMITY  = 35.0   # DI must reach this level to qualify as a true extreme
        _DI_ADX_FLOOR  = 20.0   # ADX floor — avoids firing during pure flat/ranging noise

        if len(df) >= 3:
            _plus_di_n3  = df["plus_di"].iloc[-3]
            _plus_di_n2  = df["plus_di"].iloc[-2]
            _plus_di_n1  = df["plus_di"].iloc[-1]
            _minus_di_n3 = df["minus_di"].iloc[-3]
            _minus_di_n2 = df["minus_di"].iloc[-2]
            _minus_di_n1 = df["minus_di"].iloc[-1]
        else:
            _plus_di_n3 = _plus_di_n2 = _plus_di_n1 = 0.0
            _minus_di_n3 = _minus_di_n2 = _minus_di_n1 = 0.0

        _plus_di_snapped  = (_plus_di_n3  > _plus_di_n2  > _plus_di_n1)   # 2-bar fall from peak
        _minus_di_snapped = (_minus_di_n3 > _minus_di_n2 > _minus_di_n1)  # 2-bar fall from peak
        _adx_floor_ok     = row["adx"] >= _DI_ADX_FLOOR

        # DI-snap LONG: bearish pressure exhausted, price stretched below EMA21
        # ema50_falling required: confirms the dominant downmove was real and is now
        # exhausting — prevents firing LONG snap entries during uptrends where -DI
        # briefly spikes on a pullback (T2, T4, T11, T14, T15, T16 patterns).
        # _di_balanced_long: -DI must be < 2× +DI — bulls must be showing up.
        if (
            _minus_di_n3 >= _DI_EXTREMITY   # peak was extreme
            and _minus_di_snapped            # 2-bar confirmed snap down
            and close < ema_slow            # price stretched below EMA21
            and _adx_floor_ok
            and vol_ok
            and ema50_falling               # EMA50 must confirm dominant downtrend
            and _di_balanced_long           # bulls must be showing up, not near-zero
        ):
            _snap_sl = close - 1.0 * atr_val        # 1×ATR: wider room to absorb sweeps without being stopped
            _snap_tp = max(float(ema_slow), close + 1.5 * atr_val)  # min 1.5:1 R:R floor
            log.info(
                f"SIGNAL long  |  DI-snap exhaustion"
                f"  -DI={_minus_di_n3:.1f}→{_minus_di_n2:.1f}→{_minus_di_n1:.1f}"
                f"  adx={row['adx']:.1f}  close={close:.4f}  ema21={ema_slow:.4f}"
                f"  sl={_snap_sl:.4f}  tp={_snap_tp:.4f}"
            )
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = True
            self._di_snap_levels = {"sl": _snap_sl, "tp": _snap_tp}
            # Cancel ADX-peak arm — same exhaustion event, prevent a conflicting
            # FLIP signal if the EMA cross arrives while this position is open.
            self._short_armed = False
            self._short_armed_remaining = 0
            return "long"

        # DI-snap SHORT: bullish pressure exhausted, price stretched above EMA21
        # ema50_rising required: confirms the dominant upmove was real and is now
        # exhausting — prevents firing SHORT snap entries during downtrends where +DI
        # briefly spikes on a dead-cat bounce (T18, T19, T20 patterns).
        # _di_balanced_short: +DI must be < 2× -DI — if bears have near-zero presence
        # the uptrend is still fully in control and the snap is premature (e.g. 22:15
        # trade: +DI=36, -DI=6 → blocked, trend still accelerating to ADX=61).
        if (
            _plus_di_n3 >= _DI_EXTREMITY    # peak was extreme
            and _plus_di_snapped             # 2-bar confirmed snap down
            and close > ema_slow            # price stretched above EMA21
            and _adx_floor_ok
            and vol_ok
            and ema50_rising                # EMA50 must confirm dominant uptrend
            and _di_balanced_short          # bears must be showing up, not near-zero
        ):
            _snap_sl = close + 1.0 * atr_val        # 1×ATR: wider room to absorb sweeps without being stopped
            _snap_tp = min(float(ema_slow), close - 1.5 * atr_val)  # min 1.5:1 R:R floor
            log.info(
                f"SIGNAL short |  DI-snap exhaustion"
                f"  +DI={_plus_di_n3:.1f}→{_plus_di_n2:.1f}→{_plus_di_n1:.1f}"
                f"  adx={row['adx']:.1f}  close={close:.4f}  ema21={ema_slow:.4f}"
                f"  sl={_snap_sl:.4f}  tp={_snap_tp:.4f}"
            )
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = True
            self._di_snap_levels = {"sl": _snap_sl, "tp": _snap_tp}
            # Cancel ADX-peak arm — same exhaustion event, prevent a conflicting
            # FLIP signal if the EMA cross arrives while this position is open.
            self._long_armed = False
            self._long_armed_remaining = 0
            return "short"

        # ── 2. Trend continuation entries (no fresh cross needed) ─────────────
        # Re-enters after SL+cooldown when trend is still strongly intact.
        # Requires ADX >= 50 (vs 40 for crossover) to avoid choppy re-entries.
        # EMA50 bias still enforced — never trade against long-term trend.
        if in_uptrend and adx_trend_ok and vol_ok and spike_ok_cont and bias_long and ema_gap_ok_long and ema_sep_ok and ema50_rising:
            if _pump_extended:
                log.debug(
                    f"CUMULATIVE MOVE filtered (long)  net={_net_move:.5f}  limit={_atr_limit:.5f}"
                )
            else:
                _pb_dist = (close - ema_slow) / atr_val
                log.info(
                    f"SIGNAL long  |  continuation  adx={row['adx']:.1f}  "
                    f"vol={row['volume']:.0f}  ema50={ema_trend:.4f}  close={close:.4f}  "
                    f"pullback={_pb_dist:.2f}x ATR from EMA21"
                )
                self._last_signal_was_continuation = True
                self._last_signal_was_exhaustion_reversal = False
                self._last_signal_was_di_snap = False
                return "long"

        if in_downtrend and adx_trend_ok and vol_ok and spike_ok_cont and bias_short and ema_gap_ok_short and ema_sep_ok and ema50_falling:
            if _dump_extended:
                log.debug(
                    f"CUMULATIVE MOVE filtered (short)  net={_net_move:.5f}  limit={-_atr_limit:.5f}"
                )
            else:
                _pb_dist = (ema_slow - close) / atr_val
                log.info(
                    f"SIGNAL short |  continuation  adx={row['adx']:.1f}  "
                    f"vol={row['volume']:.0f}  ema50={ema_trend:.4f}  close={close:.4f}  "
                    f"pullback={_pb_dist:.2f}x ATR from EMA21"
                )
                self._last_signal_was_continuation = True
                self._last_signal_was_exhaustion_reversal = False
                self._last_signal_was_di_snap = False
                return "short"

        # ── 3. Exhaustion arming ──────────────────────────────────────────────
        # Detects when the CURRENT trend is losing momentum (ADX peak while still
        # in that trend) and arms a flag. The actual entry fires in section 1b
        # when the subsequent EMA cross confirms the reversal.
        #
        # ARM SHORT: uptrend (ema_fast > ema_slow) exhausting
        #   - ADX formed a local peak and is now falling 2+ bars
        #   - adx_peak >= 30 (ignore micro-noise peaks)
        #
        # ARM LONG: downtrend (ema_fast < ema_slow) exhausting
        #   - Same ADX peak pattern
        #
        # Needs 4 ADX values: peak bar [-3], one bar before [-4], two falls [-2],[-1]
        _EXHAUSTION_ARM_WINDOW = 15   # candles the arm stays active waiting for cross
        _EXHAUSTION_ADX_FLOOR  = 20   # minimum adx_peak to arm — on 5m ADX peaks slower/lower;
                                      # 20 still filters pure noise while catching real exhaustion moves

        if len(df) >= 4:
            _adx_n4 = df["adx"].iloc[-4]
            _adx_n3 = df["adx"].iloc[-3]
            _adx_n2 = df["adx"].iloc[-2]
            _adx_n1 = df["adx"].iloc[-1]
            _exh_adx_peaked_falling = (
                _adx_n4 < _adx_n3 and   # ADX was rising into the peak
                _adx_n3 > _adx_n2 and   # peak bar
                _adx_n2 > _adx_n1        # falling 2nd bar (2+ consecutive falls)
            )
            _ema21_n2 = df["ema_slow"].iloc[-2]
            _ema21_n1 = df["ema_slow"].iloc[-1]
            _ema21_ticked_down = _ema21_n1 < _ema21_n2
            _ema21_ticked_up   = _ema21_n1 > _ema21_n2
        else:
            _exh_adx_peaked_falling = False
            _ema21_ticked_down = False
            _ema21_ticked_up   = False

        if _exh_adx_peaked_falling:
            log.debug(
                f"EXHAUSTION check  adx_peak={_adx_n3:.1f}  adx_now={_adx_n1:.1f}"
                f"  ema21_tick={'down' if _ema21_ticked_down else 'up' if _ema21_ticked_up else 'flat'}"
                f"  ema8>21={in_uptrend}"
            )

        # Arm SHORT: ADX peaked while in uptrend → expect bear cross soon
        if _exh_adx_peaked_falling and in_uptrend and _adx_n3 >= _EXHAUSTION_ADX_FLOOR:
            if not self._short_armed:
                log.debug(
                    f"EXHAUSTION ARM short  adx_peak={_adx_n3:.1f}  adx_now={_adx_n1:.1f}"
                    f"  ema8>21={in_uptrend}  window={_EXHAUSTION_ARM_WINDOW}"
                )
            self._short_armed = True
            self._short_armed_remaining = _EXHAUSTION_ARM_WINDOW
            self._long_armed = False   # cancel opposite arm

        # Arm LONG: ADX peaked while in downtrend → expect bull cross soon
        if _exh_adx_peaked_falling and in_downtrend and _adx_n3 >= _EXHAUSTION_ADX_FLOOR:
            if not self._long_armed:
                log.debug(
                    f"EXHAUSTION ARM long  adx_peak={_adx_n3:.1f}  adx_now={_adx_n1:.1f}"
                    f"  ema8>21={in_uptrend}  window={_EXHAUSTION_ARM_WINDOW}"
                )
            self._long_armed = True
            self._long_armed_remaining = _EXHAUSTION_ARM_WINDOW
            self._short_armed = False  # cancel opposite arm

        # Tick down arm timeouts — only decrement if arm was NOT just set this
        # candle (remaining would be exactly _EXHAUSTION_ARM_WINDOW). This prevents
        # the arm losing one count on the same candle it was armed.
        if self._short_armed and self._short_armed_remaining < _EXHAUSTION_ARM_WINDOW:
            self._short_armed_remaining -= 1
            if self._short_armed_remaining <= 0:
                log.debug("EXHAUSTION ARM short expired — no bear cross in window")
                self._short_armed = False
        if self._long_armed and self._long_armed_remaining < _EXHAUSTION_ARM_WINDOW:
            self._long_armed_remaining -= 1
            if self._long_armed_remaining <= 0:
                log.debug("EXHAUSTION ARM long expired — no bull cross in window")
                self._long_armed = False

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


def _adx(df: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder-smoothed ADX + DI lines (no external deps).
    Returns (adx, plus_di, minus_di).
    """
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
    adx = dx.ewm(span=period, adjust=False).mean().fillna(0)
    return adx, plus_di.fillna(0), minus_di.fillna(0)


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema_fast"]     = _ema(df["close"], EMA_FAST)
    df["ema_slow"]     = _ema(df["close"], EMA_SLOW)
    df["ema_trend"]    = _ema(df["close"], EMA_TREND)
    df["atr"]          = _atr(df, cfg.ATR_PERIOD)
    df["adx"], df["plus_di"], df["minus_di"] = _adx(df, ADX_PERIOD)
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