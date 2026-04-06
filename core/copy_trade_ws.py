"""Copy Trade WebSocket — Echtzeit Market-Events fuer schnellere Trade-Erkennung.

Subscribed auf Polymarket CLOB WebSocket fuer Markets, auf denen tracked Wallets
aktiv sind. Bei price_change Events wird der Poll-Loop sofort geweckt statt
auf den naechsten 1.5s-Zyklus zu warten.

Graceful Degradation: Bei WS-Ausfall laeuft der regulaere Polling-Fallback weiter.

Token -> Wallet Mapping:
    Jeder token_id ist 1:N mit Wallet-Adressen verknuepft.
    Wenn ein price_change auf token_id "abc" kommt, wissen wir:
    -> Wallets {"0x204f...", "0x6a72..."} sind auf diesem Market aktiv.
    -> Nur diese Wallets werden sofort gepollt (targeted fast-path).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Callable

import websockets
from loguru import logger

# Polymarket CLOB WebSocket
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class CopyTradeWebSocket:
    """Echtzeit-Erkennung von Marktbewegungen auf Polymarket via WebSocket.

    Subscribed auf token_ids von Markets, auf denen unsere Tracked Wallets
    aktiv sind. Bei Preisaenderungen feuert der Callback sofort.

    Architektur:
        1. subscribe_market(token_id, wallet_addr) baut das Mapping auf
        2. WS empfaengt price_change/book/trade Events
        3. Callback feuert mit token_id -> Strategy schlaegt Wallets nach
        4. Poll-Loop wird via asyncio.Event sofort geweckt
    """

    def __init__(
        self,
        on_market_activity: Callable[[str], None],
        *,
        debounce_s: float = 0.5,
        ping_interval_s: float = 10,
        max_reconnect_delay_s: float = 30.0,
    ) -> None:
        # Callback (synchron, setzt asyncio.Event im Strategy-Code)
        self._on_activity = on_market_activity

        # Token -> Wallet Mapping (1:N)
        self._subscriptions: dict[str, set[str]] = defaultdict(set)

        # Debounce: Verhindert rapid-fire callbacks fuer dasselbe Token
        self._last_event_ts: dict[str, float] = {}
        self._debounce_s = debounce_s

        # Connection Management
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = max_reconnect_delay_s
        self._ping_interval = ping_interval_s

        # Metrics
        self._events_received = 0
        self._events_fired = 0
        self._last_connected_at = 0.0
        self._reconnect_count = 0

    # ---- Public API ------------------------------------------------

    async def start(self) -> None:
        """Startet WebSocket-Connection im Hintergrund."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._ws_loop(), name="copy_trade_ws")
        logger.info(
            f"CopyTradeWS: Gestartet ({len(self._subscriptions)} Tokens subscribed)"
        )

    async def stop(self) -> None:
        """Stoppt WebSocket sauber."""
        self._running = False
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("CopyTradeWS: Gestoppt")

    def subscribe_market(self, token_id: str, wallet_addr: str) -> None:
        """Verknuepft einen token_id mit einer Wallet-Adresse.

        Mehrfache Wallets pro Token sind erlaubt (z.B. swisstony + RN1
        traden beide auf demselben Market).

        Subscription-Updates werden beim naechsten Reconnect aktiv.
        """
        if not token_id or not wallet_addr:
            return
        addr = wallet_addr.lower()
        if addr not in self._subscriptions[token_id]:
            self._subscriptions[token_id].add(addr)
            logger.debug(
                f"CopyTradeWS: +Subscribe {token_id[:16]}... <- {addr[:10]}..."
            )

    def unsubscribe_market(self, token_id: str) -> None:
        """Entfernt einen Token komplett (z.B. Market resolved)."""
        if token_id in self._subscriptions:
            del self._subscriptions[token_id]
            self._last_event_ts.pop(token_id, None)

    def get_wallets_for_token(self, token_id: str) -> set[str]:
        """Gibt alle Wallet-Adressen zurueck, die auf diesem Token aktiv sind."""
        return set(self._subscriptions.get(token_id, set()))

    def prune_stale(self, max_age_s: float = 86400) -> int:
        """Entfernt Subscriptions die seit max_age_s kein Event mehr hatten."""
        now = time.time()
        stale = [
            tid for tid, last_ts in self._last_event_ts.items()
            if now - last_ts > max_age_s
        ]
        for tid in stale:
            self._subscriptions.pop(tid, None)
            self._last_event_ts.pop(tid, None)
        if stale:
            logger.info(f"CopyTradeWS: {len(stale)} stale Subscriptions gepruned")
        return len(stale)

    def status(self) -> dict:
        """Status-Dict fuer Dashboard / get_status()."""
        return {
            "connected": self._connected,
            "subscribed_tokens": len(self._subscriptions),
            "subscribed_wallets": len(
                {a for addrs in self._subscriptions.values() for a in addrs}
            ),
            "events_received": self._events_received,
            "events_fired": self._events_fired,
            "reconnect_count": self._reconnect_count,
            "uptime_s": round(time.time() - self._last_connected_at, 1)
            if self._connected
            else 0,
        }

    # ---- WebSocket Loop --------------------------------------------

    async def _ws_loop(self) -> None:
        """WebSocket-Hauptloop mit Auto-Reconnect und Exponential Backoff."""
        delay = self._reconnect_delay

        while self._running:
            token_ids = list(self._subscriptions.keys())
            if not token_ids:
                # Nichts zu subscriben — warte und pruefe erneut
                logger.debug("CopyTradeWS: Keine Tokens — warte 10s")
                await asyncio.sleep(10)
                continue

            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_interval,
                    close_timeout=5,
                ) as ws:
                    # Connection erfolgreich
                    self._connected = True
                    self._last_connected_at = time.time()
                    delay = self._reconnect_delay  # Reset backoff

                    # Subscribe
                    subscribe_msg = json.dumps({
                        "assets_ids": token_ids,
                        "type": "market",
                    })
                    await ws.send(subscribe_msg)
                    logger.info(
                        f"CopyTradeWS: Verbunden — {len(token_ids)} Tokens subscribed"
                    )

                    # Message Loop
                    async for raw in ws:
                        if not self._running:
                            break

                        # Polymarket PING/pong Keepalive
                        if raw == "PING":
                            await ws.send("pong")
                            continue

                        try:
                            self._handle_message(json.loads(raw))
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                if self._running:
                    self._reconnect_count += 1
                    logger.warning(
                        f"CopyTradeWS: Fehler: {e} — "
                        f"Reconnect #{self._reconnect_count} in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._max_reconnect_delay)

        self._connected = False

    def _handle_message(self, msg: dict | list) -> None:
        """Verarbeitet WS-Messages und feuert Callback bei relevanten Events."""
        events = msg if isinstance(msg, list) else [msg]
        for event in events:
            event_type = event.get("event_type", "")
            token_id = event.get("asset_id", "")

            if not token_id or event_type not in (
                "price_change", "book", "trade", "last_trade_price",
            ):
                continue

            self._events_received += 1

            # Nur feuern wenn Token subscribed ist
            if token_id not in self._subscriptions:
                continue

            # Debounce: Nicht oefter als alle 0.5s pro Token
            now = time.time()
            last = self._last_event_ts.get(token_id, 0)
            if now - last < self._debounce_s:
                continue

            self._last_event_ts[token_id] = now
            self._events_fired += 1

            # Callback feuern (synchron — setzt asyncio.Event)
            try:
                self._on_activity(token_id)
            except Exception as e:
                logger.debug(f"CopyTradeWS callback error: {e}")
