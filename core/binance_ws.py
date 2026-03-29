"""Binance WebSocket Price Oracle — Echtzeit-Tick-Daten für Signalgenerierung."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field

import websockets
from loguru import logger


@dataclass
class PriceTick:
    """Einzelner Preis-Tick von Binance."""
    symbol: str       # z.B. "BTCUSDT"
    bid: float
    ask: float
    mid: float
    timestamp: float  # Unix seconds (local)


@dataclass
class PriceWindow:
    """Rolling-Window der letzten N Sekunden für ein Symbol."""
    symbol: str
    maxlen: int = 500
    _ticks: deque = field(default_factory=lambda: deque(maxlen=500))

    _tick_intervals: deque = field(default_factory=lambda: deque(maxlen=200))

    def add(self, tick: PriceTick) -> None:
        if self._ticks:
            interval_ms = (tick.timestamp - self._ticks[-1].timestamp) * 1000
            if 0 < interval_ms < 5000:  # Ignoriere Lücken > 5s (Reconnects)
                self._tick_intervals.append(interval_ms)
        self._ticks.append(tick)

    def latest(self) -> PriceTick | None:
        return self._ticks[-1] if self._ticks else None

    def momentum(self, window_s: float) -> float | None:
        """Preisänderung in % über die letzten window_s Sekunden.

        Positiv = Preis gestiegen, Negativ = Preis gefallen.
        """
        if len(self._ticks) < 2:
            return None
        now = self._ticks[-1].timestamp
        cutoff = now - window_s
        # Ältester Tick innerhalb des Fensters
        baseline = None
        for t in self._ticks:
            if t.timestamp >= cutoff:
                baseline = t
                break
        if baseline is None:
            return None
        current_mid = self._ticks[-1].mid
        return (current_mid - baseline.mid) / baseline.mid * 100

    def tick_count(self) -> int:
        return len(self._ticks)

    def age_s(self) -> float:
        """Sekunden seit dem letzten Tick."""
        if not self._ticks:
            return float("inf")
        return time.time() - self._ticks[-1].timestamp

    def latency_percentiles(self) -> tuple[float, float]:
        """P50 und P99 Tick-Intervall in ms. (0, 0) wenn zu wenig Daten."""
        if len(self._tick_intervals) < 5:
            return (0.0, 0.0)
        s = sorted(self._tick_intervals)
        n = len(s)
        return (round(s[n // 2], 1), round(s[int(n * 0.99)], 1))


# Binance WebSocket-URLs
_WS_BASE = "wss://stream.binance.com:9443/ws"

# Symbol-Mapping: intern (BTC/USDT) → Binance-Stream-Name
_STREAM_MAP = {
    "BTC/USDT": "btcusdt",
    "ETH/USDT": "ethusdt",
    "SOL/USDT": "solusdt",
    "BNB/USDT": "bnbusdt",
}


class BinanceWebSocketOracle:
    """Streamt Top-of-Book Daten von Binance via WebSocket.

    Nutzt den `bookTicker`-Stream: Snapshot des besten Bid/Ask nach jedem Tick.
    Keine REST-Polling-Latenz — Updates kommen innerhalb <50ms.
    """

    def __init__(self, symbols: list[str], window_size_s: float = 120.0) -> None:
        self.symbols = symbols
        self.window_size_s = window_size_s
        self._windows: dict[str, PriceWindow] = {}
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._reconnect_delay = 1.0
        self._on_tick_callback = None
        self._transit: dict[str, deque] = {}  # Transit Latency pro Symbol

        for sym in symbols:
            self._windows[sym] = PriceWindow(symbol=sym)

    def set_on_tick(self, callback) -> None:
        """Registriert Callback der bei JEDEM Tick sofort feuert (event-driven)."""
        self._on_tick_callback = callback

    async def start(self) -> None:
        """Startet WebSocket-Verbindungen für alle Symbole."""
        self._running = True
        for sym in self.symbols:
            task = asyncio.create_task(self._stream_symbol(sym), name=f"ws_{sym}")
            self._tasks.append(task)
        logger.info(f"Binance WS Oracle gestartet: {self.symbols}")

    async def stop(self) -> None:
        """Stoppt alle WebSocket-Verbindungen."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Binance WS Oracle gestoppt")

    async def _stream_symbol(self, symbol: str) -> None:
        """Hält eine WebSocket-Verbindung am Leben mit Auto-Reconnect."""
        stream_name = _STREAM_MAP.get(symbol)
        if not stream_name:
            logger.error(f"Kein Stream-Mapping für {symbol}")
            return

        url = f"{_WS_BASE}/{stream_name}@bookTicker"
        delay = self._reconnect_delay

        while self._running:
            try:
                logger.info(f"Verbinde {symbol} → {url}")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    delay = self._reconnect_delay  # Reset bei Erfolg
                    logger.info(f"{symbol} WebSocket verbunden")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            self._handle_message(symbol, msg)
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.debug(f"Parse-Fehler {symbol}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(f"{symbol} WS Fehler: {e} — Reconnect in {delay}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)  # Exponential backoff

    def _handle_message(self, symbol: str, msg: dict) -> None:
        """Verarbeitet einen bookTicker-Message."""
        try:
            bid = float(msg["b"])
            ask = float(msg["a"])
            mid = (bid + ask) / 2
            now = time.time()

            # Transit Latency: Binance Event-Time vs Local Time
            transit_ms = 0.0
            server_ts = msg.get("E") or msg.get("T")  # Event timestamp (ms)
            if server_ts:
                transit_ms = now * 1000 - float(server_ts)

            tick = PriceTick(
                symbol=symbol,
                bid=bid,
                ask=ask,
                mid=mid,
                timestamp=now,
            )
            self._windows[symbol].add(tick)

            # Transit Latency tracken (nur sinnvolle Werte)
            if 0 < transit_ms < 5000:
                if symbol not in self._transit:
                    self._transit[symbol] = deque(maxlen=200)
                self._transit[symbol].append(transit_ms)

            # Event-driven: Callback sofort bei jedem Tick
            if self._on_tick_callback:
                try:
                    self._on_tick_callback(symbol, tick)
                except Exception:
                    pass
        except (KeyError, ValueError):
            pass

    # --- Public Interface ---

    def get_latest(self, symbol: str) -> PriceTick | None:
        """Letzter Tick für ein Symbol."""
        return self._windows.get(symbol, PriceWindow(symbol)).latest()

    def get_momentum(self, symbol: str, window_s: float) -> float | None:
        """Momentum in % über window_s Sekunden. None wenn zu wenig Daten."""
        w = self._windows.get(symbol)
        if w is None:
            return None
        return w.momentum(window_s)

    def is_fresh(self, symbol: str, max_age_s: float = 5.0) -> bool:
        """True wenn letzter Tick jünger als max_age_s Sekunden."""
        w = self._windows.get(symbol)
        return w is not None and w.age_s() < max_age_s

    def tick_count(self, symbol: str) -> int:
        w = self._windows.get(symbol)
        return w.tick_count() if w else 0

    def status(self) -> dict[str, dict]:
        """Status aller Streams für Monitoring."""
        result = {}
        for sym, w in self._windows.items():
            tick = w.latest()
            p50, p99 = w.latency_percentiles()

            # Transit Latency (Binance Server → uns)
            transit_p50, transit_p99 = 0.0, 0.0
            transit_samples = self._transit.get(sym)
            if transit_samples and len(transit_samples) >= 5:
                s = sorted(transit_samples)
                n = len(s)
                transit_p50 = round(s[n // 2], 1)
                transit_p99 = round(s[int(n * 0.99)], 1)

            result[sym] = {
                "connected": self.is_fresh(sym),
                "last_price": tick.mid if tick else None,
                "age_s": round(w.age_s(), 2),
                "tick_count": w.tick_count(),
                "p50_ms": p50,
                "p99_ms": p99,
                "transit_p50_ms": transit_p50,
                "transit_p99_ms": transit_p99,
            }
        return result
