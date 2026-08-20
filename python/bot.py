import asyncio
import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))

from binance import AsyncClient
from config import cfg
from state import State
from strategy import ScalpingStrategy, check_flip_allowed, check_exhaustion_entry
from order_manager import OrderManager
from risk_manager import RiskManager
from ws_client import run_streams, fetch_open_interest
import logger as tlog

log      = logging.getLogger("bot")
strategy = ScalpingStrategy()
orders   = OrderManager()
risk     = RiskManager()

_pending_exhaustion: dict | None = None


# ---------------------------------------------------------------------------
# CANDLE CALLBACK  - fires once per closed 5m candle
# ---------------------------------------------------------------------------

async def on_closed_candle(state: State, client: AsyncClient) -> None:
    global _pending_exhaustion

    await fetch_open_interest(client, state)

    # ── Position sync guard ───────────────────────────────────────────────────
    # Catches state desync in three scenarios:
    #   1. Bot crash/restart — Binance crash-SL (3× dist) fired while bot was down
    #   2. Manual close on Binance UI while bot is running
    #   3. Liquidation — Binance force-closed, Python unaware
    # Runs once per 5m candle (cheap: one REST call only when position is open).
    if state.position is not None and not cfg.is_paper():
        try:
            positions = await client.futures_position_information(symbol=cfg.SYMBOL)
            actual_amt = 0.0
            for p in positions:
                if p.get("symbol") == cfg.SYMBOL:
                    actual_amt = abs(float(p.get("positionAmt", 0.0)))
                    break
            if actual_amt == 0.0:
                pos = state.position
                # ── Recover real exit price from Binance trade history ────────
                # The position was closed externally (TP bracket, crash-SL, manual,
                # or liquidation). Fetch the last fill so we log accurate PnL and
                # apply the correct post-trade treatment (TP vs SL cooldown/blocks).
                real_exit    = state.mark_price  # fallback if REST call fails
                close_reason = "sl"              # conservative fallback
                try:
                    trades = await client.futures_account_trades(
                        symbol=cfg.SYMBOL, limit=5
                    )
                    if trades:
                        last = sorted(trades, key=lambda t: t["time"], reverse=True)[0]
                        real_exit = float(last.get("price", real_exit))
                        _sign = risk.calc_pnl(pos.entry_price, real_exit, pos.qty, pos.side)
                        close_reason = "tp" if _sign > 0 else "sl"
                except Exception as exc:
                    log.warning(f"Position sync: could not fetch trade history: {exc}")

                real_pnl = risk.calc_pnl(pos.entry_price, real_exit, pos.qty, pos.side)
                log.warning(
                    f"Position sync: Binance shows no open position but state has "
                    f"{pos.side.upper()} entry={pos.entry_price:.4f} qty={pos.qty} — "
                    f"closed externally. exit={real_exit:.4f}  pnl={real_pnl:+.4f}  "
                    f"reason={close_reason}. Clearing state."
                )
                # Refresh balance so log_close shows the post-trade value
                fresh_balance = await orders._live_balance(client)
                if fresh_balance > 0:
                    state.live_balance_snapshot = fresh_balance
                tlog.log_close(
                    pos.side, pos.entry_price, real_exit,
                    pos.qty, real_pnl, close_reason, cfg.TRADING_MODE,
                    state.live_balance_snapshot,
                    pos.open_time,
                )
                state.record_pnl(real_pnl)
                state.last_close_reason = close_reason
                state.last_close_ts = time.time()
                if close_reason == "sl":
                    state.last_sl_entry_price = pos.entry_price
                    state.last_sl_atr          = pos.atr or 0.0
                    state.last_sl_side         = pos.side
                state.position = None
                tlog.log_daily_stats(state)
        except Exception as exc:
            log.warning(f"Position sync check failed: {exc}")
    # ─────────────────────────────────────────────────────────────────────────

    indicators = strategy.indicator_snapshot(state)
    if not indicators:
        from strategy import _MIN_BARS
        log.info(f"warming up: {state.candle_count()}/{_MIN_BARS} candles")
        return

    # get_signal reuses the cached dataframe — no double computation
    signal = strategy.get_signal(state)

    tlog.log_signal(signal, indicators)   # log every candle (signal=none is useful too)

    # Keep live ATR current so update_trail() uses current volatility,
    # not the stale ATR frozen at entry time.
    if indicators.get("atr"):
        state.live_atr = indicators["atr"]

    # ── Deferred exhaustion_armed: validate + enter at confirmation candle open ─
    if _pending_exhaustion is not None and state.position is None:
        pe   = _pending_exhaustion
        _pending_exhaustion = None
        conf = state._candles[-1] if state._candles else None
        if conf is not None and check_exhaustion_entry(pe, conf.close, conf.open):
            pe_sig = pe["signal"]
            pe_atr = pe["atr"]
            pe_regime = pe.get("regime", "CHOP")
            pe_live_bal = None
            if not cfg.is_paper():
                pe_live_bal = await orders._live_balance(client)
            if risk.can_trade(state, live_balance=pe_live_bal, signal_side=pe_sig):
                log.info(
                    f"exhaustion_armed entry  signal={pe_sig}  regime={pe_regime}"
                    f"  entry={conf.open:.4f}  (confirmation candle open)"
                )
                _saved_mark = state.mark_price
                state.mark_price = conf.open
                await orders.open_position(pe_sig, state, client, atr=pe_atr, strategy=strategy,
                                           signal_candle_open_time=pe.get("signal_candle_open_time", 0.0))
                state.mark_price = _saved_mark
            else:
                log.info(f"exhaustion_armed entry skipped — can_trade blocked  signal={pe_sig}")
        else:
            log.info(f"exhaustion_armed cancelled — confirmation candle not bearish  signal={pe.get('signal')}")
    # ─────────────────────────────────────────────────────────────────────────

    if signal != "none":

        live_bal = None
        if not cfg.is_paper():
            live_bal = await orders._live_balance(client)

        pos = state.position
        is_flip = (
            pos is not None
            and ((signal == "long"  and pos.side == "short") or
                 (signal == "short" and pos.side == "long"))
        )

        if is_flip and not cfg.ENABLE_FLIP:
            log.debug(f"FLIP suppressed (ENABLE_FLIP=false) — staying in {pos.side.upper()}")
            return

        if is_flip:
            if not check_flip_allowed(signal, indicators, pos.entry_price, state.mark_price, strategy):
                log.info(f"FLIP suppressed (ADX/move/EMA-DI guard) — staying in {pos.side.upper()}")
                is_flip = False
            else:
                log.info(
                    f"FLIP detected — closing {pos.side.upper()} to open {signal.upper()}"
                    f"{'  [exhaustion reversal]' if strategy.was_exhaustion_reversal() else ''}"
                )
                await orders.close_position("FLIP", state, client)

        is_exhaustion = strategy.was_exhaustion_reversal()

        # CHOP block: skip new entries (not flips) when market regime is CHOP.
        # Flips are exempt. Exhaustion reversals are exempt (fire when ADX falls).
        if cfg.CHOP_BLOCK and not is_flip and not is_exhaustion:
            regime_now = strategy.market_regime(state)
            if regime_now == "CHOP":
                log.info(
                    f"CHOP_BLOCK: {signal.upper()} skipped — regime=CHOP (ADX < 45, no edge)"
                )
                return

        # ── Exhaustion armed: defer entry to next candle ──────────────────────
        if is_exhaustion and not is_flip:
            atr    = indicators.get("atr")
            regime = strategy.market_regime(state)
            _sig_ts = state._candles[-1].open_time / 1000 if state._candles else 0.0
            _pending_exhaustion = {"signal": signal, "atr": atr, "regime": regime, "signal_candle_open_time": _sig_ts}
            log.info(f"exhaustion_armed deferred — will enter on next candle open  signal={signal}  regime={regime}")
            return
        # ─────────────────────────────────────────────────────────────────────

        if is_flip or risk.can_trade(state, live_balance=live_bal, signal_side=signal):
            atr = indicators.get("atr")
            _signal_candle_ts = state._candles[-1].open_time / 1000 if state._candles else 0.0
            await orders.open_position(signal, state, client, atr=atr, strategy=strategy,
                                       signal_candle_open_time=_signal_candle_ts)
        else:
            log.debug(f"SIGNAL={signal.upper()} blocked by can_trade — see risk log above")


# ---------------------------------------------------------------------------
# TICK CALLBACK  - fires on every order-book / mark-price update
# ---------------------------------------------------------------------------

async def on_tick(state: State, client: AsyncClient) -> None:
    global _pending_exhaustion
    had_position = state.position is not None
    await orders.maybe_exit(state, client)
    if had_position and state.position is None:
        if state.last_close_reason == "sl":
            strategy.cancel_exhaustion_arms()
            _pending_exhaustion = None
        if not cfg.is_paper():
            state.live_balance_snapshot = await orders._live_balance(client)






# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main() -> None:
    tlog.setup_logging()
    log.info(f"Starting bot  symbol={cfg.SYMBOL}  mode={cfg.TRADING_MODE}")

    client = await AsyncClient.create(
        cfg.API_KEY,
        cfg.API_SECRET,
        testnet=False,
        requests_params={"timeout": 20},
    )

    try:
        if not cfg.is_paper():
            # Set leverage and margin type on the exchange for live trading
            try:
                await client.futures_change_leverage(
                    symbol=cfg.SYMBOL, leverage=cfg.LEVERAGE
                )
                await client.futures_change_margin_type(
                    symbol=cfg.SYMBOL, marginType=cfg.MARGIN_TYPE
                )
                log.info(
                    f"Leverage set to {cfg.LEVERAGE}x  margin={cfg.MARGIN_TYPE}"
                )
            except Exception as exc:
                # Exchange returns an error if settings are already applied
                log.warning(f"Leverage/margin setup (may already be set): {exc}")

        state = State()

        # Snapshot live balance for accurate daily drawdown % in live mode
        if not cfg.is_paper():
            state.live_balance_snapshot = await orders._live_balance(client)
            log.info(f"Live balance snapshot: {state.live_balance_snapshot:.2f} USDT")

        # Wrap callbacks to bind the client
        async def _on_closed_candle(s: State) -> None:
            await on_closed_candle(s, client)

        async def _on_tick(s: State) -> None:
            await on_tick(s, client)

        await run_streams(client, state, _on_closed_candle, _on_tick)

    finally:
        await client.close_connection()
        log.info("Client connection closed.")


if __name__ == "__main__":
    asyncio.run(main())


