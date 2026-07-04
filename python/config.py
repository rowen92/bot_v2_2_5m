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
    # NOTE: these are fallback-only when ATR=None. Regime params below take over
    # at runtime once the strategy has warmed up.
    SL_ATR_MULT: float         = _float('SL_ATR_MULT', 2.0)
    TP_ATR_MULT: float         = _float('TP_ATR_MULT', 4.0)   # TP:SL ratio = 2:1

    # ── Regime-based multipliers (override SL/TP/trail per market condition) ───
    # CHOP       — ADX 40-44: marginal crossover signal, weak momentum
    #              trail_act > tp → trail intentionally never fires (fixed-TP scalp)
    # TREND      — ADX 45-49: confirmed momentum, normal settings
    # STRONG_TREND — ADX ≥ 50 + strong EMA separation: high-conviction trend
    #
    # trail_cb: fraction of sl_mult used as callback distance
    #   callback_dist = live_atr × sl_mult × trail_cb
    #   e.g. TREND: 2.0 × 0.75 = 1.5×ATR callback
    CHOP_SL_MULT:   float = _float('CHOP_SL_MULT',   1.5)
    CHOP_TP_MULT:   float = _float('CHOP_TP_MULT',   3.0)
    CHOP_TRAIL_ACT: float = _float('CHOP_TRAIL_ACT', 4.0)   # > tp → trail never fires
    CHOP_TRAIL_CB:  float = _float('CHOP_TRAIL_CB',  0.50)
    # Block all new entries when market_regime == CHOP (ADX < 45, weak momentum).
    # Trade #11: CHOP entry into a 7hr range → instant SL. No edge in this regime.
    # Set CHOP_BLOCK=false in .env to re-enable per-bot if needed.
    CHOP_BLOCK:     bool  = os.getenv('CHOP_BLOCK', 'true').lower() == 'true'

    TREND_SL_MULT:   float = _float('TREND_SL_MULT',   2.0)
    TREND_TP_MULT:   float = _float('TREND_TP_MULT',   4.0)
    TREND_TRAIL_ACT: float = _float('TREND_TRAIL_ACT', 2.0)   # lowered from 3.5 — WLD trends are short; arm trail at breakeven distance so it fires before reversal
    TREND_TRAIL_CB:  float = _float('TREND_TRAIL_CB',  0.75)   # widened from 0.60 — tolerate normal pullbacks within the trend instead of exiting on first dip

    STRONG_TREND_SL_MULT:   float = _float('STRONG_TREND_SL_MULT',   2.5)
    STRONG_TREND_TP_MULT:   float = _float('STRONG_TREND_TP_MULT',   5.0)
    STRONG_TREND_TRAIL_ACT: float = _float('STRONG_TREND_TRAIL_ACT', 2.5)  # lowered from 3.0 — genuine breakouts also reverse fast on WLD
    STRONG_TREND_TRAIL_CB:  float = _float('STRONG_TREND_TRAIL_CB',  0.75)  # widened from 0.60 — ride the move without early shake-out

    # ── Breakeven SL threshold multiplier ─────────────────────────────────────
    # Breakeven SL slides pos.sl_price to entry once:
    #   best_price >= entry + (entry_atr × sl_mult × BREAKEVEN_ATR_MULT)
    # Default: 0.5 — triggers at half the SL distance (suits BTC/ETH/WLD).
    # DOGE override (.env): 1.0 — requires a full SL-distance move before
    # breakeven fires. On low-volatility coins (ATR ~7e-05) the 0.5× threshold
    # equals ~1 ATR of pure tick noise and fires within seconds (trade #1: 42s).
    BREAKEVEN_ATR_MULT: float = _float('BREAKEVEN_ATR_MULT', 0.5)

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

    # Fallback-only — used by update_trail() when pos.atr is None (pre-warmup edge case).
    # At runtime, trail activation is controlled by CHOP/TREND/STRONG_TREND_TRAIL_ACT above.
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
