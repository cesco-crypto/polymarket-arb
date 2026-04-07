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

        # Config — Latency Arbitrage: Sub-100ms Execution
        self.trade_size_usd = 5.0          # $5 pro Trade
        self.min_entry_price = 0.05        # Min Preis (Dust Filter)
        self.max_entry_price = 0.95        # Max 0.95 (darueber = kein Edge)
        self.delay_after_close_s = 0.0     # 0ms Delay — rein event-driven!
        self.max_delay_s = 15.0            # Max 15s nach Close (Fenster ist ~2.7s median)
        self.max_concurrent = 5            # Max 5 gleichzeitige Snipes

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
        self._price_at_window_start: dict[str, float] = {}  # slug → price at start

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

        logger.info(f"Oracle Delay Arb v4 startet — Sub-100ms Latency Arb (Counter: {self._trade_count})")

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

        # Token-IDs SOFORT laden: Discovery + alle kommenden Windows
        initial_tokens = []
        try:
            from dashboard.web_ui import active_strategies
            for sn, st in active_strategies.items():
                if hasattr(st, "discovery"):
                    initial_tokens = st.discovery.get_all_token_ids()
                    break
        except Exception:
            pass
        # Zusaetzlich: Gamma API fuer kommende Windows
        for w in self._compute_next_window_closes():
            if w["seconds_to_close"] > 0:
                tids = await self._get_token_ids(w["slug"], w["asset"])
                if tids:
                    initial_tokens.extend([tids[0], tids[1]])
        if initial_tokens:
            self._clob_ws.subscribe(initial_tokens)
            logger.info(f"ODA: {len(initial_tokens)} Token-IDs SOFORT subscribed (kein 60s Warten)")
        await self._clob_ws.start()

        # Binance Tick-Callback registrieren (Event-Driven Trigger)
        self._register_binance_callback()

        try:
            await asyncio.gather(
                self._main_loop(),
                self._status_loop(),
                self._subscription_refresh_loop(),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    def _register_binance_callback(self) -> None:
        """Speichert Referenz zum Binance Oracle fuer direkten RAM-Read bei T+0ms.

        KEIN Callback — liest direkt oracle.get_latest() bei Execution.
        Vermeidet Callback-Kollision mit HMSF.
        """
        try:
            from dashboard.web_ui import active_strategies
            for sn, st in active_strategies.items():
                if hasattr(st, "oracle"):
                    self._oracle = st.oracle
                    logger.info("ODA: Binance Oracle Referenz gespeichert (direkter RAM-Read)")
                    return
        except Exception as e:
            logger.warning(f"ODA: Binance oracle reference failed: {e}")
        self._oracle = None

    async def _subscribe_clob_tokens(self) -> None:
        """Subscribed alle aktuellen + naechsten Window Token-IDs auf dem CLOB WS."""
        try:
            from dashboard.web_ui import active_strategies
            for sn, st in active_strategies.items():
                if not hasattr(st, "discovery"):
                    continue
                token_ids = st.discovery.get_all_token_ids()
                if token_ids:
                    self._clob_ws.subscribe(token_ids)
                    logger.info(f"ODA: {len(token_ids)} Token-IDs auf CLOB WS subscribed")
                return
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

    async def shutdown(self) -> None:
        self._running = False
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
                    if secs_to > 120:
                        continue  # Zu weit in der Zukunft

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
                    symbol = f"{w['asset']}/USDT"
                    if self._oracle:
                        tick = self._oracle.get_latest(symbol)
                        if tick and tick.mid > 0:
                            self._price_at_window_start[slug] = tick.mid

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
            pre_price = 0.75
            if self.executor.is_live:
                self.executor.pre_sign_order(up_tid, pre_price, self.trade_size_usd)
                self.executor.pre_sign_order(down_tid, pre_price, self.trade_size_usd)
                logger.info(f"ODA PRE-SIGN: {asset} {w['timeframe']} UP+DOWN @ ${pre_price}")

            # Speichere Start-Preis fuer Crossover-Vergleich (direkt vom Oracle)
            start_price = 0.0
            if self._oracle:
                tick = self._oracle.get_latest(symbol)
                if tick:
                    start_price = tick.mid
            if start_price <= 0:
                start_price = self._price_at_window_start.get(slug, 0)

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
            if self._oracle:
                tick = self._oracle.get_latest(symbol)
                if tick:
                    current_price = tick.mid

            if current_price <= 0 or start_price <= 0:
                logger.info(f"ODA Skip: {asset} — no price data")
                return

            if current_price > start_price:
                winner = "UP"
                winner_tid = up_tid
            else:
                winner = "DOWN"
                winner_tid = down_tid

            price_change_pct = (current_price - start_price) / start_price * 100

            # Dedup
            if slug in self._sniped_windows:
                return
            active = sum(1 for t in self._trades if not t.resolved)
            if active >= self.max_concurrent:
                return
            self._sniped_windows.add(slug)

            decision_us = (time.perf_counter() - t0) * 1_000_000

            logger.info(
                f"ODA BURST: {asset} {winner} | "
                f"Binance {start_price:.2f}->{current_price:.2f} ({price_change_pct:+.4f}%) | "
                f"decision={decision_us:.0f}us | firing 5 FAK shots..."
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

                    # Tier 2: Discovery Cache
                    if shot_ask <= 0:
                        try:
                            from dashboard.web_ui import active_strategies
                            for sn, st in active_strategies.items():
                                if not hasattr(st, "discovery"):
                                    continue
                                for s, wnd in st.discovery.windows.items():
                                    if wnd.condition_id == condition_id:
                                        shot_ask = wnd.up_best_ask if winner == "UP" else wnd.down_best_ask
                                        break
                                if shot_ask > 0:
                                    break
                        except Exception:
                            pass

                    # Tier 3: REST Fetch (langsam aber zuverlaessig, nur Shot#0)
                    if shot_ask <= 0 and shot == 0:
                        shot_ask = await self._fetch_ask(winner_tid)

                    if shot_ask <= 0 or shot_ask > self.max_entry_price:
                        if shot == 0:
                            logger.info(f"ODA Shot#{shot}: {asset} ask=${shot_ask:.3f} — no fill available")
                        break

                    if shot_ask < self.min_entry_price:
                        break

                    edge_pct = (1.0 / shot_ask - 1.0) * 100
                    shot_t = (time.perf_counter() - t0) * 1000

                    # FIRE!
                    await self._execute_snipe(window_data, winner, winner_tid, shot_ask)
                    filled = True

                    logger.info(
                        f"ODA Shot#{shot}: {asset} {winner} @ ${shot_ask:.3f} | "
                        f"edge={edge_pct:.1f}% | t={shot_t:.1f}ms"
                    )
                    break  # Erster erfolgreicher Shot genuegt

                except Exception as e:
                    err_str = str(e)
                    if "no orders found to match" in err_str.lower():
                        # FAK rejected — kein Match. Naechster Shot.
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
        """Holt Token-IDs von jeder laufenden Strategie mit MarketDiscovery.

        Prueft hmsf_decision_engine, momentum_latency_v2, und alle anderen
        Strategien die ein .discovery Attribut haben. Fallback: Gamma API.
        """
        start_ts = int(slug.split("-")[-1])

        # 1. Versuche von JEDER laufenden Strategie mit Discovery
        try:
            from dashboard.web_ui import active_strategies
            for strat_name, strat in active_strategies.items():
                if not hasattr(strat, "discovery"):
                    continue
                discovery = strat.discovery
                if not hasattr(discovery, "windows"):
                    continue
                for s, w in discovery.windows.items():
                    if w.asset.upper() == asset and w.up_token_id and w.down_token_id:
                        if abs(w.window_start_ts - start_ts) < 30:
                            logger.debug(f"ODA Token-IDs via {strat_name}: {asset} {slug[-15:]}")
                            return (w.up_token_id, w.down_token_id, w.condition_id)
        except Exception as e:
            logger.warning(f"ODA Discovery lookup error: {e}")

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
        """Holt aktive 5-Min Windows von jeder Strategie mit MarketDiscovery."""
        try:
            from dashboard.web_ui import active_strategies
            for strat_name, strat in active_strategies.items():
                if not hasattr(strat, "discovery"):
                    continue
                discovery = strat.discovery
                if not hasattr(discovery, "windows") or not discovery.windows:
                    continue
                windows = []
                for slug, w in discovery.windows.items():
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

        # Fallback: Fetch direkt von Polymarket
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
        self, window: dict, winner: str, token_id: str, ask_price: float
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

        # LIVE Order
        if self.executor.is_live and token_id:
            try:
                res = await self.executor.place_order(
                    token_id=token_id,
                    side="BUY",
                    price=min(0.99, ask_price),  # Max 0.99 (CLOB Limit!)
                    size_usd=self.trade_size_usd,
                    asset=asset,
                    direction=winner,
                )
                if res.success:
                    trade.live_order_id = res.order_id
                    trade.live_success = True
                    logger.info(f"SNIPE ORDER PLACED: {trade_id} — {res.order_id}")

                    # FILL VERIFICATION: Check if order actually filled
                    await asyncio.sleep(2)  # Give CLOB time to match
                    fill_status = await self._check_fill(res.order_id)
                    if fill_status == "FILLED":
                        filled = True
                        trade.pnl_usd = expected_pnl
                        trade.resolved = True
                        logger.info(f"SNIPE FILLED: {trade_id} — expected +${expected_pnl:.3f}")
                        # Sofort close Event schreiben — nicht auf Redeemer warten
                        # ODA kauft den Winner NACH Ergebnis, Outcome ist bei Fill sicher
                        self.journal.record_close(TradeRecord(
                            trade_id=trade_id,
                            event="close",
                            exit_ts=time.time(),
                            asset=asset,
                            direction=winner,
                            executed_price=ask_price,
                            size_usd=self.trade_size_usd,
                            shares=float(shares),
                            pnl_usd=expected_pnl,
                            pnl_pct=net_ev_pct,
                            outcome_correct=True,
                            condition_id=condition_id,
                            order_type="oracle_delay_arb",
                            live_order_id=res.order_id,
                            live_order_success=True,
                        ))
                    elif fill_status == "UNFILLED":
                        logger.warning(f"SNIPE NOT FILLED: {trade_id} — cancelling")
                        await self._cancel_order(res.order_id)
                        trade.live_success = False
                    else:
                        logger.info(f"SNIPE STATUS: {trade_id} — {fill_status}")
                else:
                    logger.error(f"SNIPE LIVE FAILED: {trade_id} — {res.error}")
            except Exception as e:
                logger.error(f"SNIPE EXCEPTION: {trade_id} — {e}")

        # Telegram Alert
        status_emoji = "✅" if filled else "📋"
        order_ref = f"\n🔗 Order: {trade.live_order_id[:20]}..." if trade.live_order_id else ""
        asyncio.create_task(telegram.send_alert(
            f"🎯 <b>{status_emoji} ORACLE SNIPE #{self._trade_count}</b>\n"
            f"{'─'*26}\n"
            f"📊 {asset} {winner} @ {ask_price:.3f}\n"
            f"💰 Size: ${self.trade_size_usd:.2f} ({shares} shares)\n"
            f"📈 Expected: ${expected_pnl:.3f} ({net_ev_pct:.2f}%)\n"
            f"💸 Fee: {fee_pct:.2f}% (${fee_usd:.3f})\n"
            f"📍 {slug[:35]}{order_ref}"
        ))

        # Journal — ODA kauft den Winner NACH Ergebnis, daher ist PnL bei Fill bekannt
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
            pnl_usd=expected_pnl if filled else 0.0,
            pnl_pct=net_ev_pct if filled else 0.0,
            outcome_correct=filled,  # Bei FILL = Winner gekauft = korrekt
            order_type="oracle_delay_arb",
            live_order_id=trade.live_order_id,
            live_order_success=trade.live_success,
            condition_id=condition_id,
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
                f"Size: ${self.trade_size_usd} | Range: {self.min_entry_price}-{self.max_entry_price}"
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
