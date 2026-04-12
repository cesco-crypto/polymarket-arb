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

    def __init__(self, settings: Settings, **kwargs) -> None:
        self.settings = settings
        self.executor = PolymarketExecutor(settings)
        self.journal = TradeJournal()

        # LearnMachine: per DI von main.py, Fallback fuer Standalone-Tests
        if "learn_machine" in kwargs and kwargs["learn_machine"] is not None:
            self._learn = kwargs["learn_machine"]
        else:
            from core.learn_machine import LearnMachine
            self._learn = LearnMachine()

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

        # Gamma Validation Cache: slug -> (up_tid, down_tid, cid) oder "INVALID"
        # Einmal pro Slug validiert, danach nur noch RAM-Read
        self._gamma_validated: dict[str, tuple | str] = {}

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
                self._daily_report_loop(),
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
        """Subscribed NUR BTC 5m Window-Tokens auf dem CLOB WS.

        Learn-Test Isolation: Nur BTC 5m. Kein ETH, kein 15m.
        Reduziert WS-Footprint auf 2-4 Tokens → stabilere Books.
        """
        try:
            active_tokens = []
            now = time.time()
            for w in self._compute_next_window_closes():
                # LEARN-TEST: Nur BTC 5m subscriben
                if w.get("asset", "").upper() != "BTC":
                    continue
                if w.get("timeframe", "") != "5m":
                    continue
                secs = w["seconds_to_close"]
                if 0 < secs <= 310:
                    tids = await self._get_token_ids(w["slug"], w["asset"])
                    if tids:
                        active_tokens.extend([tids[0], tids[1]])

            active_tokens = list(set(active_tokens))

            if self._clob_ws:
                if active_tokens:
                    self._clob_ws.set_active_tokens(active_tokens)
                logger.info(f"ODA: {len(active_tokens)} BTC-5m Token-IDs auf CLOB WS")
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

    async def _daily_report_loop(self) -> None:
        """Sendet Daily Report via Telegram um 22:00 UTC."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Alle 5min pruefen
                await self._learn.check_daily_report()
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
                        if tick and tick.mid > 0 and (time.time() - tick.timestamp) < 5.0:
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
        _tf = w.get("timeframe", "5m")

        # ═══ LEARN-TEST ISOLATION: Nur BTC 5m aktiv ═══
        # Alles andere (ETH, 15m, etc.) wird komplett stillgelegt.
        # Kein Legacy-Burst, kein Echtgeld, kein Rauschen.
        if asset != "BTC" or _tf != "5m":
            return

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

            # ── WS-ASK-TIMELINE LOGGER (diagnostisch, T-12s bis T+5s, 100ms Intervall) ──
            if "5m" in slug:
                asyncio.create_task(
                    self._log_ws_ask_timeline(slug, asset, end_ts, up_tid, down_tid, symbol)
                )

            # ═══════════════════════════════════════════════════════════════
            # PHASE 1.5: BTC-ONLY PRECLOSE TEST (T-12s → T-8s → Entry)
            # Learn-Test: früher Entry mit Richtungsstabilitäts-Filter
            # ═══════════════════════════════════════════════════════════════
            # ═══ PHASE 1.5: BTC 5m PRECLOSE TEST ═══
            # Einziger aktiver Pfad. Kein Legacy-Fallback.
            await self._preclose_test(
                slug, asset, symbol, end_ts,
                up_tid, down_tid, condition_id, w
            )
            # Nach Preclose (FIRE oder SKIP): immer return.
            # Kein T+0ms Legacy-Burst.
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

            # ═══ TOKEN-PATH DIAGNOSTIK (Phase 1: Sichtbarkeit, kein Fix) ═══
            _tf = w.get("timeframe", "?")
            _diag = {
                "slug": slug, "asset": asset, "tf": _tf,
                "winner": winner,
                "up_tid": up_tid[:12] if up_tid else "NONE",
                "down_tid": down_tid[:12] if down_tid else "NONE",
                "winner_tid": winner_tid[:12] if winner_tid else "NONE",
                "winner_is_up": winner_tid == up_tid,
            }

            # WS-Book Status fuer BEIDE Seiten
            _ws_up_ask = 0.0
            _ws_dn_ask = 0.0
            _ws_has_up = False
            _ws_has_dn = False
            if self._clob_ws:
                _ub = self._clob_ws.get_book(up_tid)
                _db = self._clob_ws.get_book(down_tid)
                if _ub and _ub.best_ask > 0:
                    _ws_up_ask = _ub.best_ask
                    _ws_has_up = True
                if _db and _db.best_ask > 0:
                    _ws_dn_ask = _db.best_ask
                    _ws_has_dn = True

            # Discovery-Cache Status fuer BEIDE Seiten
            _disc_up_ask = 0.0
            _disc_dn_ask = 0.0
            try:
                _dw = self.discovery.windows.get(slug)
                if _dw:
                    _disc_up_ask = _dw.up_best_ask
                    _disc_dn_ask = _dw.down_best_ask
            except Exception:
                pass

            _diag.update({
                "ws_has_up": _ws_has_up, "ws_has_dn": _ws_has_dn,
                "ws_up_ask": round(_ws_up_ask, 3), "ws_dn_ask": round(_ws_dn_ask, 3),
                "disc_up_ask": round(_disc_up_ask, 3), "disc_dn_ask": round(_disc_dn_ask, 3),
            })

            # Diagnostische Warnungen (kein Skip, kein Fix — nur sichtbar machen)
            if winner == "UP" and winner_tid != up_tid:
                logger.warning(f"ODA DIAG-WARN: winner=UP but winner_tid != up_tid! | {slug}")
            if winner == "DOWN" and winner_tid != down_tid:
                logger.warning(f"ODA DIAG-WARN: winner=DOWN but winner_tid != down_tid! | {slug}")

            _winner_ws_ask = _ws_up_ask if winner == "UP" else _ws_dn_ask
            _loser_ws_ask = _ws_dn_ask if winner == "UP" else _ws_up_ask
            if _winner_ws_ask <= 0.01 and _loser_ws_ask >= 0.90:
                logger.warning(
                    f"ODA DIAG-SUSPECT: {asset} {_tf} winner_ask=${_winner_ws_ask:.3f} "
                    f"loser_ask=${_loser_ws_ask:.3f} — possible side inversion! | {slug}"
                )
            if not _ws_has_up or not _ws_has_dn:
                logger.info(
                    f"ODA DIAG-WS: {asset} {_tf} ws_up={_ws_has_up}(${_ws_up_ask:.3f}) "
                    f"ws_dn={_ws_has_dn}(${_ws_dn_ask:.3f}) — fallback likely | {slug}"
                )
            if _disc_up_ask <= 0.01 or _disc_dn_ask <= 0.01:
                logger.info(
                    f"ODA DIAG-DISC: {asset} {_tf} disc_up=${_disc_up_ask:.3f} "
                    f"disc_dn=${_disc_dn_ask:.3f} — stale/empty discovery | {slug}"
                )

            # ═══ ANTI-NOISE GUARD (Deadzone) ═══
            # Bei < 0.05% Delta ist Winner-Bestimmung Rauschen → UMA Oracle unberechenbar
            if abs(price_change_pct) < self.deadzone_pct:
                logger.info(
                    f"ODA CANCEL: {asset} | Price movement too small "
                    f"({price_change_pct:+.4f}% < {self.deadzone_pct}%) -> UMA Oracle Trap avoided"
                )
                self._learn.log_burst(slug, asset, action="SKIP", abs_delta=abs(price_change_pct),
                                      tick_age_ms=tick_age_ms, skip_reason="deadzone")
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

            # Guardrail Check
            should_skip, skip_reason = self._learn.should_skip()
            if should_skip:
                logger.warning(f"ODA GUARDRAIL SKIP: {asset} | {skip_reason}")
                self._learn.log_burst(slug, asset, action="SKIP", abs_delta=abs(price_change_pct),
                                      tick_age_ms=tick_age_ms, best_ask=0, spread_pct=spread_pct,
                                      skip_reason=skip_reason)
                return

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
                    _tier_used = "none"

                    # Tier 1: CLOB WS RAM Orderbuch (O(1), <1µs)
                    if self._clob_ws:
                        book = self._clob_ws.get_book(winner_tid)
                        if book and book.best_ask > 0:
                            shot_ask = book.best_ask
                            _tier_used = "ws"

                    # Tier 2: Eigene Discovery Cache (mit Stale-Guard)
                    _raw_disc_ask = 0.0
                    if shot_ask <= 0:
                        try:
                            for s, wnd in self.discovery.windows.items():
                                if wnd.condition_id == condition_id:
                                    _raw_disc_ask = wnd.up_best_ask if winner == "UP" else wnd.down_best_ask
                                    if _raw_disc_ask > 0.02:
                                        shot_ask = _raw_disc_ask
                                        _tier_used = "discovery"
                                    else:
                                        # HOTFIX STALE-GUARD: Post-Resolution Loser-Preise
                                        # ($0.001-$0.01) verwerfen, weiter zu Tier 3
                                        _tier_used = "discovery_stale"
                                        logger.info(
                                            f"ODA STALE-GUARD: {asset} {_tf} "
                                            f"disc_ask=${_raw_disc_ask:.3f} rejected "
                                            f"(<=0.02) | {slug}"
                                        )
                                    break
                        except Exception:
                            pass

                    # Tier 3: REST Fetch (langsam aber zuverlaessig, nur Shot#0)
                    if shot_ask <= 0 and shot == 0:
                        shot_ask = await self._fetch_ask(winner_tid)
                        if shot_ask > 0:
                            _tier_used = "rest"

                    # ═══ TOKEN-PATH DEBUG SNAPSHOT (nur Shot#0, ~30µs) ═══
                    if shot == 0:
                        _diag["tier_used"] = _tier_used
                        _diag["shot_ask"] = round(shot_ask, 3)
                        _diag["raw_disc_ask"] = round(_raw_disc_ask, 3)
                        _diag["disc_rejected"] = _tier_used == "discovery_stale"
                        _diag["ts"] = round(time.time(), 3)
                        try:
                            _dbg_path = Path("data/token_path_debug.jsonl")
                            _dbg_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(_dbg_path, "a") as _dbg_f:
                                _dbg_f.write(json.dumps(_diag) + "\n")
                        except Exception:
                            pass
                        # Log-Zeile fuer Screen-Sichtbarkeit
                        logger.info(
                            f"ODA DIAG: {asset} {_tf} {winner} | "
                            f"ask=${shot_ask:.3f} tier={_tier_used} | "
                            f"ws_up=${_ws_up_ask:.3f} ws_dn=${_ws_dn_ask:.3f} | "
                            f"disc_up=${_disc_up_ask:.3f} disc_dn=${_disc_dn_ask:.3f} | "
                            f"winner_is_up={winner_tid == up_tid}"
                        )

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
                    # Learn Machine: FILL loggen (PnL wird beim Redeem aktualisiert)
                    self._learn.log_burst(slug, asset, action="FIRE", abs_delta=abs(price_change_pct),
                                          tick_age_ms=tick_age_ms, best_ask=shot_ask, spread_pct=spread_pct,
                                          outcome="FILLED", fill_price=shot_ask)

                    # FILL_VALIDATION Snapshot — 8 Felder, alle lokal, kein Netzwerk-Call
                    try:
                        last_trade = self._trades[-1] if self._trades else None
                        _fv = {
                            "ts": round(time.time(), 3),
                            "trade_id": last_trade.trade_id if last_trade else f"ODA-{self._trade_count:04d}",
                            "slug": slug,
                            "condition_id": condition_id,
                            "token_id": winner_tid,
                            "asset": asset,
                            "fill_price": round(shot_ask, 3),
                            "live_order_id": last_trade.live_order_id if last_trade else "",
                        }
                        _fv_path = Path("data/fill_validation.jsonl")
                        _fv_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(_fv_path, "a") as _fv_f:
                            _fv_f.write(json.dumps(_fv) + "\n")
                    except Exception:
                        pass  # Snapshot darf nie den Trading-Loop blockieren

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

            # Learn Machine: Burst-Outcome loggen
            if not filled:
                # Bestimme Outcome-Typ
                if shot_ask > self.max_entry_price:
                    burst_outcome = "CAP_EXCEEDED"
                elif shot_ask < self.min_entry_price and shot_ask > 0:
                    burst_outcome = "FLOOR_EXCEEDED"
                elif shot_ask <= 0:
                    burst_outcome = "NO_LIQUIDITY"
                else:
                    burst_outcome = "EXECUTION_FAIL"
                self._learn.log_burst(slug, asset, action="FIRE", abs_delta=abs(price_change_pct),
                                      tick_age_ms=tick_age_ms, best_ask=shot_ask, spread_pct=spread_pct,
                                      outcome=burst_outcome)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"ODA Timer Error ({slug}): {e}")

    # (Legacy loop code removed — execution happens in _window_timer tasks)

    # ═══════════════════════════════════════════════════════════════
    # WS-ASK TIMELINE LOGGER (rein diagnostisch)
    # ═══════════════════════════════════════════════════════════════

    async def _log_ws_ask_timeline(
        self, slug: str, asset: str, end_ts: float,
        up_tid: str, down_tid: str, symbol: str,
    ) -> None:
        """Diagnostischer WS-Ask-Verlauf: T-12s bis T+5s, alle 100ms.

        Schreibt Snapshots in data/ws_ask_timeline.jsonl.
        Rein diagnostisch — kein Einfluss auf Trading-Logik.
        Nur fuer 5m-Windows (15m ignoriert).
        """
        _TL_PATH = Path("data/ws_ask_timeline.jsonl")

        try:
            _TL_PATH.parent.mkdir(parents=True, exist_ok=True)
            log_start = end_ts - 12  # Start bei T-12s (= Pre-Sign Zeitpunkt)
            log_end = end_ts + 5     # Ende bei T+5s

            # Explizit subscriben damit WS diese Tokens waehrend des
            # gesamten Logging-Fensters trackt (additiv, verdraengt nichts)
            if self._clob_ws:
                self._clob_ws.subscribe([up_tid, down_tid])

            # Warte bis Logging-Fenster beginnt
            now = time.time()
            if now < log_start:
                await asyncio.sleep(log_start - now)

            # Kurze Pause damit WS nach Subscribe ein erstes Book liefern kann
            await asyncio.sleep(0.5)

            samples = []
            _resub_counter = 0
            while time.time() < log_end and self._running:
                now = time.time()
                secs_to_close = end_ts - now

                # Alle 50 Samples (~5s): Re-Subscribe falls set_active_tokens()
                # unsere Tokens zwischenzeitlich verdraengt hat
                _resub_counter += 1
                if _resub_counter % 50 == 0 and self._clob_ws:
                    self._clob_ws.subscribe([up_tid, down_tid])

                # WS Books lesen (O(1), <1µs)
                up_ask = 0.0
                up_bid = 0.0
                dn_ask = 0.0
                dn_bid = 0.0
                if self._clob_ws:
                    ub = self._clob_ws.get_book(up_tid)
                    db = self._clob_ws.get_book(down_tid)
                    if ub:
                        up_ask = ub.best_ask
                        up_bid = ub.best_bid
                    if db:
                        dn_ask = db.best_ask
                        dn_bid = db.best_bid

                # Binance-Preis fuer Winner-Bestimmung
                binance_mid = 0.0
                if self._oracle:
                    tick = self._oracle.get_latest(symbol)
                    if tick:
                        binance_mid = tick.mid

                # Winner aus Binance-Sicht (gleiche Logik wie _window_timer)
                snapshot = self._price_at_window_start.get(slug)
                inferred_winner = "?"
                if snapshot and binance_mid > 0:
                    start_p = snapshot[0]
                    if start_p > 0:
                        inferred_winner = "UP" if binance_mid > start_p else "DOWN"

                winner_ask = up_ask if inferred_winner == "UP" else dn_ask
                loser_ask = dn_ask if inferred_winner == "UP" else up_ask

                sample = {
                    "ts": round(now, 3),
                    "slug": slug,
                    "asset": asset,
                    "secs_to_close": round(secs_to_close, 2),
                    "up_ask": round(up_ask, 4),
                    "up_bid": round(up_bid, 4),
                    "dn_ask": round(dn_ask, 4),
                    "dn_bid": round(dn_bid, 4),
                    "winner": inferred_winner,
                    "winner_ask": round(winner_ask, 4),
                    "loser_ask": round(loser_ask, 4),
                    "binance_mid": round(binance_mid, 2),
                }
                samples.append(sample)

                await asyncio.sleep(0.1)  # 100ms Intervall

            # Batch-Write: alle Samples auf einmal (1 File-Open statt 170x)
            if samples:
                try:
                    with open(_TL_PATH, "a") as f:
                        for s in samples:
                            f.write(json.dumps(s) + "\n")
                    logger.info(
                        f"ODA WS-TIMELINE: {asset} {slug[-15:]} | "
                        f"{len(samples)} samples | "
                        f"winner_ask range: "
                        f"${min(s['winner_ask'] for s in samples):.3f}"
                        f"-${max(s['winner_ask'] for s in samples):.3f}"
                    )
                except Exception:
                    pass

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"WS-Timeline error ({slug}): {e}")

    # ═══════════════════════════════════════════════════════════════
    # BTC-ONLY PRECLOSE TEST (T-8s Entry mit Richtungsfilter)
    # ═══════════════════════════════════════════════════════════════

    _PRECLOSE_LOG = Path("data/preclose_test.jsonl")
    _PRECLOSE_SIZE = 2.0      # $2 Learn-Test Size
    _PRECLOSE_CAP = 0.80      # Hartes Ask-Maximum
    _PRECLOSE_IDEAL = 0.70    # Bevorzugter Zielbereich
    _PRECLOSE_MIN_ASK = 0.05  # Minimum (Stale-Guard)
    _PRECLOSE_MIN_DELTA = 0.10  # 0.10% Mindestbewegung
    _PRECLOSE_MARGINAL_DELTA = 0.15  # 0.15% fuer Ask 0.70-0.80
    _PRECLOSE_MAX_SPREAD = 15.0  # 15% Spread-Limit
    _PRECLOSE_MAX_TICK_AGE = 500  # 500ms Tick-Freshness

    async def _preclose_test(
        self, slug: str, asset: str, symbol: str, end_ts: float,
        up_tid: str, down_tid: str, condition_id: str, w: dict,
    ) -> bool:
        """BTC-only Preclose Learn-Test: Entry bei T-8s mit Dreierfilter.

        Returns True wenn gefeuert wurde (kein T+0ms Burst mehr noetig).
        Returns False wenn geskippt (Fallback auf Legacy-Burst).
        """
        try:
            # ── Snapshot T-12s: Binance-Preis + Richtung ──
            snapshot = self._price_at_window_start.get(slug)
            if not snapshot:
                self._log_preclose(slug, asset, "SKIP", "no_start_snapshot")
                return False
            start_price, _ = snapshot

            tick_t12 = self._oracle.get_latest(symbol) if self._oracle else None
            if not tick_t12 or tick_t12.mid <= 0:
                self._log_preclose(slug, asset, "SKIP", "no_binance_t12")
                return False

            price_t12 = tick_t12.mid
            delta_t12 = (price_t12 - start_price) / start_price * 100
            direction_t12 = "UP" if price_t12 > start_price else "DOWN"

            # ── Sleep bis T-10s ──
            now = time.time()
            t10_target = end_ts - 10
            if now < t10_target:
                await asyncio.sleep(t10_target - now)

            if not self._running:
                return False

            # ── Snapshot T-10s ──
            tick_t10 = self._oracle.get_latest(symbol) if self._oracle else None
            if not tick_t10 or tick_t10.mid <= 0:
                self._log_preclose(slug, asset, "SKIP", "no_binance_t10")
                return False

            price_t10 = tick_t10.mid
            delta_t10 = (price_t10 - start_price) / start_price * 100
            direction_t10 = "UP" if price_t10 > start_price else "DOWN"

            # ── Nahbereichs-Sampling T-10s → T-8s (100ms Intervall) ──
            nearfield_directions = []
            t8_target = end_ts - 8
            while time.time() < t8_target and self._running:
                _tick = self._oracle.get_latest(symbol) if self._oracle else None
                if _tick and _tick.mid > 0:
                    _dir = "UP" if _tick.mid > start_price else "DOWN"
                    nearfield_directions.append(_dir)
                await asyncio.sleep(0.1)

            if not self._running:
                return False

            # ── Snapshot T-8s: Entry-Decision ──
            tick_t8 = self._oracle.get_latest(symbol) if self._oracle else None
            if not tick_t8 or tick_t8.mid <= 0:
                self._log_preclose(slug, asset, "SKIP", "no_binance_t8")
                return False

            price_t8 = tick_t8.mid
            delta_t8 = (price_t8 - start_price) / start_price * 100
            direction_t8 = "UP" if price_t8 > start_price else "DOWN"
            tick_age_ms = (time.time() - tick_t8.timestamp) * 1000

            # Winner-Token bestimmen
            if direction_t8 == "UP":
                winner_tid = up_tid
                loser_tid = down_tid
            else:
                winner_tid = down_tid
                loser_tid = up_tid

            # WS-Books lesen (Primaer)
            ws_winner_ask = 0.0
            ws_loser_ask = 0.0
            ws_winner_bid = 0.0
            if self._clob_ws:
                wb = self._clob_ws.get_book(winner_tid)
                lb = self._clob_ws.get_book(loser_tid)
                if wb:
                    ws_winner_ask = wb.best_ask
                    ws_winner_bid = wb.best_bid
                if lb:
                    ws_loser_ask = lb.best_ask

            # REST-Fallback wenn WS kein Book hat (bei T-8s sind ~100ms akzeptabel)
            rest_winner_ask = 0.0
            rest_loser_ask = 0.0
            rest_fallback_used = False
            ask_source = "ws"

            if ws_winner_ask <= 0:
                try:
                    rest_winner_ask = await self._fetch_ask(winner_tid)
                    if rest_winner_ask > 0:
                        rest_fallback_used = True
                        ask_source = "rest_fallback"
                        logger.info(
                            f"ODA PRECLOSE REST-FALLBACK: {asset} winner "
                            f"ws=$0.00 -> rest=${rest_winner_ask:.3f} | {slug[-15:]}"
                        )
                    else:
                        ask_source = "rest_failed"
                except Exception:
                    ask_source = "rest_failed"

            if ws_loser_ask <= 0 and rest_fallback_used:
                try:
                    rest_loser_ask = await self._fetch_ask(loser_tid)
                except Exception:
                    pass

            # Effektiver Ask: WS wenn vorhanden, sonst REST
            effective_winner_ask = ws_winner_ask if ws_winner_ask > 0 else rest_winner_ask
            effective_loser_ask = ws_loser_ask if ws_loser_ask > 0 else rest_loser_ask

            spread_pct = 0.0
            if effective_winner_ask > 0 and ws_winner_bid > 0:
                spread_pct = (effective_winner_ask - ws_winner_bid) / effective_winner_ask * 100

            # ═══ FILTER-KETTE ═══
            abs_delta_t8 = abs(delta_t8)
            abs_delta_t12 = abs(delta_t12)
            abs_delta_t10 = abs(delta_t10)

            direction_all_consistent = (direction_t12 == direction_t10 == direction_t8)
            delta_acc_12_8 = abs_delta_t8 >= abs_delta_t12
            delta_acc_10_8 = abs_delta_t8 >= abs_delta_t10

            # Nahbereichs-Stabilität: alle Samples T-10 bis T-8 gleiche Richtung
            stable_count = len(nearfield_directions)
            all_same_dir = (
                stable_count >= 5  # Mindestens 5 Samples (~500ms Abdeckung)
                and len(set(nearfield_directions)) == 1
                and nearfield_directions[0] == direction_t8
            )

            # Ask-Zone
            ask = effective_winner_ask
            if ask <= self._PRECLOSE_IDEAL:
                ask_zone = "ideal"
            elif ask <= self._PRECLOSE_CAP:
                ask_zone = "marginal"
            else:
                ask_zone = "rejected"

            # ── Filter anwenden ──
            skip_reason = ""

            if abs_delta_t8 < self._PRECLOSE_MIN_DELTA:
                skip_reason = "delta_too_small"
            elif not direction_all_consistent:
                skip_reason = "direction_inconsistent"
            elif not delta_acc_12_8:
                skip_reason = "delta_decelerating_12_8"
            elif not delta_acc_10_8:
                skip_reason = "delta_decelerating_10_8"
            elif not all_same_dir:
                skip_reason = "nearfield_unstable"
            elif ask < self._PRECLOSE_MIN_ASK:
                skip_reason = "ask_too_low"
            elif ask > self._PRECLOSE_CAP:
                skip_reason = "ask_above_cap"
            elif ask_zone == "marginal" and abs_delta_t8 < self._PRECLOSE_MARGINAL_DELTA:
                skip_reason = "marginal_ask_weak_delta"
            elif spread_pct > self._PRECLOSE_MAX_SPREAD:
                skip_reason = "spread_too_wide"
            elif not (self._clob_ws and self._clob_ws.get_book(winner_tid) and self._clob_ws.get_book(loser_tid)):
                skip_reason = "ws_book_missing"
            elif tick_age_ms > self._PRECLOSE_MAX_TICK_AGE:
                skip_reason = "tick_too_stale"

            if skip_reason:
                self._log_preclose(
                    slug, asset, "SKIP", skip_reason,
                    delta_t12=delta_t12, delta_t10=delta_t10, delta_t8=delta_t8,
                    dir_t12=direction_t12, dir_t10=direction_t10, dir_t8=direction_t8,
                    dir_consistent=direction_all_consistent,
                    acc_12_8=delta_acc_12_8, acc_10_8=delta_acc_10_8,
                    stable_samples=stable_count, all_same=all_same_dir,
                    winner_ask=ask, loser_ask=effective_loser_ask,
                    ask_zone=ask_zone, spread=spread_pct,
                    winner_tid=winner_tid, binance_mid=price_t8,
                    binance_start=start_price, tick_age=tick_age_ms,
                    secs_to_close=end_ts - time.time(),
                    ask_source=ask_source, ws_winner_ask=ws_winner_ask,
                    ws_loser_ask=ws_loser_ask, rest_winner_ask=rest_winner_ask,
                    rest_loser_ask=rest_loser_ask, rest_fallback_used=rest_fallback_used,
                )
                logger.info(
                    f"ODA PRECLOSE SKIP: {asset} {direction_t8} | "
                    f"ask=${ask:.3f} src={ask_source} delta={delta_t8:+.3f}% | {skip_reason}"
                )
                return False

            # ═══ FIRE: Einzelner FAK-Shot ═══
            logger.info(
                f"ODA PRECLOSE FIRE: {asset} {direction_t8} | "
                f"ask=${ask:.3f} delta={delta_t8:+.3f}% | "
                f"zone={ask_zone} | {slug[-15:]}"
            )

            # Dedup
            if slug in self._sniped_windows:
                self._log_preclose(
                    slug, asset, "SKIP", "already_sniped",
                    delta_t12=delta_t12, delta_t10=delta_t10, delta_t8=delta_t8,
                    dir_t12=direction_t12, dir_t10=direction_t10, dir_t8=direction_t8,
                    dir_consistent=direction_all_consistent,
                    acc_12_8=delta_acc_12_8, acc_10_8=delta_acc_10_8,
                    stable_samples=stable_count, all_same=all_same_dir,
                    winner_ask=ask, loser_ask=effective_loser_ask,
                    ask_zone=ask_zone, spread=spread_pct,
                    winner_tid=winner_tid, binance_mid=price_t8,
                    binance_start=start_price, tick_age=tick_age_ms,
                    secs_to_close=end_ts - time.time(),
                    ask_source=ask_source, ws_winner_ask=ws_winner_ask,
                    ws_loser_ask=ws_loser_ask, rest_winner_ask=rest_winner_ask,
                    rest_loser_ask=rest_loser_ask, rest_fallback_used=rest_fallback_used,
                )
                return False
            self._sniped_windows.add(slug)

            # Order vorbereiten
            order_price = min(ask, self._PRECLOSE_CAP)
            window_data = {
                "slug": slug, "asset": asset,
                "question": f"{asset} Up or Down {w.get('timeframe', '5m')}",
                "condition_id": condition_id,
                "up_token_id": up_tid, "down_token_id": down_tid,
            }

            fill_price = 0.0
            live_order_id = ""
            outcome = "REJECTED"

            _orig_size = self.trade_size_usd
            try:
                # Temporaer Size auf $2 fuer Learn-Test
                self.trade_size_usd = self._PRECLOSE_SIZE
                await self._execute_snipe(
                    window_data, direction_t8, winner_tid, order_price,
                    entry_binance_price=price_t8,
                    tick_age_ms=tick_age_ms,
                    price_change_pct=delta_t8,
                    signal_tier="PRECLOSE",
                    regime_tag="PRECLOSE_TEST",
                    spread_pct=spread_pct,
                )
                # Check if fill succeeded via last trade
                if self._trades and self._trades[-1].live_success:
                    fill_price = order_price
                    live_order_id = self._trades[-1].live_order_id
                    outcome = "FILLED"
                else:
                    outcome = "REJECTED"
            except Exception as e:
                outcome = f"ERROR:{str(e)[:30]}"
            finally:
                self.trade_size_usd = _orig_size

            self._log_preclose(
                slug, asset, "FIRE", "",
                delta_t12=delta_t12, delta_t10=delta_t10, delta_t8=delta_t8,
                dir_t12=direction_t12, dir_t10=direction_t10, dir_t8=direction_t8,
                dir_consistent=direction_all_consistent,
                acc_12_8=delta_acc_12_8, acc_10_8=delta_acc_10_8,
                stable_samples=stable_count, all_same=all_same_dir,
                winner_ask=ask, loser_ask=effective_loser_ask,
                ask_zone=ask_zone, spread=spread_pct,
                winner_tid=winner_tid, binance_mid=price_t8,
                binance_start=start_price, tick_age=tick_age_ms,
                secs_to_close=end_ts - time.time(),
                fill_price=fill_price, live_order_id=live_order_id,
                outcome=outcome,
                ask_source=ask_source, ws_winner_ask=ws_winner_ask,
                ws_loser_ask=ws_loser_ask, rest_winner_ask=rest_winner_ask,
                rest_loser_ask=rest_loser_ask, rest_fallback_used=rest_fallback_used,
            )

            logger.info(
                f"ODA PRECLOSE {outcome}: {asset} {direction_t8} @ ${fill_price:.3f} | "
                f"ask=${ask:.3f} zone={ask_zone} | {slug[-15:]}"
            )
            return True  # Preclose hat gefeuert → kein T+0ms Burst

        except asyncio.CancelledError:
            return False
        except Exception as e:
            logger.error(f"ODA Preclose Error ({slug}): {e}")
            return False

    def _log_preclose(
        self, slug: str, asset: str, action: str, skip_reason: str,
        delta_t12: float = 0, delta_t10: float = 0, delta_t8: float = 0,
        dir_t12: str = "", dir_t10: str = "", dir_t8: str = "",
        dir_consistent: bool = False,
        acc_12_8: bool = False, acc_10_8: bool = False,
        stable_samples: int = 0, all_same: bool = False,
        winner_ask: float = 0, loser_ask: float = 0,
        ask_zone: str = "", spread: float = 0,
        winner_tid: str = "", binance_mid: float = 0,
        binance_start: float = 0, tick_age: float = 0,
        secs_to_close: float = 0,
        fill_price: float = 0, live_order_id: str = "",
        outcome: str = "",
        ask_source: str = "", ws_winner_ask: float = 0, ws_loser_ask: float = 0,
        rest_winner_ask: float = 0, rest_loser_ask: float = 0,
        rest_fallback_used: bool = False,
    ) -> None:
        """Schreibt einen Preclose-Test-Eintrag in data/preclose_test.jsonl."""
        entry = {
            "ts": round(time.time(), 3),
            "slug": slug,
            "asset": asset,
            "timeframe": "5m",
            "secs_to_close": round(secs_to_close, 2),
            "action": action,
            "skip_reason": skip_reason,
            "delta_t12": round(delta_t12, 4),
            "delta_t10": round(delta_t10, 4),
            "delta_t8": round(delta_t8, 4),
            "direction_t12": dir_t12,
            "direction_t10": dir_t10,
            "direction_t8": dir_t8,
            "direction_all_consistent": dir_consistent,
            "delta_accelerating_12_8": acc_12_8,
            "delta_accelerating_10_8": acc_10_8,
            "stable_samples_t10_t8": stable_samples,
            "all_samples_same_direction_t10_t8": all_same,
            "winner_ask": round(winner_ask, 3),
            "loser_ask": round(loser_ask, 3),
            "ask_zone": ask_zone,
            "spread_pct": round(spread, 2),
            "winner_tid": winner_tid[:16] if winner_tid else "",
            "binance_mid": round(binance_mid, 2),
            "binance_start": round(binance_start, 2),
            "tick_age_ms": round(tick_age, 1),
            "fill_price": round(fill_price, 3),
            "live_order_id": live_order_id[:20] if live_order_id else "",
            "outcome": outcome,
            "ask_source": ask_source,
            "ws_winner_ask": round(ws_winner_ask, 3),
            "ws_loser_ask": round(ws_loser_ask, 3),
            "rest_winner_ask": round(rest_winner_ask, 3),
            "rest_loser_ask": round(rest_loser_ask, 3),
            "rest_fallback_used": rest_fallback_used,
        }
        try:
            self._PRECLOSE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(self._PRECLOSE_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════
    # WINDOW DISCOVERY
    # ═══════════════════════════════════════════════════════════════

    async def _get_token_ids(self, slug: str, asset: str) -> tuple | None:
        """Holt Token-IDs per EXAKTEM Slug-Match. Idiotensicher.

        Architektur:
        1. Discovery Cache: O(1) dict.get(slug) — kein Iterieren, kein Toleranz-Match
        2. Gamma Validation: Einmal pro Slug bei erster Entdeckung, dann gecached
        3. Cross-Validation: Cache CID == Gamma CID
        4. Outcome-Mapping: Explizite Validierung (Up/Down/Yes/No)

        Prinzip: Lieber kein Trade als falscher Trade.
        Hot-Path: Nur RAM-Reads nach der ersten Validierung.
        """
        # ── SCHRITT 1: Discovery Cache — exakter Slug-Key ──
        cache_result = None
        try:
            w = self.discovery.windows.get(slug)
            if w and w.asset.upper() == asset:
                if w.up_token_id and w.down_token_id and w.condition_id:
                    cache_result = (w.up_token_id, w.down_token_id, w.condition_id)
                else:
                    logger.info(f"ODA TOKEN-INCOMPLETE: {slug} | cache hat Slug aber fehlende IDs")
        except Exception as e:
            logger.warning(f"ODA Discovery lookup error: {e}")

        # ── SCHRITT 2: Gamma Validation — einmal pro Slug, dann gecached ──
        gamma_result = self._gamma_validated.get(slug)

        if gamma_result is None:
            # Noch nie validiert → Gamma API abfragen (einmalig)
            gamma_result = await self._validate_slug_via_gamma(slug)
            # Ergebnis cachen (tuple oder "INVALID")
            self._gamma_validated[slug] = gamma_result if gamma_result else "INVALID"
        elif gamma_result == "INVALID":
            gamma_result = None

        # ── SCHRITT 3: Entscheidung mit Cross-Validation ──
        if cache_result and gamma_result:
            cache_cid = cache_result[2]
            gamma_cid = gamma_result[2]
            if cache_cid == gamma_cid:
                logger.info(f"ODA TOKEN-MATCH: {slug} | source=discovery+gamma | cid={cache_cid[:20]}...")
                return cache_result
            else:
                logger.error(
                    f"ODA TOKEN-CONFLICT: {slug} | cache_cid={cache_cid[:20]}... | "
                    f"gamma_cid={gamma_cid[:20]}... | SKIPPED"
                )
                return None

        if cache_result and not gamma_result:
            logger.info(f"ODA TOKEN-MATCH: {slug} | source=discovery (gamma unavailable) | cid={cache_result[2][:20]}...")
            return cache_result

        if not cache_result and gamma_result:
            logger.info(f"ODA TOKEN-MATCH: {slug} | source=gamma (cache empty) | cid={gamma_result[2][:20]}...")
            return gamma_result

        logger.info(f"ODA TOKEN-SKIP: {slug} | reason=no_source")
        return None

    async def _validate_slug_via_gamma(self, slug: str) -> tuple | None:
        """Einmalige Gamma API Validierung fuer einen Slug. Ergebnis wird gecached."""
        if not self._session or self._session.closed:
            return None
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    logger.warning(f"ODA GAMMA-VALIDATE: {slug} | http_{resp.status}")
                    return None
                data = await resp.json()

            if not data:
                logger.info(f"ODA GAMMA-VALIDATE: {slug} | empty")
                return None
            if len(data) > 1:
                logger.warning(f"ODA GAMMA-VALIDATE: {slug} | ambiguous (N={len(data)})")
                return None

            event = data[0]
            markets = event.get("markets", [])
            if not markets:
                logger.info(f"ODA GAMMA-VALIDATE: {slug} | no_markets")
                return None

            market = markets[0]
            cid = market.get("conditionId", "")
            tokens_raw = market.get("clobTokenIds", "[]")
            outcomes_raw = market.get("outcomes", "[]")
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

            if len(tokens) != 2:
                logger.warning(f"ODA GAMMA-VALIDATE: {slug} | invalid_tokens (N={len(tokens)})")
                return None
            if len(outcomes) != 2:
                logger.warning(f"ODA GAMMA-VALIDATE: {slug} | invalid_outcomes (N={len(outcomes)})")
                return None

            up_tid = ""
            down_tid = ""
            for i, outcome in enumerate(outcomes):
                ol = outcome.lower() if isinstance(outcome, str) else ""
                if ol in ("up", "yes"):
                    up_tid = tokens[i]
                elif ol in ("down", "no"):
                    down_tid = tokens[i]

            if not up_tid or not down_tid:
                logger.warning(f"ODA GAMMA-VALIDATE: {slug} | outcome_mapping_failed ({outcomes})")
                return None
            if up_tid == down_tid:
                logger.warning(f"ODA GAMMA-VALIDATE: {slug} | duplicate_token_ids")
                return None
            if not cid:
                logger.warning(f"ODA GAMMA-VALIDATE: {slug} | no_condition_id")
                return None

            logger.info(f"ODA GAMMA-VALIDATE: {slug} | OK | cid={cid[:20]}...")
            return (up_tid, down_tid, cid)

        except asyncio.TimeoutError:
            logger.warning(f"ODA GAMMA-VALIDATE: {slug} | timeout")
            return None
        except Exception as e:
            logger.warning(f"ODA GAMMA-VALIDATE: {slug} | error ({str(e)[:60]})")
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
