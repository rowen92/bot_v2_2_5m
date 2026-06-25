"""
logger.py – Configures coloured console logging + file logging to trades.log.

Every trade event is written as a structured line to trades.log so it can
be parsed later for analytics.
"""

from __future__ import annotations

import logging
import os
import sys
import time

import colorlog

from config import cfg

# Path for the positions-only log (sits next to trades.log)
_POSITIONS_LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(cfg.LOG_FILE)), "positions.log"
)


def setup_logging() -> None:
    """Call once at startup to configure the root logger."""

    # ── Console handler (coloured) ─────────────────────────────────────────
    console = colorlog.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s  %(levelname)-8s%(reset)s %(name)-14s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG":    "cyan",
                "INFO":     "green",
                "WARNING":  "yellow",
                "ERROR":    "red",
                "CRITICAL": "bold_red",
            },
        )
    )

    # ── File handler (plain text, all levels) ─────────────────────────────
    file_handler = logging.FileHandler(cfg.LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)-14s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)

    # ── Positions-only log (trade logger → positions.log) ──────────────────
    positions_handler = logging.FileHandler(_POSITIONS_LOG_FILE, encoding="utf-8")
    positions_handler.setLevel(logging.INFO)
    positions_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    # Only attach to the "trade" logger; propagate=True keeps it in trades.log too
    trade_logger = logging.getLogger("trade")
    trade_logger.addHandler(positions_handler)

    # Silence noisy third-party loggers
    for noisy in ("websockets", "asyncio", "urllib3", "binance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Trade event helpers ────────────────────────────────────────────────────────

_trade_log = logging.getLogger("trade")


def log_open(side: str, entry: float, qty: float, tp: float, sl: float, mode: str) -> None:
    _trade_log.info(
        f"OPEN  side={side.upper()}  entry={entry:.4f}  qty={qty}  "
        f"tp={tp:.4f}  sl={sl:.4f}  mode={mode}"
    )


def log_close(
    side: str,
    entry: float,
    exit_price: float,
    qty: float,
    pnl: float,
    reason: str,
    mode: str,
    balance: float,
    open_time: float,
) -> None:
    duration_s = int(time.time() - open_time)
    mins, secs = divmod(duration_s, 60)
    duration_str = f"{mins}m{secs:02d}s"
    emoji = "✅" if pnl >= 0 else "❌"
    _trade_log.info(
        f"CLOSE {emoji}  side={side.upper()}  entry={entry:.4f}  exit={exit_price:.4f}  "
        f"qty={qty}  pnl={pnl:+.4f} USDT  reason={reason.upper()}  "
        f"duration={duration_str}  balance={balance:.2f}  mode={mode}"
    )


def log_signal(signal: str, indicators: dict) -> None:
    ind_str = "  ".join(f"{k}={v}" for k, v in indicators.items())
    logger  = logging.getLogger("strategy")
    if signal == "none":
        logger.debug(f"SIGNAL=NONE  {ind_str}")
    else:
        logger.info(f"SIGNAL={signal.upper()}  {ind_str}")


def log_daily_stats(state) -> None:  # type: ignore[annotation-unchecked]
    balance = state.paper_balance if cfg.is_paper() else state.live_balance_snapshot
    logging.getLogger("stats").info(
        f"DAILY  pnl={state.daily_realised_pnl:+.4f}  "
        f"trades={state.total_trades}  win_rate={state.win_rate():.1f}%  "
        f"balance={balance:.2f}"
    )
