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
    KLINE_INTERVAL: str  = os.getenv("KLINE_INTERVAL", "5m")

    # ── Futures ────────────────────────────────────────────────────────────────
    LEVERAGE: int        = _int("LEVERAGE", 5)
    MARGIN_TYPE: str     = os.getenv("MARGIN_TYPE", "ISOLATED").upper()

    # (old break-retest / OI params removed — strategy is now trend-following)

    # ── Risk ───────────────────────────────────────────────────────────────────
    RISK_PER_TRADE_PCT: float  = _float("RISK_PER_TRADE_PCT", 5.0)
    # MAX_DAILY_LOSS_PCT: hard stop for the day — no new trades once daily PnL
    # drops below this % of starting balance. Raised to 5% (from 3%) so that
    # a choppy morning doesn't block the afternoon trend (see Step 10 analysis).
    # Steps 2+3 already handle bad streaks via size reduction + cooldown.
    MAX_DAILY_LOSS_PCT: float  = _float("MAX_DAILY_LOSS_PCT", 5.0)
    PAPER_INITIAL_BALANCE: float = _float("PAPER_INITIAL_BALANCE", 150.0)

    # ── ATR-based SL / TP (dynamic — preferred over fixed %) ──────────────────
    # ── SL multipliers per regime (SL = sl_mult × ATR from entry) ────────────
    # Trail activates at +2× sl_dist, callback = 1× sl_dist (unified 1R:2R system).
    # No fixed TP — exits are trail stop or hard SL only.
    # NOTE: SL_ATR_MULT is fallback-only when ATR=None (warm-up edge case).
    SL_ATR_MULT: float         = _float('SL_ATR_MULT', 2.0)   # fallback

    CHOP_SL_MULT:        float = _float('CHOP_SL_MULT',        1.0)
    TREND_SL_MULT:       float = _float('TREND_SL_MULT',       2.0)
    STRONG_TREND_SL_MULT: float = _float('STRONG_TREND_SL_MULT', 2.5)

    # Block all new entries when market_regime == CHOP (ADX < 45, weak momentum).
    # Set CHOP_BLOCK=false in .env to re-enable per-bot if needed.
    CHOP_BLOCK: bool = os.getenv('CHOP_BLOCK', 'false').lower() == 'true'

    # Minimum price movement (as ATR multiple) required before a FLIP is allowed.
    # FLIP is suppressed if abs(mark_price - entry) < FLIP_MIN_MOVE_ATR × ATR.
    # Default 0.3 — safe for most assets (WLD genuine reversals move ~0.4×ATR)
    # DOGE overrides to 0.5 in .env — tighter filter needed for low-volatility oscillation
    FLIP_MIN_MOVE_ATR: float = _float('FLIP_MIN_MOVE_ATR', 0.3)

    # Enable/disable flip trades (closing current position and reversing on opposite signal).
    # Set ENABLE_FLIP=false in .env to disable — backtest shows worse performance on WLD.
    ENABLE_FLIP: bool = os.getenv('ENABLE_FLIP', 'false').lower() == 'true'

    # ── Fixed-% fallback (used only when ATR is unavailable) ──────────────────
    TAKE_PROFIT_PCT: float = _float("TAKE_PROFIT_PCT", 0.40)  # unused at runtime
    STOP_LOSS_PCT:   float = _float("STOP_LOSS_PCT",   0.20)

    # ── Fees ───────────────────────────────────────────────────────────────────
    # Only taker fee is used — all entries and exits are MARKET orders.
    # WLDUSDT (USDT pair): 0.05% taker  |  WLDUSDC (USDC pair): 0.04% taker
    TAKER_FEE_PCT: float = _float("TAKER_FEE_PCT", 0.05)   # % per side

    # ── Signal cooldown ────────────────────────────────────────────────────────
    # Minimum closed candles to wait after any trade close before re-entering
    # 1 candle cooldown on 5m — 10 min buffer, avoids immediate re-entry on fakeouts
    COOLDOWN_CANDLES: int   = _int("COOLDOWN_CANDLES", 1)

    # ── Daily trade cap — REMOVED (Step 10 replaced by MAX_DAILY_LOSS_PCT) ────
    # WR-based cap was killing afternoon trend trades after a choppy morning.
    # The hard safety net is now the daily loss % circuit breaker above.
    # MAX_DAILY_TRADES kept as a disabled fallback (0 = off) in case we want
    # to re-enable a hard count ceiling via .env without a code change.
    MAX_DAILY_TRADES: int   = _int("MAX_DAILY_TRADES", 0)

    # ── Candle quality filters ─────────────────────────────────────────────────
    ATR_PERIOD: int         = _int("ATR_PERIOD", 14)

    # ── Symbol precision ───────────────────────────────────────────────────────
    # Qty step size for the traded symbol (Binance lot size filter)
    # WLDUSDT=0.1  BTCUSDT=0.001  ETHUSDT=0.01  SOLUSDT=0.1
    QTY_STEP: float    = _float("QTY_STEP", 0.1)
    QTY_MIN: float     = _float("QTY_MIN", 0.1)    # minimum order size
    PRICE_PRECISION: int = _int("PRICE_PRECISION", 4)  # decimal places for prices

    # ── Paths ──────────────────────────────────────────────────────────────────
    LOG_FILE: str = os.path.join(os.path.dirname(__file__), "..", "trades.log")


cfg = Config()
