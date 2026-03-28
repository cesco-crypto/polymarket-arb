"""Polymarket CLOB Client — Marktdaten und Orderbuch via REST + WebSocket."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import aiohttp
import websockets
from loguru import logger


# --- API-Endpunkte ---
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
WS_BASE = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class OutcomeBook:
    """Orderbuch für ein einzelnes Outcome-Token (z.B. BTC-UP)."""
    token_id: str
    outcome_label: str     # "UP" / "DOWN" / "YES" / "NO"
    best_bid: float = 0.0  # Höchster Käuferpreis (0–1)
    best_ask: float = 0.0  # Niedrigster Verkäuferpreis (0–1)
    mid: float = 0.0
    updated_at: float = 0.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid if self.best_ask > self.best_bid else 0.0

    @property
    def is_fresh(self) -> bool:
        return time.time() - self.updated_at < 10.0


@dataclass
class PolymarketMarket:
    """Ein aktives Polymarket-Markt mit zwei Outcomes."""
    condition_id: str
    question: str           # z.B. "Will BTC be higher in 5 minutes?"
    asset: str              # "BTC" / "ETH"
    timeframe_min: int      # 5 / 15 / 60
    end_date_iso: str       # ISO-Timestamp des Ablaufs
    end_timestamp: float    # Unix timestamp

    outcomes: dict[str, OutcomeBook] = field(default_factory=dict)
    # keys: "UP"/"DOWN" oder "YES"/"NO"

    @property
    def seconds_to_expiry(self) -> float:
        return max(0.0, self.end_timestamp - time.time())

    @property
    def is_active(self) -> bool:
        return self.seconds_to_expiry > 0

    def get_up_book(self) -> OutcomeBook | None:
        return self.outcomes.get("UP") or self.outcomes.get("YES")

    def get_down_book(self) -> OutcomeBook | None:
        return self.outcomes.get("DOWN") or self.outcomes.get("NO")


class PolymarketClient:
    """Liest Marktdaten und Orderbücher von Polymarket.

    Phase 2 (Paper Trading): Read-only, keine Authentifizierung nötig.
    """

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.target_assets = [s.replace("/USDT", "").upper() for s in (symbols or ["BTC/USDT", "ETH/USDT"])]
        self.markets: dict[str, PolymarketMarket] = {}  # condition_id → Market
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None
        self._running = False

    async def initialize(self) -> None:
        """Erstellt HTTP-Session und lädt aktive Märkte."""
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": "arbitrage-bot/1.0"},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        await self.refresh_markets()
        logger.info(f"Polymarket Client: {len(self.markets)} Märkte geladen für {self.target_assets}")

    async def close(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
        if self._session:
            await self._session.close()

    # --- Markt-Discovery ---

    async def refresh_markets(self) -> None:
        """Findet aktive BTC/ETH Kurzzeit-Märkte via Gamma API."""
        new_markets: dict[str, PolymarketMarket] = {}

        for asset in self.target_assets:
            try:
                markets = await self._fetch_asset_markets(asset)
                for m in markets:
                    new_markets[m.condition_id] = m
                logger.info(f"Polymarket: {len(markets)} aktive {asset}-Märkte gefunden")
            except Exception as e:
                logger.error(f"Polymarket Markt-Discovery Fehler für {asset}: {e}")

        self.markets = new_markets

        # Orderbücher initial laden
        if self.markets:
            await self._fetch_all_orderbooks()

    async def _fetch_asset_markets(self, asset: str) -> list[PolymarketMarket]:
        """Sucht aktive Kurzzeit-Märkte für ein Asset über die Gamma API."""
        results = []
        params = {
            "active": "true",
            "closed": "false",
            "order": "end_date_min",
            "ascending": "true",
            "limit": 100,
        }

        async with self._session.get(f"{GAMMA_BASE}/markets", params=params) as resp:
            if resp.status != 200:
                logger.warning(f"Gamma API Fehler: {resp.status}")
                return []
            data = await resp.json()

        markets_data = data if isinstance(data, list) else data.get("markets", [])

        for m in markets_data:
            question = m.get("question", "")
            # Filter: enthält Asset und Zeitrahmen-Keywords
            if not self._is_relevant_market(question, asset):
                continue

            condition_id = m.get("conditionId") or m.get("condition_id", "")
            if not condition_id:
                continue

            timeframe = self._parse_timeframe(question)
            end_date = m.get("endDate") or m.get("end_date", "")
            end_ts = self._parse_iso_timestamp(end_date)

            # Nur Märkte die noch > 30s laufen
            if end_ts - time.time() < 30:
                continue

            # Token-IDs für UP/DOWN aus CLOB laden
            outcomes = await self._fetch_market_tokens(condition_id)
            if not outcomes:
                continue

            market = PolymarketMarket(
                condition_id=condition_id,
                question=question,
                asset=asset,
                timeframe_min=timeframe,
                end_date_iso=end_date,
                end_timestamp=end_ts,
                outcomes=outcomes,
            )
            results.append(market)

        return results

    def _is_relevant_market(self, question: str, asset: str) -> bool:
        """Prüft ob eine Marktfrage für unser Asset und Zeitrahmen relevant ist."""
        q = question.upper()
        asset_match = asset.upper() in q
        # Zeitrahmen-Schlüsselwörter
        time_keywords = ["MINUTE", "MIN", "HOUR", "5-MIN", "15-MIN", "1-HOUR"]
        time_match = any(kw in q for kw in time_keywords)
        # Richtungs-Schlüsselwörter
        direction_keywords = ["HIGHER", "LOWER", "UP", "DOWN", "ABOVE", "BELOW"]
        direction_match = any(kw in q for kw in direction_keywords)
        return asset_match and (time_match or direction_match)

    def _parse_timeframe(self, question: str) -> int:
        """Extrahiert Zeitrahmen in Minuten aus der Marktfrage."""
        import re
        q = question.upper()
        patterns = [
            (r"(\d+)\s*-?\s*MINUTE", 1),
            (r"(\d+)\s*MIN", 1),
            (r"(\d+)\s*HOUR", 60),
        ]
        for pattern, multiplier in patterns:
            match = re.search(pattern, q)
            if match:
                return int(match.group(1)) * multiplier
        return 5  # Default: 5 Minuten

    def _parse_iso_timestamp(self, iso: str) -> float:
        """Konvertiert ISO-8601 Timestamp zu Unix-Timestamp."""
        if not iso:
            return 0.0
        from datetime import datetime, timezone
        try:
            # Verschiedene Formate versuchen
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S+00:00"):
                try:
                    dt = datetime.strptime(iso[:26], fmt[:len(fmt)])
                    return dt.replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue
            # Fallback: dateutil
            from datetime import datetime
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    async def _fetch_market_tokens(self, condition_id: str) -> dict[str, OutcomeBook]:
        """Holt Token-IDs und Labels für einen Markt vom CLOB."""
        try:
            async with self._session.get(
                f"{CLOB_BASE}/markets/{condition_id}"
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()

            tokens = data.get("tokens", [])
            outcomes: dict[str, OutcomeBook] = {}

            for token in tokens:
                token_id = token.get("token_id", "")
                outcome = token.get("outcome", "").upper()

                # Normalisiere Labels
                if outcome in ("YES", "UP", "HIGHER", "ABOVE"):
                    label = "UP"
                elif outcome in ("NO", "DOWN", "LOWER", "BELOW"):
                    label = "DOWN"
                else:
                    label = outcome

                outcomes[label] = OutcomeBook(
                    token_id=token_id,
                    outcome_label=label,
                )

            return outcomes
        except Exception as e:
            logger.debug(f"Token-Fetch Fehler für {condition_id}: {e}")
            return {}

    # --- Orderbuch ---

    async def _fetch_all_orderbooks(self) -> None:
        """Lädt Orderbücher für alle aktiven Märkte."""
        tasks = []
        for market in self.markets.values():
            for outcome in market.outcomes.values():
                if outcome.token_id:
                    tasks.append(self._fetch_orderbook(outcome))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_orderbook(self, outcome: OutcomeBook) -> None:
        """Holt aktuelles Orderbuch für ein Outcome-Token."""
        try:
            async with self._session.get(
                f"{CLOB_BASE}/book",
                params={"token_id": outcome.token_id},
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            # Bestes Bid (höchster Preis)
            if bids:
                outcome.best_bid = max(float(b["price"]) for b in bids)
            # Bester Ask (niedrigster Preis)
            if asks:
                outcome.best_ask = min(float(a["price"]) for a in asks)

            if outcome.best_bid > 0 and outcome.best_ask > 0:
                outcome.mid = (outcome.best_bid + outcome.best_ask) / 2

            outcome.updated_at = time.time()
        except Exception as e:
            logger.debug(f"Orderbuch-Fehler {outcome.token_id[:8]}...: {e}")

    async def refresh_orderbooks(self) -> None:
        """Aktualisiert alle Orderbücher (periodisch aufrufen)."""
        await self._fetch_all_orderbooks()

    # --- WebSocket (optionaler Echtzeit-Stream) ---

    async def start_ws(self) -> None:
        """Startet WebSocket-Subscription für alle aktiven Märkte."""
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def _ws_loop(self) -> None:
        """WebSocket-Loop mit Auto-Reconnect."""
        token_ids = [
            outcome.token_id
            for market in self.markets.values()
            for outcome in market.outcomes.values()
            if outcome.token_id
        ]
        if not token_ids:
            logger.warning("Polymarket WS: keine Token-IDs zum Subscriben")
            return

        subscribe_msg = json.dumps({
            "assets_ids": token_ids,
            "type": "market",
        })

        while self._running:
            try:
                async with websockets.connect(WS_BASE, ping_interval=10) as ws:
                    await ws.send(subscribe_msg)
                    logger.info(f"Polymarket WS: {len(token_ids)} Tokens abonniert")

                    async for raw in ws:
                        if not self._running:
                            break
                        if raw == "PING":
                            await ws.send("pong")
                            continue
                        try:
                            self._handle_ws_message(json.loads(raw))
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(f"Polymarket WS Fehler: {e} — Reconnect in 3s")
                    await asyncio.sleep(3)

    def _handle_ws_message(self, msg: dict | list) -> None:
        """Verarbeitet WebSocket-Updates und aktualisiert Orderbücher."""
        events = msg if isinstance(msg, list) else [msg]
        for event in events:
            event_type = event.get("event_type", "")
            token_id = event.get("asset_id", "")

            # Finde zugehöriges Outcome
            outcome = self._find_outcome_by_token(token_id)
            if not outcome:
                continue

            if event_type in ("book", "price_change"):
                bids = event.get("bids", [])
                asks = event.get("asks", [])
                if bids:
                    outcome.best_bid = max(float(b["price"]) for b in bids)
                if asks:
                    outcome.best_ask = min(float(a["price"]) for a in asks)
                if outcome.best_bid and outcome.best_ask:
                    outcome.mid = (outcome.best_bid + outcome.best_ask) / 2
                outcome.updated_at = time.time()

    def _find_outcome_by_token(self, token_id: str) -> OutcomeBook | None:
        for market in self.markets.values():
            for outcome in market.outcomes.values():
                if outcome.token_id == token_id:
                    return outcome
        return None

    # --- Public Interface ---

    def get_active_markets(self) -> list[PolymarketMarket]:
        """Gibt alle noch aktiven Märkte zurück."""
        return [m for m in self.markets.values() if m.is_active]

    def get_markets_for_asset(self, asset: str) -> list[PolymarketMarket]:
        """Märkte für ein bestimmtes Asset (BTC/ETH)."""
        return [m for m in self.get_active_markets() if m.asset == asset]

    def status(self) -> dict:
        """Status-Überblick für Dashboard."""
        active = self.get_active_markets()
        return {
            "total_markets": len(self.markets),
            "active_markets": len(active),
            "assets": self.target_assets,
            "markets": [
                {
                    "question": m.question[:60],
                    "asset": m.asset,
                    "timeframe_min": m.timeframe_min,
                    "seconds_to_expiry": round(m.seconds_to_expiry, 0),
                    "up_ask": m.get_up_book().best_ask if m.get_up_book() else None,
                    "down_ask": m.get_down_book().best_ask if m.get_down_book() else None,
                }
                for m in active[:10]
            ],
        }
