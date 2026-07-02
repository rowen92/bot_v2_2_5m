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

    # (old break-retest / OI params removed — strategy is now trend-following)

    # ── Risk ───────────────────────────────────────────────────────────────────
    RISK_PER_TRADE_PCT: float  = _float("RISK_PER_TRADE_PCT", 1.0)
    # MAX_DAILY_LOSS_PCT: hard stop for the day — no new trades once daily PnL
    # drops below this % of starting balance. Raised to 5% (from 3%) so that
    # a choppy morning doesn't block the afternoon trend (see Step 10 analysis).
    # Steps 2+3 already handle bad streaks via size reduction + cooldown.
    MAX_DAILY_LOSS_PCT: float  = _float("MAX_DAILY_LOSS_PCT", 5.0)
    PAPER_INITIAL_BALANCE: float = _float("PAPER_INITIAL_BALANCE", 150.0)

    # ── ATR-based SL / TP (dynamic — preferred over fixed %) ──────────────────
    # SL is placed SL_ATR_MULT × ATR away from entry.
    # TP is placed TP_ATR_MULT × ATR away from entry.
    # This keeps risk proportional to the current volatility regime.
    # e.g. ATR=0.0010, SL_ATR_MULT=1.5 → SL distance = 0.0015
    # On 1m candles, 1.5× ATR SL is too tight — normal wicks tag it before the
    # trade develops. 2.0× ATR gives enough breathing room while keeping the
    # same dollar risk (position size shrinks proportionally via position_size()).
    # TP scaled to 4.0× ATR to maintain the 2:1 TP:SL ratio.
    SL_ATR_MULT: float         = _float("SL_ATR_MULT", 2.0)
    TP_ATR_MULT: float         = _float("TP_ATR_MULT", 4.0)   # TP:SL ratio = 2:1

    # ── Fixed-% fallback (used only when ATR is unavailable) ──────────────────
    TAKE_PROFIT_PCT: float     = _float("TAKE_PROFIT_PCT", 0.40)
    STOP_LOSS_PCT: float       = _float("STOP_LOSS_PCT", 0.20)

    # ── Fees ───────────────────────────────────────────────────────────────────
    # Only taker fee is used — all entries and exits are MARKET orders.
    # WLDUSDT (USDT pair): 0.05% taker  |  WLDUSDC (USDC pair): 0.04% taker
    TAKER_FEE_PCT: float = _float("TAKER_FEE_PCT", 0.05)   # % per side

    # ── Trailing Take-Profit ───────────────────────────────────────────────────
    # TRAIL_ACTIVATE_PCT: how far price must move from entry to arm the trail
    # TRAIL_CALLBACK_PCT: pullback from peak that triggers close
    TRAIL_ACTIVATE_PCT: float  = _float("TRAIL_ACTIVATE_PCT", 0.20)
    TRAIL_CALLBACK_PCT: float  = _float("TRAIL_CALLBACK_PCT", 0.10)

    # ATR-based trail activation (used when ATR is available, preferred over fixed %).
    # Price must move TRAIL_ACTIVATE_ATR_MULT × ATR in your favour before the
    # trail arms. Kept higher than SL_ATR_MULT so a brief retest near entry
    # doesn't arm the trail prematurely and close before the real move develops.
    # e.g. ATR=0.00091, mult=2.0 → trail arms only after 0.00182 move (~2× SL dist)
    # Raised from 2.0 → 3.0: trail only arms after a real move (3× ATR).
    # At 2.0 the trail was activating too close to entry, then giving back
    # almost all the profit via callback — resulting in R:R < 0.5:1.
    # With 3.0 activation and tighter callback (0.4 in risk_manager),
    # minimum guaranteed profit = 3×ATR - 0.8×ATR = 2.2×ATR → R:R ≈ 1.1:1
    TRAIL_ACTIVATE_ATR_MULT: float = _float("TRAIL_ACTIVATE_ATR_MULT", 3.5)

    # ── Signal cooldown ────────────────────────────────────────────────────────
    # Minimum closed candles to wait after any trade close before re-entering
    # 1 candle cooldown on 1m — allows catching the next trend leg quickly
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
