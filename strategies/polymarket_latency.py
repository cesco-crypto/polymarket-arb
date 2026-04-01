"""Polymarket Latency Arbitrage Strategy — Slug-basierte Discovery.

Kernlogik:
1. Binance WebSocket liefert BTC/ETH Tick-Daten in Echtzeit (Sub-50ms)
2. MarketDiscovery findet 5m/15m Märkte via Slug-Berechnung, hält Orderbücher live
3. Wenn Binance-Momentum stark genug → prüfe ob Polymarket-Orderbuch noch nicht angepasst hat
4. PreTrade-Kalkulator prüft Edge, 1.80% Gebühren (ab 30.03.2026), Half-Kelly Sizing
5. Paper Trader loggt den fiktiven Trade
6. Kill-Switch stoppt bei -20% Tages-Drawdown

Resolution Oracle: Chainlink BTC/USD (nicht Binance!)
Wir nutzen Binance als MOMENTUM-INDIKATOR, Chainlink entscheidet die Auflösung.
"""

from __future__ import annotations

import asyncio
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
from utils import telegram


@dataclass
class StrategySignal:
    """Ein erkanntes Latenz-Arbitrage-Signal."""

    asset: str
    direction: str
    momentum_pct: float
    binance_price: float
    polymarket_ask: float
    seconds_to_expiry: float
    market_question: str
    detected_at: float


class PolymarketLatencyStrategy:
    """Orchestriert den kompletten Polymarket Latency Arb Loop.

    Komponenten:
    - BinanceWSOracle: Sub-50ms Preisstream (Momentum-Indikator)
    - MarketDiscovery: Slug-basierte 5m/15m Markt-Erkennung + Orderbuch-Refresh
    - Signal-Loop: alle 500ms auf neue Signale prüfen
    - Position-Resolver: abgelaufene Positionen auflösen
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.oracle = BinanceWebSocketOracle(
            symbols=settings.oracle_symbols,
            window_size_s=max(settings.momentum_window_s * 2, 120),
        )
        self.discovery = MarketDiscovery(
            assets=[s.replace("/USDT", "").lower() for s in settings.oracle_symbols],
            timeframes=["5m", "15m"],
            min_liquidity_usd=15000.0,  # Min $15K Liquidität (verhindert Slippage)
        )
        self.calculator = PreTradeCalculator(settings)
        self.paper_trader = PaperTrader(settings)
        self.risk_manager = RiskManager(settings)
        self.executor = PolymarketExecutor(settings)
        self.journal = TradeJournal()

        self._running = False
        self._signals_detected: int = 0
        self._recent_signals: deque = deque(maxlen=30)
        self._scan_cycles: int = 0
        self._heartbeat: dict = {}
        self._tick_event = asyncio.Event()
        self._last_tick_symbol: str = ""
        self._traded_windows: set = set()  # Slug-Set: verhindert Duplicate Trades pro Fenster

    async def run(self) -> None:
        """Startet alle Komponenten und läuft bis zum Shutdown."""
        self._running = True
        telegram.configure(self.settings)
        logger.info("Polymarket Latency Strategy startet...")
        logger.info(
            f"Kapital: ${self.settings.paper_capital_usd:.0f} | "
            f"Min-Momentum: {self.settings.min_momentum_pct}% | "
            f"Min-Edge: {self.settings.min_edge_pct}% | "
            f"Max-Fee: {self.settings.polymarket_max_fee_pct}% | "
            f"Kelly: {self.settings.kelly_fraction} (Half-Kelly)"
        )
        await telegram.alert_startup(
            self.settings.paper_capital_usd, self.settings.mode,
            live=self.settings.live_trading,
            wallet=self.settings.polymarket_funder,
        )

        # Binance WebSocket starten + Event-driven Callback registrieren
        def on_tick(symbol, tick):
            self._last_tick_symbol = symbol
            self._tick_event.set()
        self.oracle.set_on_tick(on_tick)
        await self.oracle.start()

        # Market Discovery starten (Slug-basiert, ersetzt Gamma-API-Suche)
        await self.discovery.start()

        # Executor initialisieren (Live-Trading nur wenn alle Flags gesetzt)
        if self.settings.live_trading:
            live_ok = await self.executor.initialize()
            if live_ok:
                logger.info("LIVE TRADING MODUS — Echte Orders auf Polymarket!")
            else:
                logger.warning("Live Trading angefordert aber Init fehlgeschlagen → Paper Mode")
        else:
            logger.info("Paper Trading Modus (live_trading=False)")

        # Warte kurz bis erste Ticks ankommen
        logger.info("Warte auf erste Binance-Ticks + Polymarket-Orderbücher...")
        await asyncio.sleep(3)

        # Hauptloops parallel starten
        # MarketDiscovery hat eigenen Refresh-Loop (alle 3s)
        # CRITICAL: return_exceptions=True verhindert dass ein Loop-Crash alle anderen killt
        try:
            results = await asyncio.gather(
                self._signal_loop(),
                self._position_resolver_loop(),
                self._status_loop(),
                return_exceptions=True,
            )
            # Log crashed loops statt silent fail
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    loop_names = ["signal_loop", "position_resolver", "status_loop"]
                    logger.error(f"Loop '{loop_names[i]}' crashed: {result}")
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._running = False
        try:
            await self.oracle.stop()
        except Exception as e:
            logger.error(f"Oracle stop error: {e}")
        try:
            await self.discovery.stop()
        except Exception as e:
            logger.error(f"Discovery stop error: {e}")
        try:
            stats = self.paper_trader.stats()
            logger.info(
                f"Strategy beendet. "
                f"Trades: {stats.get('trades_closed', 0)} | "
                f"Win Rate: {stats.get('win_rate_pct', 0):.1f}% | "
                f"PnL: ${stats.get('daily_pnl_usd', 0):+.2f} | "
                f"Kapital: ${stats.get('capital_usd', 0):.2f}"
            )
            await telegram.alert_shutdown(
                stats.get('capital_usd', 0), stats.get('trades_closed', 0), stats.get('daily_pnl_usd', 0)
            )
        except Exception as e:
            logger.error(f"Shutdown stats/telegram error: {e}")
        try:
            await telegram.close()
        except Exception:
            pass

    # --- Signal Loop ---

    async def _signal_loop(self) -> None:
        """Event-driven: Reagiert sofort auf jeden Binance-Tick statt 500ms Sleep.

        Alte Architektur: await asyncio.sleep(0.5) → 250ms durchschnittliche Verzögerung
        Neue Architektur: await self._tick_event → <5ms Reaktionszeit
        """
        logger.info("Signal-Loop gestartet (EVENT-DRIVEN)")

        while self._running:
            try:
                # Warte auf nächsten Binance Tick (max 2s Timeout als Safety)
                try:
                    await asyncio.wait_for(self._tick_event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass  # Kein Tick in 2s → prüfe trotzdem (Heartbeat)
                self._tick_event.clear()

                await self._check_all_signals()
            except Exception as e:
                logger.error(f"Signal-Loop Fehler: {e}")
                await asyncio.sleep(0.5)  # Nur bei Fehler warten

    async def _check_all_signals(self) -> None:
        """Prüft alle aktiven Polymarket-Fenster auf Signale."""
        self._scan_cycles += 1

        if not self.paper_trader.can_trade():
            return

        # Mark-out Recording (Asset-gefiltert: BTC-Preis nur für BTC-Positionen!)
        for symbol in self.settings.oracle_symbols:
            tick = self.oracle.get_latest(symbol)
            if tick:
                asset = symbol.replace("/USDT", "")
                self.paper_trader.record_markout(tick.mid, asset_filter=asset)

        # Heartbeat: aktuelles Momentum für jedes Asset erfassen
        hb = {}
        for asset in ["BTC", "ETH"]:
            symbol = f"{asset}/USDT"

            # Binance-Daten prüfen
            if not self.oracle.is_fresh(symbol, max_age_s=5.0):
                continue

            tick = self.oracle.get_latest(symbol)
            momentum = self.oracle.get_momentum(symbol, self.settings.momentum_window_s)

            if tick is None or momentum is None:
                continue

            # Heartbeat: Momentum + Hurdle aufzeichnen (auch wenn zu schwach)
            hurdle = self.settings.min_momentum_pct
            hb[asset] = {
                "price": round(tick.mid, 2),
                "momentum": round(momentum, 4),
                "abs_momentum": round(abs(momentum), 4),
                "hurdle": hurdle,
                "pct_of_hurdle": round(abs(momentum) / hurdle * 100, 1) if hurdle > 0 else 0,
                "status": "SIGNAL" if abs(momentum) >= hurdle else "WATCHING",
            }

            # Data Freshness Guard: Skip stale ticks (>300ms Transit = ghost momentum)
            transit = self.oracle.status().get(symbol, {}).get("transit_p50_ms", 0)
            if transit > 300:
                continue  # Signal zu alt — würde Adverse Selection verursachen

            # Signal stark genug?
            if abs(momentum) < self.settings.min_momentum_pct:
                continue

            # Richtung bestimmen
            direction = "UP" if momentum > 0 else "DOWN"

            # Passende Polymarket-Fenster prüfen
            windows = self.discovery.get_windows_for_asset(asset)
            for window in windows:
                self._evaluate_window(
                    asset, direction, momentum, tick.mid, window
                )

        # Heartbeat aktualisieren
        self._heartbeat = hb

        # Pre-Signing: Orders für aktive Fenster vorab signieren (Latenz-Killer)
        if self.executor.is_live and self._scan_cycles % 20 == 0:  # Alle ~10s
            for asset_lower in ["btc", "eth"]:
                windows = self.discovery.get_windows_for_asset(asset_lower.upper())
                for w in windows[:2]:  # Nur die nächsten 2 Fenster
                    for tid in [w.up_token_id, w.down_token_id]:
                        if tid:
                            self.executor.pre_sign_order(
                                tid, 0.50, self.settings.max_live_position_usd
                            )

    def _evaluate_window(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        binance_price: float,
        window: MarketWindow,
    ) -> None:
        """Bewertet ein einzelnes Polymarket-Fenster."""
        seconds_to_expiry = window.seconds_remaining

        # Zeitfenster-Check
        if not (
            self.settings.min_seconds_to_expiry
            <= seconds_to_expiry
            <= self.settings.max_seconds_to_expiry
        ):
            return

        # Orderbuch muss frisch sein
        if not window.orderbook_fresh:
            return

        # Ask-Preis für die gewünschte Richtung bestimmen
        if direction == "UP":
            ask_price = window.up_best_ask
            ask_depth_usd = window.up_ask_size * window.up_best_ask if window.up_best_ask > 0 else 0
        else:
            ask_price = window.down_best_ask
            ask_depth_usd = 0  # DOWN-Depth nicht separat getrackt, ok für Paper

        if ask_price <= 0 or ask_price >= 1:
            return

        # Preis-Filter: Nur handeln wenn Markt noch offen ist (nicht schon entschieden)
        if ask_price < 0.20 or ask_price > 0.80:
            return

        # Spread-Cost-Check: Wenn Spread zwischen Bid und Ask > 2 Cent,
        # fressen die Spread-Kosten unseren Edge
        if direction == "UP":
            bid = window.up_best_bid
            ask = ask_price
        else:
            bid = window.down_best_bid
            ask = ask_price
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
            if spread_pct > 3.0:  # Spread > 3% = unprofitabel
                return

        # Partizipations-Check: Order darf max 1% der Marktliquidität sein
        # $5 Order bei $15K Liq = 0.03% → OK
        # $80 Order bei $3.9K Liq = 2% → BLOCK (Slippage-Gefahr)
        max_participation_pct = 1.0
        market_liq = window.liquidity_usd
        if market_liq > 0:
            max_order_by_liq = market_liq * (max_participation_pct / 100)
            order_size = self.settings.max_live_position_usd if self.executor.is_live else self.settings.max_order_size_usd
            if order_size > max_order_by_liq:
                return  # Order wäre > 1% der Marktliquidität

        # Duplicate Prevention: Nur 1 Trade pro Fenster pro Richtung
        trade_key = f"{window.slug}_{direction}"
        if trade_key in self._traded_windows:
            return

        # PreTrade-Analyse
        result = self.calculator.evaluate(
            asset=asset,
            direction=direction,
            momentum_pct=momentum_pct,
            polymarket_ask=ask_price,
            seconds_to_expiry=seconds_to_expiry,
            available_capital_usd=min(
                self.paper_trader.capital_usd,
                self.settings.max_live_position_usd / self.settings.max_position_pct
            ) if self.executor.is_live else self.paper_trader.capital_usd,
        )

        # Signal in History aufnehmen (EXECUTE + ABORT)
        self._recent_signals.appendleft({
            "ts": time.strftime("%H:%M:%S"),
            "asset": asset,
            "direction": direction,
            "momentum": round(momentum_pct, 4),
            "p_true": round(result.p_true, 4),
            "p_market": round(result.p_market, 4),
            "fee_pct": round(result.fee_pct, 2),
            "net_ev_pct": round(result.net_ev_pct, 2),
            "kelly": round(result.kelly_fraction, 4),
            "size_usd": round(result.position_usd, 2),
            "decision": result.decision.value,
            "reason": result.abort_reason[:40] if result.abort_reason else "",
            "slug": window.slug,
        })

        if result.decision == TradeDecision.EXECUTE:
            self._signals_detected += 1

            logger.info(
                f"SIGNAL #{self._signals_detected}: {asset} {direction} | "
                f"Binance: {binance_price:.2f} ({momentum_pct:+.3f}%) | "
                f"PM-Ask: {ask_price:.3f} | "
                f"p_true: {result.p_true:.3f} | "
                f"Net-EV: {result.net_ev_pct:.2f}% | "
                f"Fee: {result.fee_pct:.2f}% | "
                f"Size: ${result.position_usd:.2f} | "
                f"Ablauf: {seconds_to_expiry:.0f}s | "
                f"{window.question}"
            )

            pos = self.paper_trader.open_position(
                result=result,
                condition_id=window.condition_id,
                question=window.question,
                market_end_timestamp=float(window.window_end_ts),
                momentum_pct=momentum_pct,
                seconds_to_expiry=seconds_to_expiry,
                oracle_price=binance_price,
            )

            # Forensisches Journal: Trade Open
            if pos:
                transit = self.oracle.status().get(f"{asset}/USDT", {}).get("transit_p50_ms", 0)
                bid = window.up_best_bid if direction == "UP" else window.down_best_bid
                spread = (ask_price - bid) / ((ask_price + bid) / 2) * 100 if bid > 0 else 0
                self.journal.record_open(TradeRecord(
                    trade_id=pos.trade_id, asset=asset, direction=direction,
                    signal_ts=time.time(), entry_ts=pos.entered_at,
                    window_slug=window.slug, market_question=window.question[:60],
                    timeframe=window.timeframe,
                    oracle_price_entry=binance_price,
                    polymarket_bid=bid, polymarket_ask=ask_price,
                    executed_price=ask_price,
                    momentum_pct=momentum_pct, p_true=result.p_true,
                    p_market=result.p_market, raw_edge_pct=result.raw_edge_pct,
                    fee_pct=result.fee_pct, net_ev_pct=result.net_ev_pct,
                    size_usd=result.position_usd, shares=pos.shares,
                    fee_usd=pos.fee_usd, kelly_fraction=result.kelly_fraction,
                    transit_latency_ms=transit,
                    order_type=self.settings.order_type,
                    seconds_to_expiry=seconds_to_expiry,
                    market_liquidity_usd=window.liquidity_usd,
                    spread_pct=round(spread, 2),
                ))

            # Fenster als getraded markieren (Duplicate Prevention)
            self._traded_windows.add(trade_key)

            # LIVE ORDER platzieren (wenn Executor aktiv)
            if self.executor.is_live:
                token_id = window.up_token_id if direction == "UP" else window.down_token_id
                live_size = min(result.position_usd, self.settings.max_live_position_usd)

                async def _execute_live_order(tid, ap, sz, a, d, trade_id):
                    t0 = time.time()
                    try:
                        res = await self.executor.place_order(
                            token_id=tid, side="BUY", price=ap,
                            size_usd=sz, asset=a, direction=d,
                        )
                        latency = (time.time() - t0) * 1000
                        if not res.success:
                            logger.error(f"LIVE ORDER FAILED: {a} {d} — {res.error}")
                            await telegram.alert_live_order(
                                trade_id, a, d, success=False,
                                price=ap, size=sz, error=str(res.error)[:120],
                            )
                            from core import db
                            await db.insert_trade({
                                "event": "live_error", "trade_id": trade_id,
                                "asset": a, "direction": d,
                                "live_order_success": False,
                                "live_error": str(res.error)[:200],
                                "executed_price": ap, "size_usd": sz,
                            })
                        else:
                            logger.info(f"LIVE ORDER OK: {a} {d} — ID={res.order_id} ({latency:.0f}ms)")
                            await telegram.alert_live_order(
                                trade_id, a, d, success=True,
                                price=ap, size=sz, order_id=str(res.order_id),
                                latency_ms=latency,
                            )
                    except Exception as e:
                        logger.error(f"LIVE ORDER EXCEPTION: {a} {d} — {e}")
                        await telegram.alert_live_order(
                            trade_id, a, d, success=False,
                            price=ap, size=sz, error=str(e)[:120],
                        )
                        from core import db
                        await db.insert_trade({
                            "event": "live_error", "trade_id": trade_id,
                            "asset": a, "direction": d,
                            "live_order_success": False, "live_error": str(e)[:200],
                        })

                asyncio.create_task(_execute_live_order(
                    token_id, ask_price, live_size, asset, direction,
                    pos.trade_id if pos else "unknown",
                ))

            # Telegram Alert (erweitert mit allen Metriken)
            transit = self.oracle.status().get(f"{asset}/USDT", {}).get("transit_p50_ms", 0)
            asyncio.create_task(telegram.alert_signal(
                asset, direction, momentum_pct,
                ask_price, result.net_ev_pct, result.position_usd, window.question,
                p_true=result.p_true, fee_pct=result.fee_pct, kelly=result.kelly_fraction,
                liquidity=window.liquidity_usd, spread_pct=round((ask_price - (window.up_best_bid if direction == "UP" else window.down_best_bid)) / max(ask_price, 0.01) * 100, 1),
                seconds_to_expiry=seconds_to_expiry, transit_ms=transit,
            ))

    # --- Position Resolver Loop ---

    async def _position_resolver_loop(self) -> None:
        """Löst abgelaufene Paper-Positionen auf."""
        while self._running:
            await asyncio.sleep(5)
            try:
                self._resolve_expired_positions()
            except Exception as e:
                logger.error(f"Resolver Fehler: {e}")

    def _resolve_expired_positions(self) -> None:
        """Löst Positionen auf — NUR mit dem richtigen Asset-Oracle.

        KRITISCH: BTC-Preis darf nur BTC-Positionen resolven,
        ETH-Preis nur ETH-Positionen. Sonst falsche Resolution!
        """
        for symbol in self.settings.oracle_symbols:
            current_tick = self.oracle.get_latest(symbol)
            if current_tick is None:
                continue

            # Asset-Filter: BTC/USDT → "BTC"
            asset = symbol.replace("/USDT", "")

            resolved = self.paper_trader.check_and_resolve_expired(
                oracle_price_now=current_tick.mid,
                asset_filter=asset,
            )

            if resolved:
                for pos in resolved:
                    for d in ["UP", "DOWN"]:
                        self._traded_windows.discard(f"{pos.market_condition_id}_{d}")

                    # Forensisches Journal: Trade Close
                    mo = pos.markout_prices
                    ref = pos.oracle_price_at_entry
                    def _mo_pct(ts_key):
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
                        markout_1s=_mo_pct(1), markout_5s=_mo_pct(5),
                        markout_10s=_mo_pct(10), markout_30s=_mo_pct(30),
                        markout_60s=_mo_pct(60),
                    ))

                stats = self.paper_trader.stats()
                logger.info(
                    f"Positionen aufgelöst: {len(resolved)} | "
                    f"Kapital: ${stats['capital_usd']:.2f} | "
                    f"Win Rate: {stats['win_rate_pct']:.1f}% | "
                    f"PnL: ${stats['daily_pnl_usd']:+.2f}"
                )
                for pos in resolved:
                    asyncio.create_task(telegram.alert_resolved(
                        pos.trade_id, pos.asset, pos.direction,
                        pos.outcome_correct, pos.pnl_usd, stats['capital_usd'],
                        entry_price=pos.entry_price,
                        oracle_entry=pos.oracle_price_at_entry,
                        oracle_exit=current_tick.mid if current_tick else 0,
                        markout_1s=_mo_pct(1), markout_5s=_mo_pct(5), markout_60s=_mo_pct(60),
                        fee_usd=pos.fee_usd, size_usd=pos.size_usd,
                        exec_type="LIVE" if self.executor.is_live else "PAPER",
                    ))
                # Drawdown-Alert
                dd = stats.get('drawdown_pct', 0)
                if dd >= 15:
                    asyncio.create_task(telegram.alert_kill_switch(dd, stats['capital_usd']))
                elif dd >= 5:
                    asyncio.create_task(telegram.alert_drawdown(dd, stats['capital_usd']))

    # --- Status Loop ---

    async def _status_loop(self) -> None:
        """Periodischer Status-Log (alle 60s) + stündlicher Telegram Heartbeat."""
        heartbeat_counter = 0
        while self._running:
            await asyncio.sleep(60)
            heartbeat_counter += 1
            try:
                discovery_status = self.discovery.status()
                trader_stats = self.paper_trader.stats()
                oracle_status = self.oracle.status()

                # Binance tick count
                ticks = sum(v.get("ticks", 0) for v in oracle_status.values())

                logger.info(
                    f"STATUS | "
                    f"Fenster: {discovery_status.get('tradeable', 0)}/{discovery_status.get('total_windows', 0)} | "
                    f"Signale: {self._signals_detected} | "
                    f"Trades: {trader_stats.get('trades_closed', 0)} | "
                    f"WR: {trader_stats.get('win_rate_pct', 0):.1f}% | "
                    f"PnL: ${trader_stats.get('daily_pnl_usd', 0):+.2f} | "
                    f"Kapital: ${trader_stats.get('capital_usd', 0):.2f}"
                )

                # Stündlicher Heartbeat via Telegram (alle 60 Minuten)
                if heartbeat_counter % 60 == 0:
                    # CLOB Latenz messen
                    clob_ms = 0
                    try:
                        import aiohttp as _aio
                        t0 = time.time()
                        async with _aio.ClientSession(timeout=_aio.ClientTimeout(total=5)) as s:
                            async with s.get("https://clob.polymarket.com/markets?limit=1") as r:
                                await r.read()
                        clob_ms = (time.time() - t0) * 1000
                    except Exception:
                        pass

                    asyncio.create_task(telegram.alert_heartbeat(
                        trades=trader_stats.get('trades_closed', 0),
                        pnl=trader_stats.get('daily_pnl_usd', 0),
                        capital=trader_stats.get('capital_usd', 0),
                        windows_total=discovery_status.get('total_windows', 0),
                        windows_tradeable=discovery_status.get('tradeable', 0),
                        clob_latency_ms=clob_ms,
                        binance_ticks=ticks,
                    ))
            except Exception as e:
                logger.error(f"Status-Loop Fehler: {e}")

    # --- Status für Dashboard ---

    def get_status(self) -> dict:
        """Vollständiger Status für Web-Dashboard."""
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
            "strategy": "polymarket_latency",
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
            "config": {
                "momentum_window_s": self.settings.momentum_window_s,
                "min_momentum_pct": self.settings.min_momentum_pct,
                "min_edge_pct": self.settings.min_edge_pct,
                "max_fee_pct": self.settings.polymarket_max_fee_pct,
                "kelly_fraction": self.settings.kelly_fraction,
                "paper_capital_usd": self.settings.paper_capital_usd,
            },
        }
