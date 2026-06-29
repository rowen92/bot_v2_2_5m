import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from binance import AsyncClient
from config import cfg
from state import State
from strategy import ScalpingStrategy
from order_manager import OrderManager
from risk_manager import RiskManager
from ws_client import run_streams, fetch_open_interest
import logger as tlog

log      = logging.getLogger("bot")
strategy = ScalpingStrategy()
orders   = OrderManager()
risk     = RiskManager()


# ---------------------------------------------------------------------------
# CANDLE CALLBACK  - fires once per closed 1m candle
# ---------------------------------------------------------------------------

async def on_closed_candle(state: State, client: AsyncClient) -> None:
    # Fetch OI via REST on every closed candle — this is the only reliable
    # source of OI data on Binance Futures (not available in any WS stream)
    await fetch_open_interest(client, state)

    indicators = strategy.indicator_snapshot(state)
    if not indicators:
        min_bars = cfg.BREAK_LOOKBACK + cfg.SWING_LOOKBACK + cfg.ATR_PERIOD + 5
        log.info(f"warming up: {state.candle_count()}/{min_bars} candles")
        return

    # get_signal reuses the cached dataframe — no double computation
    signal = strategy.get_signal(state)

    tlog.log_signal(signal, indicators)   # log every candle (signal=none is useful too)

    if signal != "none":
        live_bal = None
        if not cfg.is_paper():
            live_bal = await orders._live_balance(client)
        if risk.can_trade(state, live_balance=live_bal):
            atr = indicators.get("atr")   # float from strategy snapshot, or None
            await orders.open_position(signal, state, client, atr=atr)
        else:
            log.debug(f"SIGNAL={signal.upper()} blocked by can_trade — see risk log above")


# ---------------------------------------------------------------------------
# TICK CALLBACK  - fires on every order-book / mark-price update
# ---------------------------------------------------------------------------

async def on_tick(state: State, client: AsyncClient) -> None:
    had_position = state.position is not None
    await orders.maybe_exit(state, client)
    # Refresh live balance snapshot after a position closes (live mode only)
    if had_position and state.position is None and not cfg.is_paper():
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


