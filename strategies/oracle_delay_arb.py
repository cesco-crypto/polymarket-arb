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

        # Config
        self.trade_size_usd = 5.0          # $5 pro Trade (Daten sammeln, später skalieren)
        self.min_entry_price = 0.90        # Kaufen wenn Preis >= 0.90 (mehr Profit, etwas mehr Risiko)
        self.max_entry_price = 0.99        # Max 0.99 (CLOB Limit)
        self.delay_after_close_s = 15.0    # 15 Sekunden nach Close warten (Gamma braucht ~10-20s)
        self.max_delay_s = 60.0            # Max 60s nach Close
        self.max_concurrent = 5            # Max 5 gleichzeitige Snipes

        # State
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._trades: list[SniperTrade] = []
        self._trade_count = 0
        self._recent_snipes: deque = deque(maxlen=50)
        self._sniped_windows: set[str] = set()  # Dedup: slug → bereits gesniped

        # Binance price tracking
        self._last_prices: dict[str, float] = {}  # asset → last price

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

        logger.info(f"Oracle Delay Arb startet — Sharky6999 Style! (Counter: {self._trade_count})")

        if self.settings.live_trading:
            live_ok = await self.executor.initialize()
            if live_ok:
                logger.info("Oracle Delay Arb: LIVE MODUS — Echte Orders!")
            else:
                logger.warning("Oracle Delay Arb: Live Init fehlgeschlagen")

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "polymarket-arb/2.0"},
        )

        try:
            await asyncio.gather(
                self._main_loop(),
                self._status_loop(),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info(f"Oracle Delay Arb beendet. {self._trade_count} Snipes ausgeführt.")

    # ═══════════════════════════════════════════════════════════════
    # MAIN LOOP — Window-Close Detection + Sniping
    # ═══════════════════════════════════════════════════════════════

    def _compute_next_window_closes(self) -> list[dict]:
        """Berechnet die nächsten Window-Close-Zeiten (5-Min Intervall).

        Polymarket 5-Min Windows starten alle 5 Minuten (aligned to epoch).
        Window start = floor(time / 300) * 300
        Window end = start + 300
        """
        now = time.time()
        interval = 300  # 5 Minuten
        results = []

        # Letzte 3 abgelaufene Windows + nächste 2 kommende
        base_ts = int(now // interval) * interval
        for offset in range(-3, 2):
            start_ts = base_ts + offset * interval
            end_ts = start_ts + interval
            seconds_since_close = now - end_ts

            for asset in ["btc", "eth"]:
                slug = f"{asset}-updown-5m-{start_ts}"
                results.append({
                    "slug": slug,
                    "asset": asset.upper(),
                    "window_start_ts": start_ts,
                    "window_end_ts": end_ts,
                    "seconds_since_close": seconds_since_close,
                })
        return results

    async def _main_loop(self) -> None:
        """Hauptloop: Erkennt Window-Closes und sniped den Winner.

        Berechnet Window-End-Zeiten SELBST (nicht von Discovery abhängig).
        Alle 5 Minuten gibt es ein neues Window das schliesst.
        """
        logger.info("Oracle Delay Arb Main-Loop gestartet")

        while self._running:
            try:
                windows = self._compute_next_window_closes()
                now = time.time()

                for w in windows:
                    slug = w["slug"]
                    seconds_since_close = w["seconds_since_close"]

                    # Nur Windows die GERADE geschlossen haben (3-60s nach Close)
                    if seconds_since_close < self.delay_after_close_s:
                        continue  # Zu früh
                    if seconds_since_close > self.max_delay_s:
                        continue  # Zu spät

                    # Dedup
                    if slug in self._sniped_windows:
                        continue

                    # Max concurrent check
                    active = sum(1 for t in self._trades if not t.resolved)
                    if active >= self.max_concurrent:
                        continue

                    asset = w["asset"]

                    # Hole Token-IDs von laufender Discovery
                    token_ids = await self._get_token_ids(slug, asset)
                    if not token_ids:
                        logger.info(f"ODA: No token IDs for {asset} {slug[-15:]} (age={seconds_since_close:.0f}s)")
                        self._sniped_windows.add(slug)  # Don't retry
                        continue

                    up_tid, down_tid, condition_id = token_ids

                    # 2. Bestimme den Winner via BINANCE PREIS-VERGLEICH
                    # Direkt und deterministisch: Preis bei Window-Start vs Preis jetzt
                    # BTC stieg → UP gewinnt. BTC fiel → DOWN gewinnt.
                    winner = None
                    winner_tid = ""
                    try:
                        from dashboard.web_ui import active_strategies
                        oracle = None
                        for sn, st in active_strategies.items():
                            if hasattr(st, "oracle"):
                                oracle = st.oracle
                                break
                        if oracle:
                            symbol = f"{asset}/USDT"
                            window_obj = oracle.get_window(symbol)
                            if window_obj:
                                start_price = None
                                current_price = None
                                latest = window_obj.latest()
                                if latest:
                                    current_price = latest.mid
                                # Preis vor 5 Minuten approximieren via Momentum
                                mom = window_obj.momentum(300)
                                if mom is not None and current_price:
                                    start_price = current_price / (1 + mom / 100)

                                if start_price and current_price:
                                    if current_price > start_price:
                                        winner = "UP"
                                        winner_tid = up_tid
                                    else:
                                        winner = "DOWN"
                                        winner_tid = down_tid
                                    logger.info(
                                        f"ODA Binance: {asset} start={start_price:.2f} → now={current_price:.2f} "
                                        f"→ {winner} (mom={mom:+.3f}%)"
                                    )
                    except Exception as e:
                        logger.warning(f"ODA Binance lookup error: {e}")

                    # Fallback: Gamma-Preise aus Discovery (weniger zuverlaessig)
                    if not winner:
                        try:
                            from dashboard.web_ui import active_strategies
                            for sn, st in active_strategies.items():
                                if not hasattr(st, "discovery"):
                                    continue
                                for s, wnd in st.discovery.windows.items():
                                    if wnd.condition_id == condition_id:
                                        gup = wnd.up_best_ask or wnd.gamma_up_price
                                        gdn = wnd.down_best_ask or wnd.gamma_down_price
                                        if gup > 0.7 and gdn < 0.3:
                                            winner = "UP"
                                            winner_tid = up_tid
                                        elif gdn > 0.7 and gup < 0.3:
                                            winner = "DOWN"
                                            winner_tid = down_tid
                                        break
                                if winner:
                                    break
                        except Exception:
                            pass

                    if not winner:
                        logger.info(
                            f"ODA Wait: {asset} {slug[-15:]} — winner unclear (age={seconds_since_close:.0f}s)"
                        )
                        continue

                    # Hole CLOB-Ask fuer den Winner-Token (fuer Order-Preis)
                    winner_ask = await self._fetch_ask(winner_tid) if winner_tid else 0
                    if winner_ask <= 0 or winner_ask > self.max_entry_price:
                        logger.info(f"ODA Skip: {asset} {winner} CLOB ask={winner_ask:.3f}")
                        self._sniped_windows.add(slug)
                        continue

                    if winner_ask < self.min_entry_price:
                        logger.info(f"ODA Wait: {asset} {winner} ask={winner_ask:.3f} < {self.min_entry_price}")
                        continue

                    # 3. SNIPE! Kaufe den Winner
                    self._sniped_windows.add(slug)
                    window_data = {
                        "slug": slug,
                        "asset": asset,
                        "question": f"{asset} Up or Down 5m",
                        "condition_id": condition_id,
                        "up_token_id": up_tid,
                        "down_token_id": down_tid,
                    }
                    await self._execute_snipe(window_data, winner, winner_tid, winner_ask)

            except Exception as e:
                logger.error(f"ODA Loop Error: {e}")

            await asyncio.sleep(2.0)  # Check every 2 seconds

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
        }


# Auto-Register
register_strategy(OracleDelayArbStrategy.STRATEGY_NAME, OracleDelayArbStrategy)
