"""CLOB WebSocket — Lokales RAM-Orderbuch fuer Sub-100ms Latency Arbitrage.

Haelt ein lokales Orderbuch pro Token im RAM, synchronisiert via WebSocket-Deltas.
Kein REST-Polling, kein I/O fuer Preisabfragen — alles O(1) dict-Reads.

Architektur:
    - asyncio Task: WS Reader empfaengt Deltas, updated RAM sofort
    - Orderbuch: dict[token_id] -> OrderbookSnapshot (best_ask, best_bid, depth)
    - Keine Locks noetig (single-threaded asyncio, kooperatives Scheduling)
    - orjson fuer C-Speed JSON Parsing (kein GIL-Blocking)

Polymarket WS Docs:
    - Initial Subscribe: {"assets_ids": [...], "type": "market", "initial_dump": true, "level": 2, "custom_feature_enabled": true}
    - Dynamic Re-Subscribe: {"operation": "subscribe", "assets_ids": [...]}
    - Dynamic Unsubscribe: {"operation": "unsubscribe", "assets_ids": [...]}
    - Client muss alle 10s PING senden, Server antwortet PONG
    - Events: book, price_change, best_bid_ask, last_trade_price, tick_size_change, new_market, market_resolved
    - Max 500 instruments pro Connection
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

import websockets

try:
    import orjson
    def json_loads(data): return orjson.loads(data)
    def json_dumps(data):
        raw = orjson.dumps(data)
        return raw.decode() if isinstance(raw, bytes) else raw
except ImportError:
    import json
    def json_loads(data): return json.loads(data)
    def json_dumps(data): return json.dumps(data)

from loguru import logger

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class OrderbookSnapshot:
    """Lokaler Orderbuch-Snapshot pro Token — O(1) Read."""
    best_ask: float = 0.0
    best_bid: float = 0.0
    ask_depth_usd: float = 0.0   # USD-Volumen auf den Top-3 Asks
    bid_depth_usd: float = 0.0   # USD-Volumen auf den Top-3 Bids
    ask_levels: int = 0           # Anzahl Ask-Levels
    updated_at: float = 0.0       # Unix timestamp letztes Update

    @property
    def is_fresh(self) -> bool:
        return time.time() - self.updated_at < 10.0

    @property
    def spread_pct(self) -> float:
        if self.best_ask > 0 and self.best_bid > 0:
            return (self.best_ask - self.best_bid) / self.best_ask * 100
        return 0.0


class CLOBWebSocket:
    """Polymarket CLOB WebSocket — lokales RAM-Orderbuch.

    Subscribed auf Token-IDs und pflegt ein lokales Orderbuch das
    in O(1) ohne I/O gelesen werden kann.
    """

    def __init__(self, on_update: Callable[[str], None] | None = None) -> None:
        # RAM-Orderbuch: token_id -> OrderbookSnapshot
        self._books: dict[str, OrderbookSnapshot] = {}

        # Callback bei jedem Update (optional, fuer Event-Trigger)
        self._on_update = on_update

        # Token subscriptions
        self._token_ids: set[str] = set()

        # Connection state
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._reconnect_count = 0
        self._events_processed = 0
        self._last_event_ts = 0.0
        self._resub_needed = False
        self._ws_ref = None  # Referenz fuer Re-Subscribe + PING

    # ── Public API ────────────────────────────────

    def get_book(self, token_id: str) -> OrderbookSnapshot | None:
        """O(1) Read — kein I/O, kein Lock."""
        return self._books.get(token_id)

    def get_best_ask(self, token_id: str) -> float:
        """O(1) — gibt best_ask oder 0.0 zurueck."""
        book = self._books.get(token_id)
        return book.best_ask if book and book.is_fresh else 0.0

    def subscribe(self, token_ids: list[str]) -> None:
        """Fuegt Token-IDs zur Subscription hinzu und triggert Re-Subscribe."""
        new_added = False
        for tid in token_ids:
            if tid and tid not in self._token_ids:
                self._token_ids.add(tid)
                self._books[tid] = OrderbookSnapshot()
                new_added = True
        if new_added:
            self._resub_needed = True

    def set_active_tokens(self, token_ids: list[str]) -> None:
        """ERSETZT die Token-Liste (statt nur hinzufuegen). Entfernt alte Tokens.

        Polymarket WS: max 500 Instrumente pro Connection.
        Alte abgelaufene Tokens werden entfernt damit aktuelle Tokens Updates bekommen.
        """
        new_set = set(tid for tid in token_ids if tid)
        old_set = self._token_ids

        # Entferne alte Tokens + ihre Books
        removed = old_set - new_set
        for tid in removed:
            self._books.pop(tid, None)

        # Fuege neue hinzu
        for tid in new_set:
            if tid not in self._books:
                self._books[tid] = OrderbookSnapshot()

        if new_set != old_set:
            self._token_ids = new_set
            self._resub_needed = True
            logger.info(f"CLOB WS: Active tokens set to {len(new_set)} (removed {len(removed)}, added {len(new_set - old_set)})")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._ws_loop(), name="clob_ws")
        logger.info(f"CLOB WS: Gestartet ({len(self._token_ids)} tokens)")

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("CLOB WS: Gestoppt")

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "tokens": len(self._token_ids),
            "events": self._events_processed,
            "reconnects": self._reconnect_count,
            "freshness_ms": round((time.time() - self._last_event_ts) * 1000)
            if self._last_event_ts else 0,
        }

    # ── Aktiver Client-PING (Fix 3) ──────────────

    async def _ping_loop(self, ws) -> None:
        """Sendet alle 10s PING an den Server. Pflicht laut Polymarket-Docs."""
        try:
            while self._running and self._connected:
                await asyncio.sleep(10)
                try:
                    await ws.send("PING")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    # ── WebSocket Loop ────────────────────────────

    async def _ws_loop(self) -> None:
        delay = 1.0

        while self._running:
            tids = list(self._token_ids)
            if not tids:
                await asyncio.sleep(5)
                continue

            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=None,   # Wir machen eigenes PING
                    ping_timeout=None,
                    close_timeout=3,
                    max_size=2**20,  # 1MB max message
                ) as ws:
                    self._connected = True
                    self._ws_ref = ws
                    delay = 1.0

                    # ── FIX 2: Initial Subscribe mit allen Feldern ──
                    sub_msg = json_dumps({
                        "assets_ids": tids,
                        "type": "market",
                        "initial_dump": True,
                        "level": 2,
                        "custom_feature_enabled": True,
                    })
                    await ws.send(sub_msg)
                    self._resub_needed = False
                    logger.info(f"CLOB WS: Verbunden — {len(tids)} tokens subscribed (initial_dump=true, custom_features=true)")

                    # ── FIX 3: Aktiver Client-PING Task starten ──
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))

                    async for raw in ws:
                        if not self._running:
                            break

                        # Server-PING beantworten (falls Server auch PINGs sendet)
                        if raw == "PING":
                            await ws.send("PONG")
                            continue
                        # Eigene PONG-Antworten ignorieren
                        if raw == "PONG":
                            continue

                        # ── FIX 1: Re-Subscribe mit "operation" Feld ──
                        if self._resub_needed:
                            new_tids = list(self._token_ids)
                            resub = json_dumps({
                                "operation": "subscribe",
                                "assets_ids": new_tids,
                            })
                            await ws.send(resub)
                            self._resub_needed = False
                            logger.info(f"CLOB WS: Re-Subscribed (operation=subscribe) — {len(new_tids)} tokens")

                        # Parse + Process
                        try:
                            self._process_message(
                                json_loads(raw) if isinstance(raw, (str, bytes)) else raw
                            )
                        except Exception:
                            pass

                    # PING-Task aufräumen wenn WS-Loop endet
                    if self._ping_task:
                        self._ping_task.cancel()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                if self._ping_task:
                    self._ping_task.cancel()
                if self._running:
                    self._reconnect_count += 1
                    logger.warning(f"CLOB WS: {e} — reconnect in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

        self._connected = False

    def _process_message(self, msg: dict | list) -> None:
        """Verarbeitet WS-Events und updated RAM-Orderbuch — SCHNELL.

        Event-Typen:
        1. "book": Initialer Snapshot mit vollen asks[]/bids[] Arrays
        2. "price_change": Delta-Updates mit price_changes[] (best_bid/best_ask)
        3. "best_bid_ask": Kompaktes Event mit nur best_bid, best_ask, spread (Fix 5)
        """
        events = msg if isinstance(msg, list) else [msg]
        now = time.time()

        for event in events:
            event_type = event.get("event_type", "")
            token_id = ""

            # ── BOOK SNAPSHOT: Volles Orderbuch (kommt bei Subscribe) ──
            if event_type == "book":
                token_id = event.get("asset_id", "")
                if not token_id:
                    continue
                book = self._books.get(token_id)
                if not book:
                    continue

                asks = event.get("asks", [])
                bids = event.get("bids", [])

                if asks:
                    prices = [float(a["price"]) for a in asks]
                    sizes = [float(a.get("size", 0)) for a in asks]
                    book.best_ask = min(prices)
                    book.ask_depth_usd = sum(
                        p * s for p, s in zip(prices[:5], sizes[:5])
                    )
                    book.ask_levels = len(asks)

                if bids:
                    prices = [float(b["price"]) for b in bids]
                    sizes = [float(b.get("size", 0)) for b in bids]
                    book.best_bid = max(prices)
                    book.bid_depth_usd = sum(
                        p * s for p, s in zip(prices[:5], sizes[:5])
                    )

                book.updated_at = now
                self._events_processed += 1
                self._last_event_ts = now

            # ── PRICE_CHANGE: Echtzeit-Updates mit best_bid/best_ask ──
            elif event_type == "price_change":
                price_changes = event.get("price_changes", [])
                for pc in price_changes:
                    token_id = pc.get("asset_id", "")
                    if not token_id:
                        continue
                    book = self._books.get(token_id)
                    if not book:
                        continue

                    # best_bid/best_ask aus dem price_change Event
                    best_ask_str = pc.get("best_ask", "")
                    best_bid_str = pc.get("best_bid", "")

                    if best_ask_str:
                        book.best_ask = float(best_ask_str)
                    if best_bid_str:
                        book.best_bid = float(best_bid_str)

                    book.updated_at = now

                self._events_processed += 1
                self._last_event_ts = now

            # ── FIX 5: BEST_BID_ASK Event (mit custom_feature_enabled) ──
            elif event_type == "best_bid_ask":
                token_id = event.get("asset_id", "")
                if not token_id:
                    continue
                book = self._books.get(token_id)
                if not book:
                    continue

                best_ask_str = event.get("best_ask", "")
                best_bid_str = event.get("best_bid", "")

                if best_ask_str:
                    book.best_ask = float(best_ask_str)
                if best_bid_str:
                    book.best_bid = float(best_bid_str)

                book.updated_at = now
                self._events_processed += 1
                self._last_event_ts = now

            # Optional Callback (fuer Event-Trigger)
            if token_id and self._on_update:
                try:
                    self._on_update(token_id)
                except Exception:
                    pass
