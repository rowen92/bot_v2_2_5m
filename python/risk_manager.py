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
from config import cfg
from state import State

log = logging.getLogger("risk")


class RiskManager:

    # ── Pre-trade checks ──────────────────────────────────────────────────────

    def can_trade(self, state: State, live_balance: float | None = None) -> bool:
        """Return True if it is safe to open a new trade right now."""

        # Cooldown after last trade close
        candles_since_close = state.candle_count() - state.last_close_candle
        if candles_since_close < cfg.COOLDOWN_CANDLES:
            log.debug(
                f"can_trade=False  reason=cooldown  "
                f"candles_since_close={candles_since_close}  need={cfg.COOLDOWN_CANDLES}"
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

        # Daily drawdown breached
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
    ) -> float:
        """
        Calculate the position quantity in base asset (WLD).
        Returned value is already rounded to 1 decimal (Binance WLDUSDT step).
        """
        risk_usdt    = balance * (cfg.RISK_PER_TRADE_PCT / 100)
        sl_distance  = entry_price * (cfg.STOP_LOSS_PCT / 100)

        if sl_distance == 0:
            return 0.0

        notional = risk_usdt * cfg.LEVERAGE   # effective buying power at risk
        qty      = notional / sl_distance

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

    def tp_price(self, entry: float, side: str) -> float:
        mult = cfg.TAKE_PROFIT_PCT / 100
        return entry * (1 + mult) if side == "long" else entry * (1 - mult)

    def sl_price(self, entry: float, side: str) -> float:
        mult = cfg.STOP_LOSS_PCT / 100
        return entry * (1 - mult) if side == "long" else entry * (1 + mult)

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
        """
        activate_dist = pos.entry_price * (cfg.TRAIL_ACTIVATE_PCT / 100)
        callback_dist = pos.entry_price * (cfg.TRAIL_CALLBACK_PCT / 100)

        if pos.side == "long":
            # Track the highest price seen
            if mark_price > pos.best_price:
                pos.best_price = mark_price

            # Activate trail once price moves far enough above entry
            if not pos.trail_active:
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
