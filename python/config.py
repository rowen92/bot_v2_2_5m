"""
config.py – loads every setting from .env and exposes a single Config object.
All other modules import `cfg` from here.
"""

import os
from dotenv import load_dotenv

# Load .env from the project root (one level above python/)
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(dotenv_path=_ENV_PATH)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Required env variable '{key}' is not set in .env")
    return val


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


class Config:
    # ── Credentials ────────────────────────────────────────────────────────────
    API_KEY: str    = _require("BINANCE_API_KEY")
    API_SECRET: str = _require("BINANCE_SECRET_KEY")

    # ── Mode ───────────────────────────────────────────────────────────────────
    TRADING_MODE: str = os.getenv("TRADING_MODE", "PAPER").upper()  # PAPER | LIVE

    @classmethod
    def is_paper(cls) -> bool:
        return cls.TRADING_MODE == "PAPER"

    # ── Symbol ─────────────────────────────────────────────────────────────────
    SYMBOL: str          = os.getenv("SYMBOL", "WLDUSDT").upper()
    KLINE_INTERVAL: str  = os.getenv("KLINE_INTERVAL", "1m")

    # ── Futures ────────────────────────────────────────────────────────────────
    LEVERAGE: int        = _int("LEVERAGE", 10)
    MARGIN_TYPE: str     = os.getenv("MARGIN_TYPE", "ISOLATED").upper()

    # ── Strategy: Break & Retest + Open Interest ──────────────────────────────
    # How many recent bars to scan for the breakout candle
    SWING_LOOKBACK: int          = _int("SWING_LOOKBACK", 10)
    # How many bars before the breakout to measure the prior swing level
    BREAK_LOOKBACK: int          = _int("BREAK_LOOKBACK", 20)
    # Max distance from the broken level (in ATR units) that counts as a retest touch
    STRUCTURE_TOUCH_ATR: float   = _float("STRUCTURE_TOUCH_ATR", 0.5)
    # How many consecutive OI readings must be rising to confirm entry
    OI_CONFIRM_BARS: int         = _int("OI_CONFIRM_BARS", 3)
    # Rolling window for OI mean filter (OI must be above this average)
    OI_MEAN_BARS: int            = _int("OI_MEAN_BARS", 20)

    # ── Risk ───────────────────────────────────────────────────────────────────
    RISK_PER_TRADE_PCT: float  = _float("RISK_PER_TRADE_PCT", 1.0)
    TAKE_PROFIT_PCT: float     = _float("TAKE_PROFIT_PCT", 0.40)
    STOP_LOSS_PCT: float       = _float("STOP_LOSS_PCT", 0.20)
    MAX_DAILY_LOSS_PCT: float  = _float("MAX_DAILY_LOSS_PCT", 3.0)
    PAPER_INITIAL_BALANCE: float = _float("PAPER_INITIAL_BALANCE", 1000.0)

    # ── Fees ───────────────────────────────────────────────────────────────────
    # Only taker fee is used — all entries and exits are MARKET orders.
    # For regular USDT pairs set TAKER_FEE_PCT=0.05
    TAKER_FEE_PCT: float = _float("TAKER_FEE_PCT", 0.04)   # % per side

    # ── Trailing Take-Profit ───────────────────────────────────────────────────
    # TRAIL_ACTIVATE_PCT: how far price must move from entry to arm the trail
    # TRAIL_CALLBACK_PCT: pullback from peak that triggers close
    TRAIL_ACTIVATE_PCT: float  = _float("TRAIL_ACTIVATE_PCT", 0.20)
    TRAIL_CALLBACK_PCT: float  = _float("TRAIL_CALLBACK_PCT", 0.10)

    # ── Signal cooldown ────────────────────────────────────────────────────────
    # Minimum closed candles to wait after any trade close before re-entering
    COOLDOWN_CANDLES: int   = _int("COOLDOWN_CANDLES", 3)

    # ── Candle quality filters ─────────────────────────────────────────────────
    ATR_PERIOD: int         = _int("ATR_PERIOD", 14)
    ATR_MAX_MULT: float     = _float("ATR_MAX_MULT", 1.8)      # reject spike candles
    VOLUME_MIN_MULT: float  = _float("VOLUME_MIN_MULT", 0.6)   # dead-market filter

    # ── Post-panic recovery filter ─────────────────────────────────────────────
    # Suppresses OI-falling SHORT signals during short-covering rallies after a flush.
    # PANIC_VOL_MULT: candle volume must be this many × avg to count as a panic flush
    # POST_PANIC_BARS: how many bars after the panic to remain in suppression mode
    PANIC_VOL_MULT: float   = _float("PANIC_VOL_MULT", 4.0)
    POST_PANIC_BARS: int    = _int("POST_PANIC_BARS", 20)

    # ── Symbol precision ───────────────────────────────────────────────────────
    # Qty step size for the traded symbol (Binance lot size filter)
    # WLDUSDT=0.1  BTCUSDT=0.001  ETHUSDT=0.01  SOLUSDT=0.1
    QTY_STEP: float    = _float("QTY_STEP", 0.1)
    QTY_MIN: float     = _float("QTY_MIN", 0.1)    # minimum order size
    PRICE_PRECISION: int = _int("PRICE_PRECISION", 4)  # decimal places for prices

    # ── WebSocket multiplex stream names ───────────────────────────────────────
    @classmethod
    def ws_streams(cls) -> list[str]:
        sym = cls.SYMBOL.lower()
        interval = cls.KLINE_INTERVAL
        return [
            f"{sym}@kline_{interval}",
            f"{sym}@depth20@100ms",
            f"{sym}@markPrice@1s",   # @1s cadence carries the OI field "o"
        ]

    # ── Paths ──────────────────────────────────────────────────────────────────
    LOG_FILE: str = os.path.join(os.path.dirname(__file__), "..", "trades.log")


cfg = Config()
