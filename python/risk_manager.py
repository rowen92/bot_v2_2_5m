"""
risk_manager.py – Position sizing and pre-trade risk checks.

Position size formula (risk-based):
    risk_usdt   = balance * RISK_PER_TRADE_PCT / 100
    sl_distance = atr * SL_ATR_MULT          # or entry * STOP_LOSS_PCT / 100 as fallback
    qty         = risk_usdt / sl_distance

Leverage is applied by the exchange on the margin side — do NOT multiply here
or the position becomes leverage× too large.
The position is sized so that if the SL is hit, you lose exactly
RISK_PER_TRADE_PCT % of your balance (before leverage).
"""

from __future__ import annotations

import logging
import math
import time
from config import cfg
from state import State

log = logging.getLogger("risk")


class RiskManager:

    # ── Pre-trade checks ──────────────────────────────────────────────────────

    def _dynamic_cooldown_seconds(self, state: State) -> int:
        """
        Flat cooldown of COOLDOWN_CANDLES × 300s after every close.
        Mirrors backtest.py COOLDOWN_CANDLES = 1 (one 5m candle = 300s).
        Streak pausing is handled separately by the consecutive-SL circuit
        breaker in can_trade() — no need to also scale cooldown here.
        """
        return cfg.COOLDOWN_CANDLES * 300

    def _dynamic_risk_pct(self, state: State) -> float:
        """Flat risk — full RISK_PER_TRADE_PCT every trade, no streak/drawdown scaling.
        Mirrors backtest.py calc_qty() which uses RISK_PCT directly.
        """
        return cfg.RISK_PER_TRADE_PCT

    def _in_sl_zone(self, state: State, current_price: float, signal_side: str = "") -> bool:
        """
        Return True if current_price is within ANTI_REVENGE_ATR_MULT × ATR of
        the last SL entry price AND the new signal is in the OPPOSITE direction
        to the last SL trade — i.e. a revenge trade (going long after a short SL).

        Same-direction signals (continuing the trend that just stopped us out)
        are NOT blocked — the cooldown and ADX/EMA filters already gate those.
        Only active when consecutive_sl >= 1 (first retry after an SL).
        Clears automatically once price moves far enough away.
        """
        if state.consecutive_sl < 1:
            return False
        if state.last_sl_entry_price <= 0 or state.last_sl_atr <= 0:
            return False

        zone_radius = state.last_sl_atr * 1.5   # 1.5 × ATR either side of SL entry
        in_zone = abs(current_price - state.last_sl_entry_price) < zone_radius
        if not in_zone:
            return False

        # Only block if the new signal is the OPPOSITE side to the last SL.
        # Same-direction re-entry = trend continuation, not revenge.
        last_side = getattr(state, "last_sl_side", "")  # 'long' | 'short' | ''
        opposite = {"long": "short", "short": "long"}.get(last_side, "")
        is_revenge = (signal_side == opposite) if (signal_side and opposite) else True  # default block if unknown

        if is_revenge:
            log.debug(
                f"ANTI-REVENGE blocked  price={current_price:.6f}"
                f"  last_sl_entry={state.last_sl_entry_price:.6f}"
                f"  zone_radius={zone_radius:.6f}  atr={state.last_sl_atr:.6f}"
                f"  last_sl_side={last_side}  signal_side={signal_side}"
            )
        return is_revenge

    def can_trade(self, state: State, live_balance: float | None = None, signal_side: str = "") -> bool:
        """Return True if it is safe to open a new trade right now."""

        # ── Consecutive-SL circuit breaker ────────────────────────────────────
        # After 3 straight SLs, block all new entries for 5 hours (60 × 5m candles).
        # Mirrors backtest.py MAX_CONSECUTIVE_SL=3 / SL_PAUSE_CANDLES=60 logic.
        MAX_CONSECUTIVE_SL = 3
        SL_PAUSE_SECONDS   = 60 * 300  # 60 candles × 5 min = 5 hours
        if getattr(state, "consecutive_sl", 0) >= MAX_CONSECUTIVE_SL:
            seconds_since_close = time.time() - state.last_close_ts
            if seconds_since_close < SL_PAUSE_SECONDS:
                log.warning(
                    f"can_trade=False  reason=consecutive_sl_circuit_breaker  "
                    f"consecutive_sl={state.consecutive_sl}  "
                    f"pause_remaining={int((SL_PAUSE_SECONDS - seconds_since_close) / 60)}min"
                )
                return False
            else:
                # Pause expired — reset the streak so we don't re-trigger immediately
                state.consecutive_sl = 0

        # Dynamic cooldown — longer after SL hits and consecutive losses
        cooldown_seconds = self._dynamic_cooldown_seconds(state)
        seconds_since_close = time.time() - state.last_close_ts
        if seconds_since_close < cooldown_seconds:
            log.debug(
                f"can_trade=False  reason=cooldown  "
                f"seconds_since_close={seconds_since_close:.0f}  need={cooldown_seconds}"
            )
            return False

        # Already in a position
        if state.position is not None:
            pos = state.position
            log.debug(
                f"can_trade=False  reason=position_open  "
                f"side={pos.side}  entry={pos.entry_price:.4f}  "
                f"trail_active={pos.trail_active}"
            )
            return False

        # Anti-revenge zone: block re-entry if price is still inside 1.5×ATR
        # of the level where the last SL hit. Prevents chasing the same zone
        # twice (e.g. trades 4+5 both entered at 0.0749 on the same dying move).
        if self._in_sl_zone(state, state.mark_price, signal_side=signal_side):
            log.debug("can_trade=False  reason=anti_revenge_zone")
            return False

        # No hard drawdown stop — position sizing already scales down via
        # _dynamic_risk_pct() as losses accumulate. Mirrors backtest.py behaviour.

        # Paper: balance must be positive. By this point position is guaranteed
        # to be None (checked above), so paper_balance is the settled balance.
        if cfg.is_paper():
            if state.paper_balance <= 0:
                log.warning("can_trade=False  reason=paper_balance_depleted")
                return False
            log.debug(f"can_trade=True  balance={state.paper_balance:.2f}")

        # Live: balance check (caller passes account balance in USDT)
        if not cfg.is_paper() and live_balance is not None:
            if live_balance < 5.0:  # hard minimum
                log.warning(f"can_trade=False  reason=low_live_balance  balance={live_balance}")
                return False

        return True

    # ── Position sizing ───────────────────────────────────────────────────────

    # ── Regime-based multipliers ──────────────────────────────────────────────

    def regime_params(self, regime: str) -> dict:
        """Return SL multiplier for the given market regime.
        Trail activation (+2R) and callback (1R) are derived from sl_mult
        in update_trail() — no separate trail_act/trail_cb needed.
        """
        params = {
            "STRONG_TREND": {"sl": cfg.STRONG_TREND_SL_MULT},
            "TREND":        {"sl": cfg.TREND_SL_MULT},
            "CHOP":         {"sl": cfg.CHOP_SL_MULT},
        }
        return params.get(regime, params["TREND"])

    def position_size(
        self,
        entry_price: float,
        balance: float,
        state: State | None = None,
        atr: float | None = None,
        regime: str = "TREND",
    ) -> float:
        """
        Calculate the position quantity in base asset (WLD).
        Returned value is already rounded to 1 decimal (Binance WLDUSDT step).

        When `atr` is provided the SL distance is ATR-based (sl_mult × ATR),
        where sl_mult is chosen based on the current market regime.
        Falls back to fixed STOP_LOSS_PCT when ATR is unavailable.
        """
        risk_pct  = self._dynamic_risk_pct(state) if state else cfg.RISK_PER_TRADE_PCT
        risk_usdt = balance * (risk_pct / 100)

        if atr and atr > 0:
            sl_mult     = self.regime_params(regime)["sl"]
            sl_distance = atr * sl_mult
        else:
            sl_distance = entry_price * (cfg.STOP_LOSS_PCT / 100)

        if sl_distance == 0:
            return 0.0

        # risk_usdt is the dollar amount we are willing to lose if SL is hit.
        # sl_distance is the loss-per-unit if price moves to SL.
        # qty = risk_usdt / sl_distance gives the correct unlevered size.
        # Leverage is implicitly applied by the exchange on the margin side —
        # do NOT multiply here or the position becomes leverage× too large.
        qty = risk_usdt / sl_distance

        # Leverage cap — prevents oversized positions relative to account balance.
        # Mirrors backtest.py calc_qty() behaviour.
        max_notional = balance * cfg.LEVERAGE
        max_qty = max_notional / entry_price
        qty = min(qty, max_qty)

        # Round down to nearest QTY_STEP (configured per symbol in .env).
        # Use math.floor(round(...)) to avoid float precision errors where
        # e.g. 0.3 / 0.1 = 2.9999999999996 causing int() to truncate wrongly.
        step = cfg.QTY_STEP
        qty  = math.floor(round(qty / step, 8)) * step
        qty  = round(qty, 10)  # eliminate any remaining float artefacts

        # If natural size is below minimum, don't force a trade — skip it
        if qty < cfg.QTY_MIN:
            return 0.0
        return qty

    # ── SL price level ────────────────────────────────────────────────────────

    def sl_price(self, entry: float, side: str, atr: float | None = None, regime: str = "TREND") -> float:
        """
        Stop-loss price.  Uses ATR-based distance (sl_mult × ATR) when
        available, where sl_mult is chosen by market regime.
        Falls back to fixed STOP_LOSS_PCT otherwise.
        """
        if atr and atr > 0:
            sl_mult = self.regime_params(regime)["sl"]
            dist    = atr * sl_mult
        else:
            dist = entry * (cfg.STOP_LOSS_PCT / 100)
        return entry - dist if side == "long" else entry + dist

    # ── PnL calculation ───────────────────────────────────────────────────────

    def calc_pnl(self, entry: float, exit_price: float, qty: float, side: str) -> float:
        """Realised PnL in USDT after fees (taker open + taker close)."""
        if side == "long":
            raw_pnl = (exit_price - entry) * qty
        else:
            raw_pnl = (entry - exit_price) * qty

        # Fee cost: taker on entry notional + taker on exit notional
        open_fee  = entry      * qty * (cfg.TAKER_FEE_PCT / 100)
        close_fee = exit_price * qty * (cfg.TAKER_FEE_PCT / 100)
        return raw_pnl - open_fee - close_fee
    # ── Trailing TP helpers ───────────────────────────────────────────────────

    # ── Trailing TP helpers ───────────────────────────────────────────────────

    def update_trail(self, pos, mark_price: float, live_atr: float | None = None) -> bool:
        """
        Unified 1R:2R trailing system — all regimes, no fixed TP.

        Rules:
          - sl_dist  = ATR × sl_mult (frozen at entry, same as SL placement)
          - Trail activates when best_price moves +2× sl_dist from entry
          - Trail callback = 1× sl_dist (floor ratchets up 1R for every 1R gained)
          - No breakeven SL — trail activation at +2R already guarantees +1R floor
          - No fixed TP — only exits are trail stop or hard SL

        Example (1R = $1 risk):
          best=+2R → trail floor = +1R
          best=+3R → trail floor = +2R
          best=+5R → trail floor = +4R
        """
        entry_atr = getattr(pos, "atr", None)
        regime    = getattr(pos, "regime", "TREND")
        rp        = self.regime_params(regime)

        # Signal type — continuations arm the trail earlier (1.5R) because they
        # ride existing momentum. All other signals get 2.0R breathing room.
        # Mirrors backtest.py check_trail() logic.
        signal_type = getattr(pos, "signal_type", "")

        if entry_atr and entry_atr > 0:
            sl_dist       = entry_atr * rp["sl"]   # 1R in price terms (frozen at entry)
            activate_dist = sl_dist * 1.5 if signal_type == "continuation" else sl_dist * 2.0
            callback_dist = sl_dist * 1.0           # trail floor moves up 1R at a time
        else:
            # Fallback: fixed-% when ATR unavailable (warm-up edge case)
            sl_dist       = pos.entry_price * (cfg.STOP_LOSS_PCT / 100)
            activate_dist = sl_dist * 2.0
            callback_dist = sl_dist * 1.0

        if pos.side == "long":
            if mark_price > pos.best_price:
                pos.best_price = mark_price

            if not pos.trail_active:
                # Arm trail at +2R
                if pos.best_price >= pos.entry_price + activate_dist:
                    pos.trail_active = True
                    pos.trail_stop   = pos.best_price - callback_dist
                    log.info(
                        f"Trail activated LONG  best={pos.best_price:.4f}"
                        f"  trail_stop={pos.trail_stop:.4f}"
                        f"  floor=+{(pos.trail_stop - pos.entry_price) / sl_dist:.1f}R"
                    )
            else:
                # Ratchet floor up 1R at a time
                new_stop = pos.best_price - callback_dist
                if new_stop > pos.trail_stop:
                    pos.trail_stop = new_stop
                    log.debug(
                        f"Trail ratchet LONG  best={pos.best_price:.4f}"
                        f"  trail_stop={pos.trail_stop:.4f}"
                        f"  floor=+{(pos.trail_stop - pos.entry_price) / sl_dist:.1f}R"
                    )
                if mark_price <= pos.trail_stop:
                    return True

        else:  # short
            if pos.best_price == 0.0 or mark_price < pos.best_price:
                pos.best_price = mark_price

            if not pos.trail_active:
                if pos.best_price <= pos.entry_price - activate_dist:
                    pos.trail_active = True
                    pos.trail_stop   = pos.best_price + callback_dist
                    log.info(
                        f"Trail activated SHORT  best={pos.best_price:.4f}"
                        f"  trail_stop={pos.trail_stop:.4f}"
                        f"  floor=+{(pos.entry_price - pos.trail_stop) / sl_dist:.1f}R"
                    )
            else:
                new_stop = pos.best_price + callback_dist
                if new_stop < pos.trail_stop:
                    pos.trail_stop = new_stop
                    log.debug(
                        f"Trail ratchet SHORT  best={pos.best_price:.4f}"
                        f"  trail_stop={pos.trail_stop:.4f}"
                        f"  floor=+{(pos.entry_price - pos.trail_stop) / sl_dist:.1f}R"
                    )
                if mark_price >= pos.trail_stop:
                    return True  # close signal

        return False

    # ── Breakeven price ───────────────────────────────────────────────────────

    def breakeven_price(self, entry: float, side: str, buffer: float = 0.0010) -> float:
        """Return the minimum exit price that covers both taker fees + a small profit buffer.

        Formula (round-trip fee on notional):
            long  → entry × (1 + f + buffer) / (1 - f)
            short → entry × (1 - f - buffer) / (1 + f)

        Args:
            entry:  position entry price
            side:   'long' or 'short'
            buffer: fractional profit cushion on top of fees (default 0.10%)
        """
        f = cfg.TAKER_FEE_PCT / 100.0
        if side == "long":
            return entry * (1 + f + buffer) / (1 - f)
        else:
            return entry * (1 - f - buffer) / (1 + f)
