"""
strategy.py – Trend-Following Strategy (Optimized, Item 1 Rolled Back)

Entry logic:
  - EMA crossover (fast crosses above/below slow) to detect trend direction immediately
  - ADX > threshold to confirm real momentum (filters out choppy sideways markets)
  - Volume above rolling average AND expanding to confirm participation
  - ATR computed for SL/TP sizing in risk_manager / order_manager
  - EMA 50 as long-term bias filter — only long above it, only short below it
  - Trend continuation re-entry — re-enter in trend direction after SL/cooldown
    when EMA alignment + ADX still strong, no fresh cross required
  - Price-Boundary Spike Lockouts to catch aggressive V-shape reversals

Signal:
  - 'long'  — cross=bull + ADX>=40 + vol + no spike + EMA8>EMA21 + close>EMA50 + EMA50 rising
           OR — continuation: EMA8>EMA21 + ADX>=50 + vol + no spike + close>EMA50 + EMA50 rising
  - 'short' — cross=bear + ADX>=40 + vol + no spike + EMA8<EMA21 + close<EMA50 + EMA50 falling
           OR — continuation: EMA8<EMA21 + ADX>=50 + vol + no spike + close<EMA50 + EMA50 falling
  - 'none'  otherwise
"""

from __future__ import annotations

import logging
import pandas as pd
from typing import Optional

from config import cfg
from state import State

log = logging.getLogger("strategy")

# ── Tuneable parameters ───────────────────────────────────────────────────────
EMA_FAST      = 8
EMA_SLOW      = 21
EMA_TREND     = 50
ADX_PERIOD    = 14
ADX_MIN       = 25.0
ADX_TREND_MIN = 45.0
ADX_SLOPE_BARS = 3
ADX_STRONG     = 45.0
EMA_TREND_SLOPE_BARS = 3
RSI_PERIOD    = 14
RSI_OB        = 70.0
RSI_OS        = 30.0
VOL_MA        = 10
VOL_MULT      = 0.6
SPIKE_ATR_MULT          = 1.5
SPIKE_ATR_MULT_TREND    = 2.5
SPIKE_VOL_MULT     = 4.0

_MIN_BARS = max(EMA_SLOW, ADX_PERIOD, VOL_MA) + 4


class ScalpingStrategy:
    """Trend-following strategy using EMA crossover + ADX + volume + EMA50 bias."""

    def __init__(self) -> None:
        self._last_df_hash: Optional[int] = None
        self._cached_df: Optional[pd.DataFrame] = None

        # --- OPTIMIZATION 2: Price-Boundary Spike Lockout ---
        self._spike_active: bool = False
        self._spike_high: float = 0.0
        self._spike_low: float = 0.0
        # ----------------------------------------------------

        self._spike_direction: str = "none"
        self._last_signal_was_continuation: bool = False
        self._last_signal_was_exhaustion_reversal: bool = False
        self._last_signal_was_di_snap: bool = False
        self._di_snap_levels: dict = {}
        self._cross_window_remaining: int = 0
        self._cross_window_direction: str = "none"
        self._short_armed: bool = False
        self._long_armed: bool = False
        self._short_armed_remaining: int = 0
        self._long_armed_remaining: int = 0
        self._exhaustion_sl_lockout: int = 0
        self._last_signal_was_grind_short: bool = False

    def cancel_exhaustion_arms(self, lockout_candles: int = 6) -> None:
        log.debug(
            f"EXHAUSTION ARM cancelled after SL  lockout={lockout_candles} candles  "
            f"long_armed={self._long_armed}  short_armed={self._short_armed}"
        )
        self._long_armed             = False
        self._long_armed_remaining   = 0
        self._short_armed            = False
        self._short_armed_remaining  = 0
        self._exhaustion_sl_lockout  = lockout_candles

    def was_continuation(self) -> bool:
        return self._last_signal_was_continuation

    def was_exhaustion_reversal(self) -> bool:
        return self._last_signal_was_exhaustion_reversal

    def was_di_snap(self) -> bool:
        return self._last_signal_was_di_snap

    def was_grind_short(self) -> bool:
        return self._last_signal_was_grind_short

    def di_snap_levels(self) -> dict:
        return self._di_snap_levels

    def last_ema21(self) -> float:
        if self._cached_df is None or self._cached_df.empty:
            return 0.0
        return float(self._cached_df["ema_slow"].iloc[-1])

    def indicator_snapshot(self, state: State) -> Optional[dict]:
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
            "rsi":       round(row["rsi"],       2),
            "volume":    round(row["volume"],    2),
            "vol_avg":   round(row["vol_avg"],   2),
            "cross":     row["cross"],
            "close":     round(row["close"], cfg.PRICE_PRECISION),
        }

    def market_regime(self, state: State) -> str:
        df = self._cached_df
        if df is None or df.empty:
            return "CHOP"

        row       = df.iloc[-1]
        adx       = row["adx"]
        atr       = row["atr"]
        ema_fast  = row["ema_fast"]
        ema_trend = row["ema_trend"]
        ema_separation = abs(ema_fast - ema_trend)

        if adx >= 50 and ema_separation >= 1.5 * atr:
            regime = "STRONG_TREND"
        elif adx >= 35 and ema_separation >= 1.0 * atr:
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
        df = self._cached_df
        if df is None or df.empty:
            return "none"

        row = df.iloc[-1]
        regime = self.market_regime(state)

        if len(df) > ADX_SLOPE_BARS:
            adx_prev   = df["adx"].iloc[-1 - ADX_SLOPE_BARS]
            adx_rising = (row["adx"] >= ADX_STRONG) or (row["adx"] > adx_prev)
        else:
            adx_rising = True

        adx_ok       = (row["adx"] >= ADX_MIN)       and adx_rising
        adx_trend_ok = (row["adx"] >= ADX_TREND_MIN) and adx_rising
        vol_ok       = row["volume"]       >= row["vol_avg"] * VOL_MULT

        is_volume_breakout = row["volume"] >= row["vol_avg"] * 3.0
        _spike_mult_cross = SPIKE_ATR_MULT_TREND if is_volume_breakout else SPIKE_ATR_MULT
        spike_ok     = row["candle_range"] <= row["atr"] * _spike_mult_cross

        _spike_mult_cont = SPIKE_ATR_MULT_TREND if (regime == "STRONG_TREND" or is_volume_breakout) else SPIKE_ATR_MULT
        spike_ok_cont    = row["candle_range"] <= row["atr"] * _spike_mult_cont
        cross        = row["cross"]

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
                cross = self._cross_window_direction
                self._cross_window_remaining -= 1
            else:
                self._cross_window_remaining = 0
                self._cross_window_direction = "none"

        ema_fast  = row["ema_fast"]
        ema_slow  = row["ema_slow"]
        ema_trend = row["ema_trend"]
        close     = row["close"]

        in_uptrend   = ema_fast > ema_slow
        in_downtrend = ema_fast < ema_slow
        bias_long  = close > ema_trend
        bias_short = close < ema_trend

        # --- Breakout & Anti-Chop Filters (Calculated but NOT strictly enforced in cross entries per request) ---
        struct_high = row["struct_high"]
        struct_low  = row["struct_low"]
        struct_break_long = (close > struct_high) or (row["adx"] >= 50)
        struct_break_short = (close < struct_low) or (row["adx"] >= 50)
        vol_breakout_ok = row["volume"] >= (row["vol_avg"] * 2.5)
        ema_splay_pct = (abs(ema_fast - ema_slow) / ema_slow) * 100
        ema_splay_ok = ema_splay_pct >= 0.15
        # ------------------------------------

        atr_val = row["atr"]
        PULLBACK_MIN_ATR = 0.1
        PULLBACK_MAX_ATR = 1.5
        ema_gap_ok_long  = PULLBACK_MIN_ATR * atr_val < (close - ema_slow) <= PULLBACK_MAX_ATR * atr_val
        ema_gap_ok_short = PULLBACK_MIN_ATR * atr_val < (ema_slow - close) <= PULLBACK_MAX_ATR * atr_val

        # --- OPTIMIZATION 4: ATR Shrink Trap Fix (Absolute Floor Separation) ---
        _ema_sep = abs(ema_fast - ema_trend)
        _abs_min_sep = close * 0.001 # 0.1% of absolute price minimum guard
        ema_sep_ok       = _ema_sep >= max(0.5 * atr_val, _abs_min_sep)
        ema_sep_ok_cross = _ema_sep >= max(0.1 * atr_val, _abs_min_sep * 0.2)
        # -----------------------------------------------------------------------

        ema50_slope_min = row["atr"] * 0.15
        if len(df) > EMA_TREND_SLOPE_BARS:
            ema_trend_prev = df["ema_trend"].iloc[-1 - EMA_TREND_SLOPE_BARS]
            ema50_delta   = ema_trend - ema_trend_prev
            ema50_rising  = ema50_delta >  ema50_slope_min
            ema50_falling = ema50_delta < -ema50_slope_min
        else:
            ema50_rising  = True
            ema50_falling = True

        # --- OPTIMIZATION 2: Price-Boundary Spike Lockout Implementation ---
        vol_avg = row["vol_avg"] if row["vol_avg"] > 0 else 1
        if row["volume"] > vol_avg * SPIKE_VOL_MULT:
            self._spike_active = True
            self._spike_high = row["high"]
            self._spike_low = row["low"]
            prev_close = df["close"].iloc[-2] if len(df) >= 2 else close
            self._spike_direction = "down" if close < prev_close else "up"
            log.debug(
                f"SPIKE BOUNDARY SET  vol={row['volume']:.0f}"
                f"  avg={vol_avg:.0f}  range={self._spike_low:.4f}-{self._spike_high:.4f}"
                f"  spike_dir={self._spike_direction}"
            )
            return "none"
        elif self._spike_active:
            if close > self._spike_high or close < self._spike_low:
                self._spike_active = False # Price escaped the spike block, unlock immediately
                log.debug(f"SPIKE BOUNDARY CLEARED  close={close:.4f} broke range {self._spike_low:.4f}-{self._spike_high:.4f}")
            else:
                log.debug(f"SPIKE BOUNDARY ACTIVE  close={close:.4f} stuck inside {self._spike_low:.4f}-{self._spike_high:.4f}")
                return "none"
        elif self._spike_direction != "none":
            _ema_sep_local = abs(ema_fast - ema_trend)
            _is_strong_trend = row["adx"] >= 50 and _ema_sep_local >= 1.5 * atr_val
            if self._spike_direction == "down" and in_downtrend:
                if not _is_strong_trend:
                    self._spike_direction = "none"
                    return "none"
            if self._spike_direction == "up" and in_uptrend:
                if not _is_strong_trend:
                    self._spike_direction = "none"
                    return "none"
            self._spike_direction = "none"
        # -------------------------------------------------------------------

        # --- OPTIMIZATION 3: Relaxed RSI Guard during Parabolic Trends ---
        rsi_val       = row["rsi"]
        rsi_ok_long   = (rsi_val <= RSI_OB) or (regime == "STRONG_TREND")
        rsi_ok_short  = (rsi_val >= RSI_OS) or (regime == "STRONG_TREND")
        # -----------------------------------------------------------------

        # --- OPTIMIZATION 4: Require Volume Expansion (Bar over Bar) ---
        vol_expanding = (row["volume"] > df["volume"].iloc[-2]) if len(df) >= 2 else True
        vol_waking_up = (row["volume"] >= (row["vol_avg"] * 1.2)) and vol_expanding
        # ---------------------------------------------------------------

        # ── 1. Crossover entries (fresh cross signal) ─────────────────────────
        if cross == "bull" and adx_ok and vol_waking_up and spike_ok and in_uptrend and bias_long and ema_gap_ok_long and ema_sep_ok_cross and rsi_ok_long:
            _pb_dist = (close - ema_slow) / atr_val
            log.info(
                f"SIGNAL long  |  cross=bull  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema50={ema_trend:.4f}  close={close:.4f}  "
                f"pullback={_pb_dist:.2f}x ATR from EMA21"
            )
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = False
            self._last_signal_was_di_snap = False
            self._last_signal_was_grind_short = False
            self._long_armed = False
            return "long"

        if cross == "bear" and adx_ok and vol_waking_up and spike_ok and in_downtrend and bias_short and ema_gap_ok_short and ema_sep_ok_cross and rsi_ok_short:
            _pb_dist = (ema_slow - close) / atr_val
            log.info(
                f"SIGNAL short |  cross=bear  adx={row['adx']:.1f}  "
                f"vol={row['volume']:.0f}  ema50={ema_trend:.4f}  close={close:.4f}  "
                f"pullback={_pb_dist:.2f}x ATR from EMA21"
            )
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = False
            self._last_signal_was_di_snap = False
            self._last_signal_was_grind_short = False
            self._short_armed = False
            return "short"

        # ── 1b. Exhaustion-armed entries (no cross required) ──────────────────
        _plus_di  = row["plus_di"]
        _minus_di = row["minus_di"]
        _di_balanced_short = _plus_di < _minus_di * 2.0
        _open                = row["open"]
        _bearish_candle      = close < _open
        _bullish_candle      = close > _open
        _deep_bear_waterfall = _plus_di  > 45 and row["adx"] > 50
        _deep_bull_waterfall = _minus_di > 45 and row["adx"] > 50
        _short_candle_ok     = (not _deep_bear_waterfall) or _bearish_candle
        _long_candle_ok      = (not _deep_bull_waterfall) or _bullish_candle

        if self._short_armed and self._exhaustion_sl_lockout == 0 and vol_ok and in_uptrend and ema_sep_ok and close > ema_slow and ema50_rising and _di_balanced_short and _short_candle_ok and rsi_ok_short:
            self._short_armed = False
            self._short_armed_remaining = 0
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = False
            self._last_signal_was_grind_short = False
            return "short"

        _di_balanced_long = _minus_di < _plus_di * 2.0
        _bulls_leading = _plus_di > _minus_di

        if self._long_armed and self._exhaustion_sl_lockout == 0 and vol_ok and in_downtrend and ema_sep_ok and close < ema_slow and ema50_falling and _di_balanced_long and _bulls_leading and _long_candle_ok and rsi_ok_long:
            self._long_armed = False
            self._long_armed_remaining = 0
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = False
            self._last_signal_was_grind_short = False
            return "long"

        # ── 1c. DI-snap exhaustion entries ────────────────────────────────────
        _DI_EXTREMITY  = 35.0
        _DI_ADX_FLOOR  = 20.0

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

        _plus_di_snapped  = (_plus_di_n3  > _plus_di_n2  > _plus_di_n1)
        _minus_di_snapped = (_minus_di_n3 > _minus_di_n2 > _minus_di_n1)
        _adx_floor_ok     = row["adx"] >= _DI_ADX_FLOOR

        if (
            _minus_di_n3 >= _DI_EXTREMITY
            and _minus_di_snapped
            and close < ema_slow
            and _adx_floor_ok
            and vol_ok
            and ema50_falling
            and _di_balanced_long
        ):
            _snap_sl = close - 1.0 * atr_val
            _snap_tp = max(float(ema_slow), close + 1.5 * atr_val)
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = True
            self._last_signal_was_grind_short = False
            self._di_snap_levels = {"sl": _snap_sl, "tp": _snap_tp}
            self._short_armed = False
            self._short_armed_remaining = 0
            return "long"

        if (
            _plus_di_n3 >= _DI_EXTREMITY
            and _plus_di_snapped
            and close > ema_slow
            and _adx_floor_ok
            and vol_ok
            and ema50_rising
            and _di_balanced_short
        ):
            _snap_sl = close + 1.0 * atr_val
            _snap_tp = min(float(ema_slow), close - 1.5 * atr_val)
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = True
            self._last_signal_was_di_snap = True
            self._last_signal_was_grind_short = False
            self._di_snap_levels = {"sl": _snap_sl, "tp": _snap_tp}
            self._long_armed = False
            self._long_armed_remaining = 0
            return "short"

        # ── 2. Trend continuation entries (The Dip & Rip) ─────────────
        strong_uptrend = (ema_fast > ema_slow > ema_trend) and (row["adx"] >= 35.0)
        strong_downtrend = (ema_fast < ema_slow < ema_trend) and (row["adx"] >= 35.0)

        _di_gap_long  = (_plus_di  - _minus_di) >= 20.0
        _di_gap_short = (_minus_di - _plus_di)  >= 20.0

        dipped_long = row["low"] <= ema_fast
        dipped_short = row["high"] >= ema_fast

        rip_long = (close > row["open"]) and (close > ema_fast)
        rip_short = (close < row["open"]) and (close < ema_fast)

        if strong_uptrend and spike_ok_cont and bias_long and ema_sep_ok and ema50_rising and dipped_long and rip_long and rsi_ok_long and _di_gap_long and not _pump_extended:
            self._last_signal_was_continuation = True
            self._last_signal_was_exhaustion_reversal = False
            self._last_signal_was_di_snap = False
            self._last_signal_was_grind_short = False
            return "long"

        if strong_downtrend and spike_ok_cont and bias_short and ema_sep_ok and ema50_falling and dipped_short and rip_short and rsi_ok_short and _di_gap_short and not _dump_extended:
            self._last_signal_was_continuation = True
            self._last_signal_was_exhaustion_reversal = False
            self._last_signal_was_di_snap = False
            self._last_signal_was_grind_short = False
            return "short"

        # ── 2b. Grind Short continuation ─────────────────────────────────────
        _grind_di_gap_short  = (_minus_di - _plus_di) >= 20.0
        _grind_bulls_fading  = _plus_di <= 12.0
        _grind_regime_ok     = row["adx"] >= 50.0

        _grind_short = (
            strong_downtrend
            and spike_ok_cont
            and bias_short
            and ema_sep_ok
            and ema50_falling
            and _grind_di_gap_short
            and _grind_bulls_fading
            and _grind_regime_ok
            and close < row["open"]
            and close < ema_fast
            and rsi_ok_short
            and not _dump_extended
        )
        if _grind_short:
            self._last_signal_was_continuation = False
            self._last_signal_was_exhaustion_reversal = False
            self._last_signal_was_di_snap = False
            self._last_signal_was_grind_short = True
            return "short"

        # ── 3. Exhaustion arming ──────────────────────────────────────────────
        _EXHAUSTION_ARM_WINDOW = 15
        _EXHAUSTION_ADX_FLOOR  = 20

        if len(df) >= 4:
            _adx_n4 = df["adx"].iloc[-4]
            _adx_n3 = df["adx"].iloc[-3]
            _adx_n2 = df["adx"].iloc[-2]
            _adx_n1 = df["adx"].iloc[-1]
            _exh_adx_peaked_falling = (
                _adx_n4 < _adx_n3 and
                _adx_n3 > _adx_n2 and
                _adx_n2 > _adx_n1
            )
            _ema21_n2 = df["ema_slow"].iloc[-2]
            _ema21_n1 = df["ema_slow"].iloc[-1]
            _ema21_ticked_down = _ema21_n1 < _ema21_n2
            _ema21_ticked_up   = _ema21_n1 > _ema21_n2
        else:
            _exh_adx_peaked_falling = False
            _ema21_ticked_down = False
            _ema21_ticked_up   = False

        if self._exhaustion_sl_lockout > 0:
            self._exhaustion_sl_lockout -= 1
        else:
            if _exh_adx_peaked_falling and in_uptrend and _adx_n3 >= _EXHAUSTION_ADX_FLOOR:
                self._short_armed = True
                self._short_armed_remaining = _EXHAUSTION_ARM_WINDOW
                self._long_armed = False

            if _exh_adx_peaked_falling and in_downtrend and _adx_n3 >= _EXHAUSTION_ADX_FLOOR:
                self._long_armed = True
                self._long_armed_remaining = _EXHAUSTION_ARM_WINDOW
                self._short_armed = False

            if self._short_armed and self._short_armed_remaining < _EXHAUSTION_ARM_WINDOW:
                self._short_armed_remaining -= 1
                if self._short_armed_remaining <= 0:
                    self._short_armed = False
            if self._long_armed and self._long_armed_remaining < _EXHAUSTION_ARM_WINDOW:
                self._long_armed_remaining -= 1
                if self._long_armed_remaining <= 0:
                    self._long_armed = False

        return "none"

    def _compute(self, state: State) -> Optional[pd.DataFrame]:
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


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(50)


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema_fast"]     = _ema(df["close"], EMA_FAST)
    df["ema_slow"]     = _ema(df["close"], EMA_SLOW)
    df["ema_trend"]    = _ema(df["close"], EMA_TREND)
    df["atr"]          = _atr(df, cfg.ATR_PERIOD)
    df["adx"], df["plus_di"], df["minus_di"] = _adx(df, ADX_PERIOD)
    df["rsi"]          = _rsi(df["close"], RSI_PERIOD)
    df["vol_avg"]      = df["volume"].rolling(VOL_MA, min_periods=1).median()
    df["candle_range"] = df["high"] - df["low"]

    STRUCT_LOOKBACK = 20
    df["struct_high"] = df["high"].shift(1).rolling(STRUCT_LOOKBACK).max()
    df["struct_low"]  = df["low"].shift(1).rolling(STRUCT_LOOKBACK).min()

    prev_fast = df["ema_fast"].shift(1)
    prev_slow = df["ema_slow"].shift(1)

    bull = (df["ema_fast"] > df["ema_slow"]) & (prev_fast <= prev_slow)
    bear = (df["ema_fast"] < df["ema_slow"]) & (prev_fast >= prev_slow)

    df["cross"] = "none"
    df.loc[bull, "cross"] = "bull"
    df.loc[bear, "cross"] = "bear"

    return df