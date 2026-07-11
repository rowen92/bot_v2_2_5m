from __future__ import annotations
import asyncio
import logging
import time
import uuid
from typing import Optional
from binance import AsyncClient
from config import cfg
from risk_manager import RiskManager
from state import Position, State
import logger as tlog

log = logging.getLogger("orders")
rm  = RiskManager()


class OrderManager:

    # ------------------------------------------------------------------
    # OPEN POSITION
    # ------------------------------------------------------------------

    async def open_position(
        self,
        signal: str,
        state: State,
        client: Optional[AsyncClient] = None,
        atr: float | None = None,
        strategy=None,
    ) -> Optional[Position]:
        """Open a long or short futures position.

        `atr` — current ATR value from the strategy.  When supplied, SL/TP
        distances and position size are all ATR-based (dynamic).  Falls back
        to fixed-% config values when None.
        """
        entry = state.mark_price
        if entry <= 0:
            log.warning("open skipped: mark_price not ready")
            return None

        if cfg.is_paper():
            balance = state.paper_balance
        else:
            balance = await self._live_balance(client)
            # Keep snapshot current so daily_loss_pct() always has a fresh base.
            if balance > 0:
                state.live_balance_snapshot = balance
        # Classify market regime — drives SL/TP/trail multipliers for this position.
        # Uses the caller's strategy singleton (which has a warm cached DataFrame).
        # Falls back to "TREND" if strategy not passed or df not yet warmed up.
        regime = strategy.market_regime(state) if strategy is not None else "TREND"

        qty = rm.position_size(entry, balance, state=state, atr=atr, regime=regime)
        if qty <= 0:
            log.warning(f"open skipped: position_size=0  balance={balance:.2f}  entry={entry:.4f}")
            return None

        # DI-snap entries use fixed SL (trigger candle high/low) and fixed TP (EMA21).
        # All other entries use ATR-based SL + trail (no fixed TP).
        is_di_snap = strategy is not None and strategy.was_di_snap()
        if is_di_snap:
            snap = strategy.di_snap_levels()
            sl   = snap["sl"]
            tp   = snap["tp"]
            log.info(
                f"open_position  regime={regime}  signal={signal}  [DI-snap]"
                f"  entry={entry:.4f}  sl={sl:.4f}  tp={tp:.4f}  qty={qty}"
            )
        else:
            is_exhaustion_armed_check = strategy is not None and strategy.was_exhaustion_reversal() and not is_di_snap
            if is_exhaustion_armed_check and atr:
                # Cap SL at 1.5×ATR for exhaustion reversals — regime SL (2.5–3×ATR in
                # TREND/STRONG_TREND) gave 0.6:1 R:R vs 1.5×ATR TP, bleeding even good setups.
                dist = atr * 1.5
                sl = entry + dist if signal == "short" else entry - dist
            else:
                sl = rm.sl_price(entry, signal, atr=atr, regime=regime)
            tp = sl  # no fixed TP — trail at +2R is the only exit; tp field kept for dataclass compat
            log.info(
                f"open_position  regime={regime}  signal={signal}  "
                f"entry={entry:.4f}  sl={sl:.4f}  qty={qty}"
            )

        if cfg.is_paper():
            pos = self._paper_open(signal, entry, qty, tp, sl, state)
        else:
            pos = await self._live_open(signal, entry, qty, tp, sl, client)

        is_exhaustion_armed = strategy is not None and strategy.was_exhaustion_reversal() and not is_di_snap

        if pos:
            pos.best_price          = entry    # initialise trailing high-water-mark
            pos.atr                 = atr      # stored so update_trail() can use ATR-based distances
            pos.regime              = regime   # frozen at entry — governs trail for the life of this position
            pos.is_di_snap          = is_di_snap
            pos.di_snap_tp          = tp if is_di_snap else 0.0
            pos.is_exhaustion_armed = is_exhaustion_armed
            pos.ema21_trail_stop    = 0.0

            # Flat single TP for exhaustion-armed entries — frozen at open.
            # TP = entry ± 1.5×ATR  → full close, booked as a win.
            # DI filter (minus_di >= plus_di for SHORT, plus_di >= minus_di for LONG)
            # now blocks the bad entries that made 1.5×ATR unprofitable before.
            # 2.5×ATR TP was too far: TREND SL=2.5×ATR → 1:1 R:R, STRONG → 0.6:1 R:R.
            if is_exhaustion_armed and atr:
                if signal == "long":
                    pos.tp1_price = entry + 1.5 * atr
                else:
                    pos.tp1_price = entry - 1.5 * atr
                log.info(
                    f"exhaustion-armed TP  tp={pos.tp1_price:.4f} (+1.5×ATR)  sl={sl:.4f}"
                )
            state.position = pos
            state.first_trade_done = True  # unlock continuation entries on next candle
            tlog.log_open(signal, entry, qty, tp, sl, cfg.TRADING_MODE, regime=regime)

        return pos

    # ------------------------------------------------------------------
    # CLOSE POSITION
    # ------------------------------------------------------------------

    async def close_position(
        self,
        reason: str,
        state: State,
        client: Optional[AsyncClient] = None,
    ) -> float:
        """Close the current open position. Returns realised PnL in USDT."""
        pos = state.position
        if pos is None:
            return 0.0

        exit_price = state.mark_price

        if cfg.is_paper():
            pnl = self._paper_close(pos, exit_price, state)
        else:
            result = await self._live_close(pos, exit_price, client)
            # _live_close returns None only on exception — position may still be
            # open on Binance. Keep state.position intact so the next tick retries.
            if result is None:
                log.error(
                    f"live_close failed — keeping position open in state to retry.  "
                    f"side={pos.side}  entry={pos.entry_price:.4f}  qty={pos.qty}"
                )
                return 0.0
            pnl = result

        if cfg.is_paper():
            # _paper_close already restored margin + pnl into paper_balance,
            # so state.paper_balance now reflects the settled post-trade balance.
            display_balance = state.paper_balance
        else:
            display_balance = state.live_balance_snapshot

        tlog.log_close(
            pos.side, pos.entry_price, exit_price,
            pos.qty, pnl, reason, cfg.TRADING_MODE,
            display_balance,
            pos.open_time,
        )
        state.last_close_reason = reason   # used by dynamic cooldown in risk_manager
        state.record_pnl(pnl)
        state.last_close_ts = time.time()   # start cooldown (wall-clock, survives restarts)

        # Record SL zone for anti-revenge block in risk_manager.can_trade()
        if reason == "sl":
            state.last_sl_entry_price = pos.entry_price
            state.last_sl_atr = pos.atr or 0.0
            state.last_sl_side = pos.side  # used by anti-revenge zone direction check

        state.position = None
        tlog.log_daily_stats(state)
        return pnl

    # ------------------------------------------------------------------
    # TICK CHECK  (call on every mark-price update)
    # ------------------------------------------------------------------

    async def maybe_exit(
        self,
        state: State,
        client: Optional[AsyncClient] = None,
    ) -> None:
        """Check if TP or SL has been touched; close if so.
        Called on every price tick.

        TP  — fires immediately on any tick (grab profit as soon as it's there).
        SL  — fires only when the last 5m candle CLOSED beyond the SL level.
               A wick that spikes through SL and recovers within the same candle
               is a liquidity sweep, not a real break. Requiring a candle close
               means the market has to sustain the move for a full 5 minutes
               before we accept the loss — wicks cannot trigger it.
               last_candle_close is updated by ws_client on every candle close.
        """
        pos = state.position
        if pos is None:
            return

        # Guard: if a close is already in-flight (previous tick still awaiting
        # Binance response), skip this tick entirely to avoid a double-close.
        if state.is_closing:
            return

        price = state.mark_price
        if price <= 0:
            return

        # Candle-close price used for ALL SL decisions (0.0 = no candle closed yet)
        candle_close = state.last_candle_close

        hit = None

        if pos.is_di_snap:
            # ── DI-snap exits: fixed TP at EMA21, candle-close SL ────────────
            if pos.side == "long":
                if price >= pos.di_snap_tp:
                    hit = "tp"
                elif candle_close > 0 and candle_close <= pos.sl_price:
                    hit = "sl"
            else:  # short
                if price <= pos.di_snap_tp:
                    hit = "tp"
                elif candle_close > 0 and candle_close >= pos.sl_price:
                    hit = "sl"

        elif pos.is_exhaustion_armed:
            # ── Exhaustion-armed (1b): flat TP exit, candle-close SL ─────────
            # Single flat TP at +1.5×ATR — full close, booked immediately.
            # FLIP (opposite signal) takes priority and is handled before this block.
            # tp1_price is set at open; 0.0 means no TP (degraded warmup state → SL only).
            if pos.side == "long":
                if pos.tp1_price > 0 and price >= pos.tp1_price:
                    hit = "tp"
                elif candle_close > 0 and candle_close <= pos.sl_price:
                    hit = "sl"
            else:  # short
                if pos.tp1_price > 0 and price <= pos.tp1_price:
                    hit = "tp"
                elif candle_close > 0 and candle_close >= pos.sl_price:
                    hit = "sl"

        else:
            # ── ATR 1R:2R trailing (crossover + continuation entries) ─────────
            if rm.update_trail(pos, price, live_atr=state.live_atr):
                hit = "trail_tp"

            # ── SL: only on candle close, not on a wick ───────────────────────
            if hit is None:
                if pos.side == "long":
                    if candle_close > 0 and candle_close <= pos.sl_price:
                        hit = "sl"
                else:  # short
                    if candle_close > 0 and candle_close >= pos.sl_price:
                        hit = "sl"

        if hit:
            state.is_closing = True
            try:
                await self.close_position(hit, state, client)
            finally:
                state.is_closing = False

    # ------------------------------------------------------------------
    # PAPER HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _paper_open(
        signal: str, entry: float, qty: float,
        tp: float, sl: float, state: State,
    ) -> Position:
        notional = entry * qty / cfg.LEVERAGE          # margin locked
        open_fee = entry * qty * (cfg.TAKER_FEE_PCT / 100)
        state.paper_balance -= (notional + open_fee)
        return Position(
            side=signal,
            entry_price=entry,
            qty=qty,
            tp_price=tp,
            sl_price=sl,
            order_id=str(uuid.uuid4())[:8],
            open_fee=open_fee,
        )

    @staticmethod
    def _paper_close(pos: Position, exit_price: float, state: State) -> float:
        if pos.side == "long":
            raw_pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            raw_pnl = (pos.entry_price - exit_price) * pos.qty
        close_fee = exit_price * pos.qty * (cfg.TAKER_FEE_PCT / 100)

        # True pnl includes both open and close fees — open_fee was pre-deducted
        # from paper_balance at entry, so add it back here to keep balance correct.
        pnl = raw_pnl - pos.open_fee - close_fee

        margin = pos.entry_price * pos.qty / cfg.LEVERAGE
        state.paper_balance += margin + pos.open_fee + pnl
        return pnl

    # ------------------------------------------------------------------
    # LIVE HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    async def _emergency_close(
        client: AsyncClient, symbol: str, close_side: str, qty: float,
        retries: int = 3,
    ) -> None:
        """
        Best-effort market close to flatten a naked position.
        Retries up to `retries` times before giving up and alerting.
        """
        for attempt in range(1, retries + 1):
            try:
                await client.futures_create_order(
                    symbol=symbol, side=close_side, type="MARKET",
                    quantity=qty, reduceOnly=True,
                )
                log.info(f"Emergency close succeeded on attempt {attempt}")
                return
            except Exception as exc:
                log.error(f"Emergency close attempt {attempt}/{retries} failed: {exc}")
                if attempt < retries:
                    await asyncio.sleep(1)
        log.critical(
            f"EMERGENCY CLOSE FAILED after {retries} attempts — "
            f"MANUAL INTERVENTION REQUIRED: {symbol} {close_side} qty={qty}"
        )

    @staticmethod
    async def _live_open(
        signal: str, entry: float, qty: float,
        tp: float, sl: float, client: AsyncClient,
    ) -> Optional[Position]:
        binance_side = "BUY" if signal == "long" else "SELL"
        close_side   = "SELL" if signal == "long" else "BUY"
        symbol = cfg.SYMBOL

        order_id     = ""
        actual_entry = entry

        try:
            # 1. Market entry
            resp = await client.futures_create_order(
                symbol=symbol,
                side=binance_side,
                type="MARKET",
                quantity=qty,
            )
            order_id     = str(resp.get("orderId", ""))
            actual_entry = float(resp.get("avgPrice", entry) or entry)
            if actual_entry == 0:
                actual_entry = entry

        except Exception as exc:
            log.error(f"live_open entry failed: {exc}")
            return None   # nothing was placed — safe to return

        # Entry is now LIVE. Guard every subsequent call so we never leave a naked position.
        try:
            # 2. Take-profit order
            await client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=round(tp, cfg.PRICE_PRECISION),
                closePosition=True,
                timeInForce="GTE_GTC",
            )
        except Exception as exc:
            log.error(f"live_open TP placement failed — emergency close: {exc}")
            await OrderManager._emergency_close(client, symbol, close_side, qty)
            return None

        try:
            # 3. Stop-loss order
            await client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=round(sl, cfg.PRICE_PRECISION),
                closePosition=True,
                timeInForce="GTE_GTC",
            )
        except Exception as exc:
            log.error(f"live_open SL placement failed — emergency close: {exc}")
            await client.futures_cancel_all_open_orders(symbol=symbol)
            await OrderManager._emergency_close(client, symbol, close_side, qty)
            return None

        return Position(
            side=signal,
            entry_price=actual_entry,
            qty=qty,
            tp_price=tp,
            sl_price=sl,
            order_id=order_id,
        )

    @staticmethod
    async def _live_close(pos: Position, exit_price: float, client: AsyncClient) -> float | None:
        """
        Returns the realised PnL (float) on success, or None on failure.
        Returning None (not 0.0) lets the caller distinguish a failed close
        from a legitimate breakeven trade.
        """
        close_side = "SELL" if pos.side == "long" else "BUY"
        try:
            # Cancel any open TP/SL orders first
            await client.futures_cancel_all_open_orders(symbol=cfg.SYMBOL)

            # ── Race-condition guard ──────────────────────────────────────────
            # Binance may have already filled our static TP/SL order at the same
            # moment Python's trail fired. Check the real position size before
            # sending a market close to avoid opening an unintended reverse position.
            actual_qty = pos.qty
            try:
                positions = await client.futures_position_information(symbol=cfg.SYMBOL)
                for p in positions:
                    if p.get("symbol") == cfg.SYMBOL:
                        actual_qty = abs(float(p.get("positionAmt", pos.qty)))
                        break
            except Exception as exc:
                log.warning(f"live_close: could not verify position size on exchange: {exc}")

            if actual_qty == 0:
                log.warning(
                    "live_close: position already closed on Binance (TP/SL filled first) — "
                    "skipping duplicate market order"
                )
                # Best-effort PnL estimate using the mark price we have
                return rm.calc_pnl(pos.entry_price, exit_price, pos.qty, pos.side)
            # ─────────────────────────────────────────────────────────────────

            # Market close
            resp = await client.futures_create_order(
                symbol=cfg.SYMBOL,
                side=close_side,
                type="MARKET",
                quantity=actual_qty,
                reduceOnly=True,
            )
            actual_exit = float(resp.get("avgPrice", exit_price) or exit_price)
            return rm.calc_pnl(pos.entry_price, actual_exit, pos.qty, pos.side)

        except Exception as exc:
            log.error(f"live_close failed: {exc}")
            return None  # sentinel: caller must NOT clear state.position

    # ------------------------------------------------------------------
    # ACCOUNT BALANCE (live only)
    # ------------------------------------------------------------------

    @staticmethod
    async def _live_balance(client: AsyncClient) -> float:
        # Derive quote asset from symbol (e.g. WLDUSDT→USDT, WLDUSDC→USDC)
        quote = cfg.SYMBOL[-4:] if cfg.SYMBOL.endswith(("USDT", "USDC", "BUSD")) else "USDT"
        try:
            balances = await client.futures_account_balance()
            for b in balances:
                if b["asset"] == quote:
                    return float(b["availableBalance"])
        except Exception as exc:
            log.error(f"balance fetch failed: {exc}")
        return 0.0