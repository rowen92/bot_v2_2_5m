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
from strategy import classify_signal
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

        # Signal type classification — mirrors backtest.py exactly.
        # was_di_squeeze() = di_squeeze_fade or di_squeeze_cont (fixed TP at 2×SL dist).
        # was_exhaustion_reversal() = exhaustion_armed (deferred entry, SL=1×ATR, TP=2×ATR).
        sig_type       = classify_signal(strategy) if strategy is not None else "cross"
        is_di_squeeze  = sig_type in ("di_squeeze_fade", "di_squeeze_cont")
        is_grind_short = sig_type == "grind_short"
        is_exhaustion  = sig_type == "exhaustion_armed"

        sl = rm.sl_price(entry, signal, atr=atr, regime=regime, signal_type=sig_type)

        # Grind short = slot blocker only. Override qty to minimum and SL to
        # 0.5×ATR so it always zombies out cheaply. Mirrors backtest.py logic.
        if is_grind_short and atr and atr > 0:
            qty = cfg.QTY_MIN
            sl  = entry + atr * 0.5 if signal == "short" else entry - atr * 0.5
            log.info(f"open_position  [GRIND BLOCKER]  qty={qty}  sl={sl:.4f}")

        if is_di_squeeze:
            # Fixed TP at 2× SL distance — mirrors backtest.py:
            # atr_dist = abs(sl - entry); tp = entry ± atr_dist * 2
            atr_dist = abs(sl - entry)
            tp = entry - atr_dist * 2 if signal == "short" else entry + atr_dist * 2
            log.info(
                f"open_position  regime={regime}  signal={signal}  [{sig_type}]"
                f"  entry={entry:.4f}  sl={sl:.4f}  tp={tp:.4f}  qty={qty}"
            )
        elif is_exhaustion and atr and atr > 0:
            sl  = entry + atr * 1.0 if signal == "short" else entry - atr * 1.0
            tp  = entry - atr * 2.0 if signal == "short" else entry + atr * 2.0
            sl_dist = abs(entry - sl)
            qty = rm.position_size(entry, balance, state=None, atr=sl_dist, regime="CHOP")
            if qty <= 0:
                log.warning(f"open skipped: exhaustion_armed position_size=0  balance={balance:.2f}  entry={entry:.4f}")
                return None
            log.info(
                f"open_position  regime={regime}  signal={signal}  [exhaustion_armed]"
                f"  entry={entry:.4f}  sl={sl:.4f}  tp={tp:.4f}  qty={qty}"
            )
        else:
            tp = 0.0
            log.info(
                f"open_position  regime={regime}  signal={signal}  [{sig_type}]"
                f"  entry={entry:.4f}  sl={sl:.4f}  qty={qty}"
            )

        if cfg.is_paper():
            pos = self._paper_open(signal, entry, qty, tp, sl, state)
        else:
            pos = await self._live_open(signal, entry, qty, tp, sl, client)

        if pos:
            pos.best_price          = entry
            pos.atr                 = atr
            pos.regime              = regime
            pos.signal_type         = sig_type
            pos.fixed_tp          = tp if (is_di_squeeze or is_exhaustion) else 0.0
            pos.is_exhaustion_armed = is_exhaustion
            pos.ema21_trail_stop    = 0.0
            state.position = pos
            tlog.log_open(signal, entry, qty, tp, sl, cfg.TRADING_MODE, regime=regime, signal=pos.signal_type)

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

        # Zombie scratch pins the exit to exact breakeven — use that price,
        # not the current mark_price tick (which may have moved on).
        if reason == "zombie_scratch" and getattr(pos, "zombie_exit_price", 0.0) > 0:
            exit_price = pos.zombie_exit_price
        else:
            exit_price = state.mark_price

        if cfg.is_paper():
            pnl = self._paper_close(pos, exit_price, state)
        else:
            result = await self._live_close(pos, exit_price, client, reason=reason)
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
            # Fetch fresh balance from Binance so the logged value reflects the
            # actual post-trade account balance, not the stale pre-close snapshot.
            fresh_balance = await self._live_balance(client)
            if fresh_balance > 0:
                state.live_balance_snapshot = fresh_balance
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

        # ── Max hold duration: force-close after 10 hours (120 × 5m candles) ──
        # A trade open 10+ hours without hitting SL/TP is a zombie — exit at SL.
        # Mirrors backtest.py MAX_HOLD_CANDLES=120 logic.
        MAX_HOLD_SECONDS = 120 * 300  # 120 candles × 5 min
        open_duration = time.time() - pos.open_time
        if open_duration >= MAX_HOLD_SECONDS:
            hit = "sl"

        # ── Zombie Scratch: breakeven exit after 6 candles (30 min) ──────────
        # di_squeeze signals get 24 candles (2 hours) — mirrors backtest.py _zombie_candles logic.
        _zombie_candles = 24 if getattr(pos, "signal_type", "") in ("di_squeeze_fade", "di_squeeze_cont") else 6
        ZOMBIE_CANDLES_SECONDS = _zombie_candles * 300
        if hit is None and open_duration >= ZOMBIE_CANDLES_SECONDS:
            breakeven_long  = rm.breakeven_price(pos.entry_price, "long")
            breakeven_short = rm.breakeven_price(pos.entry_price, "short")

            if pos.side == "long":
                # Let it run if we are already in profit at current close
                if candle_close > 0 and candle_close > breakeven_long:
                    pass
                elif price >= breakeven_long:
                    # Wicked up to breakeven — scratch it
                    if not (pos.trail_active and pos.trail_stop > breakeven_long):
                        pos.zombie_exit_price = breakeven_long
                        hit = "zombie_scratch"
            elif pos.side == "short":
                # Let it run if we are already in profit at current close
                if candle_close > 0 and candle_close < breakeven_short:
                    pass
                elif price <= breakeven_short:
                    # Wicked down to breakeven — scratch it
                    if not (pos.trail_active and pos.trail_stop < breakeven_short):
                        pos.zombie_exit_price = breakeven_short
                        hit = "zombie_scratch"

        has_fixed_tp = pos.fixed_tp > 0

        if has_fixed_tp:
            # ── Fixed TP: di_squeeze (2×SL dist) and exhaustion_armed (2×ATR) ──
            if pos.side == "long":
                if price >= pos.fixed_tp:
                    hit = "tp"
                elif hit is None and candle_close > 0 and candle_close <= pos.sl_price:
                    hit = "sl"
            else:  # short
                if price <= pos.fixed_tp:
                    hit = "tp"
                elif hit is None and candle_close > 0 and candle_close >= pos.sl_price:
                    hit = "sl"

        else:
            # ── ATR 1R:2R trailing (crossover + continuation entries) ─────────
            if rm.update_trail(pos, price, live_atr=state.live_atr):
                hit = "trail_tp"

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

        # Uses futures_create_algo_order (python-binance >= 1.0.36) which calls
        # /fapi/v1/algoOrder directly. On failure: log and continue, trail handles exit.
        if tp > 0:
            try:
                await client.futures_create_algo_order(
                    symbol=symbol,
                    side=close_side,
                    type="TAKE_PROFIT_MARKET",
                    algoType="CONDITIONAL",
                    triggerPrice=round(tp, cfg.PRICE_PRECISION),
                    quantity=qty,
                    reduceOnly=True,
                )
            except Exception as exc:
                log.error(f"live_open TP placement failed — trail will handle exit: {exc}")

        try:
            # SL bracket — crash-protection ONLY (in case bot process dies).
            # Placed at 2× the Python SL distance — far enough to not interfere
            # with normal wicks or the candle-close Python SL, but inside
            # liquidation distance (~20% at 5× leverage).
            # Hard cap at 8% from entry: continuation signals have sl_dist=3.5×ATR
            # which at 3× would push past liquidation — cap prevents that.
            sl_dist = abs(sl - actual_entry)
            raw_crash_dist = sl_dist * 2
            max_crash_dist = actual_entry * 0.08   # 8% hard cap — inside liq at 5× leverage
            crash_dist = min(raw_crash_dist, max_crash_dist)
            crash_sl = (
                actual_entry - crash_dist if signal == "long"
                else actual_entry + crash_dist
            )
            await client.futures_create_algo_order(
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                algoType="CONDITIONAL",
                triggerPrice=round(crash_sl, cfg.PRICE_PRECISION),
                quantity=qty,
                reduceOnly=True,
            )
        except Exception as exc:
            log.error(f"live_open SL placement failed — emergency close: {exc}")
            await client.futures_cancel_all_open_orders(symbol=symbol)
            try:
                algo_orders = await client.futures_get_open_algo_orders(symbol=symbol)
                for o in algo_orders:
                    await client.futures_cancel_algo_order(symbol=symbol, algoId=o["algoId"])
            except Exception:
                pass
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
    async def _live_close(pos: Position, exit_price: float, client: AsyncClient, reason: str = "") -> float | None:
        """
        Returns the realised PnL (float) on success, or None on failure.
        Returning None (not 0.0) lets the caller distinguish a failed close
        from a legitimate breakeven trade.
        """
        close_side = "SELL" if pos.side == "long" else "BUY"
        try:
            # ── Race-condition guard (FIRST) ──────────────────────────────────
            # Check actual position size on Binance BEFORE cancelling orders or
            # placing a close. The TP/SL bracket algo may have already been filled
            # by Binance (e.g. while WS was down). In that case the position is
            # already flat and a reduceOnly market order would be rejected (-2022).
            # Must come first: if futures_cancel_all_open_orders throws on a flaky
            # network, we would jump to the outer except and skip this check entirely.
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
                    "live_close: position already closed on Binance (TP/SL bracket filled) — "
                    "skipping duplicate market order"
                )
                # Return best-effort PnL; the sync block in bot.py will fetch the
                # real fill price on the next candle and log it accurately.
                return rm.calc_pnl(pos.entry_price, exit_price, pos.qty, pos.side)
            # ─────────────────────────────────────────────────────────────────

            # Cancel standard orders first
            await client.futures_cancel_all_open_orders(symbol=cfg.SYMBOL)
            # Also cancel algo orders (SL/TP placed via /fapi/v1/algoOrder).
            # Standard cancel-all does NOT cancel algo orders on Binance.
            try:
                algo_orders = await client.futures_get_open_algo_orders(symbol=cfg.SYMBOL)
                for o in algo_orders:
                    await client.futures_cancel_algo_order(
                        symbol=cfg.SYMBOL, algoId=o["algoId"]
                    )
            except Exception as exc:
                log.warning(f"live_close: algo order cancel failed (may already be filled): {exc}")

            # Market close — reduceOnly ensures we never flip into a reverse position
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