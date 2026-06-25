"""
state.py – shared in-memory state (candles, order book, current position, stats).
A single `State` instance is created in bot.py and passed around.
Thread-safe via asyncio (single-threaded event loop).
"""

from __future__ import annotations
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

    # Trailing TP state --------------------------------------------------------
    # best_price: highest mark for long, lowest mark for short since entry
    best_price:      float = 0.0   # set to entry_price after open
    trail_active:    bool  = False  # True once activate threshold is crossed
    trail_stop:      float = 0.0   # current trailing stop level


# ── Main state object ──────────────────────────────────────────────────────────

class State:
    def __init__(self):
        # Rolling candle buffer (max 200 candles kept in memory)
        self._candles: deque[Candle] = deque(maxlen=200)

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
        self.oi_history: deque[float] = deque(maxlen=200)

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

        # Cooldown: candle index of the last closed trade
        # Strategy will not re-enter for COOLDOWN_CANDLES bars after any close
        self.last_close_candle: int = -999

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

    def ob_imbalance(self) -> float:
        """bid_volume / ask_volume ratio from top-20 levels."""
        bid_vol = sum(self.bids.values())
        ask_vol = sum(self.asks.values())
        if ask_vol == 0:
            return 1.0
        return bid_vol / ask_vol

    # ── Daily PnL ─────────────────────────────────────────────────────────────

    def record_pnl(self, pnl: float) -> None:
        self._reset_daily_if_needed()
        self.daily_realised_pnl += pnl
        self.total_trades += 1
        if pnl >= 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

    def daily_loss_pct(self) -> float:
        self._reset_daily_if_needed()
        if cfg.is_paper():
            base = self.paper_start_balance
        else:
            base = self.live_balance_snapshot  # set at startup by bot.py
        if base == 0:
            return 0.0
        return (self.daily_realised_pnl / base) * 100

    def _reset_daily_if_needed(self) -> None:
        now = time.time()
        if now - self.daily_reset_ts >= 86_400:
            self.daily_realised_pnl = 0.0
            self.daily_reset_ts = now
            if cfg.is_paper():
                # Only snapshot the balance when flat — paper_balance is
                # negative mid-trade because margin is pre-deducted.
                if self.position is None:
                    self.paper_start_balance = self.paper_balance

    # ── Win rate ──────────────────────────────────────────────────────────────

    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100
