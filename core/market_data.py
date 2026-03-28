"""Modul 1: Market Data Collector — Orderbuch-Daten von allen Börsen.

Architektur (mit WS Price Feed):
- Bid/Ask: WebSocket (Binance !bookTicker, KuCoin /market/ticker, Bybit tickers.*)
  → Latenz ~5ms statt ~1100ms REST-Batch
- Volume (24h): REST-Fetch alle 5 Minuten, gecacht
- Fallback: reine REST-Abfrage wenn WS noch nicht live
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import ccxt.async_support as ccxt
from loguru import logger

from config import Settings, get_exchange_keys
from core.ws_price_feed import WebSocketPriceFeed
from utils.rate_limiter import RateLimiter


@dataclass
class TickerData:
    """Normalisierte Ticker-Daten einer Börse für ein Symbol."""

    exchange_id: str
    symbol: str
    best_ask: float
    best_bid: float
    ask_volume: float   # USD-Wert am besten Ask (L1-Tiefe, 0 wenn unbekannt)
    bid_volume: float   # USD-Wert am besten Bid (L1-Tiefe, 0 wenn unbekannt)
    last_price: float
    volume_24h_usd: float
    timestamp: float


@dataclass
class LatencyStats:
    """P50/P95/P99 Latenz für eine Börse."""

    exchange_id: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    last_ms: float
    sample_count: int


@dataclass
class ExchangeBalance:
    """USDT-Balance auf einer Börse."""

    exchange_id: str
    usdt_free: float
    usdt_total: float
    has_keys: bool
    error: str = ""


class MarketDataCollector:
    """Sammelt Orderbuch-Daten von mehreren Börsen.

    Primär via WebSocket (sub-10ms), Fallback via REST.
    24h-Volumen wird per REST gecacht (5-Minuten-Intervall).
    """

    _LATENCY_WINDOW = 100
    _VOLUME_REFRESH_INTERVAL_S = 300.0  # 5 Minuten

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchanges: dict[str, ccxt.Exchange] = {}
        self.common_symbols: set[str] = set()
        self.rate_limiter = RateLimiter()
        self._ticker_cache: dict[str, dict[str, TickerData]] = {}
        self._latencies: dict[str, deque[float]] = {}
        self._has_keys: dict[str, bool] = {}

        # WebSocket Price Feed (wird in initialize() gestartet)
        self._ws_feed: WebSocketPriceFeed | None = None

        # Volume-Cache: {exchange_id: {symbol: volume_24h_usd}}
        # Wird initial via REST befüllt, dann alle 5 Min aktualisiert
        self._volume_cache: dict[str, dict[str, float]] = {}
        self._volume_refresh_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        """Erstellt Exchange-Verbindungen, findet gemeinsame Paare, startet WS Feed."""
        for ex_id in self.settings.exchanges:
            exchange = self._create_exchange(ex_id)
            self.exchanges[ex_id] = exchange
            self._latencies[ex_id] = deque(maxlen=self._LATENCY_WINDOW)
            logger.info(f"Exchange {ex_id} initialisiert")

        await self._find_common_symbols()
        logger.info(f"{len(self.common_symbols)} gemeinsame {self.settings.quote_currency}-Paare gefunden")

        # Initiales Volume-Fetch via REST (befüllt den Cache für erste Scans)
        logger.info("Lade initiale 24h-Volumina via REST...")
        await self._refresh_volumes()

        # WebSocket Feed starten (läuft parallel, übernimmt Bid/Ask-Preise)
        if self.settings.use_ws_price_feed:
            self._ws_feed = WebSocketPriceFeed(self.common_symbols)
            await self._ws_feed.start()

            # Hintergrund-Task: Volume alle 5 Min aktualisieren
            self._volume_refresh_task = asyncio.create_task(
                self._volume_refresh_loop(), name="volume-refresh"
            )

    def _create_exchange(self, exchange_id: str) -> ccxt.Exchange:
        """Erstellt eine ccxt Exchange-Instanz."""
        keys = get_exchange_keys(exchange_id)
        exchange_class = getattr(ccxt, exchange_id)

        config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }

        has_keys = bool(keys.api_key and keys.secret)
        self._has_keys[exchange_id] = has_keys

        if keys.api_key:
            config["apiKey"] = keys.api_key
        if keys.secret:
            config["secret"] = keys.secret
        if keys.passphrase:
            config["password"] = keys.passphrase

        return exchange_class(config)

    async def _find_common_symbols(self) -> None:
        """Findet alle USDT-Handelspaare die auf allen Börsen verfügbar sind."""
        all_symbols: dict[str, set[str]] = {}

        for ex_id, exchange in self.exchanges.items():
            try:
                await self.rate_limiter.wait(ex_id)
                t0 = time.perf_counter()
                markets = await exchange.load_markets()
                self._record_latency(ex_id, t0)
                self.rate_limiter.record_success(ex_id)

                symbols = {
                    symbol
                    for symbol, market in markets.items()
                    if market.get("quote") == self.settings.quote_currency
                    and market.get("active", True)
                    and market.get("spot", True)
                }
                all_symbols[ex_id] = symbols
                logger.info(f"{ex_id}: {len(symbols)} aktive {self.settings.quote_currency}-Paare")
            except Exception as e:
                logger.error(f"Fehler beim Laden der Markets von {ex_id}: {e}")
                self.rate_limiter.record_failure(ex_id)
                all_symbols[ex_id] = set()

        if all_symbols:
            self.common_symbols = set.intersection(*all_symbols.values())
        else:
            self.common_symbols = set()

    # --- Latenz-Tracking ---

    def _record_latency(self, ex_id: str, t0: float) -> float:
        """Misst und speichert Latenz seit t0 (perf_counter)."""
        ms = (time.perf_counter() - t0) * 1000
        if ex_id in self._latencies:
            self._latencies[ex_id].append(ms)
        return ms

    def get_latency_stats(self) -> dict[str, LatencyStats]:
        """Berechnet P50/P95/P99 für alle Börsen."""
        result: dict[str, LatencyStats] = {}
        for ex_id, samples in self._latencies.items():
            if not samples:
                result[ex_id] = LatencyStats(ex_id, 0, 0, 0, 0, 0)
                continue

            sorted_samples = sorted(samples)
            n = len(sorted_samples)

            def percentile(p: float) -> float:
                idx = int(p / 100 * n)
                return sorted_samples[min(idx, n - 1)]

            result[ex_id] = LatencyStats(
                exchange_id=ex_id,
                p50_ms=round(percentile(50), 1),
                p95_ms=round(percentile(95), 1),
                p99_ms=round(percentile(99), 1),
                last_ms=round(sorted_samples[-1], 1),
                sample_count=n,
            )
        return result

    # --- Hauptfunktion: Preisabfrage ---

    async def fetch_orderbooks(
        self, symbols: list[str] | None = None
    ) -> dict[str, dict[str, TickerData]]:
        """Holt aktuelle Bid/Ask-Daten.

        Wenn WS Feed live → liest aus Speicher (~1ms).
        Sonst → REST-Batch-Fetch (~1100ms, Fallback).
        """
        if self._ws_feed and self._all_ws_live():
            return self._build_from_ws(symbols)

        # Fallback: REST
        return await self._fetch_via_rest(symbols)

    def _all_ws_live(self) -> bool:
        """True wenn alle konfigurierten Börsen Live-Daten liefern."""
        for ex_id in self.exchanges:
            if not self._ws_feed.is_live(ex_id):
                return False
            # Mindestabdeckung: mind. 50% der Common Symbols mit Live-Quote
            coverage = self._ws_feed.coverage(ex_id)
            if coverage < max(1, len(self.common_symbols) * 0.5):
                return False
        return True

    def _build_from_ws(
        self, symbols: list[str] | None = None
    ) -> dict[str, dict[str, TickerData]]:
        """Baut TickerData-Dict aus WS-Cache + Volume-Cache (kein Netzwerkzugriff)."""
        target = set(symbols) if symbols else self.common_symbols
        max_quote_age_s = 10.0  # Quotes älter als 10s verwerfen
        now = time.time()
        result: dict[str, dict[str, TickerData]] = {}

        t0 = time.perf_counter()
        for ex_id in self.exchanges:
            quotes = self._ws_feed.get_all_quotes(ex_id)
            vol_cache = self._volume_cache.get(ex_id, {})
            ticker_data: dict[str, TickerData] = {}

            for symbol, quote in quotes.items():
                if symbol not in target:
                    continue
                if now - quote.ts > max_quote_age_s:
                    continue  # Veraltetes Quote überspringen

                mid = (quote.bid + quote.ask) / 2
                ticker_data[symbol] = TickerData(
                    exchange_id=ex_id,
                    symbol=symbol,
                    best_ask=quote.ask,
                    best_bid=quote.bid,
                    ask_volume=quote.ask_qty_usd,
                    bid_volume=quote.bid_qty_usd,
                    last_price=mid,
                    volume_24h_usd=vol_cache.get(symbol, 0.0),
                    timestamp=quote.ts,
                )
            result[ex_id] = ticker_data
            # Latenz = Cache-Lesezeit (sollte ~1ms sein)
            self._record_latency(ex_id, t0)

        self._ticker_cache = result
        return result

    async def _fetch_via_rest(
        self, symbols: list[str] | None = None
    ) -> dict[str, dict[str, TickerData]]:
        """REST-Batch-Fetch (Fallback wenn WS nicht live)."""
        target_symbols = symbols or list(self.common_symbols)
        result: dict[str, dict[str, TickerData]] = {}

        tasks = [
            self._fetch_exchange_tickers(ex_id, exchange, target_symbols)
            for ex_id, exchange in self.exchanges.items()
        ]
        exchange_results = await asyncio.gather(*tasks, return_exceptions=True)

        for ex_id, data in zip(self.exchanges.keys(), exchange_results):
            if isinstance(data, Exception):
                logger.error(f"REST-Fetch Fehler bei {ex_id}: {data}")
                result[ex_id] = {}
            else:
                result[ex_id] = data
                # Volume-Cache aus REST-Daten befüllen (Synergieeffekt)
                if ex_id not in self._volume_cache:
                    self._volume_cache[ex_id] = {}
                for sym, td in data.items():
                    if td.volume_24h_usd > 0:
                        self._volume_cache[ex_id][sym] = td.volume_24h_usd

        self._ticker_cache = result
        return result

    async def _fetch_exchange_tickers(
        self, ex_id: str, exchange: ccxt.Exchange, symbols: list[str]
    ) -> dict[str, TickerData]:
        """REST-Ticker-Fetch von einer Börse (inkl. Volume)."""
        data: dict[str, TickerData] = {}
        try:
            await self.rate_limiter.wait(ex_id)
            t0 = time.perf_counter()
            tickers = await exchange.fetch_tickers(symbols)
            self._record_latency(ex_id, t0)
            self.rate_limiter.record_success(ex_id)

            for symbol, ticker in tickers.items():
                if symbol not in symbols:
                    continue

                ask = ticker.get("ask")
                bid = ticker.get("bid")
                last = ticker.get("last")

                if not ask or not bid or not last:
                    continue

                quote_vol = ticker.get("quoteVolume") or 0
                base_vol = ticker.get("baseVolume") or 0
                vol_usd = quote_vol if quote_vol > 0 else base_vol * last

                data[symbol] = TickerData(
                    exchange_id=ex_id,
                    symbol=symbol,
                    best_ask=float(ask),
                    best_bid=float(bid),
                    ask_volume=0,  # REST gibt keine L1-Tiefe
                    bid_volume=0,
                    last_price=float(last),
                    volume_24h_usd=float(vol_usd),
                    timestamp=ticker.get("timestamp", 0) or 0,
                )
        except Exception as e:
            logger.error(f"REST-Fetch Fehler {ex_id}: {e}")
            if not self.rate_limiter.record_failure(ex_id):
                logger.warning(f"Überspringe {ex_id} nach max Retries")

        return data

    # --- Volume-Cache (REST, periodisch) ---

    async def _volume_refresh_loop(self) -> None:
        """Aktualisiert 24h-Volume-Cache alle 5 Minuten via REST."""
        while True:
            await asyncio.sleep(self._VOLUME_REFRESH_INTERVAL_S)
            try:
                logger.debug("Volume-Cache Refresh via REST...")
                await self._refresh_volumes()
            except Exception as e:
                logger.warning(f"Volume-Refresh Fehler: {e}")

    async def _refresh_volumes(self) -> None:
        """REST-Fetch für 24h-Volumina aller Börsen."""
        target_symbols = list(self.common_symbols)
        if not target_symbols:
            return

        tasks = [
            self._fetch_volumes_for_exchange(ex_id, exchange, target_symbols)
            for ex_id, exchange in self.exchanges.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for ex_id, result in zip(self.exchanges.keys(), results):
            if isinstance(result, dict):
                self._volume_cache[ex_id] = result
                logger.debug(f"Volume-Cache {ex_id}: {len(result)} Symbole aktualisiert")
            else:
                logger.warning(f"Volume-Refresh {ex_id} fehlgeschlagen: {result}")

    async def _fetch_volumes_for_exchange(
        self, ex_id: str, exchange: ccxt.Exchange, symbols: list[str]
    ) -> dict[str, float]:
        """Holt 24h-Volume von einer Börse (REST)."""
        try:
            await self.rate_limiter.wait(ex_id)
            t0 = time.perf_counter()
            tickers = await exchange.fetch_tickers(symbols)
            self._record_latency(ex_id, t0)
            self.rate_limiter.record_success(ex_id)

            sym_set = set(symbols)
            volumes: dict[str, float] = {}
            for symbol, ticker in tickers.items():
                if symbol not in sym_set:
                    continue
                last = ticker.get("last") or 0
                quote_vol = ticker.get("quoteVolume") or 0
                base_vol = ticker.get("baseVolume") or 0
                vol_usd = float(quote_vol if quote_vol > 0 else base_vol * last)
                volumes[symbol] = vol_usd
            return volumes
        except Exception as e:
            logger.error(f"Volume-Fetch Fehler {ex_id}: {e}")
            self.rate_limiter.record_failure(ex_id)
            return {}

    # --- Balances ---

    async def fetch_balances(self) -> dict[str, ExchangeBalance]:
        """Holt USDT-Balances von allen Börsen (benötigt API-Keys)."""
        tasks = [
            self._fetch_single_balance(ex_id, exchange)
            for ex_id, exchange in self.exchanges.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            ex_id: (
                result
                if not isinstance(result, Exception)
                else ExchangeBalance(ex_id, 0, 0, self._has_keys.get(ex_id, False), str(result))
            )
            for ex_id, result in zip(self.exchanges.keys(), results)
        }

    async def _fetch_single_balance(
        self, ex_id: str, exchange: ccxt.Exchange
    ) -> ExchangeBalance:
        """Holt Balance von einer Börse."""
        has_keys = self._has_keys.get(ex_id, False)
        if not has_keys:
            return ExchangeBalance(ex_id, 0, 0, False, "No API keys")

        try:
            t0 = time.perf_counter()
            bal = await exchange.fetch_balance()
            self._record_latency(ex_id, t0)
            usdt = bal.get("USDT", {})
            return ExchangeBalance(
                exchange_id=ex_id,
                usdt_free=float(usdt.get("free") or 0),
                usdt_total=float(usdt.get("total") or 0),
                has_keys=True,
            )
        except Exception as e:
            return ExchangeBalance(ex_id, 0, 0, has_keys, str(e)[:80])

    async def fetch_orderbook(
        self, exchange_id: str, symbol: str
    ) -> dict[str, Any] | None:
        """Holt ein einzelnes L2-Orderbuch für tiefere Depth-Prüfung."""
        exchange = self.exchanges.get(exchange_id)
        if not exchange:
            return None

        try:
            await self.rate_limiter.wait(exchange_id)
            t0 = time.perf_counter()
            ob = await exchange.fetch_order_book(symbol, limit=self.settings.orderbook_depth)
            self._record_latency(exchange_id, t0)
            self.rate_limiter.record_success(exchange_id)
            return ob
        except Exception as e:
            logger.error(f"Orderbuch-Fehler {exchange_id}/{symbol}: {e}")
            self.rate_limiter.record_failure(exchange_id)
            return None

    # --- WS-Status für Dashboard ---

    def ws_status(self) -> dict:
        """WebSocket-Verbindungsstatus für Dashboard."""
        if not self._ws_feed:
            return {"enabled": False}
        conn = self._ws_feed.connection_status()
        return {
            "enabled": True,
            "connections": conn,
            "coverage": {
                ex: self._ws_feed.coverage(ex) for ex in self.exchanges
            },
            "live": {
                ex: self._ws_feed.is_live(ex) for ex in self.exchanges
            },
        }

    async def close(self) -> None:
        """Schließt alle Verbindungen."""
        if self._volume_refresh_task:
            self._volume_refresh_task.cancel()
            try:
                await self._volume_refresh_task
            except asyncio.CancelledError:
                pass

        if self._ws_feed:
            await self._ws_feed.stop()

        for ex_id, exchange in self.exchanges.items():
            try:
                await exchange.close()
                logger.debug(f"{ex_id} Verbindung geschlossen")
            except Exception as e:
                logger.warning(f"Fehler beim Schließen von {ex_id}: {e}")
