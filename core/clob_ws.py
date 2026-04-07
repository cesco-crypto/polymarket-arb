"""CLOB WebSocket — Lokales RAM-Orderbuch fuer Sub-100ms Latency Arbitrage.

Haelt ein lokales Orderbuch pro Token im RAM, synchronisiert via WebSocket-Deltas.
Kein REST-Polling, kein I/O fuer Preisabfragen — alles O(1) dict-Reads.

Architektur:
    - asyncio Task: WS Reader empfaengt Deltas, updated RAM sofort
    - Orderbuch: dict[token_id] -> OrderbookSnapshot (best_ask, best_bid, depth)
    - Keine Locks noetig (single-threaded asyncio, kooperatives Scheduling)
    - orjson fuer C-Speed JSON Parsing (kein GIL-Blocking)
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
except ImportError:
    import json
    def json_loads(data): return json.loads(data)

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
        self._reconnect_count = 0
        self._events_processed = 0
        self._last_event_ts = 0.0
        self._resub_needed = False
        self._ws_ref = None  # Referenz fuer Re-Subscribe

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

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._ws_loop(), name="clob_ws")
        logger.info(f"CLOB WS: Gestartet ({len(self._token_ids)} tokens)")

    async def stop(self) -> None:
        self._running = False
        self._connected = False
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
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=3,
                    max_size=2**20,  # 1MB max message
                ) as ws:
                    self._connected = True
                    delay = 1.0

                    # Subscribe
                    sub_msg = orjson.dumps({
                        "assets_ids": tids,
                        "type": "market",
                    }) if "orjson" in dir() else __import__("json").dumps({
                        "assets_ids": tids,
                        "type": "market",
                    })
                    await ws.send(sub_msg if isinstance(sub_msg, str) else sub_msg.decode())
                    self._ws_ref = ws
                    self._resub_needed = False
                    logger.info(f"CLOB WS: Verbunden — {len(tids)} tokens subscribed")

                    async for raw in ws:
                        if not self._running:
                            break
                        if raw == "PING":
                            await ws.send("pong")
                            continue

                        # Re-Subscribe wenn neue Tokens hinzugefuegt wurden
                        if self._resub_needed:
                            new_tids = list(self._token_ids)
                            resub = orjson.dumps({
                                "assets_ids": new_tids,
                                "type": "market",
                            }) if "orjson" in dir() else __import__("json").dumps({
                                "assets_ids": new_tids,
                                "type": "market",
                            })
                            await ws.send(resub if isinstance(resub, str) else resub.decode())
                            self._resub_needed = False
                            logger.info(f"CLOB WS: Re-Subscribed — {len(new_tids)} tokens")

                        # orjson Parse: C-Speed, kein GIL-Blocking
                        try:
                            self._process_message(
                                json_loads(raw) if isinstance(raw, (str, bytes)) else raw
                            )
                        except Exception:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                if self._running:
                    self._reconnect_count += 1
                    logger.warning(f"CLOB WS: {e} — reconnect in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

        self._connected = False

    def _process_message(self, msg: dict | list) -> None:
        """Verarbeitet WS-Deltas und updated RAM-Orderbuch — SCHNELL."""
        events = msg if isinstance(msg, list) else [msg]
        now = time.time()

        for event in events:
            event_type = event.get("event_type", "")
            token_id = event.get("asset_id", "")

            if not token_id or event_type not in ("book", "price_change"):
                continue

            book = self._books.get(token_id)
            if not book:
                continue

            # Update best ask/bid + depth
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

            # Optional Callback (fuer Event-Trigger)
            if self._on_update:
                try:
                    self._on_update(token_id)
                except Exception:
                    pass
