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
from strategies.base import StrategyBase
from strategies.registry import create_strategy, list_strategies
import strategies.polymarket_latency  # noqa: F401 — registriert momentum_latency_v2
import strategies.hmsf_strategy       # noqa: F401 — registriert hmsf_decision_engine
import strategies.copy_trading        # noqa: F401 — registriert copy_trading
from utils.logger import setup_logger

app = FastAPI(title="Polymarket Latency Arb")

# --- Global State ---
strategy: StrategyBase | None = None  # Primary strategy (für Dashboard-Status Broadcast)
active_strategies: dict[str, StrategyBase] = {}  # name → instance (mehrere gleichzeitig)
_strategy_tasks: dict[str, asyncio.Task] = {}  # name → asyncio.Task
start_time: float = 0
connected_clients: set[WebSocket] = set()
_broadcast_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup() -> None:
    """Startet die Polymarket-Strategie und den Broadcast-Loop."""
    global strategy, start_time, _broadcast_task

    setup_logger(level=settings.log_level, data_dir=settings.data_dir)
    logger.info("Polymarket Latency Arb Dashboard startet...")

    start_time = time.time()

    # Primäre Strategie starten (aus Config)
    strat = create_strategy(settings.strategy_name, settings)
    strategy = strat  # Primär für Dashboard-Broadcast
    active_strategies[strat.name] = strat
    _strategy_tasks[strat.name] = asyncio.create_task(strat.run())
    logger.info(f"Strategie geladen: {strat.name} — {strat.description}")

    # Dashboard broadcastet Strategy-Status an WebSocket-Clients (1Hz)
    _broadcast_task = asyncio.create_task(_broadcast_loop())

    # Self-Ping Keep-Alive (nur auf Render, verhindert Sleep nach 15 Min)
    asyncio.create_task(_keep_alive_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    global _broadcast_task
    if _broadcast_task:
        _broadcast_task.cancel()
    # Alle aktiven Strategien stoppen
    for name, task in _strategy_tasks.items():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    for name, strat in active_strategies.items():
        try:
            await strat.shutdown()
        except Exception:
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
    if executor.get("live") and strategy and hasattr(strategy, 'executor') and strategy.executor.is_live:
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
    if not strategy or not hasattr(strategy, 'executor') or not strategy.executor.is_live:
        return {"live": False, "balance": 0, "wallet": "", "chain": "Polygon"}
    balance = await strategy.executor.get_balance()
    stats = strategy.executor.stats()
    # PnL sicher lesen (nicht jede Strategie hat paper_trader)
    pnl = 0
    if hasattr(strategy, 'paper_trader'):
        try:
            pnl = strategy.paper_trader.daily_pnl_usd()
        except Exception:
            pass
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


@app.get("/api/live-trades")
async def api_live_trades() -> dict:
    """Echte Live Trades — aus Supabase (primär) oder JSONL (fallback).

    Gibt NUR echte Trades zurück, keine Paper Trades.
    """
    import json
    from core import db

    def _classify_and_stats(trades: list[dict]) -> dict:
        """Classify trades by live status and compute separate stats."""
        for t in trades:
            # Determine live_status: "live", "failed", or "unknown" (legacy)
            if t.get("live_order_success") is True:
                t["live_status"] = "live"
            elif t.get("live_order_id") or t.get("live_error"):
                t["live_status"] = "failed"
            else:
                t["live_status"] = "unknown"  # Legacy: no live data recorded

        # All trades stats (paper simulation)
        all_wins = [t for t in trades if t.get("outcome_correct")]
        all_losses = [t for t in trades if not t.get("outcome_correct")]
        all_pnl = sum(t.get("pnl_usd", 0) for t in trades)

        # Live-only stats (only where live order succeeded)
        live_trades = [t for t in trades if t.get("live_status") == "live"]
        live_wins = [t for t in live_trades if t.get("outcome_correct")]
        live_losses = [t for t in live_trades if not t.get("outcome_correct")]
        live_pnl = sum(t.get("pnl_usd", 0) for t in live_trades)

        return {
            "trades": trades,
            "count": len(trades),
            "wins": len(all_wins),
            "losses": len(all_losses),
            "win_rate": round(len(all_wins) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(all_pnl, 2),
            "live": {
                "count": len(live_trades),
                "wins": len(live_wins),
                "losses": len(live_losses),
                "win_rate": round(len(live_wins) / len(live_trades) * 100, 1) if live_trades else 0,
                "total_pnl": round(live_pnl, 2),
            },
        }

    # Primär: Supabase
    db_trades = await db.get_closed_trades(200)
    if db_trades:
        # Sortierung: Neueste zuerst (nach exit_ts absteigend)
        db_trades.sort(key=lambda t: t.get("exit_ts", t.get("entry_ts", 0)), reverse=True)
        result = _classify_and_stats(db_trades)
        result["source"] = "supabase"
        return result

    # Fallback: JSONL Journal
    journal_path = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"
    if journal_path.exists():
        trades = []
        open_records: dict = {}   # trade_id → open record (for field merging)
        live_updates: dict = {}   # trade_id → {live_order_success, live_order_id, ...}
        with open(journal_path) as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get("event") == "open":
                        open_records[rec.get("trade_id", "")] = rec
                    elif rec.get("event") == "close":
                        trades.append(rec)
                    elif rec.get("event") == "live_update":
                        live_updates[rec.get("trade_id", "")] = rec
                except Exception:
                    pass
        # Merge open-time fields + live_update results into close records
        _open_fields = (
            "signal_ts", "window_slug", "market_question", "timeframe",
            "polymarket_bid", "polymarket_ask",
            "p_market", "raw_edge_pct", "fee_pct", "net_ev_pct",
            "shares", "kelly_fraction",
            "signal_to_order_ms", "transit_latency_ms", "tick_age_ms",
            "order_type", "seconds_to_expiry", "market_liquidity_usd", "spread_pct",
        )
        for t in trades:
            tid = t.get("trade_id", "")
            # Copy open-time fields if missing in close record
            if tid in open_records:
                opn = open_records[tid]
                for fld in _open_fields:
                    if not t.get(fld):
                        val = opn.get(fld)
                        if val:
                            t[fld] = val
            if tid in live_updates:
                upd = live_updates[tid]
                t["live_order_success"] = upd.get("live_order_success", False)
                t["live_order_id"] = upd.get("live_order_id", "")
                t["live_error"] = upd.get("live_error", "")
                t["order_post_ts"] = upd.get("order_post_ts", 0)
        # Sortierung: Neueste zuerst (nach exit_ts absteigend)
        trades.sort(key=lambda t: t.get("exit_ts", t.get("entry_ts", 0)), reverse=True)
        result = _classify_and_stats(trades)
        result["source"] = "jsonl"
        return result

    return {"trades": [], "count": 0, "source": "none"}


@app.get("/api/positions")
async def api_positions() -> dict:
    """Echte Polymarket Positionen — direkt von der Data API."""
    import json
    from urllib.request import urlopen, Request

    wallet = settings.polymarket_funder
    if not wallet:
        return {"positions": [], "error": "no wallet configured"}

    try:
        url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=50"
        req = Request(url, headers={"User-Agent": "polymarket-arb/2.0"})
        with urlopen(req, timeout=10) as resp:
            positions = json.loads(resp.read())

        # Filter: Nur aktive Positionen (nicht resolved/wertlos)
        active = [p for p in positions if p.get("currentValue", 0) > 0 or p.get("redeemable")]
        resolved = [p for p in positions if p.get("currentValue", 0) == 0 and not p.get("redeemable")]

        total_value = sum(p.get("currentValue", 0) for p in active)
        redeemable = sum(p.get("currentValue", 0) for p in active if p.get("redeemable") and p.get("currentValue", 0) > 0)
        total_invested = sum(p.get("initialValue", 0) for p in active)
        total_pnl = sum(p.get("cashPnl", 0) for p in active)

        return {
            "positions": active,  # Nur aktive/redeemable Positionen
            "count": len(active),
            "resolved_count": len(resolved),
            "total_value": round(total_value, 2),
            "redeemable": round(redeemable, 2),
            "total_invested": round(total_invested, 2),
            "total_pnl": round(total_pnl, 2),
            "wallet": wallet,
        }
    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.post("/api/toggle-order-type")
async def toggle_order_type() -> dict:
    """Toggle zwischen Maker und Taker Order-Typ."""
    current = settings.order_type
    new_type = "maker" if current == "taker" else "taker"
    settings.order_type = new_type
    logger.info(f"Order-Typ umgestellt: {current} → {new_type}")
    return {"order_type": new_type, "previous": current}


@app.get("/api/trading-mode")
async def get_trading_mode() -> dict:
    """Aktueller Trading-Modus: Paper oder Live."""
    return {
        "live_trading": settings.live_trading,
        "mode": "live" if settings.live_trading else "paper",
        "wallet": settings.polymarket_funder if settings.live_trading else "",
    }


@app.post("/api/trading-mode")
async def set_trading_mode(body: dict) -> dict:
    """Toggle zwischen Paper und Live Trading."""
    global strategy, active_strategies, _strategy_tasks

    new_mode = body.get("live", None)
    if new_mode is None:
        # Toggle
        new_mode = not settings.live_trading

    old_mode = settings.live_trading
    settings.live_trading = bool(new_mode)

    mode_str = "LIVE" if settings.live_trading else "PAPER"
    logger.info(f"Trading-Modus geändert: {'LIVE' if old_mode else 'PAPER'} → {mode_str}")

    # Alle aktiven Strategien neu starten damit der Executor reinitialisiert
    for name in list(active_strategies.keys()):
        task = _strategy_tasks.pop(name, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        strat = active_strategies.pop(name, None)
        if strat:
            try:
                await strat.shutdown()
            except Exception:
                pass

        # Neu starten mit aktualisiertem live_trading Flag
        new_strat = create_strategy(name, settings)
        active_strategies[name] = new_strat
        _strategy_tasks[name] = asyncio.create_task(new_strat.run())

    strategy = next(iter(active_strategies.values()), None)

    return {
        "live_trading": settings.live_trading,
        "mode": mode_str,
        "restarted_strategies": list(active_strategies.keys()),
    }


@app.get("/api/strategies")
async def api_strategies() -> dict:
    """Alle verfügbaren Strategien + welche aktiv sind (Multi-Strategy)."""
    available = list_strategies()
    active_names = set(active_strategies.keys())
    return {
        "strategies": [
            {"name": name, "description": desc, "active": name in active_names}
            for name, desc in available.items()
        ],
        "active": list(active_names),
    }


@app.post("/api/strategy/enable")
async def enable_strategy(body: dict) -> dict:
    """Aktiviert eine Strategie mit Smart-Conflict-Resolution.

    Regeln:
    - copy_trading kann PARALLEL zu einer Momentum-Strategie laufen (keine Überlappung)
    - momentum_latency_v2 und hmsf_decision_engine schliessen sich aus (gleiche Signale)
    """
    global strategy

    name = body.get("strategy", "")
    available = list_strategies()

    if name not in available:
        return {"error": f"Unbekannte Strategie: {name}", "available": list(available.keys())}

    if name in active_strategies:
        return {"status": "already_active", "active": list(active_strategies.keys())}

    # Smart Conflict Resolution:
    # copy_trading = eigene Kategorie (Sport, Politik, Events)
    # momentum_latency_v2 + hmsf_decision_engine = gleiche Kategorie (5-Min Crypto)
    MOMENTUM_GROUP = {"momentum_latency_v2", "hmsf_decision_engine"}

    if name in MOMENTUM_GROUP:
        # Deaktiviere NUR die andere Momentum-Strategie (nicht Copy Trading)
        for other_name in list(active_strategies.keys()):
            if other_name in MOMENTUM_GROUP and other_name != name:
                task = _strategy_tasks.pop(other_name, None)
                if task:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                strat_old = active_strategies.pop(other_name, None)
                if strat_old:
                    try:
                        await strat_old.shutdown()
                    except Exception as e:
                        logger.error(f"Shutdown {other_name}: {e}")
                logger.info(f"Strategie DEAKTIVIERT (Momentum-Conflict): {other_name}")
    # copy_trading kann immer parallel laufen — kein Conflict

    # Neue Strategie erstellen und starten
    strat = create_strategy(name, settings)
    active_strategies[name] = strat
    _strategy_tasks[name] = asyncio.create_task(strat.run())

    # Primäre Strategie für Dashboard-Broadcast:
    # Momentum-Strategien haben Oracle/Discovery Daten → bevorzugt als primär
    # Copy Trading hat keine Dashboard-Daten → nie als primär setzen
    MOMENTUM_GROUP_NAMES = {"momentum_latency_v2", "hmsf_decision_engine"}
    if name in MOMENTUM_GROUP_NAMES or strategy is None:
        strategy = strat
    # Wenn Copy Trading aktiviert wird und eine Momentum bereits läuft → primär bleibt Momentum

    logger.info(f"Strategie AKTIVIERT: {name} | Primär für Dashboard: {strategy.name if strategy else '?'}")
    return {"status": "enabled", "strategy": name, "active": list(active_strategies.keys())}


@app.post("/api/strategy/disable")
async def disable_strategy(body: dict) -> dict:
    """Deaktiviert eine laufende Strategie (stoppt sie sauber)."""
    global strategy

    name = body.get("strategy", "")

    if name not in active_strategies:
        return {"error": f"Strategie '{name}' ist nicht aktiv", "active": list(active_strategies.keys())}

    # Strategie stoppen
    task = _strategy_tasks.pop(name, None)
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    strat = active_strategies.pop(name, None)
    if strat:
        try:
            await strat.shutdown()
        except Exception as e:
            logger.error(f"Shutdown Fehler für {name}: {e}")

    # Primäre Strategie updaten
    if strategy and strategy.name == name:
        strategy = next(iter(active_strategies.values()), None)

    logger.info(f"Strategie DEAKTIVIERT: {name} ({len(active_strategies)} aktiv)")
    return {"status": "disabled", "strategy": name, "active": list(active_strategies.keys())}


@app.get("/api/redeemable")
async def api_redeemable() -> dict:
    """Zeigt alle redeembaren Positionen + Gesamtwert."""
    import json as _json
    from urllib.request import urlopen, Request

    wallet = settings.polymarket_funder
    if not wallet:
        return {"redeemable": [], "total": 0}

    try:
        url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=100"
        req = Request(url, headers={"User-Agent": "polymarket-arb/2.0"})
        with urlopen(req, timeout=10) as resp:
            positions = _json.loads(resp.read())

        redeemable = [p for p in positions if p.get("redeemable")]
        total = sum(p.get("currentValue", 0) for p in redeemable)

        return {
            "redeemable": [{
                "title": (p.get("title", "") or "")[:50],
                "outcome": p.get("outcome", ""),
                "value": round(p.get("currentValue", 0), 2),
                "size": round(p.get("size", 0), 2),
            } for p in redeemable],
            "count": len(redeemable),
            "total_usd": round(total, 2),
        }
    except Exception as e:
        return {"redeemable": [], "total": 0, "error": str(e)}


@app.post("/api/redeem")
async def api_redeem() -> dict:
    """Manueller Redeem — löst alle redeembaren Positionen ein."""
    # Finde die aktive Strategie mit Redeemer
    for strat in active_strategies.values():
        if hasattr(strat, 'redeemer'):
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, strat.redeemer.redeem_all)
                return {
                    "status": "ok",
                    "redeemed": result.get("redeemed", 0),
                    "value_usd": result.get("value_usd", 0),
                }
            except Exception as e:
                return {"status": "error", "error": str(e)}

    return {"status": "error", "error": "Kein Redeemer verfügbar (keine Strategie mit Redeemer aktiv)"}


@app.get("/api/hmsf/config")
async def get_hmsf_config() -> dict:
    """HMSF Module-Toggles und Konfiguration."""
    from strategies.hmsf_core import hmsf_config
    return hmsf_config.get_all()


@app.post("/api/hmsf/config")
async def set_hmsf_config(body: dict) -> dict:
    """HMSF Module-Toggles live ändern (kein Restart nötig)."""
    from strategies.hmsf_core import hmsf_config
    changed = hmsf_config.update_many(body)
    return {"updated": changed, "config": hmsf_config.get_all()}


@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page() -> HTMLResponse:
    """Strategy Management Page — Strategien aktivieren/deaktivieren."""
    html_path = Path(__file__).parent / "strategy.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


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
