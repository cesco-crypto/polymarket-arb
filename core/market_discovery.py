"""Market Discovery — Slug-basierte Erkennung von Polymarket 5m/15m Krypto-Märkten.

Diese Märkte sind ephemeral: alle 5 bzw. 15 Minuten wird ein neuer Markt erstellt.
Sie sind NICHT über Text-Suche oder Standard-Gamma-API auffindbar.

Discovery-Methode:
1. Berechne Unix-Timestamp für aktuelles/nächstes Zeitfenster (5m: :00/:05/:10/... | 15m: :00/:15/:30/:45)
2. Konstruiere Slug: {asset}-updown-{timeframe}-{timestamp}
3. Fetch Marktdaten + Token-IDs von Gamma API
4. Rollover: Wechsle automatisch zum nächsten Fenster wenn das aktuelle abläuft

Resolution Oracle: Chainlink BTC/USD bzw. ETH/USD (NICHT Binance!)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import aiohttp
from loguru import logger


GAMMA_API = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"

# Unterstützte Assets und Zeitfenster
ASSETS = ["btc", "eth"]
TIMEFRAMES = {
    "5m": 300,    # 5 Minuten
    "15m": 900,   # 15 Minuten
}


@dataclass
class MarketWindow:
    """Ein einzelnes 5m/15m Prediction-Market-Fenster."""

    slug: str
    asset: str               # "btc" / "eth"
    timeframe: str           # "5m" / "15m"
    window_start_ts: int     # Unix timestamp Fenster-Start
    window_end_ts: int       # Unix timestamp Fenster-Ende
    condition_id: str = ""
    question: str = ""

    # Token IDs für UP/DOWN
    up_token_id: str = ""
    down_token_id: str = ""

    # Orderbook (live aktualisiert)
    up_best_bid: float = 0.0
    up_best_ask: float = 0.0
    down_best_bid: float = 0.0
    down_best_ask: float = 0.0
    up_bid_size: float = 0.0   # Shares am Best Bid
    up_ask_size: float = 0.0   # Shares am Best Ask
    orderbook_ts: float = 0.0  # Letztes Orderbook-Update

    # Gamma-Preise (Mid-Price Referenz)
    gamma_up_price: float = 0.0
    gamma_down_price: float = 0.0
    liquidity_usd: float = 0.0

    fetched: bool = False     # True wenn Token-IDs geladen

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.window_end_ts - time.time())

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.window_end_ts

    @property
    def is_tradeable(self) -> bool:
        """True wenn: Token-IDs vorhanden, noch >15s bis Ablauf, Orderbook vorhanden."""
        return (
            self.fetched
            and self.up_token_id
            and self.seconds_remaining > 15
            and self.up_best_ask > 0
        )

    @property
    def orderbook_fresh(self) -> bool:
        return time.time() - self.orderbook_ts < 5.0


class MarketDiscovery:
    """Dynamische Erkennung und Verwaltung von Polymarket 5m/15m Märkten.

    Erzeugt Slugs mathematisch, fetcht Token-IDs, hält Orderbücher aktuell.
    Rollt automatisch zum nächsten Fenster über.
    """

    def __init__(
        self,
        assets: list[str] | None = None,
        timeframes: list[str] | None = None,
        min_liquidity_usd: float = 5000.0,
    ) -> None:
        self.target_assets = [a.lower() for a in (assets or ["btc", "eth"])]
        self.target_timeframes = timeframes or ["5m", "15m"]
        self.min_liquidity_usd = min_liquidity_usd

        # Aktive Fenster: {slug: MarketWindow}
        self.windows: dict[str, MarketWindow] = {}
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._refresh_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Startet Discovery und periodische Aktualisierung."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "polymarket-arb/2.0"},
        )
        self._running = True

        # Initial: aktuelle + nächste Fenster laden
        await self._discover_all_windows()

        # Background: Rollover + Orderbook-Refresh
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            f"MarketDiscovery gestartet: {len(self.windows)} Fenster für "
            f"{self.target_assets} × {self.target_timeframes}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    # --- Public Interface ---

    def get_tradeable_windows(self) -> list[MarketWindow]:
        """Gibt alle aktuell handelbaren Fenster zurück."""
        return [w for w in self.windows.values() if w.is_tradeable]

    def get_windows_for_asset(self, asset: str) -> list[MarketWindow]:
        """Alle handelbaren Fenster für ein Asset."""
        asset_lower = asset.lower()
        return [w for w in self.get_tradeable_windows() if w.asset == asset_lower]

    def get_all_token_ids(self) -> list[str]:
        """Alle Token-IDs für WebSocket-Subscription."""
        ids = []
        for w in self.windows.values():
            if w.up_token_id:
                ids.append(w.up_token_id)
            if w.down_token_id:
                ids.append(w.down_token_id)
        return ids

    # --- Slug-Generierung ---

    @staticmethod
    def generate_slugs(
        asset: str, timeframe: str, count: int = 3
    ) -> list[tuple[str, int, int]]:
        """Generiert Slugs für aktuelle + nächste N Fenster.

        Returns:
            List of (slug, window_start_ts, window_end_ts)
        """
        interval_s = TIMEFRAMES[timeframe]
        now = int(time.time())
        current_start = (now // interval_s) * interval_s

        result = []
        for i in range(count):
            start_ts = current_start + (i * interval_s)
            end_ts = start_ts + interval_s
            slug = f"{asset}-updown-{timeframe}-{start_ts}"
            result.append((slug, start_ts, end_ts))

        return result

    # --- Discovery ---

    async def _discover_all_windows(self) -> None:
        """Findet aktuelle und nächste Fenster für alle Assets/Timeframes."""
        tasks = []
        for asset in self.target_assets:
            for tf in self.target_timeframes:
                if tf not in TIMEFRAMES:
                    continue
                slugs = self.generate_slugs(asset, tf, count=3)
                for slug, start_ts, end_ts in slugs:
                    if slug not in self.windows:
                        tasks.append(self._fetch_window(slug, asset, tf, start_ts, end_ts))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Abgelaufene Fenster entfernen
        self._cleanup_expired()

    async def _fetch_window(
        self, slug: str, asset: str, timeframe: str, start_ts: int, end_ts: int
    ) -> None:
        """Holt Marktdaten für ein Fenster von der Gamma API."""
        try:
            async with self._session.get(GAMMA_API, params={"slug": slug}) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            if not data:
                return

            event = data[0]
            markets = event.get("markets", [])
            if not markets:
                return

            market = markets[0]

            # Token-IDs extrahieren
            clob_ids = json.loads(market.get("clobTokenIds", "[]"))
            outcomes = json.loads(market.get("outcomes", "[]"))
            prices = json.loads(market.get("outcomePrices", "[]"))

            if len(clob_ids) < 2 or len(outcomes) < 2:
                return

            # UP/DOWN zuordnen (Reihenfolge: ["Up", "Down"])
            up_idx = 0 if outcomes[0].lower() in ("up", "yes") else 1
            down_idx = 1 - up_idx

            window = MarketWindow(
                slug=slug,
                asset=asset,
                timeframe=timeframe,
                window_start_ts=start_ts,
                window_end_ts=end_ts,
                condition_id=market.get("conditionId", ""),
                question=market.get("question", "")[:80],
                up_token_id=clob_ids[up_idx],
                down_token_id=clob_ids[down_idx],
                gamma_up_price=float(prices[up_idx]) if len(prices) > up_idx else 0,
                gamma_down_price=float(prices[down_idx]) if len(prices) > down_idx else 0,
                liquidity_usd=float(market.get("liquidityNum", 0)),
                fetched=True,
            )

            self.windows[slug] = window

            # Sofort Orderbook holen
            await self._refresh_orderbook(window)

            logger.info(
                f"Market entdeckt: {window.question} | "
                f"Up={window.gamma_up_price:.3f} Down={window.gamma_down_price:.3f} | "
                f"Liq=${window.liquidity_usd:.0f} | "
                f"Ablauf in {window.seconds_remaining:.0f}s"
            )

        except Exception as e:
            logger.debug(f"Fetch-Fehler für {slug}: {e}")

    async def _refresh_orderbook(self, window: MarketWindow) -> None:
        """Holt aktuelles Orderbuch für ein Fenster."""
        if not window.up_token_id:
            return

        try:
            # UP und DOWN Orderbücher parallel holen
            up_task = self._fetch_book(window.up_token_id)
            down_task = self._fetch_book(window.down_token_id)
            up_book, down_book = await asyncio.gather(up_task, down_task)

            if up_book:
                bids = sorted(up_book.get("bids", []), key=lambda x: -float(x["price"]))
                asks = sorted(up_book.get("asks", []), key=lambda x: float(x["price"]))
                if bids:
                    window.up_best_bid = float(bids[0]["price"])
                    window.up_bid_size = float(bids[0]["size"])
                if asks:
                    window.up_best_ask = float(asks[0]["price"])
                    window.up_ask_size = float(asks[0]["size"])

            if down_book:
                bids = sorted(down_book.get("bids", []), key=lambda x: -float(x["price"]))
                asks = sorted(down_book.get("asks", []), key=lambda x: float(x["price"]))
                if bids:
                    window.down_best_bid = float(bids[0]["price"])
                if asks:
                    window.down_best_ask = float(asks[0]["price"])

            window.orderbook_ts = time.time()

        except Exception as e:
            logger.debug(f"Orderbook-Fehler {window.slug}: {e}")

    async def _fetch_book(self, token_id: str) -> dict | None:
        """Holt Orderbuch für ein Token."""
        try:
            async with self._session.get(
                CLOB_BOOK, params={"token_id": token_id}
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    # --- Refresh Loop ---

    async def _refresh_loop(self) -> None:
        """Periodischer Refresh: neue Fenster entdecken, Orderbücher aktualisieren."""
        while self._running:
            try:
                # 1. Neue Fenster entdecken (Rollover)
                await self._discover_all_windows()

                # 2. Orderbücher für aktive Fenster aktualisieren
                active = [w for w in self.windows.values() if not w.is_expired and w.fetched]
                if active:
                    tasks = [self._refresh_orderbook(w) for w in active]
                    await asyncio.gather(*tasks, return_exceptions=True)

                # 3. Abgelaufene entfernen
                removed = self._cleanup_expired()
                if removed:
                    logger.debug(f"Abgelaufene Fenster entfernt: {removed}")

            except Exception as e:
                logger.error(f"Discovery Refresh Fehler: {e}")

            await asyncio.sleep(3)  # Alle 3s refreshen

    def _cleanup_expired(self) -> int:
        """Entfernt abgelaufene Fenster (>30s nach Ablauf)."""
        expired = [
            slug for slug, w in self.windows.items()
            if time.time() > w.window_end_ts + 30
        ]
        for slug in expired:
            del self.windows[slug]
        return len(expired)

    # --- Status ---

    def status(self) -> dict:
        """Status für Dashboard/Logging."""
        windows = []
        for w in sorted(self.windows.values(), key=lambda x: x.window_start_ts):
            windows.append({
                "slug": w.slug,
                "asset": w.asset.upper(),
                "tf": w.timeframe,
                "remaining_s": round(w.seconds_remaining),
                "up_bid": w.up_best_bid,
                "up_ask": w.up_best_ask,
                "down_bid": w.down_best_bid,
                "down_ask": w.down_best_ask,
                "liquidity": round(w.liquidity_usd),
                "tradeable": w.is_tradeable,
            })
        return {
            "total_windows": len(self.windows),
            "tradeable": len(self.get_tradeable_windows()),
            "windows": windows,
        }
