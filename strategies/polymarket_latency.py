"""Polymarket Latency Arbitrage Strategy — Slug-basierte Discovery.

Kernlogik:
1. Binance WebSocket liefert BTC/ETH Tick-Daten in Echtzeit (Sub-50ms)
2. MarketDiscovery findet 5m/15m Märkte via Slug-Berechnung, hält Orderbücher live
3. Wenn Binance-Momentum stark genug → prüfe ob Polymarket-Orderbuch noch nicht angepasst hat
4. PreTrade-Kalkulator prüft Edge, 3.15% Gebühren, Half-Kelly Sizing
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
            min_liquidity_usd=5000.0,
        )
        self.calculator = PreTradeCalculator(settings)
        self.paper_trader = PaperTrader(settings)
        self.risk_manager = RiskManager(settings)
        self.executor = PolymarketExecutor(settings)

        self._running = False
        self._signals_detected: int = 0
        self._recent_signals: deque = deque(maxlen=30)
        self._scan_cycles: int = 0
        self._heartbeat: dict = {}
        self._tick_event = asyncio.Event()  # Event-driven: gesetzt bei jedem Binance Tick
        self._last_tick_symbol: str = ""

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
        try:
            await asyncio.gather(
                self._signal_loop(),
                self._position_resolver_loop(),
                self._status_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._running = False
        await self.oracle.stop()
        await self.discovery.stop()
        stats = self.paper_trader.stats()
        logger.info(
            f"Strategy beendet. "
            f"Trades: {stats['trades_closed']} | "
            f"Win Rate: {stats['win_rate_pct']:.1f}% | "
            f"PnL: ${stats['daily_pnl_usd']:+.2f} | "
            f"Kapital: ${stats['capital_usd']:.2f}"
        )
        await telegram.alert_shutdown(stats['capital_usd'], stats['trades_closed'], stats['daily_pnl_usd'])
        await telegram.close()

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

        # Mark-out Recording
        for symbol in self.settings.oracle_symbols:
            tick = self.oracle.get_latest(symbol)
            if tick:
                self.paper_trader.record_markout(tick.mid)

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
        # Bei Ask=0.01 weiss der Markt zu 99% wie es ausgeht — kein Edge möglich
        if ask_price < 0.20 or ask_price > 0.80:
            return

        # Depth-Check: genug Liquidität für unsere Ordergröße?
        min_depth = self.settings.max_order_size_usd if hasattr(self.settings, 'max_order_size_usd') else 50
        if ask_depth_usd > 0 and ask_depth_usd < min_depth:
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

            self.paper_trader.open_position(
                result=result,
                condition_id=window.condition_id,
                question=window.question,
                market_end_timestamp=float(window.window_end_ts),
                momentum_pct=momentum_pct,
                seconds_to_expiry=seconds_to_expiry,
                oracle_price=binance_price,
            )

            # LIVE ORDER platzieren (wenn Executor aktiv)
            if self.executor.is_live:
                token_id = window.up_token_id if direction == "UP" else window.down_token_id
                live_size = min(result.position_usd, self.settings.max_live_position_usd)
                asyncio.create_task(self.executor.place_order(
                    token_id=token_id,
                    side="BUY",
                    price=ask_price,
                    size_usd=live_size,
                    asset=asset,
                    direction=direction,
                ))

            # Telegram Alert
            asyncio.create_task(telegram.alert_signal(
                asset, direction, momentum_pct,
                ask_price, result.net_ev_pct, result.position_usd, window.question,
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
                    ))
                # Drawdown-Alert
                dd = stats.get('drawdown_pct', 0)
                if dd >= 15:
                    asyncio.create_task(telegram.alert_kill_switch(dd, stats['capital_usd']))
                elif dd >= 5:
                    asyncio.create_task(telegram.alert_drawdown(dd, stats['capital_usd']))

    # --- Status Loop ---

    async def _status_loop(self) -> None:
        """Periodischer Status-Log (alle 60s)."""
        while self._running:
            await asyncio.sleep(60)
            discovery_status = self.discovery.status()
            trader_stats = self.paper_trader.stats()
            logger.info(
                f"STATUS | "
                f"Fenster: {discovery_status['tradeable']}/{discovery_status['total_windows']} | "
                f"Signale: {self._signals_detected} | "
                f"Trades: {trader_stats['trades_closed']} | "
                f"WR: {trader_stats['win_rate_pct']:.1f}% | "
                f"PnL: ${trader_stats['daily_pnl_usd']:+.2f} | "
                f"Kapital: ${trader_stats['capital_usd']:.2f}"
            )

    # --- Status für Dashboard ---

    def get_status(self) -> dict:
        """Vollständiger Status für Web-Dashboard."""
        oracle_status = self.oracle.status()
        discovery_status = self.discovery.status()
        trader_stats = self.paper_trader.stats()

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
