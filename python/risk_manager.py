"""
risk_manager.py – Position sizing and pre-trade risk checks.

Position size formula (risk-based):
    risk_usdt = balance * RISK_PER_TRADE_PCT / 100
    sl_distance = entry_price * STOP_LOSS_PCT / 100
    qty = (risk_usdt * LEVERAGE) / sl_distance

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
        Cooldown scaled by last close reason and consecutive SL streak.

        After TP  → base cooldown (COOLDOWN_CANDLES)
        After SL  → 2× base cooldown  (market rejected us, wait longer)
        After 2+ consecutive SLs → 4× base cooldown  (market is fighting us hard)

        All values in seconds (assumes 1m candles: 1 candle = 60s).
        """
        base = cfg.COOLDOWN_CANDLES * 60
        reason = getattr(state, "last_close_reason", "")
        streak = getattr(state, "consecutive_sl", 0)

        if streak >= 2:
            cooldown = base * 2   # was 4× — too aggressive, was blocking valid signals
        elif reason == "sl":
            cooldown = base * 2
        else:
            cooldown = base

        # Hard cap: never wait more than 120s regardless of streak.
        # On fast-moving 1m markets a 240s block misses entire trend legs.
        cooldown = min(cooldown, 120)

        if cooldown != base:
            log.debug(
                f"dynamic_cooldown={cooldown}s  reason={reason}  consecutive_sl={streak}"
            )
        return cooldown

    def _dynamic_risk_pct(self, state: State) -> float:
        """
        Scale RISK_PER_TRADE_PCT down linearly as daily loss grows.

        At 0% daily loss  → full base risk (RISK_PER_TRADE_PCT)
        At MAX_DAILY_LOSS → 0% risk (no trade would pass anyway, but sizing is 0)

        Also applies a consecutive-SL penalty: each SL in a row cuts risk by 20%
        (capped at 60% reduction) so a losing streak doesn't blow up the account.

        Examples (base=1%, max_loss=3%):
          daily_loss=0%,  streak=0 → 1.00%
          daily_loss=1.5%, streak=0 → 0.50%
          daily_loss=0%,  streak=2 → 0.60%
          daily_loss=1.5%, streak=2 → 0.30%
        """
        base = cfg.RISK_PER_TRADE_PCT
        daily_loss = abs(min(state.daily_loss_pct(), 0.0))   # 0 if positive day
        max_loss   = cfg.MAX_DAILY_LOSS_PCT

        # Linear scale: 1.0 at 0% loss, 0.0 at MAX_DAILY_LOSS
        daily_factor = max(0.0, 1.0 - (daily_loss / max_loss))

        # Consecutive SL penalty: -20% per SL, max -60%
        streak = getattr(state, "consecutive_sl", 0)
        streak_factor = max(0.4, 1.0 - (streak * 0.20))

        dynamic = base * daily_factor * streak_factor

        if dynamic < base:
            log.debug(
                f"dynamic_risk={dynamic:.3f}%  base={base}%  "
                f"daily_loss={daily_loss:.2f}%  consecutive_sl={streak}"
            )
        return dynamic

    def can_trade(self, state: State, live_balance: float | None = None) -> bool:
        """Return True if it is safe to open a new trade right now."""

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

        # Daily drawdown circuit breaker (Option B — replaces WR-based trade cap).
        # Steps 2+3 already shrink size and slow pacing during bad streaks.
        # This is the hard stop: if we've lost MAX_DAILY_LOSS_PCT of balance
        # today, no new trades for the rest of the day.
        daily_loss = state.daily_loss_pct()
        if daily_loss <= -cfg.MAX_DAILY_LOSS_PCT:
            log.warning(
                "can_trade=False  reason=max_daily_loss  "
                f"daily_loss={daily_loss:.2f}%  limit={cfg.MAX_DAILY_LOSS_PCT}%"
            )
            return False

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

    def position_size(
        self,
        entry_price: float,
        balance: float,
        state: State | None = None,
        atr: float | None = None,
    ) -> float:
        """
        Calculate the position quantity in base asset (WLD).
        Returned value is already rounded to 1 decimal (Binance WLDUSDT step).

        When `atr` is provided the SL distance is ATR-based (SL_ATR_MULT × ATR),
        keeping position size proportional to current volatility.
        Falls back to fixed STOP_LOSS_PCT when ATR is unavailable.
        """
        risk_pct  = self._dynamic_risk_pct(state) if state else cfg.RISK_PER_TRADE_PCT
        risk_usdt = balance * (risk_pct / 100)

        if atr and atr > 0:
            sl_distance = atr * cfg.SL_ATR_MULT
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

    # ── TP / SL price levels ──────────────────────────────────────────────────

    def tp_price(self, entry: float, side: str, atr: float | None = None) -> float:
        """
        Take-profit price.  Uses ATR-based distance (TP_ATR_MULT × ATR) when
        available; falls back to fixed TAKE_PROFIT_PCT otherwise.
        """
        if atr and atr > 0:
            dist = atr * cfg.TP_ATR_MULT
        else:
            dist = entry * (cfg.TAKE_PROFIT_PCT / 100)
        return entry + dist if side == "long" else entry - dist

    def sl_price(self, entry: float, side: str, atr: float | None = None) -> float:
        """
        Stop-loss price.  Uses ATR-based distance (SL_ATR_MULT × ATR) when
        available; falls back to fixed STOP_LOSS_PCT otherwise.
        """
        if atr and atr > 0:
            dist = atr * cfg.SL_ATR_MULT
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

    def update_trail(self, pos, mark_price: float) -> bool:
        """
        Update trailing TP state on `pos` (a Position dataclass).
        Returns True when the trail stop has been hit and we should close.

        Uses ATR-based distances when pos.atr is set (stored at open time),
        otherwise falls back to fixed TRAIL_ACTIVATE_PCT / TRAIL_CALLBACK_PCT.
        """
        atr = getattr(pos, "atr", None)
        if atr and atr > 0:
            activate_dist = atr * cfg.TRAIL_ACTIVATE_ATR_MULT  # arm after N× ATR in profit (decoupled from SL dist)
            callback_dist = atr * cfg.SL_ATR_MULT * 0.5         # trail gives back 50% of SL distance — balanced between noise tolerance and profit capture
        else:
            activate_dist = pos.entry_price * (cfg.TRAIL_ACTIVATE_PCT / 100)
            callback_dist = pos.entry_price * (cfg.TRAIL_CALLBACK_PCT / 100)

        # Breakeven distance = 1×SL_ATR_MULT × ATR (halfway to trail activation).
        # Once price moves this far in our favour, SL is slid to entry so a
        # reversal before the trail arms costs nothing instead of a full SL hit.
        if atr and atr > 0:
            breakeven_dist = atr * cfg.SL_ATR_MULT
        else:
            breakeven_dist = pos.entry_price * (cfg.STOP_LOSS_PCT / 100)

        if pos.side == "long":
            # Track the highest price seen
            if mark_price > pos.best_price:
                pos.best_price = mark_price

            if not pos.trail_active:
                # Breakeven stop: slide SL to entry once price moves 1×SL dist in profit
                if pos.best_price >= pos.entry_price + breakeven_dist:
                    if pos.sl_price < pos.entry_price:
                        pos.sl_price = pos.entry_price
                        log.debug(
                            f"Breakeven SL  LONG  entry={pos.entry_price:.4f}"
                            f"  best={pos.best_price:.4f}  new_sl={pos.sl_price:.4f}"
                        )
                # Activate trail once price moves far enough above entry
                if pos.best_price >= pos.entry_price + activate_dist:
                    pos.trail_active = True
                    pos.trail_stop = pos.best_price - callback_dist
                    log.info(
                        f"Trail activated LONG  best={pos.best_price:.4f}"
                        f"  trail_stop={pos.trail_stop:.4f}"
                    )
            else:
                # Ratchet up the trail stop
                new_stop = pos.best_price - callback_dist
                if new_stop > pos.trail_stop:
                    pos.trail_stop = new_stop
                # Check hit
                if mark_price <= pos.trail_stop:
                    return True  # close signal

        else:  # short
            # Track the lowest price seen
            if pos.best_price == 0.0 or mark_price < pos.best_price:
                pos.best_price = mark_price

            if not pos.trail_active:
                # Breakeven stop: slide SL to entry once price moves 1×SL dist in profit
                if pos.best_price <= pos.entry_price - breakeven_dist:
                    if pos.sl_price > pos.entry_price:
                        pos.sl_price = pos.entry_price
                        log.debug(
                            f"Breakeven SL  SHORT  entry={pos.entry_price:.4f}"
                            f"  best={pos.best_price:.4f}  new_sl={pos.sl_price:.4f}"
                        )
                # Activate trail once price moves far enough below entry
                if pos.best_price <= pos.entry_price - activate_dist:
                    pos.trail_active = True
                    pos.trail_stop = pos.best_price + callback_dist
                    log.info(
                        f"Trail activated SHORT  best={pos.best_price:.4f}"
                        f"  trail_stop={pos.trail_stop:.4f}"
                    )
            else:
                new_stop = pos.best_price + callback_dist
                if new_stop < pos.trail_stop:
                    pos.trail_stop = new_stop
                if mark_price >= pos.trail_stop:
                    return True  # close signal

        return False
