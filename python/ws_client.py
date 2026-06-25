from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets
from binance import AsyncClient
from config import cfg
from state import Candle, State

log = logging.getLogger("ws")

# Binance Futures routed endpoints (new routing, replaces legacy /ws/)
# Public streams: kline, depth, aggTrade, etc.
# Market streams: markPrice
# OI is fetched via REST (fetch_open_interest) — not available in any WS stream
_WS_PUBLIC       = "wss://fstream.binance.com/public/ws/"
_WS_MARKET       = "wss://fstream.binance.com/market/ws/"
_RECONNECT_DELAY = 5  # seconds before retrying after a disconnect


def _parse_kline(data: dict, state: State) -> None:
    k = data["k"]
    candle = Candle(
        open_time=int(k["t"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        is_closed=bool(k["x"]),
    )
    state.live_candle = candle
    if candle.is_closed:
        state.add_closed_candle(candle)
        log.debug(
            f"candle closed  close={candle.close}  vol={candle.volume:.1f}"
            f"  total={state.candle_count()}"
        )


def _parse_depth(data: dict, state: State) -> None:
    state.bids = {float(p): float(q) for p, q in data.get("b", []) if float(q) > 0}
    state.asks = {float(p): float(q) for p, q in data.get("a", []) if float(q) > 0}


def _parse_mark_price(data: dict, state: State) -> None:
    """
    markPrice stream payload carries: p (mark price), i (index price),
    r (funding rate), T (next funding time).
    OI is NOT in this stream — it is fetched via REST in fetch_open_interest().
    """
    mark = data.get("p") or data.get("markPrice")
    if mark:
        state.mark_price = float(mark)


async def fetch_open_interest(client: AsyncClient, state: State) -> None:
    """
    Poll REST GET /fapi/v1/openInterest once per closed candle.
    Always appends to state.oi_history — flat readings are still valid data
    for the rising-OI check and must not be deduplicated.
    This is the correct way to get OI on Binance Futures — it is NOT
    available in any WebSocket stream.
    """
    try:
        resp = await client.futures_open_interest(symbol=cfg.SYMBOL)
        oi_val = float(resp.get("openInterest", 0))
        if oi_val > 0:
            # Always append — even flat OI is a real reading needed by _oi_is_rising.
            # Deduplication was preventing the rising-OI check from getting fresh data.
            state.oi_history.append(oi_val)
            log.debug(f"OI fetched  oi={oi_val:.2f}  history_len={len(state.oi_history)}")
    except Exception as exc:
        log.warning(f"OI fetch failed: {exc}")


async def _run_stream(
    base_url: str,
    stream_name: str,
    handler: Callable[[dict], Awaitable[None]],
) -> None:
    """Connect to a single Binance stream with automatic reconnection."""
    url = base_url + stream_name
    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                log.info(f"Connected: {stream_name}")
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        log.warning(f"recv timeout ({stream_name}) — reconnecting …")
                        break
                    except websockets.ConnectionClosed as exc:
                        log.warning(f"connection closed ({stream_name}): {exc} — reconnecting …")
                        break
                    await handler(json.loads(raw))
        except Exception as exc:
            log.error(f"ws error ({stream_name}): {exc}")
        log.warning(f"Disconnected ({stream_name}) — reconnecting in {_RECONNECT_DELAY}s …")
        await asyncio.sleep(_RECONNECT_DELAY)


async def run_streams(
    client: AsyncClient,
    state: State,
    on_closed_candle: Callable[[State], Awaitable[None]],
    on_tick: Callable[[State], Awaitable[None]],
) -> None:
    sym      = cfg.SYMBOL.lower()
    interval = cfg.KLINE_INTERVAL

    kline_stream = f"{sym}@kline_{interval}"
    depth_stream = f"{sym}@depth20@100ms"
    mark_stream  = f"{sym}@markPrice@1s"

    log.info(
        f"Subscribing — kline: {kline_stream}  depth: {depth_stream}"
        f"  markPrice: {mark_stream}  (OI via REST per candle)"
    )

    async def handle_kline(data: dict) -> None:
        # Capture whether the *previous* live candle was already closed before
        # we parse this message. The callback fires only on the transition
        # open→closed, not on every tick of an already-closed candle.
        prev_closed = state.live_candle is not None and state.live_candle.is_closed
        _parse_kline(data, state)
        just_closed = state.live_candle is not None and state.live_candle.is_closed
        if just_closed and not prev_closed:
            await on_closed_candle(state)

    async def handle_depth(data: dict) -> None:
        _parse_depth(data, state)
        # depth updates order book only — TP/SL checks run on mark price ticks

    async def handle_mark(data: dict) -> None:
        _parse_mark_price(data, state)
        await on_tick(state)  # only mark price drives exit checks

    await asyncio.gather(
        _run_stream(_WS_MARKET, kline_stream, handle_kline),   # kline = market stream
        _run_stream(_WS_PUBLIC, depth_stream, handle_depth),   # depth = public stream
        _run_stream(_WS_MARKET, mark_stream,  handle_mark),    # markPrice = market stream
    )
