"""HMSF Strategy — 4-Module Decision Engine als StrategyBase.

Nutzt die DecisionEngine mit 4 parallelen Modulen:
- Modul 0: Legacy Momentum (v2.0 Logik, isoliert)
- Modul 1: Enhanced Latency-Momentum (Price-Gap + Imbalance + Regime)
- Modul 2: Last-Seconds-Sniping (15-60s vor Expiry, Ask >= 0.72)
- Modul 3: Both-Sides-Arbitrage (UP+DOWN < effektiv $1.00 nach Fees)

Registriert sich als "hmsf_decision_engine" im Strategy Framework.
User wählt per /strategy Page welche Strategie(n) aktiv sind.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from collections import deque
from dataclasses import dataclass

from loguru import logger

from config import Settings
from core.binance_ws import BinanceWebSocketOracle
from core.executor import PolymarketExecutor
from core.market_discovery import MarketDiscovery, MarketWindow
from core.paper_trader import PaperTrader
from core.pretrade_calculator import PreTradeCalculator, TradeDecision
from core.risk_manager import RiskManager
from core.trade_journal import TradeJournal, TradeRecord
from strategies.base import StrategyBase
from strategies.registry import register as register_strategy
from strategies.hmsf_engine import DecisionEngine
from strategies.hmsf_modules import WindowData
from utils import telegram


class HMSFStrategy(StrategyBase):
    """4-Module HMSF Decision Engine Strategy.

    Kann parallel zur bestehenden momentum_latency_v2 laufen.
    Eigene Instanzen aller Komponenten (kein State-Sharing).
    """

    STRATEGY_NAME = "hmsf_decision_engine"
    DESCRIPTION = (
        "4-Module HMSF: Legacy Momentum, Enhanced Latency, "
        "Last-Seconds-Sniping, Both-Sides-Arb. "
        "Quarter-Kelly, Regime-Filter, korrigierte Fees."
    )

    @property
    def name(self) -> str:
        return self.STRATEGY_NAME

    @property
    def description(self) -> str:
        return self.DESCRIPTION

    def __init__(self, settings: Settings, **kwargs) -> None:
        self.settings = settings

        # Eigene Instanzen (kein Sharing mit v2.0)
        self.oracle = BinanceWebSocketOracle(
            symbols=settings.oracle_symbols,
            window_size_s=max(settings.momentum_window_s * 2, 120),
        )
        self.discovery = MarketDiscovery(
            assets=[s.replace("/USDT", "").lower() for s in settings.oracle_symbols],
            timeframes=["5m", "15m"],
            min_liquidity_usd=15000.0,
        )
        self.calculator = PreTradeCalculator(settings)
        self.paper_trader = PaperTrader(settings)
        self.risk_manager = RiskManager(settings)
        self.executor = PolymarketExecutor(settings)
        self.journal = TradeJournal()

        from core.redeemer import AutoRedeemer
        self.redeemer = AutoRedeemer(
            private_key=settings.polymarket_private_key,
            wallet_address=settings.polymarket_funder,
        )

        # HMSF Decision Engine
        self.engine = DecisionEngine(settings)

        # State
        self._running = False
        self._signals_detected: int = 0
        self._recent_signals: deque = deque(maxlen=30)
        self._scan_cycles: int = 0
        self._heartbeat: dict = {}
        self._tick_event = asyncio.Event()
        self._last_tick_symbol: str = ""
        self._traded_windows: set = set()

        # Redeemer Thread-Pool
        self._redeem_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hmsf-redeemer"
        )
        self._redeeming = False

    # ═══════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═══════════════════════════════════════════════════════════════

    async def run(self) -> None:
        self._running = True
        telegram.configure(self.settings)
        logger.info("HMSF Strategy startet...")

        # Binance WebSocket + Event-driven Tick
        def on_tick(symbol, tick):
            self._last_tick_symbol = symbol
            self._tick_event.set()
        self.oracle.set_on_tick(on_tick)
        await self.oracle.start()

        # Market Discovery
        await self.discovery.start()

        # Executor (Live Trading)
        if self.settings.live_trading:
            live_ok = await self.executor.initialize()
            if live_ok:
                logger.info("HMSF: LIVE TRADING MODUS — Echte Orders auf Polymarket!")
            else:
                logger.warning("HMSF: Live Trading Init fehlgeschlagen → Paper Mode")
        else:
            logger.info("HMSF: Paper Trading Modus")

        await telegram.alert_startup(
            self.settings.paper_capital_usd, self.settings.mode,
            live=self.settings.live_trading,
            wallet=self.settings.polymarket_funder,
        )

        await asyncio.sleep(3)

        try:
            results = await asyncio.gather(
                self._signal_loop(),
                self._position_resolver_loop(),
                self._status_loop(),
                self._auto_redeem_loop(),
                return_exceptions=True,
            )
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    names = ["signal", "resolver", "status", "redeem"]
                    logger.error(f"HMSF Loop '{names[i]}' crashed: {result}")
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._running = False
        try:
            self._redeem_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            await self.oracle.stop()
        except Exception:
            pass
        try:
            await self.discovery.stop()
        except Exception:
            pass
        logger.info("HMSF Strategy beendet.")

    # ═══════════════════════════════════════════════════════════════
    # SIGNAL LOOP (Event-Driven)
    # ═══════════════════════════════════════════════════════════════

    async def _signal_loop(self) -> None:
        logger.info("HMSF Signal-Loop gestartet (EVENT-DRIVEN)")
        while self._running:
            try:
                await asyncio.wait_for(self._tick_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._tick_event.clear()
            try:
                await self._check_all_signals()
            except Exception as e:
                logger.error(f"HMSF Signal-Loop Fehler: {e}")

    async def _check_all_signals(self) -> None:
        self._scan_cycles += 1

        if not self.paper_trader.can_trade():
            return

        # Mark-out Recording
        for symbol in self.settings.oracle_symbols:
            tick = self.oracle.get_latest(symbol)
            if tick:
                asset = symbol.replace("/USDT", "")
                self.paper_trader.record_markout(tick.mid, asset_filter=asset)

        # Heartbeat + Signal Check pro Asset
        hb = {}
        for asset in ["BTC", "ETH"]:
            symbol = f"{asset}/USDT"

            if not self.oracle.is_fresh(symbol, max_age_s=5.0):
                continue

            tick = self.oracle.get_latest(symbol)
            momentum = self.oracle.get_momentum(symbol, self.settings.momentum_window_s)

            if tick is None or momentum is None:
                continue

            hurdle = self.settings.min_momentum_pct
            hb[asset] = {
                "price": round(tick.mid, 2),
                "momentum": round(momentum, 4),
                "abs_momentum": round(abs(momentum), 4),
                "hurdle": hurdle,
                "pct_of_hurdle": round(abs(momentum) / hurdle * 100, 1) if hurdle > 0 else 0,
                "status": "SIGNAL" if abs(momentum) >= hurdle else "WATCHING",
            }

            # Freshness Guard
            transit = self.oracle.status().get(symbol, {}).get("transit_p50_ms", 0)
            if transit > 300:
                continue

            # Richtung (auch bei schwachem Momentum — Modul 3 Arb braucht kein Momentum)
            direction = "UP" if momentum > 0 else "DOWN"

            # Alle Windows evaluieren
            windows = self.discovery.get_windows_for_asset(asset)
            for window in windows:
                # Modul 3 (Arb) braucht kein Momentum — immer prüfen
                # Module 0/1/2 brauchen Momentum >= threshold
                has_momentum = abs(momentum) >= self.settings.min_momentum_pct
                await self._evaluate_with_engine(
                    asset, direction, momentum, tick.mid, window, transit, has_momentum
                )

        self._heartbeat = hb

        # Pre-Signing
        if self.executor.is_live and self._scan_cycles % 20 == 0:
            for asset_lower in ["btc", "eth"]:
                windows = self.discovery.get_windows_for_asset(asset_lower.upper())
                for w in windows[:2]:
                    for tid in [w.up_token_id, w.down_token_id]:
                        if tid:
                            self.executor.pre_sign_order(
                                tid, 0.50, self.settings.max_live_position_usd
                            )

    # ═══════════════════════════════════════════════════════════════
    # CORE: HMSF Engine Integration
    # ═══════════════════════════════════════════════════════════════

    async def _evaluate_with_engine(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        binance_price: float,
        window: MarketWindow,
        transit_latency_ms: float,
        has_momentum: bool,
    ) -> None:
        """Evaluiert ein Window durch die HMSF Decision Engine."""

        # MarketWindow → WindowData
        wd = WindowData(
            slug=window.slug,
            asset=window.asset,
            timeframe=window.timeframe,
            condition_id=window.condition_id,
            question=window.question,
            window_end_ts=window.window_end_ts,
            seconds_to_expiry=window.seconds_remaining,
            up_best_bid=window.up_best_bid,
            up_best_ask=window.up_best_ask,
            down_best_bid=window.down_best_bid,
            down_best_ask=window.down_best_ask,
            up_bid_size=window.up_bid_size,
            up_ask_size=window.up_ask_size,
            up_token_id=window.up_token_id,
            down_token_id=window.down_token_id,
            liquidity_usd=window.liquidity_usd,
            orderbook_fresh=window.orderbook_fresh,
            orderbook_ts=window.orderbook_ts,
        )

        # Kapital berechnen
        if self.executor.is_live:
            capital = min(
                self.paper_trader.capital_usd,
                self.settings.max_live_position_usd / self.settings.max_position_pct,
            )
        else:
            capital = self.paper_trader.capital_usd

        # Decision Engine aufrufen
        decision = self.engine.evaluate_tick(
            asset=asset,
            direction=direction,
            momentum_pct=momentum_pct if has_momentum else 0.0,
            binance_price=binance_price,
            window=wd,
            calculator=self.calculator if has_momentum else None,
            capital_usd=capital,
            transit_latency_ms=transit_latency_ms,
        )

        # ── TRADE Action ──
        if decision.action == "TRADE" and has_momentum:
            signal = decision.signals[0]
            await self._execute_trade(signal, window, binance_price, momentum_pct, transit_latency_ms)

        # ── ARB Action ──
        elif decision.action == "ARB":
            arb = decision.arb_signal
            if arb:
                await self._execute_arb(arb, window)

    async def _execute_trade(
        self, signal, window: MarketWindow,
        binance_price: float, momentum_pct: float, transit_ms: float,
    ) -> None:
        """Führt einen TRADE aus (Paper + Live)."""

        # Calculator nochmal für PreTradeResult (PaperTrader braucht es)
        capital = self.paper_trader.capital_usd
        if self.executor.is_live:
            capital = min(capital, self.settings.max_live_position_usd / self.settings.max_position_pct)

        result = self.calculator.evaluate(
            asset=signal.asset,
            direction=signal.direction,
            momentum_pct=signal.momentum_pct,
            polymarket_ask=signal.ask_price,
            seconds_to_expiry=signal.seconds_to_expiry,
            available_capital_usd=capital,
        )

        if result.decision != TradeDecision.EXECUTE:
            return

        self._signals_detected += 1
        logger.info(
            f"HMSF SIGNAL #{self._signals_detected}: {signal.asset} {signal.direction} | "
            f"Modules: {signal.confidence_tag} | "
            f"EV: {signal.net_ev_pct:+.2f}% | Size: ${result.position_usd:.2f}"
        )

        # Paper Position
        pos = self.paper_trader.open_position(
            result=result,
            condition_id=window.condition_id,
            question=window.question,
            market_end_timestamp=float(window.window_end_ts),
            momentum_pct=momentum_pct,
            seconds_to_expiry=signal.seconds_to_expiry,
            oracle_price=binance_price,
        )

        if not pos:
            return

        # Journal
        bid = window.up_best_bid if signal.direction == "UP" else window.down_best_bid
        spread = (signal.ask_price - bid) / ((signal.ask_price + bid) / 2) * 100 if bid > 0 else 0
        self.journal.record_open(TradeRecord(
            trade_id=pos.trade_id, asset=signal.asset, direction=signal.direction,
            signal_ts=time.time(), entry_ts=pos.entered_at,
            window_slug=window.slug, market_question=window.question[:60],
            timeframe=window.timeframe,
            oracle_price_entry=binance_price,
            polymarket_bid=bid, polymarket_ask=signal.ask_price,
            executed_price=signal.ask_price,
            momentum_pct=momentum_pct, p_true=result.p_true,
            p_market=result.p_market, raw_edge_pct=result.raw_edge_pct,
            fee_pct=result.fee_pct, net_ev_pct=result.net_ev_pct,
            size_usd=result.position_usd, shares=pos.shares,
            fee_usd=pos.fee_usd, kelly_fraction=result.kelly_fraction,
            transit_latency_ms=transit_ms,
            order_type=self.settings.order_type,
            seconds_to_expiry=signal.seconds_to_expiry,
            market_liquidity_usd=window.liquidity_usd,
            spread_pct=round(spread, 2),
        ))

        # Duplicate Prevention
        self._traded_windows.add(f"{window.slug}_{signal.direction}")

        # Live Order
        if self.executor.is_live:
            token_id = window.up_token_id if signal.direction == "UP" else window.down_token_id
            live_size = min(result.position_usd, self.settings.max_live_position_usd)

            async def _live_order():
                t0 = time.time()
                try:
                    res = await self.executor.place_order(
                        token_id=token_id, side="BUY", price=signal.ask_price,
                        size_usd=live_size, asset=signal.asset, direction=signal.direction,
                    )
                    latency = (time.time() - t0) * 1000
                    if res.success:
                        logger.info(f"HMSF LIVE ORDER OK: {signal.asset} {signal.direction} ({latency:.0f}ms)")
                        self.journal.update_live_result(
                            pos.trade_id, success=True, order_id=str(res.order_id), error="",
                        )
                    else:
                        logger.error(f"HMSF LIVE ORDER FAILED: {res.error}")
                        self.journal.update_live_result(
                            pos.trade_id, success=False, order_id="", error=str(res.error)[:200],
                        )
                except Exception as e:
                    logger.error(f"HMSF LIVE ORDER EXCEPTION: {e}")
                    self.journal.update_live_result(
                        pos.trade_id, success=False, order_id="", error=str(e)[:200],
                    )

            asyncio.create_task(_live_order())

        # Telegram
        asyncio.create_task(telegram.alert_signal(
            signal.asset, signal.direction, momentum_pct,
            signal.ask_price, result.net_ev_pct, result.position_usd, window.question,
            p_true=result.p_true, fee_pct=result.fee_pct, kelly=result.kelly_fraction,
            liquidity=window.liquidity_usd, spread_pct=round(spread, 1),
            seconds_to_expiry=signal.seconds_to_expiry, transit_ms=transit_ms,
        ))

    async def _execute_arb(self, arb, window: MarketWindow) -> None:
        """Führt eine Both-Sides-Arbitrage aus."""
        logger.info(
            f"HMSF ARB: {arb.asset} UP@{arb.up_ask_price:.3f} + DOWN@{arb.down_ask_price:.3f} "
            f"= {arb.combined_cost:.3f} → Profit {arb.guaranteed_profit_pct:.2f}%"
        )

        if not self.executor.is_live:
            logger.info("HMSF ARB: Paper-only (kein Live-Executor)")
            return

        # Sequentielle Ausführung (batch post_orders noch nicht implementiert)
        for side_label, token_id, price, size_usd in [
            ("UP", arb.up_token_id, arb.up_ask_price, arb.up_size_usd),
            ("DOWN", arb.down_token_id, arb.down_ask_price, arb.down_size_usd),
        ]:
            try:
                res = await self.executor.place_order(
                    token_id=token_id, side="BUY", price=price,
                    size_usd=size_usd, asset=arb.asset, direction=side_label,
                )
                if res.success:
                    logger.info(f"HMSF ARB {side_label} OK: {res.order_id}")
                else:
                    logger.error(f"HMSF ARB {side_label} FAILED: {res.error}")
                    break  # Leg failed — stop (Failure Unwind TODO)
            except Exception as e:
                logger.error(f"HMSF ARB {side_label} EXCEPTION: {e}")
                break

    # ═══════════════════════════════════════════════════════════════
    # POSITION RESOLVER
    # ═══════════════════════════════════════════════════════════════

    async def _position_resolver_loop(self) -> None:
        while self._running:
            await asyncio.sleep(5)
            try:
                self._resolve_expired()
            except Exception as e:
                logger.error(f"HMSF Resolver Fehler: {e}")

    def _resolve_expired(self) -> None:
        for symbol in self.settings.oracle_symbols:
            current_tick = self.oracle.get_latest(symbol)
            if current_tick is None:
                continue
            asset = symbol.replace("/USDT", "")

            resolved = self.paper_trader.check_and_resolve_expired(
                oracle_price_now=current_tick.mid, asset_filter=asset,
            )
            if resolved:
                for pos in resolved:
                    for d in ["UP", "DOWN"]:
                        self._traded_windows.discard(f"{pos.market_condition_id}_{d}")
                        self.engine.discard_window(pos.market_condition_id, d)

                    mo = pos.markout_prices
                    ref = pos.oracle_price_at_entry
                    def _mo(ts_key):
                        v = mo.get(ts_key, 0)
                        return round((v - ref) / ref * 100, 4) if ref > 0 and v > 0 else 0.0

                    self.journal.record_close(TradeRecord(
                        trade_id=pos.trade_id, asset=pos.asset, direction=pos.direction,
                        entry_ts=pos.entered_at, exit_ts=pos.resolved_at or time.time(),
                        oracle_price_entry=pos.oracle_price_at_entry,
                        oracle_price_exit=current_tick.mid,
                        executed_price=pos.entry_price,
                        momentum_pct=pos.momentum_at_entry,
                        p_true=pos.p_true_at_entry,
                        size_usd=pos.size_usd, fee_usd=pos.fee_usd,
                        outcome_correct=pos.outcome_correct,
                        pnl_usd=pos.pnl_usd,
                        pnl_pct=round(pos.pnl_usd / pos.size_usd * 100, 2) if pos.size_usd > 0 else 0,
                        markout_1s=_mo(1), markout_5s=_mo(5),
                        markout_10s=_mo(10), markout_30s=_mo(30),
                        markout_60s=_mo(60),
                    ))

                stats = self.paper_trader.stats()
                for pos in resolved:
                    asyncio.create_task(telegram.alert_resolved(
                        pos.trade_id, pos.asset, pos.direction,
                        pos.outcome_correct, pos.pnl_usd, stats['capital_usd'],
                        entry_price=pos.entry_price,
                        oracle_entry=pos.oracle_price_at_entry,
                        oracle_exit=current_tick.mid if current_tick else 0,
                    ))

    # ═══════════════════════════════════════════════════════════════
    # STATUS + REDEEM
    # ═══════════════════════════════════════════════════════════════

    async def _status_loop(self) -> None:
        counter = 0
        while self._running:
            await asyncio.sleep(60)
            counter += 1
            try:
                stats = self.paper_trader.stats()
                disc = self.discovery.status()
                logger.info(
                    f"HMSF STATUS | "
                    f"Fenster: {disc.get('tradeable',0)}/{disc.get('total_windows',0)} | "
                    f"Signale: {self._signals_detected} | "
                    f"Trades: {stats.get('trades_closed',0)} | "
                    f"WR: {stats.get('win_rate_pct',0):.1f}% | "
                    f"PnL: ${stats.get('daily_pnl_usd',0):+.2f}"
                )
            except Exception as e:
                logger.error(f"HMSF Status Fehler: {e}")

    async def _auto_redeem_loop(self) -> None:
        await asyncio.sleep(60)
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                if self.executor.is_live and self.settings.polymarket_private_key:
                    if not self._redeeming:
                        self._redeeming = True
                        try:
                            result = await loop.run_in_executor(
                                self._redeem_executor, self.redeemer.redeem_all
                            )
                        finally:
                            self._redeeming = False
                        if result.get("redeemed", 0) > 0:
                            logger.info(f"HMSF AutoRedeem: {result.get('redeemed')} redeemed")
            except Exception as e:
                logger.error(f"HMSF AutoRedeem Fehler: {e}")
                self._redeeming = False
            await asyncio.sleep(300)

    # ═══════════════════════════════════════════════════════════════
    # DASHBOARD STATUS
    # ═══════════════════════════════════════════════════════════════

    def get_status(self) -> dict:
        try:
            oracle_status = self.oracle.status()
        except Exception:
            oracle_status = {}
        try:
            discovery_status = self.discovery.status()
        except Exception:
            discovery_status = {"total_windows": 0, "tradeable": 0, "windows": []}
        try:
            trader_stats = self.paper_trader.stats()
        except Exception:
            trader_stats = {}

        return {
            "strategy": self.STRATEGY_NAME,
            "running": self._running,
            "signals_detected": self._signals_detected,
            "scan_cycles": self._scan_cycles,
            "heartbeat": self._heartbeat,
            "executor": self.executor.stats(),
            "oracle": oracle_status,
            "discovery": discovery_status,
            "recent_signals": list(self._recent_signals),
            "paper_trading": {
                **trader_stats,
                "open_positions": self.paper_trader.open_positions_summary(),
                "closed_positions": self.paper_trader.closed_positions_summary(),
            },
            "engine": self.engine.get_status(),
            "config": {
                "momentum_window_s": self.settings.momentum_window_s,
                "min_momentum_pct": self.settings.min_momentum_pct,
                "min_edge_pct": self.settings.min_edge_pct,
                "max_fee_pct": self.settings.polymarket_max_fee_pct,
                "kelly_fraction": self.settings.kelly_fraction,
                "paper_capital_usd": self.settings.paper_capital_usd,
            },
        }


# Auto-Register bei Import
register_strategy(HMSFStrategy.STRATEGY_NAME, HMSFStrategy)
