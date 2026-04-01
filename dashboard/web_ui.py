"""Polymarket Latency Arb — Web Dashboard Backend.

FastAPI + WebSocket: Serves the dashboard UI and pushes strategy status at 1Hz.
The strategy runs its own async loops; the dashboard is a passive observer.
"""

from __future__ import annotations

import asyncio
import os
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
        "instance_label": os.environ.get("INSTANCE_LABEL", "LOCAL"),
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


@app.get("/api/journal")
async def api_journal() -> dict:
    """Forensisches Trade Journal — alle Daten, unverlierbar."""
    from core import db
    # Primär: Supabase (persistent). Fallback: lokales Journal.
    db_trades = await db.get_all_trades()
    if db_trades:
        db_stats = await db.get_trade_stats()
        return {"records": db_trades, "stats": db_stats, "count": len(db_trades), "source": "supabase"}
    # Fallback: lokales Journal
    if not strategy:
        return {"error": "not initialized"}
    records = strategy.journal.get_all_records()
    stats = strategy.journal.stats()
    return {"records": records, "stats": stats, "count": len(records), "source": "local"}


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


@app.get("/masterplan", response_class=HTMLResponse)
async def masterplan() -> HTMLResponse:
    """Masterplan — 10-Phase Roadmap to Profitable Live Bot."""
    html_path = Path(__file__).parent / "masterplan.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/backtest/realistic")
async def api_backtest_realistic() -> dict:
    """Realistische Backtest-Ergebnisse (kalibriert mit echten Polymarket-Daten)."""
    import json
    f = Path(__file__).parent.parent / "data" / "backtest" / "grid_results_realistic.json"
    if not f.exists():
        return {"error": "No realistic backtest data. Run: python backtest_engine.py"}
    with open(f) as fp:
        results = json.load(fp)
    return {"results": results, "count": len(results), "mode": "realistic"}


@app.get("/api/backtest/compare")
async def api_backtest_compare() -> dict:
    """Side-by-side Vergleich: Idealistic vs Realistic."""
    import json
    base = Path(__file__).parent.parent / "data" / "backtest"
    out = {"idealistic": [], "realistic": []}
    for mode in ["idealistic", "realistic"]:
        f = base / f"grid_results_{mode}.json"
        if f.exists():
            with open(f) as fp:
                out[mode] = json.load(fp)
    return out


@app.get("/api/monte-carlo")
async def api_monte_carlo(
    trades: int = 200, simulations: int = 1000,
    win_rate: float = 0, avg_win: float = 0, avg_loss: float = 0,
) -> dict:
    """Monte Carlo Simulation — 1000 Permutationen des Trade-Outcomes.

    Kann mit expliziten Parametern oder aus Supabase-Daten laufen.
    """
    import random as rng
    rng.seed(42)

    # Wenn keine Parameter → aus Supabase oder Backtest laden
    if win_rate == 0:
        from core import db
        closed = await db.get_closed_trades(500)
        if closed and len(closed) >= 5:
            wins = [t for t in closed if t.get("outcome_correct")]
            losses = [t for t in closed if not t.get("outcome_correct")]
            win_rate = len(wins) / len(closed)
            avg_win = sum(t.get("pnl_usd", 0) for t in wins) / max(len(wins), 1)
            avg_loss = sum(t.get("pnl_usd", 0) for t in losses) / max(len(losses), 1)
        else:
            # Fallback: aus realistischem Backtest
            win_rate = 0.97
            avg_win = 3.68  # $5 Position bei 0.4965 entry, correct
            avg_loss = -5.10  # $5 + fees, lost

    # Simulationen
    equity_curves = []
    final_capitals = []
    max_drawdowns = []
    initial = 80.0

    for _ in range(simulations):
        capital = initial
        peak = capital
        max_dd = 0
        curve = [capital]

        for _ in range(trades):
            if rng.random() < win_rate:
                capital += avg_win
            else:
                capital += avg_loss  # avg_loss is negative

            if capital > peak:
                peak = capital
            dd = (peak - capital) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

            curve.append(round(capital, 2))

            if capital <= initial * 0.60:  # -40% kill switch
                break

        equity_curves.append(curve)
        final_capitals.append(round(capital, 2))
        max_drawdowns.append(round(max_dd * 100, 1))

    final_capitals.sort()
    n = len(final_capitals)

    return {
        "params": {
            "trades": trades, "simulations": simulations,
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4), "avg_loss": round(avg_loss, 4),
            "initial_capital": initial,
        },
        "results": {
            "median_final": final_capitals[n // 2],
            "p5": final_capitals[int(n * 0.05)],
            "p25": final_capitals[int(n * 0.25)],
            "p75": final_capitals[int(n * 0.75)],
            "p95": final_capitals[int(n * 0.95)],
            "min": final_capitals[0],
            "max": final_capitals[-1],
            "prob_profit": round(sum(1 for c in final_capitals if c > initial) / n * 100, 1),
            "prob_ruin": round(sum(1 for c in final_capitals if c <= initial * 0.60) / n * 100, 1),
            "avg_max_dd": round(sum(max_drawdowns) / len(max_drawdowns), 1),
        },
        "distribution": final_capitals,
        "sample_curves": [equity_curves[i] for i in range(0, simulations, simulations // 10)],
    }


@app.get("/api/markout")
async def api_markout() -> dict:
    """Mark-out Analyse — Signal-Qualität über Zeit (T+1s bis T+60s)."""
    from core import db
    closed = await db.get_closed_trades(500)
    if not closed:
        return {"error": "No closed trades in Supabase", "count": 0}

    # Aggregiere Mark-out Daten
    horizons = ["markout_1s", "markout_5s", "markout_10s", "markout_30s", "markout_60s"]
    labels = ["T+1s", "T+5s", "T+10s", "T+30s", "T+60s"]

    # Pro Horizon: avg, median, positive rate
    agg = {}
    for h, label in zip(horizons, labels):
        values = [t.get(h, 0) for t in closed if t.get(h, 0) != 0]
        if values:
            values.sort()
            n = len(values)
            agg[label] = {
                "count": n,
                "mean": round(sum(values) / n, 6),
                "median": round(values[n // 2], 6),
                "positive_rate": round(sum(1 for v in values if v > 0) / n * 100, 1),
                "p25": round(values[int(n * 0.25)], 6),
                "p75": round(values[int(n * 0.75)], 6),
            }
        else:
            agg[label] = {"count": 0}

    # Per-trade markout data for scatter plot
    trade_markouts = []
    for t in closed[:200]:
        entry = {
            "trade_id": t.get("trade_id", ""),
            "asset": t.get("asset", ""),
            "direction": t.get("direction", ""),
            "momentum_pct": t.get("momentum_pct", 0),
            "outcome_correct": t.get("outcome_correct", False),
            "pnl_usd": t.get("pnl_usd", 0),
        }
        for h in horizons:
            entry[h] = t.get(h, 0)
        trade_markouts.append(entry)

    return {
        "aggregated": agg,
        "trades": trade_markouts,
        "count": len(closed),
        "source": "supabase",
    }


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
