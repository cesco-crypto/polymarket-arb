"""Oracle Delay Arbitrage — Kauft den Winner NACH Window-Close.

Repliziert die "Sharky6999" Strategie:
1. Beobachtet 5-Min Crypto Windows (BTC, ETH Up/Down)
2. Wartet bis das Window SCHLIESST (Ergebnis ist bekannt)
3. CLOB ist noch offen (Oracle hat noch nicht resolved)
4. Kauft den Winner @ 0.97-0.99 (fast sicherer Gewinn)
5. Wartet auf Oracle Resolution → redeemed @ 1.00
6. Profit: ~1% pro Trade, ~$2-5/Stunde bei $50-100 Trades

KEY INSIGHT: Keine Prediction nötig — das Ergebnis ist bereits bekannt.
Fee-Vorteil: Bei 0.99 Entry zahlen wir nur 0.07% Fee (vs 1.80% bei 0.50).

RISIKEN:
- Oracle resolved anders als Binance zeigt (selten, aber -100% Loss)
- CLOB Liquidität bei 0.99 ist dünn (Split-Orders nötig)
- Andere Bots machen dasselbe (Konkurrenz um Liquidität)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
from loguru import logger

from config import Settings
from core.executor import PolymarketExecutor
from core.market_discovery import MarketDiscovery
from core.trade_journal import TradeJournal, TradeRecord
from strategies.base import StrategyBase
from strategies.registry import register as register_strategy
from utils import telegram


# ═══════════════════════════════════════════════════════════════════
# DATA TYPES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class WindowClose:
    """Ein gerade geschlossenes 5-Min Window."""
    slug: str
    asset: str               # "BTC" / "ETH"
    window_end_ts: int        # Unix timestamp des Window-Endes
    winner: str               # "UP" oder "DOWN"
    binance_price_at_close: float
    price_to_beat: float      # Referenzpreis des Windows
    up_token_id: str
    down_token_id: str
    condition_id: str


@dataclass
class SniperTrade:
    """Ein ausgeführter Oracle Delay Trade."""
    trade_id: str
    slug: str
    asset: str
    direction: str            # "UP" oder "DOWN"
    entry_price: float
    size_usd: float
    token_id: str
    condition_id: str
    timestamp: float
    live_order_id: str = ""
    live_success: bool = False
    resolved: bool = False
    pnl_usd: float = 0.0


# ═══════════════════════════════════════════════════════════════════
# ORACLE DELAY ARBITRAGE STRATEGY
# ═══════════════════════════════════════════════════════════════════

class OracleDelayArbStrategy(StrategyBase):
    """Kauft den Winner NACH Window-Close, BEVOR Oracle resolved.

    Funktionsweise:
    1. Tracked alle aktiven 5-Min Crypto Windows
    2. Bei Window-Close: liest Binance-Preis → bestimmt Winner
    3. Kauft Winner-Token @ 0.97-0.99 auf dem noch offenen CLOB
    4. Wartet auf Oracle Resolution → redeemed @ 1.00 → Profit ~1%

    Sharky6999-Style: $4K+ pro Window, 50-67 Split-Orders.
    Wir: $10-50 pro Window, 1-3 Orders (skaliert mit Kapital).
    """

    STRATEGY_NAME = "oracle_delay_arb"
    DESCRIPTION = (
        "Oracle Delay Arb: Kauft den Winner NACH Window-Close @ 0.97-0.99. "
        "Kein Prediction — Ergebnis ist bereits bekannt. ~1% Profit/Trade."
    )

    @property
    def name(self) -> str:
        return self.STRATEGY_NAME

    @property
    def description(self) -> str:
        return self.DESCRIPTION

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.executor = PolymarketExecutor(settings)
        self.journal = TradeJournal()

        # EIGENE MarketDiscovery — autark, keine Abhaengigkeit von anderen Strategien
        self.discovery = MarketDiscovery(
            assets=["btc", "eth"],
            timeframes=["5m", "15m"],
        )

        # Config — Latency Arbitrage: Sub-100ms Execution
        self.trade_size_usd = 5.0          # $5 pro Trade
        self.min_entry_price = 0.60        # Min Preis (Floor: nur Asks >= 60c — eliminiert Fake-Edge-Zone)
        self.max_entry_price = 0.93        # Max 0.93 (Latenz-Filter: nur fruehe Fills mit fettem Edge)
        self.delay_after_close_s = 0.0     # 0ms Delay — rein event-driven!
        self.max_delay_s = 15.0            # Max 15s nach Close (Fenster ist ~2.7s median)
        self.max_concurrent = 5            # Max 5 gleichzeitige Snipes
        self.deadzone_pct = 0.05           # Deadzone: < 0.05% Binance-Move = Noise (war 0.01%)

        # Volatility Tracking (fuer Regime-Detection)
        self._recent_deltas: deque = deque(maxlen=20)

        # State
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._trades: list[SniperTrade] = []
        self._trade_count = 0
        self._recent_snipes: deque = deque(maxlen=50)
        self._sniped_windows: set[str] = set()  # Dedup: slug → bereits gesniped
        self._scheduled_windows: set[str] = set()  # Timer bereits geplant

        # CLOB WebSocket — lokales RAM-Orderbuch
        self._clob_ws = None  # Initialisiert in run()

        # Binance Oracle Referenz — direkter RAM-Read bei Execution
        self._oracle = None  # Gesetzt in _register_binance_callback()
        self._price_at_window_start: dict[str, tuple] = {}  # slug → (price, timestamp)

    # ═══════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═══════════════════════════════════════════════════════════════

    def _load_trade_counter(self) -> None:
        """Laedt den hoechsten ODA Trade-Counter aus dem Journal (verhindert ID-Kollisionen)."""
        journal_path = Path("data/trade_journal.jsonl")
        max_num = 0
        try:
            if journal_path.exists():
                with open(journal_path) as f:
                    for line in f:
                        if "ODA-" not in line:
                            continue
                        try:
                            e = json.loads(line)
                            tid = e.get("trade_id", "")
                            if tid.startswith("ODA-"):
                                num = int(tid.split("-")[1])
                                max_num = max(max_num, num)
                        except (json.JSONDecodeError, ValueError, IndexError):
                            continue
        except Exception as e:
            logger.warning(f"ODA Counter Load Error: {e}")
        self._trade_count = max_num
        if max_num > 0:
            logger.info(f"ODA: Trade-Counter bei {max_num} fortgesetzt (aus Journal)")

    async def run(self) -> None:
        self._running = True
        telegram.configure(self.settings)

        # Trade-Counter aus Journal laden (verhindert ID-Kollisionen nach Restart)
        self._load_trade_counter()

        logger.info(f"ODA v8 startet — Autarke Discovery + Async Execution (Counter: {self._trade_count})")
        logger.info(
            f"ODA CONFIG | size=${self.trade_size_usd} | range={self.min_entry_price}-{self.max_entry_price} | "
            f"deadzone={self.deadzone_pct}% | max_concurrent={self.max_concurrent}"
        )

        # Eigene MarketDiscovery starten (autark, kein momentum_latency_v2 noetig)
        await self.discovery.start()
        logger.info(f"ODA: Eigene MarketDiscovery gestartet (BTC+ETH, 5m+15m)")

        if self.settings.live_trading:
            live_ok = await self.executor.initialize()
            if live_ok:
                logger.info("ODA: LIVE MODUS — Echte FAK Orders!")
            else:
                logger.warning("ODA: Live Init fehlgeschlagen")

        # Persistente HTTP Session — TCP Keep-Alive, kein TLS-Handshake pro Order
        connector = aiohttp.TCPConnector(
            keepalive_timeout=300,  # 5min Keep-Alive
            limit=10,
            enable_cleanup_closed=True,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=5, connect=2),
            headers={"User-Agent": "polymarket-arb/2.0"},
        )

        # CLOB WebSocket — lokales RAM-Orderbuch
        from core.clob_ws import CLOBWebSocket
        self._clob_ws = CLOBWebSocket()

        # Token-IDs aus eigener Discovery laden
        initial_tokens = self.discovery.get_all_token_ids()
        # Zusaetzlich: Gamma API fuer kommende Windows
        for w in self._compute_next_window_closes():
            if w["seconds_to_close"] > 0:
                tids = await self._get_token_ids(w["slug"], w["asset"])
                if tids:
                    initial_tokens.extend([tids[0], tids[1]])
        if initial_tokens:
            self._clob_ws.subscribe(initial_tokens)
            logger.info(f"ODA: {len(initial_tokens)} Token-IDs subscribed (eigene Discovery)")
        await self._clob_ws.start()

        # Binance Tick-Callback registrieren (Event-Driven Trigger)
        self._register_binance_callback()

        try:
            await asyncio.gather(
                self._main_loop(),
                self._status_loop(),
                self._subscription_refresh_loop(),
                self._session_warmer(),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    def _register_binance_callback(self) -> None:
        """Startet eigenen Binance Oracle oder nutzt einen existierenden.

        Versucht zuerst eine existierende Referenz zu finden (spart Connections).
        Falls keine vorhanden: startet eigenen Oracle.
        """
        # Versuche existierenden Oracle zu finden (spart eine WS Connection)
        try:
            from dashboard.web_ui import active_strategies
            for sn, st in active_strategies.items():
                if hasattr(st, "oracle") and st.oracle:
                    self._oracle = st.oracle
                    logger.info(f"ODA: Binance Oracle shared von {sn}")
                    return
        except Exception:
            pass

        # Eigenen Oracle starten (autark)
        try:
            from core.binance_ws import BinanceWebSocketOracle
            self._oracle = BinanceWebSocketOracle(
                symbols=["BTC/USDT", "ETH/USDT"],
                window_size_s=600,  # 10min History fuer 5m Window-Start Lookup
            )
            asyncio.create_task(self._oracle.start())
            logger.info("ODA: Eigener Binance Oracle gestartet (BTC+ETH)")
        except Exception as e:
            logger.error(f"ODA: Binance Oracle start failed: {e}")
            self._oracle = None

    async def _subscribe_clob_tokens(self) -> None:
        """Subscribed Token-IDs aus eigener Discovery auf dem CLOB WS."""
        try:
            token_ids = self.discovery.get_all_token_ids()
            if token_ids and self._clob_ws:
                self._clob_ws.subscribe(token_ids)
                logger.info(f"ODA: {len(token_ids)} Token-IDs auf CLOB WS subscribed (eigene Discovery)")
        except Exception as e:
            logger.warning(f"ODA: Token subscription failed: {e}")

    async def _subscription_refresh_loop(self) -> None:
        """Aktualisiert CLOB WS Subscriptions alle 60s fuer neue Windows."""
        while self._running:
            try:
                await asyncio.sleep(60)
                await self._subscribe_clob_tokens()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _session_warmer(self) -> None:
        """Haelt die CLOB aiohttp Session warm — kein TLS-Penalty bei echten Orders.

        Sendet alle 45s einen leichtgewichtigen GET /time an den CLOB.
        Erster Call waermt die Session auf, folgende halten TCP Keep-Alive aktiv.
        """
        from core.executor import PolymarketExecutor
        await asyncio.sleep(5)  # Warte auf Executor-Init

        while self._running:
            try:
                session = await PolymarketExecutor._get_async_session()
                t0 = time.perf_counter()
                async with session.get("https://clob.polymarket.com/time") as resp:
                    await resp.text()
                ms = (time.perf_counter() - t0) * 1000
                logger.debug(f"ODA Session Warm: CLOB ping {ms:.0f}ms")
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(45)

    async def _track_markout(self, trade_id: str, asset: str,
                             entry_binance_price: float) -> None:
        """Fire-and-forget: Misst BINANCE-Preisentwicklung nach Fill.

        Vergleicht Binance-Preis bei Entry mit Binance-Preis nach 1s, 5s, 10s.
        NICHT Polymarket-Token vs Binance (verschiedene Preis-Domaenen!).
        """
        try:
            symbol = f"{asset}/USDT"
            markouts = {}
            # DELTAS: sleep(1)=1s total, sleep(4)=5s total, sleep(5)=10s total
            for sleep_delta, label in [(1, "1s"), (4, "5s"), (5, "10s")]:
                await asyncio.sleep(sleep_delta)
                if not self._running or not self._oracle:
                    break
                tick = self._oracle.get_latest(symbol)
                if tick and entry_binance_price > 0:
                    pct = (tick.mid - entry_binance_price) / entry_binance_price * 100
                    markouts[label] = round(pct, 4)
            if markouts:
                logger.info(f"ODA MARKOUT {trade_id}: {markouts}")
        except asyncio.CancelledError:
            return  # Sauberer Shutdown — Task wird vom Event-Loop abgebrochen
        except Exception as e:
            logger.debug(f"ODA MARKOUT {trade_id} error: {e}")

    async def shutdown(self) -> None:
        self._running = False
        try:
            await self.discovery.stop()
        except Exception:
            pass
        if self._clob_ws:
            await self._clob_ws.stop()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info(f"Oracle Delay Arb beendet. {self._trade_count} Snipes ausgefuehrt.")

    # ═══════════════════════════════════════════════════════════════
    # MAIN LOOP — Window-Close Detection + Sniping
    # ═══════════════════════════════════════════════════════════════

    def _compute_next_window_closes(self) -> list[dict]:
        """Berechnet Window-Close-Zeiten fuer 5m UND 15m Intervalle."""
        now = time.time()
        results = []

        for interval in [300, 900]:  # 5m + 15m
            tf = "5m" if interval == 300 else "15m"
            base_ts = int(now // interval) * interval
            for offset in range(-2, 2):
                start_ts = base_ts + offset * interval
                end_ts = start_ts + interval
                seconds_since_close = now - end_ts
                seconds_to_close = end_ts - now

                for asset in ["btc", "eth"]:
                    slug = f"{asset}-updown-{tf}-{start_ts}"
                    results.append({
                        "slug": slug,
                        "asset": asset.upper(),
                        "timeframe": tf,
                        "interval": interval,
                        "window_start_ts": start_ts,
                        "window_end_ts": end_ts,
                        "seconds_since_close": seconds_since_close,
                        "seconds_to_close": seconds_to_close,
                    })
        return results

    async def _main_loop(self) -> None:
        """Praezisions-Timer Architektur: Plant dedizierte Tasks fuer jeden Window-Close.

        Kein Polling-Loop. Stattdessen:
        1. Scannt alle 5s nach kommenden Windows
        2. Plant fuer jedes Window einen asyncio.sleep-Timer bis exakt window_end_ts
        3. Timer wacht auf → liest RAM-Preis → feuert Pre-Signed Order → 0ms CPU
        """
        logger.info("ODA Main-Loop gestartet (PRAEZISIONS-TIMER + PRE-SIGN)")

        while self._running:
            try:
                windows = self._compute_next_window_closes()

                for w in windows:
                    slug = w["slug"]
                    secs_to = w["seconds_to_close"]

                    # Ueberspringe bereits geschlossene oder schon geplante Windows
                    if secs_to < 0:
                        continue
                    if slug in self._scheduled_windows:
                        continue
                    if slug in self._sniped_windows:
                        continue
                    if secs_to > 310:
                        continue  # 5min + 10s Puffer (war 120s — zu kurz fuer volle Kerze)

                    # Token-IDs holen + auf CLOB WS subscriben
                    token_ids = await self._get_token_ids(slug, w["asset"])
                    if not token_ids:
                        continue
                    up_tid, down_tid, condition_id = token_ids

                    # CLOB WS subscriben + Re-Subscribe triggern
                    if self._clob_ws:
                        self._clob_ws.subscribe([up_tid, down_tid])
                        # Verifiziere: Haben wir Orderbuch-Daten fuer beide Tokens?
                        up_book = self._clob_ws.get_book(up_tid)
                        dn_book = self._clob_ws.get_book(down_tid)
                        has_up = up_book and up_book.best_ask > 0
                        has_dn = dn_book and dn_book.best_ask > 0
                        if not has_up or not has_dn:
                            logger.debug(f"ODA: Orderbuch leer fuer {w['asset']} — WS re-subscribe triggered")

                    # Snapshot: Binance-Preis JETZT speichern (= Window-Start Referenz)
                    # Timestamp mitspeichern fuer measured_s Validation
                    symbol = f"{w['asset']}/USDT"
                    if self._oracle:
                        tick = self._oracle.get_latest(symbol)
                        if tick and tick.mid > 0:
                            self._price_at_window_start[slug] = (tick.mid, time.time())

                    # PLANEN: Dedizierter Timer-Task fuer dieses Window
                    self._scheduled_windows.add(slug)
                    asyncio.create_task(
                        self._window_timer(w, up_tid, down_tid, condition_id),
                        name=f"oda_{slug}",
                    )
                    logger.info(
                        f"ODA SCHEDULED: {w['asset']} {w['timeframe']} | "
                        f"close in {secs_to:.0f}s | {slug[-15:]}"
                    )

            except Exception as e:
                logger.error(f"ODA Scheduler Error: {e}")

            await asyncio.sleep(5)  # Scanne alle 5s nach neuen Windows

    async def _window_timer(
        self, w: dict, up_tid: str, down_tid: str, condition_id: str
    ) -> None:
        """Praezisions-Timer fuer EIN Window. Schlaeft bis exakt window_end_ts.

        Phase 1: Schlaeft bis T-12s → Pre-Sign beide Orders
        Phase 2: Schlaeft bis T+0ms → Liest RAM-Preis → Feuert Order
        """
        slug = w["slug"]
        asset = w["asset"]
        symbol = f"{asset}/USDT"
        end_ts = w["window_end_ts"]

        try:
            # ── PHASE 1: Pre-Sign bei T-12s ──
            pre_sign_time = end_ts - 12
            now = time.time()
            if now < pre_sign_time:
                await asyncio.sleep(pre_sign_time - now)

            if not self._running:
                return

            # Pre-Sign BEIDE Seiten
            pre_price = 0.85  # Optimiert fuer profitable Zone $0.81-$0.89 (war 0.75)
            if self.executor.is_live:
                self.executor.pre_sign_order(up_tid, pre_price, self.trade_size_usd)
                self.executor.pre_sign_order(down_tid, pre_price, self.trade_size_usd)
                logger.info(f"ODA PRE-SIGN: {asset} {w['timeframe']} UP+DOWN @ ${pre_price}")

            # Momentum wird NACH Phase 2 berechnet (braucht current_price von T+0s)

            # ── PHASE 2: Sleep bis T-15ms, dann BURST-FIRE ──
            # Aufwachen 15ms VOR Close — Netzwerk-Laufzeit kompensieren
            now = time.time()
            wake_at = end_ts - 0.015  # T-15ms
            sleep_s = wake_at - now
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

            if not self._running:
                return

            # ═══ BURST-FIRE: Schrotflinten-Muster (5 Schuesse, 5ms Abstand) ═══
            # Eines der Pakete trifft bei T+0ms auf die Matching-Engine
            t0 = time.perf_counter()

            # 1. Winner bestimmen (einmalig, <1µs)
            current_price = 0.0
            tick_age_ms = 0.0
            entry_binance_price = 0.0
            if self._oracle:
                tick = self._oracle.get_latest(symbol)
                if tick:
                    current_price = tick.mid
                    entry_binance_price = tick.mid
                    tick_age_ms = (time.time() - tick.timestamp) * 1000

            if current_price <= 0:
                logger.info(f"ODA Skip: {asset} — no price data")
                return

            # ═══ PRICE CHANGE: Snapshot-basiert (T-310s → T+0s) ═══
            # NICHT get_momentum() verwenden — Tick-Buffer (maxlen=500) ist zu klein
            # fuer 300s bei ~50-200 Ticks/s. Stattdessen: zuverlaessiger Snapshot
            # aus Schedule-Time (~T-310s), gespeichert in _price_at_window_start.
            snapshot = self._price_at_window_start.get(slug)
            if snapshot is None:
                logger.info(f"ODA Skip: {asset} — no start_price snapshot for {slug[-15:]}")
                return

            start_price, snapshot_ts = snapshot
            measured_s = time.time() - snapshot_ts

            if start_price <= 0:
                logger.info(f"ODA Skip: {asset} — start_price invalid")
                return

            price_change_pct = (current_price - start_price) / start_price * 100

            # Stale-Detection: wenn start_price == current_price exakt → wahrscheinlich Bug
            if start_price == current_price:
                logger.warning(
                    f"ODA STALE: {asset} start_price == current_price (${start_price:.2f}) "
                    f"after {measured_s:.0f}s — possible stale snapshot"
                )

            if current_price > start_price:
                winner = "UP"
                winner_tid = up_tid
            else:
                winner = "DOWN"
                winner_tid = down_tid

            # ═══ ANTI-NOISE GUARD (Deadzone) ═══
            # Bei < 0.05% Delta ist Winner-Bestimmung Rauschen → UMA Oracle unberechenbar
            if abs(price_change_pct) < self.deadzone_pct:
                logger.info(
                    f"ODA CANCEL: {asset} | Price movement too small "
                    f"({price_change_pct:+.4f}% < {self.deadzone_pct}%) -> UMA Oracle Trap avoided"
                )
                return

            # ═══ SIGNAL-TIER + REGIME (Measurement Layer) ═══
            abs_delta = abs(price_change_pct)
            self._recent_deltas.append(abs_delta)
            median_delta = sorted(self._recent_deltas)[len(self._recent_deltas) // 2] if len(self._recent_deltas) >= 3 else 0.05

            if abs_delta >= 0.20 and tick_age_ms < 150:
                signal_tier = "STRONG"
            elif abs_delta >= 0.10 and tick_age_ms < 300:
                signal_tier = "MID"
            else:
                signal_tier = "LOW"

            if abs_delta > 0.15 and abs_delta > median_delta * 2:
                regime_tag = "HIGH_VOL"
            elif abs_delta > median_delta:
                regime_tag = "MID_VOL"
            else:
                regime_tag = "LOW_VOL"

            # Spread aus CLOB Book (wenn verfügbar)
            spread_pct = 0.0
            best_bid_at_event = 0.0
            if self._clob_ws:
                book = self._clob_ws.get_book(winner_tid)
                if book and book.best_bid > 0 and book.best_ask > 0:
                    best_bid_at_event = book.best_bid
                    spread_pct = (book.best_ask - book.best_bid) / book.best_ask * 100

            # Dedup
            if slug in self._sniped_windows:
                return

            # Auto-Resolve: Trades aelter als 10 Min → resolved (Sicherheitsnetz)
            now_gc = time.time()
            for t in self._trades:
                if not t.resolved and (now_gc - t.timestamp) > 600:
                    t.resolved = True
                    logger.debug(f"ODA GC: {t.trade_id} auto-resolved (>10min)")

            active = sum(1 for t in self._trades if not t.resolved)
            if active >= self.max_concurrent:
                logger.warning(
                    f"ODA BLOCKED: max concurrent ({self.max_concurrent}) erreicht "
                    f"({active} active). Ueberspringe {asset} {slug[-15:]}"
                )
                return
            self._sniped_windows.add(slug)

            decision_us = (time.perf_counter() - t0) * 1_000_000

            event_progress_ms = (time.perf_counter() - t0) * 1000  # Approx, ±10-50ms drift
            logger.info(
                f"ODA BURST: {asset} {winner} | "
                f"Binance {start_price:.2f}->{current_price:.2f} ({price_change_pct:+.4f}%) | "
                f"measured={measured_s:.0f}s | tick_age={tick_age_ms:.1f}ms | signal={signal_tier} | "
                f"spread={spread_pct:.1f}% | {regime_tag} | firing 5 FAK shots..."
            )

            # 2. BURST-FIRE: 5 FAK Orders, Fire-and-Forget, 5ms Abstand
            #    Die Matching-Engine lehnt zu fruehe ab, der "goldene Schuss" trifft
            window_data = {
                "slug": slug,
                "asset": asset,
                "question": f"{asset} Up or Down {w['timeframe']}",
                "condition_id": condition_id,
                "up_token_id": up_tid,
                "down_token_id": down_tid,
            }

            filled = False
            for shot in range(5):
                try:
                    # Lese Ask aus 3-Tier Fallback (RAM → Discovery → REST)
                    shot_ask = 0.0

                    # Tier 1: CLOB WS RAM Orderbuch (O(1), <1µs)
                    if self._clob_ws:
                        book = self._clob_ws.get_book(winner_tid)
                        if book and book.best_ask > 0:
                            shot_ask = book.best_ask

                    # Tier 2: Eigene Discovery Cache
                    if shot_ask <= 0:
                        try:
                            for s, wnd in self.discovery.windows.items():
                                if wnd.condition_id == condition_id:
                                    shot_ask = wnd.up_best_ask if winner == "UP" else wnd.down_best_ask
                                    break
                        except Exception:
                            pass

                    # Tier 3: REST Fetch (langsam aber zuverlaessig, nur Shot#0)
                    if shot_ask <= 0 and shot == 0:
                        shot_ask = await self._fetch_ask(winner_tid)

                    if shot_ask <= 0 or shot_ask > self.max_entry_price:
                        if shot == 0:
                            logger.info(f"ODA Shot#{shot}: {asset} ask=${shot_ask:.3f} — no fill available")
                            logger.info(f"ODA SKIP: ask=${shot_ask:.3f} cap=${self.max_entry_price} | CAP_EXCEEDED")
                        break

                    if shot_ask < self.min_entry_price:
                        if shot == 0:
                            logger.info(f"ODA SKIP: ask=${shot_ask:.3f} floor=${self.min_entry_price} | FLOOR_EXCEEDED")
                        break

                    edge_pct = (1.0 / shot_ask - 1.0) * 100
                    shot_t = (time.perf_counter() - t0) * 1000

                    # FIRE!
                    await self._execute_snipe(window_data, winner, winner_tid, shot_ask,
                                              entry_binance_price=entry_binance_price,
                                              tick_age_ms=tick_age_ms,
                                              price_change_pct=price_change_pct,
                                              signal_tier=signal_tier,
                                              regime_tag=regime_tag,
                                              spread_pct=spread_pct)
                    filled = True

                    logger.info(
                        f"ODA Shot#{shot}: {asset} {winner} @ ${shot_ask:.3f} | "
                        f"edge={edge_pct:.1f}% | t={shot_t:.1f}ms"
                    )
                    logger.info(f"ODA FILL: ask=${shot_ask:.3f} cap=${self.max_entry_price} edge={edge_pct:.1f}% signal={signal_tier} spread={spread_pct:.1f}% | FILLED")
                    break  # Erster erfolgreicher Shot genuegt

                except Exception as e:
                    err_str = str(e)
                    if "no orders found to match" in err_str.lower():
                        # FAK rejected — kein Match. Naechster Shot.
                        reject_ms = (time.perf_counter() - t0) * 1000
                        logger.info(f"ODA EXECUTION_FAIL: Shot#{shot} ask=${shot_ask:.3f} | liq=True | signal={signal_tier} | total={reject_ms:.0f}ms")
                        if shot < 4:
                            await asyncio.sleep(0.005)  # 5ms Pause
                            continue
                    else:
                        logger.warning(f"ODA Shot#{shot} Error: {err_str[:80]}")
                        break

            total_ms = (time.perf_counter() - t0) * 1000
            status = "FILLED" if filled else "NO_FILL"
            logger.info(f"ODA BURST DONE: {slug[-15:]} | {status} | total={total_ms:.1f}ms")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"ODA Timer Error ({slug}): {e}")

    # (Legacy loop code removed — execution happens in _window_timer tasks)

    # ═══════════════════════════════════════════════════════════════
    # WINDOW DISCOVERY
    # ═══════════════════════════════════════════════════════════════

    async def _get_token_ids(self, slug: str, asset: str) -> tuple | None:
        """Holt Token-IDs aus der EIGENEN MarketDiscovery (autark).

        Kein Zugriff auf active_strategies — ODA ist self-sufficient.
        Fallback: Gamma API direkt.
        """
        start_ts = int(slug.split("-")[-1])

        # 1. EIGENE Discovery (self.discovery)
        try:
            for s, w in self.discovery.windows.items():
                if w.asset.upper() == asset and w.up_token_id and w.down_token_id:
                    if abs(w.window_start_ts - start_ts) < 30:
                        return (w.up_token_id, w.down_token_id, w.condition_id)
        except Exception as e:
            logger.warning(f"ODA own Discovery error: {e}")

        # 2. Fallback: Gamma API (korrekte Quelle fuer Crypto Markets)
        if not self._session or self._session.closed:
            logger.warning(f"ODA: No session for fallback — {slug}")
            return None
        try:
            asset_lower = asset.lower()
            asset_name = "bitcoin" if asset_lower == "btc" else "ethereum"
            url = f"https://gamma-api.polymarket.com/markets?closed=false&limit=50"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"ODA Gamma API error: status {resp.status}")
                    return None
                markets = await resp.json()

            for m in markets if isinstance(markets, list) else []:
                q = m.get("question", "").lower()
                if asset_name not in q or "up or down" not in q:
                    continue

                cid = m.get("conditionId", "")
                tokens = m.get("clobTokenIds", [])
                outcomes = m.get("outcomes", [])

                if len(tokens) < 2 or len(outcomes) < 2:
                    continue

                up_tid = ""
                down_tid = ""
                for i, outcome in enumerate(outcomes):
                    ol = outcome.lower() if isinstance(outcome, str) else ""
                    if ol in ("up", "yes") and i < len(tokens):
                        up_tid = tokens[i]
                    elif ol in ("down", "no") and i < len(tokens):
                        down_tid = tokens[i]

                if up_tid and down_tid:
                    logger.info(f"ODA Token-IDs via Gamma API: {asset} up={up_tid[:16]}... dn={down_tid[:16]}...")
                    return (up_tid, down_tid, cid)

            logger.info(f"ODA: No matching market in Gamma API for {asset} {slug[-15:]}")
        except Exception as e:
            logger.warning(f"ODA Gamma API token fetch error: {e}")

        return None

    async def _fetch_active_windows(self) -> list[dict]:
        """Holt aktive Windows aus eigener Discovery."""
        try:
            windows = []
            for slug, w in self.discovery.windows.items():
                windows.append({
                    "slug": slug,
                    "asset": w.asset,
                    "window_end_ts": w.window_end_ts,
                    "up_token_id": w.up_token_id,
                    "down_token_id": w.down_token_id,
                    "condition_id": w.condition_id,
                    "question": w.question,
                    "up_best_ask": w.up_best_ask,
                    "down_best_ask": w.down_best_ask,
                })
            if windows:
                return windows
        except Exception:
            pass
        return await self._fetch_windows_from_api()

    async def _fetch_windows_from_api(self) -> list[dict]:
        """Fallback: Holt Windows direkt von der Polymarket API."""
        if not self._session or self._session.closed:
            return []
        try:
            # Polymarket Gamma API für aktive Crypto Markets
            url = "https://gamma-api.polymarket.com/markets?closed=false&tag=crypto&limit=20"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return []
                markets = await resp.json()

            windows = []
            for m in markets:
                question = m.get("question", "").lower()
                if "up or down" not in question and "above" not in question:
                    continue

                end_date = m.get("endDate", "")
                # Parse simple cases
                slug = m.get("conditionId", "")[:20]
                cid = m.get("conditionId", "")

                # Get token IDs from outcomes
                tokens = m.get("clobTokenIds", [])
                if len(tokens) >= 2:
                    windows.append({
                        "slug": slug,
                        "asset": "BTC" if "bitcoin" in question or "btc" in question else "ETH",
                        "window_end_ts": 0,  # Müssen wir aus dem Slug/endDate parsen
                        "up_token_id": tokens[0] if "up" in question else "",
                        "down_token_id": tokens[1] if "down" in question else "",
                        "condition_id": cid,
                        "question": m.get("question", ""),
                    })
            return windows
        except Exception as e:
            logger.debug(f"ODA Fetch Windows Error: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════
    # WINNER DETERMINATION
    # ═══════════════════════════════════════════════════════════════

    async def _determine_winner(self, window: dict) -> str | None:
        """Bestimmt den Winner anhand des aktuellen Binance-Preises.

        Für 5-Min Up/Down: Preis bei Close > Preis bei Open → UP gewinnt.
        Wir schauen uns die CLOB-Preise an: wenn Up-Ask >= 0.95 → UP hat gewonnen.
        """
        up_ask = window.get("up_best_ask", 0.5)
        down_ask = window.get("down_best_ask", 0.5)

        # Wenn einer der Asks > 0.95, hat diese Seite gewonnen
        if up_ask >= 0.95:
            return "UP"
        if down_ask >= 0.95:
            return "DOWN"

        # Fallback: Wenn Discovery nicht aktuell ist, ASK vom CLOB holen
        up_tid = window.get("up_token_id", "")
        down_tid = window.get("down_token_id", "")

        if up_tid:
            up_price = await self._fetch_ask(up_tid)
            if up_price >= 0.95:
                return "UP"
        if down_tid:
            down_price = await self._fetch_ask(down_tid)
            if down_price >= 0.95:
                return "DOWN"

        return None  # Kann Winner nicht bestimmen

    async def _fetch_ask(self, token_id: str) -> float:
        """Holt den aktuellen Best-Ask vom CLOB."""
        if not self._session or self._session.closed or not token_id:
            return 0.0
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    book = await resp.json()
                    asks = book.get("asks", [])
                    if asks:
                        return float(asks[0].get("price", 0))
            return 0.0
        except Exception:
            return 0.0

    # ═══════════════════════════════════════════════════════════════
    # EXECUTION — Der Snipe
    # ═══════════════════════════════════════════════════════════════

    async def _execute_snipe(
        self, window: dict, winner: str, token_id: str, ask_price: float,
        entry_binance_price: float = 0.0, tick_age_ms: float = 0.0,
        price_change_pct: float = 0.0, signal_tier: str = "LOW",
        regime_tag: str = "", spread_pct: float = 0.0,
    ) -> None:
        """Führt den Oracle Delay Snipe aus."""
        self._trade_count += 1
        trade_id = f"ODA-{self._trade_count:04d}"
        asset = window.get("asset", "?")
        slug = window.get("slug", "?")
        condition_id = window.get("condition_id", "")

        logger.info(
            f"SNIPE #{self._trade_count}: {asset} {winner} @ {ask_price:.3f} | "
            f"${self.trade_size_usd:.2f} | {slug[:30]}"
        )

        trade = SniperTrade(
            trade_id=trade_id,
            slug=slug,
            asset=asset,
            direction=winner,
            entry_price=ask_price,
            size_usd=self.trade_size_usd,
            token_id=token_id,
            condition_id=condition_id,
            timestamp=time.time(),
        )
        self._trades.append(trade)

        # Recent snipes für Dashboard
        self._recent_snipes.appendleft({
            "ts": time.strftime("%H:%M:%S"),
            "trade_id": trade_id,
            "asset": asset,
            "direction": winner,
            "price": round(ask_price, 3),
            "size": self.trade_size_usd,
            "slug": slug[:30],
        })

        # Berechnungen (VOR Order — brauchen wir fuer Journal + Telegram)
        import math
        shares = math.floor(self.trade_size_usd / ask_price)
        fee_pct = 1.80 * 4 * ask_price * (1 - ask_price)  # z.B. 0.99 -> 0.07%
        fee_usd = shares * ask_price * fee_pct / 100
        expected_pnl = shares * (1.0 - ask_price) - fee_usd
        net_ev_pct = (1.0 / ask_price - 1.0) * 100 - fee_pct
        filled = False
        order_latency_ms = 0.0

        # LIVE Order — Async mit ehrlicher Response-Validierung
        if self.executor.is_live and token_id:
            try:
                res = await self.executor.place_order_async(
                    token_id=token_id,
                    side="BUY",
                    price=min(0.99, ask_price),
                    size_usd=self.trade_size_usd,
                    asset=asset,
                    direction=winner,
                )
                if res.success and res.order_id and res.order_id != "unknown":
                    trade.live_order_id = res.order_id
                    trade.live_success = True
                    trade.resolved = True  # Sofort resolved — PnL kommt vom Redeemer
                    filled = True
                    order_latency_ms = res.latency_ms
                    logger.info(
                        f"SNIPE CONFIRMED: {trade_id} — {res.order_id} | "
                        f"{res.latency_ms:.0f}ms | BLOCKCHAIN-VERIFIZIERT"
                    )
                    # Markout Tracking — Fire-and-Forget (blockiert NICHT den Trading-Loop)
                    if entry_binance_price > 0:
                        asyncio.create_task(
                            self._track_markout(trade_id, asset, entry_binance_price)
                        )
                else:
                    error = res.error or "unknown error"
                    trade.resolved = True  # Auch bei Reject resolved (Slot freigeben)
                    logger.error(f"SNIPE REJECTED: {trade_id} — {error} ({res.latency_ms:.0f}ms)")
            except Exception as e:
                trade.resolved = True  # Bei Exception resolved (Slot freigeben)
                import traceback
                logger.error(
                    f"SNIPE EXCEPTION: {trade_id} — {type(e).__name__}: {e}\n"
                    f"{traceback.format_exc()}"
                )

        # Telegram Alert — nur echte Daten, keine Annahmen
        fill_icon = "✅ FILLED" if filled else "❌ REJECTED"
        mode = "LIVE" if trade.live_order_id else "PAPER"
        latency = order_latency_ms

        # Window-Zeiten aus Slug extrahieren (z.B. btc-updown-5m-1775838000)
        import datetime
        timeframe = "5m"
        window_str = ""
        try:
            slug_parts = slug.split("-")
            timeframe = slug_parts[2] if len(slug_parts) >= 3 else "5m"
            window_start_ts = int(slug_parts[-1])
            interval = 300 if timeframe == "5m" else 900
            dt_start = datetime.datetime.fromtimestamp(window_start_ts, tz=datetime.timezone(datetime.timedelta(hours=-4)))
            dt_end = datetime.datetime.fromtimestamp(window_start_ts + interval, tz=datetime.timezone(datetime.timedelta(hours=-4)))
            window_str = f"{dt_start.strftime('%I:%M')}-{dt_end.strftime('%I:%M%p')} ET"
        except Exception:
            window_str = timeframe

        pm_link = f"https://polymarket.com/markets/crypto/{asset.lower()}"

        asyncio.create_task(telegram.send_alert(
            f"🎯 <b>SNIPE #{self._trade_count} {fill_icon}</b>\n"
            f"{asset} {winner} @ ${ask_price:.3f} | ${self.trade_size_usd:.2f}\n"
            f"⏱ {timeframe} | {window_str}\n"
            f"Edge: {net_ev_pct:.1f}% | Fee: {fee_pct:.2f}% | {signal_tier}\n"
            f"⚡ {latency:.0f}ms | tick: {tick_age_ms:.0f}ms | Δ{price_change_pct:+.3f}%\n"
            f"{mode} | <a href=\"{pm_link}\">Polymarket</a>"
        ))

        # Journal — NUR open Event. PnL=0 bis AutoRedeemer on-chain bestaetigt.
        raw_edge = (1.0 / ask_price - 1.0) * 100 if ask_price > 0 else 0.0
        self.journal.record_open(TradeRecord(
            trade_id=trade_id,
            asset=asset,
            direction=winner,
            entry_ts=time.time(),
            window_slug=slug,
            market_question=window.get("question", f"{asset} Up or Down 5m")[:60],
            executed_price=ask_price,
            size_usd=self.trade_size_usd,
            shares=float(shares),
            fee_pct=fee_pct,
            fee_usd=fee_usd,
            net_ev_pct=net_ev_pct,
            pnl_usd=0.0,              # Kein PnL bis Redeem!
            pnl_pct=0.0,              # Kein PnL bis Redeem!
            outcome_correct=False,     # Nicht bestaetigt bis Redeem!
            signal_to_order_ms=tick_age_ms,  # Alter des Binance-Ticks bei Decision
            order_type="oracle_delay_arb",
            live_order_id=trade.live_order_id,
            live_order_success=trade.live_success,
            condition_id=condition_id,
            # Forensik-Metriken (fuer PnL-Attribution + Signal-Tier Analyse)
            raw_edge_pct=raw_edge,
            tick_age_ms=tick_age_ms,
            momentum_pct=price_change_pct,
            oracle_price_entry=entry_binance_price,
            confidence_score={"STRONG": 3, "MID": 2, "LOW": 1}.get(signal_tier, 0),
            regime_tag=regime_tag,
            spread_pct=spread_pct,
            fill_type="FILLED" if filled else "",
            transit_latency_ms=order_latency_ms,
        ))

    async def _check_fill(self, order_id: str) -> str:
        """Prüft ob eine Order gefüllt wurde via CLOB API."""
        if not self.executor.is_live or not self.executor._client:
            return "NO_CLIENT"
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            order = await loop.run_in_executor(None, self.executor._client.get_order, order_id)
            if order:
                status = order.get("status", "unknown")
                filled = float(order.get("size_matched", 0))
                total = float(order.get("original_size", 0))
                if status == "MATCHED" or (filled > 0 and filled >= total * 0.9):
                    return "FILLED"
                elif filled > 0:
                    return f"PARTIAL ({filled}/{total})"
                else:
                    return "UNFILLED"
            return "UNKNOWN"
        except Exception as e:
            logger.debug(f"Fill check error: {e}")
            return f"ERROR: {e}"

    async def _cancel_order(self, order_id: str) -> bool:
        """Cancelt eine unfilled Order."""
        if not self.executor.is_live or not self.executor._client:
            return False
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.executor._client.cancel, order_id)
            logger.info(f"ODA: Order {order_id[:16]}... cancelled")
            return True
        except Exception as e:
            logger.debug(f"Cancel error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════════════

    async def _status_loop(self) -> None:
        while self._running:
            active = sum(1 for t in self._trades if not t.resolved)
            logger.info(
                f"ODA STATUS | Snipes: {self._trade_count} | Active: {active} | "
                f"Size: ${self.trade_size_usd} | Range: {self.min_entry_price}-{self.max_entry_price} | "
                f"Deadzone: {self.deadzone_pct}%"
            )
            await asyncio.sleep(60)

    def get_status(self) -> dict:
        active = sum(1 for t in self._trades if not t.resolved)
        return {
            "strategy": self.STRATEGY_NAME,
            "running": self._running,
            "snipes_total": self._trade_count,
            "snipes_active": active,
            "recent_snipes": list(self._recent_snipes),
            "sniped_windows": len(self._sniped_windows),
            "config": {
                "trade_size_usd": self.trade_size_usd,
                "min_entry_price": self.min_entry_price,
                "max_entry_price": self.max_entry_price,
                "delay_after_close_s": self.delay_after_close_s,
                "max_concurrent": self.max_concurrent,
            },
            "clob_ws": self._clob_ws.status() if self._clob_ws else {"connected": False},
        }


# Auto-Register
register_strategy(OracleDelayArbStrategy.STRATEGY_NAME, OracleDelayArbStrategy)
