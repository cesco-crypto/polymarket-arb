"""Polymarket Latency Arb — Web Dashboard Backend.

FastAPI + WebSocket: Serves the dashboard UI and pushes strategy status at 1Hz.
The strategy runs its own async loops; the dashboard is a passive observer.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from loguru import logger

from config import settings
from strategies.polymarket_latency import PolymarketLatencyStrategy
from utils.logger import setup_logger

app = FastAPI(title="Polymarket Latency Arb")

# --- Global State ---
strategy: PolymarketLatencyStrategy | None = None
start_time: float = 0
connected_clients: set[WebSocket] = set()
_strategy_task: asyncio.Task | None = None
_broadcast_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup() -> None:
    """Startet die Polymarket-Strategie und den Broadcast-Loop."""
    global strategy, start_time, _strategy_task, _broadcast_task

    setup_logger(level=settings.log_level, data_dir=settings.data_dir)
    logger.info("Polymarket Latency Arb Dashboard startet...")

    start_time = time.time()
    strategy = PolymarketLatencyStrategy(settings)

    # Strategy läuft in eigenem Task (selbstständige Loops)
    _strategy_task = asyncio.create_task(strategy.run())

    # Dashboard broadcastet Strategy-Status an WebSocket-Clients (1Hz)
    _broadcast_task = asyncio.create_task(_broadcast_loop())

    # Self-Ping Keep-Alive (nur auf Render, verhindert Sleep nach 15 Min)
    asyncio.create_task(_keep_alive_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    global _strategy_task, _broadcast_task
    if _broadcast_task:
        _broadcast_task.cancel()
    if _strategy_task:
        _strategy_task.cancel()
        try:
            await _strategy_task
        except asyncio.CancelledError:
            pass
    logger.info("Dashboard beendet.")


async def _keep_alive_loop() -> None:
    """Pingt sich selbst alle 10 Minuten um Render Free Tier wach zu halten."""
    import os
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return  # Nur auf Render aktiv, nicht lokal

    await asyncio.sleep(30)
    logger.info(f"Keep-Alive aktiv: {url}")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"{url}/api/status", timeout=aiohttp.ClientTimeout(total=10)) as r:
                    pass
            except Exception:
                pass
            await asyncio.sleep(600)  # 10 Minuten


async def _broadcast_loop() -> None:
    """Broadcastet Strategy-Status alle 1s an alle WebSocket-Clients."""
    # Warte bis Strategy initialisiert ist
    await asyncio.sleep(5)

    while True:
        try:
            if connected_clients and strategy:
                payload = _build_payload()
                await _broadcast(payload)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Broadcast Fehler: {e}")

        await asyncio.sleep(1)  # 1Hz für Live-Countdowns


def _build_payload() -> dict:
    """Liest den aktuellen Strategy-Status und formt ihn für das Dashboard."""
    status = strategy.get_status()

    uptime = int(time.time() - start_time)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)

    executor = status.get("executor", {})
    trading = status.get("paper_trading", {})

    # Wenn Live-Trading aktiv: Balance async holen
    if executor.get("live") and strategy and strategy.executor.is_live:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Wir sind bereits in einer async-Umgebung
                pass  # Balance wird über /api/wallet Endpoint geholt
        except Exception:
            pass

    # Donation tracker: 10% der Gewinne
    total_pnl = trading.get("daily_pnl_usd", 0)
    donation = round(max(0, total_pnl * 0.10), 2)

    return {
        "oracle": status.get("oracle", {}),
        "discovery": status.get("discovery", {}),
        "signals": status.get("recent_signals", []),
        "trading": trading,
        "config": status.get("config", {}),
        "executor": executor,
        "donation_usd": donation,
        "signals_detected": status.get("signals_detected", 0),
        "scan_cycles": status.get("scan_cycles", 0),
        "heartbeat": status.get("heartbeat", {}),
        "running": status.get("running", False),
        "uptime": f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        "timestamp": datetime.now().isoformat(),
    }


async def _broadcast(payload: dict) -> None:
    """Sendet Daten an alle verbundenen WebSocket-Clients."""
    if not connected_clients:
        return
    disconnected = set()
    for ws in connected_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            disconnected.add(ws)
    connected_clients.difference_update(disconnected)


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/status")
async def api_status() -> dict:
    """REST-Endpoint für Debugging."""
    return _build_payload() if strategy else {"error": "not initialized"}


@app.get("/api/backtest")
async def api_backtest() -> dict:
    """Backtest Grid Search Ergebnisse für Quant Lab."""
    import json
    grid_file = Path(__file__).parent.parent / "data" / "backtest" / "grid_results.json"
    if not grid_file.exists():
        return {"error": "No backtest data. Run: python backtest_engine.py"}
    with open(grid_file) as f:
        results = json.load(f)
    return {"results": results, "count": len(results)}


@app.get("/api/wallet")
async def api_wallet() -> dict:
    """Wallet-Balance und Deposit-Info."""
    if not strategy or not strategy.executor.is_live:
        return {"live": False, "balance": 0, "wallet": "", "chain": "Polygon"}
    balance = await strategy.executor.get_balance()
    stats = strategy.executor.stats()
    pnl = strategy.paper_trader.daily_pnl_usd()
    return {
        "live": True,
        "balance": round(balance, 2),
        "wallet": stats.get("wallet", ""),
        "chain": "Polygon",
        "token": "USDC.e",
        "orders_placed": stats.get("orders_placed", 0),
        "total_volume": stats.get("total_volume_usd", 0),
        "donation_10pct": round(max(0, pnl * 0.10), 2),
    }


@app.post("/api/toggle-order-type")
async def toggle_order_type() -> dict:
    """Toggle zwischen Maker und Taker Order-Typ."""
    current = settings.order_type
    new_type = "maker" if current == "taker" else "taker"
    settings.order_type = new_type
    logger.info(f"Order-Typ umgestellt: {current} → {new_type}")
    return {"order_type": new_type, "previous": current}


@app.get("/wallet", response_class=HTMLResponse)
async def wallet_page() -> HTMLResponse:
    """Wallet Management Page."""
    html_path = Path(__file__).parent / "wallet.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/quant-lab", response_class=HTMLResponse)
async def quant_lab() -> HTMLResponse:
    """Quant Research & Backtesting Lab."""
    html_path = Path(__file__).parent / "quant_lab.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    connected_clients.add(ws)
    logger.info(f"Dashboard Client verbunden ({len(connected_clients)} aktiv)")

    try:
        if strategy:
            await ws.send_json(_build_payload())
    except Exception:
        pass

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(ws)
