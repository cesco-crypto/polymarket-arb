"""WebSocket Price Feed — Echtzeit Bid/Ask + L1-Tiefe für alle 3 Börsen.

Latenz: ~5ms statt ~1100ms REST-Batch.

Protokolle:
- Binance:  wss://stream.binance.com:9443/ws — SUBSCRIBE Methode (bookTicker)
- KuCoin:   wss://..?token=...  — /market/ticker:SYM (symbol im topic-Feld)
- Bybit:    wss://stream.bybit.com/v5/public/spot — tickers.SYM
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import aiohttp
import websockets
from loguru import logger


@dataclass
class L1Quote:
    """Best Bid/Ask + L1-Tiefe eines Symbols (WebSocket-Snapshot)."""

    bid: float
    ask: float
    bid_qty_usd: float  # USD-Wert am besten Bid (qty * price)
    ask_qty_usd: float  # USD-Wert am besten Ask (qty * price)
    ts: float           # Unix timestamp des letzten Updates


class WebSocketPriceFeed:
    """Hält Echtzeit-Quotes für alle Symbole via WebSocket.

    Symbol-Format ist immer "BASE/QUOTE" (z.B. "BTC/USDT").
    """

    _BINANCE_WS = "wss://stream.binance.com:9443/ws"
    _KUCOIN_TOKEN_URL = "https://api.kucoin.com/api/v1/bullet-public"
    _BYBIT_WS = "wss://stream.bybit.com/v5/public/spot"

    def __init__(self, symbols: set[str]) -> None:
        self.symbols = symbols
        self._quotes: dict[str, dict[str, L1Quote]] = {
            "binance": {},
            "kucoin": {},
            "bybit": {},
        }
        self._connected: dict[str, bool] = {ex: False for ex in self._quotes}
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._binance_loop(), name="ws-binance"),
            asyncio.create_task(self._kucoin_loop(), name="ws-kucoin"),
            asyncio.create_task(self._bybit_loop(), name="ws-bybit"),
        ]
        logger.info(f"WS Price Feed gestartet ({len(self.symbols)} Symbole, 3 Börsen)")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("WS Price Feed gestoppt")

    # --- Public Interface ---

    def get_all_quotes(self, exchange_id: str) -> dict[str, L1Quote]:
        return dict(self._quotes.get(exchange_id, {}))

    def is_live(self, exchange_id: str, max_stale_s: float = 10.0) -> bool:
        if not self._connected.get(exchange_id):
            return False
        quotes = self._quotes.get(exchange_id, {})
        if not quotes:
            return False
        now = time.time()
        return any(now - q.ts < max_stale_s for q in quotes.values())

    def coverage(self, exchange_id: str) -> int:
        return len(self._quotes.get(exchange_id, {}))

    def connection_status(self) -> dict[str, bool]:
        return dict(self._connected)

    # --- Binance ---

    async def _binance_loop(self) -> None:
        """Binance: Verbinde zu /ws, dann SUBSCRIBE bookTicker für alle Symbole."""
        sym_map = {s.replace("/", "").lower(): s for s in self.symbols}
        # Subscription-Params: ["btcusdt@bookTicker", "ethusdt@bookTicker", ...]
        all_streams = [f"{raw}@bookTicker" for raw in sym_map.keys()]

        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self._BINANCE_WS, ping_interval=20, ping_timeout=10,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self._connected["binance"] = True
                    backoff = 1.0

                    # Subscribe in Batches von 200 (Binance Limit: 200 pro SUBSCRIBE)
                    for i in range(0, len(all_streams), 200):
                        batch = all_streams[i : i + 200]
                        await ws.send(json.dumps({
                            "method": "SUBSCRIBE",
                            "params": batch,
                            "id": i + 1,
                        }))

                    logger.info(f"Binance WS SUBSCRIBE gesendet ({len(all_streams)} Streams)")

                    msg_count = 0
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        # Skip subscription confirmations
                        if "result" in msg or "id" in msg:
                            continue

                        raw_sym = msg.get("s", "").lower()
                        symbol = sym_map.get(raw_sym)
                        if not symbol:
                            continue

                        try:
                            bid = float(msg["b"])
                            ask = float(msg["a"])
                            bid_qty = float(msg.get("B", 0))
                            ask_qty = float(msg.get("A", 0))
                        except (KeyError, ValueError):
                            continue

                        if bid > 0 and ask > 0:
                            self._quotes["binance"][symbol] = L1Quote(
                                bid=bid, ask=ask,
                                bid_qty_usd=bid_qty * bid,
                                ask_qty_usd=ask_qty * ask,
                                ts=time.time(),
                            )
                            msg_count += 1
                            if msg_count == 1:
                                logger.info(f"Binance WS: erste Quote empfangen ({symbol})")
                            elif msg_count == 100:
                                logger.info(f"Binance WS: {self.coverage('binance')} Symbole live")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["binance"] = False
                if self._running:
                    logger.warning(f"Binance WS Fehler: {e} — Retry in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        self._connected["binance"] = False

    # --- KuCoin ---

    async def _kucoin_loop(self) -> None:
        """KuCoin: Symbol wird aus dem topic-Feld extrahiert (nicht subject)."""
        kc_map = {s.replace("/", "-"): s for s in self.symbols}
        kc_symbols = list(kc_map.keys())

        backoff = 1.0
        while self._running:
            try:
                ws_url = await self._kucoin_ws_url()
                if not ws_url:
                    logger.warning("KuCoin WS Token fehlgeschlagen — Retry in 30s")
                    await asyncio.sleep(30)
                    continue

                async with websockets.connect(ws_url, ping_interval=18) as ws:
                    self._connected["kucoin"] = True
                    backoff = 1.0

                    # Batches à 100 subscriben
                    for i in range(0, len(kc_symbols), 100):
                        batch = kc_symbols[i : i + 100]
                        await ws.send(json.dumps({
                            "id": str(int(time.time() * 1000) + i),
                            "type": "subscribe",
                            "topic": "/market/ticker:" + ",".join(batch),
                            "privateChannel": False,
                            "response": True,
                        }))
                    logger.info(f"KuCoin WS verbunden ({len(kc_symbols)} Symbole)")

                    msg_count = 0
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        if msg.get("type") != "message":
                            continue

                        # Symbol aus topic extrahieren: "/market/ticker:BTC-USDT" → "BTC-USDT"
                        topic = msg.get("topic", "")
                        if ":" not in topic:
                            continue
                        kc_sym = topic.split(":")[-1]
                        symbol = kc_map.get(kc_sym)
                        if not symbol:
                            continue

                        data = msg.get("data", {})
                        try:
                            bid = float(data["bestBid"])
                            ask = float(data["bestAsk"])
                            bid_sz = float(data.get("bestBidSize", 0))
                            ask_sz = float(data.get("bestAskSize", 0))
                        except (KeyError, ValueError):
                            continue

                        if bid > 0 and ask > 0:
                            self._quotes["kucoin"][symbol] = L1Quote(
                                bid=bid, ask=ask,
                                bid_qty_usd=bid_sz * bid,
                                ask_qty_usd=ask_sz * ask,
                                ts=time.time(),
                            )
                            msg_count += 1
                            if msg_count == 1:
                                logger.info(f"KuCoin WS: erste Quote empfangen ({symbol})")
                            elif msg_count == 100:
                                logger.info(f"KuCoin WS: {self.coverage('kucoin')} Symbole live")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["kucoin"] = False
                if self._running:
                    logger.warning(f"KuCoin WS Fehler: {e} — Retry in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        self._connected["kucoin"] = False

    async def _kucoin_ws_url(self) -> str | None:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.post(self._KUCOIN_TOKEN_URL) as resp:
                    data = await resp.json()
                    token = data["data"]["token"]
                    endpoint = data["data"]["instanceServers"][0]["endpoint"]
                    return f"{endpoint}?token={token}"
        except Exception as e:
            logger.error(f"KuCoin WS Token Fehler: {e}")
            return None

    # --- Bybit ---

    async def _bybit_loop(self) -> None:
        """Bybit V5 Spot — orderbook.1 für L1 Bid/Ask (Spot-Tickers haben kein bid1Price!)."""
        bb_map = {s.replace("/", ""): s for s in self.symbols}
        bb_symbols = list(bb_map.keys())[:200]  # Bybit Limit: 200 Subs/Connection

        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self._BYBIT_WS,
                    ping_interval=None,  # Bybit handelt Pings selbst
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self._connected["bybit"] = True
                    backoff = 1.0

                    # orderbook.1.SYMBOL — Level 1 Orderbuch (bester Bid/Ask)
                    for i in range(0, len(bb_symbols), 10):
                        batch = bb_symbols[i : i + 10]
                        await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": [f"orderbook.1.{s}" for s in batch],
                        }))
                        await asyncio.sleep(0.3)

                    logger.info(f"Bybit WS verbunden — orderbook.1 für {len(bb_symbols)} Symbole")

                    # Bybit Heartbeat: alle 20s ping senden
                    async def _bybit_heartbeat(ws_ref):
                        while self._running:
                            await asyncio.sleep(20)
                            try:
                                await ws_ref.send(json.dumps({"op": "ping"}))
                            except Exception:
                                break
                    hb_task = asyncio.create_task(_bybit_heartbeat(ws))

                    msg_count = 0
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        # Pong und subscription confirmations überspringen
                        if msg.get("op"):
                            continue

                        topic = msg.get("topic", "")
                        # orderbook.1.BTCUSDT → extract "BTCUSDT"
                        if not topic.startswith("orderbook.1."):
                            continue

                        raw_sym = topic[12:]  # len("orderbook.1.") == 12
                        symbol = bb_map.get(raw_sym)
                        if not symbol:
                            continue

                        data = msg.get("data", {})
                        bids = data.get("b", [])
                        asks = data.get("a", [])

                        if not bids or not asks:
                            continue

                        try:
                            bid = float(bids[0][0])
                            bid_sz = float(bids[0][1])
                            ask = float(asks[0][0])
                            ask_sz = float(asks[0][1])
                        except (IndexError, ValueError):
                            continue

                        if bid > 0 and ask > 0:
                            self._quotes["bybit"][symbol] = L1Quote(
                                bid=bid, ask=ask,
                                bid_qty_usd=bid_sz * bid,
                                ask_qty_usd=ask_sz * ask,
                                ts=time.time(),
                            )
                            msg_count += 1
                            if msg_count == 1:
                                logger.info(f"Bybit WS: erste Quote empfangen ({symbol})")
                            elif msg_count == 100:
                                logger.info(f"Bybit WS: {self.coverage('bybit')} Symbole live")
                    hb_task.cancel()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["bybit"] = False
                if self._running:
                    logger.warning(f"Bybit WS Fehler: {e} — Retry in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        self._connected["bybit"] = False
