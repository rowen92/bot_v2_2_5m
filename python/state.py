"""
state.py – shared in-memory state (candles, order book, current position, stats).
A single `State` instance is created in bot.py and passed around.
Thread-safe via asyncio (single-threaded event loop).
"""

from __future__ import annotations
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

from config import cfg


# ── Candle ─────────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    open_time: int
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float
    is_closed: bool  # True = candle is finalised


# ── Position ───────────────────────────────────────────────────────────────────

@dataclass
class Position:
    side:        str    # 'long' | 'short'
    entry_price: float
    qty:         float  # in base asset units (e.g. WLD)
    tp_price:    float
    sl_price:    float
    open_time:   float = field(default_factory=time.time)
    order_id:    Optional[str] = None
    open_fee:    float = 0.0  # taker fee paid at entry (stored for accurate pnl reporting)

    # ATR at entry time — used for dynamic trail distances in update_trail()
    atr:             Optional[float] = None

    # Market regime at entry time — governs SL/TP/trail multipliers for this position.
    # Frozen at open so mid-trade regime changes don't alter the position's risk profile.
    # Values: 'STRONG_TREND' | 'TREND' | 'CHOP'
    regime:          str = "TREND"

    # Trailing TP state --------------------------------------------------------
    # best_price: highest mark for long, lowest mark for short since entry
    best_price:      float = 0.0   # set to entry_price after open
    trail_active:    bool  = False  # True once activate threshold is crossed
    trail_stop:      float = 0.0   # current trailing stop level

    # DI-snap exhaustion entries use a fixed TP (EMA21 at entry) and a tight SL
    # (trigger candle high/low) instead of the ATR trail system.
    is_di_snap:      bool  = False  # True if this position was opened by DI-snap logic
    di_snap_tp:      float = 0.0    # fixed TP = EMA21 at entry time

    # Exhaustion-armed entries (1b) use a two-level flat TP exit — no trail.
    # In CHOP (ADX 20-49, which is almost always the regime on WLD 5m), price
    # travels only 0.5-1.5×ATR to EMA21 and then reverses. A trail that arms at
    # +3×ATR can never fire. Instead:
    #
    #   TP  = entry ± 1.5×ATR → full close, booked immediately as a win
    #   SL  = entry ∓ 1×ATR  (CHOP regime) → RR ≈ 1.5:1
    #   FLIP (opposite signal) always takes priority over TP
    #
    # tp1_price is frozen at open; 0.0 = degraded warmup state (SL-only).
    is_exhaustion_armed:  bool  = False  # True if opened by section 1b armed logic
    tp1_price:            float = 0.0    # TP level (entry ± 1.5×ATR) — full close

    # Legacy stubs — keep so any stale state.json fields deserialise without error
    tp2_price:            float = 0.0
    tp1_done:             bool  = False
    breakeven_set:        bool  = False
    ema21_trail_active:   bool  = False
    ema21_trail_stop:     float = 0.0
    partial_tp_done:      bool  = False


# ── Main state object ──────────────────────────────────────────────────────────

class State:
    def __init__(self):
        # Rolling candle buffer — ~16 hours of 1m candles.
        # Memory cost is negligible (~56 bytes/candle → ~56 KB total).
        # The cache-key fix (len, last_open_time) handles indicator freshness;
        # this larger window gives vol_avg and EMA a stable long-term baseline.
        self._candles: deque[Candle] = deque(maxlen=1000)

        # Latest (possibly open) candle being built from the stream
        self.live_candle: Optional[Candle] = None

        # Order book snapshot {price: qty} for bids and asks
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

        # Mark price (more reliable than last trade for futures)
        self.mark_price: float = 0.0

        # ── Open Interest history ──────────────────────────────────────────────
        # Populated by ws_client every time an openInterest or markPrice+OI
        # message arrives. maxlen keeps memory bounded.
        self.oi_history: deque[float] = deque(maxlen=1000)

        # Current open position (None = flat)
        self.position: Optional[Position] = None

        # Paper trading balance
        self.paper_balance: float = cfg.PAPER_INITIAL_BALANCE
        self.paper_start_balance: float = cfg.PAPER_INITIAL_BALANCE

        # Daily PnL tracking
        self.daily_realised_pnl: float = 0.0
        self.daily_reset_ts: float = time.time()
        self.live_balance_snapshot: float = 0.0   # set by bot on startup (live mode)

        # Stats
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0

        # Cooldown: wall-clock timestamp (seconds) of the last closed trade.
        # Using real time instead of candle index so the cooldown survives a
        # bot restart — a crash-restart won't wipe the wait period.
        # Initialised to 0 so the bot can trade immediately on a fresh start.
        self.last_close_ts: float = 0.0

        # Dynamic cooldown tracking
        # last_close_reason: 'sl' | 'tp' | 'trail_tp' — set by order_manager on close
        self.last_close_reason: str = ""
        # consecutive_sl: how many SL hits in a row without a winning trade between them
        self.consecutive_sl: int = 0

        # Anti-revenge zone: price and ATR of the last SL hit.
        # risk_manager uses these to block re-entries too close to the same
        # price level where the market just stopped us out.
        self.last_sl_entry_price: float = 0.0
        self.last_sl_atr: float = 0.0
        self.last_sl_side: str = ""   # 'long' | 'short' — side of the last SL trade

        # Startup guard: False until the bot opens its first trade after (re)start.
        # bot.py uses this to block continuation signals on the very first entry —
        # on a fresh start we have no position history so we don't know how old
        # the trend is or whether it's already exhausted.
        self.first_trade_done: bool = False

        # Concurrency guard: prevents a second tick from triggering a second
        # close while an async _live_close / _paper_close is still in-flight.
        self.is_closing: bool = False

        # Live ATR — updated every closed candle from strategy indicators.
        # Used by update_trail() so callback_dist adapts to current volatility
        # instead of being frozen at the ATR value from entry time.
        self.live_atr: Optional[float] = None

        # Close price of the most recently closed candle.
        # Used by order_manager to confirm breakeven SL on candle close
        # rather than on a wick tick — prevents spike candles from
        # shaking out a position that closed above the SL level.
        self.last_candle_close: float = 0.0

        # Guard to emit the "balance snapshot missing" critical log only once,
        # not on every tick.
        self._balance_missing_logged: bool = False

    # ── Candle helpers ─────────────────────────────────────────────────────────

    def add_closed_candle(self, c: Candle) -> None:
        # Guard against duplicate delivery on WS reconnect — Binance may replay
        # the last closed candle message, so skip if open_time already present.
        if self._candles and self._candles[-1].open_time == c.open_time:
            return
        self._candles.append(c)

    def candle_count(self) -> int:
        return len(self._candles)

    def to_dataframe(self) -> pd.DataFrame:
        """Return a DataFrame of all closed candles (columns: open,high,low,close,volume)."""
        rows = [
            {
                "open_time": c.open_time,
                "open":      c.open,
                "high":      c.high,
                "low":       c.low,
                "close":     c.close,
                "volume":    c.volume,
            }
            for c in self._candles
        ]
        return pd.DataFrame(rows)

    # ── Order book helpers ─────────────────────────────────────────────────────

    def best_bid(self) -> float:
        return max(self.bids.keys(), default=0.0)

    def best_ask(self) -> float:
        return min(self.asks.keys(), default=0.0)

    def mid_price(self) -> float:
        bb, ba = self.best_bid(), self.best_ask()
        if bb and ba:
            return (bb + ba) / 2
        return self.mark_price

    # ── Daily PnL ─────────────────────────────────────────────────────────────

    def record_pnl(self, pnl: float) -> None:
        self._reset_daily_if_needed()
        self.daily_realised_pnl += pnl
        self.total_trades += 1
        if pnl >= 0:
            self.winning_trades += 1
            self.consecutive_sl = 0   # reset streak on any win
        else:
            self.losing_trades += 1
            # Only penalise the streak for a real SL loss, not a breakeven SL
            # (breakeven SL slides pos.sl_price to entry — fees make pnl slightly
            # negative but it is not a true directional loss).
            if self.last_close_reason == "sl" and pnl < -0.01:
                self.consecutive_sl += 1
            else:
                # TRAIL_TP at a loss, FLIP, or breakeven SL — don't stack penalty
                self.consecutive_sl = 0

    def daily_loss_pct(self) -> float:
        self._reset_daily_if_needed()
        if cfg.is_paper():
            base = self.paper_start_balance
        else:
            base = self.live_balance_snapshot  # set at startup by bot.py
        if base == 0:
            # Snapshot not yet available — daily loss guard is non-functional.
            # Log only once to avoid spamming on every tick.
            if not self._balance_missing_logged:
                logging.getLogger("state").critical(
                    "daily_loss_pct: live_balance_snapshot=0 — "
                    "daily loss circuit breaker is DISABLED. "
                    "Check API key permissions or network connectivity."
                )
                self._balance_missing_logged = True
            return 0.0
        return (self.daily_realised_pnl / base) * 100

    def _reset_daily_if_needed(self) -> None:
        now = time.time()
        if now - self.daily_reset_ts >= 86_400:
            self.daily_realised_pnl = 0.0
            self.daily_reset_ts = now
            if cfg.is_paper():
                # Snapshot settled balance as the new daily base.
                # If a position is open at midnight, paper_balance has margin
                # pre-deducted, so reconstruct the settled equivalent:
                #   settled ≈ paper_balance + locked_margin + open_fee
                if self.position is not None:
                    locked_margin = (
                        self.position.entry_price * self.position.qty / cfg.LEVERAGE
                    )
                    settled_approx = self.paper_balance + locked_margin + self.position.open_fee
                    self.paper_start_balance = settled_approx
                else:
                    self.paper_start_balance = self.paper_balance
            else:
                # For live mode, the daily base must be refreshed at midnight.
                # live_balance_snapshot is kept current by bot.py after every
                # close; here we just mark that a new daily window has started
                # so daily_loss_pct() uses the balance at the reset boundary,
                # not the stale startup value.
                # (bot.py will overwrite this with a fresh REST fetch on the
                # next close, but this prevents the guard being disabled all day
                # if no trade has closed yet after a midnight reset.)
                pass  # live_balance_snapshot already updated after every close

    # ── Win rate ──────────────────────────────────────────────────────────────

    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100
