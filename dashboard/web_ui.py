"""Polymarket Latency Arb — Web Dashboard Backend.

FastAPI + WebSocket: Serves the dashboard UI and pushes strategy status at 1Hz.
The strategy runs its own async loops; the dashboard is a passive observer.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from config import settings
from strategies.base import StrategyBase
from strategies.registry import create_strategy, list_strategies
import strategies.polymarket_latency  # noqa: F401 — registriert momentum_latency_v2
import strategies.hmsf_strategy       # noqa: F401 — registriert hmsf_decision_engine
import strategies.copy_trading        # noqa: F401 — registriert copy_trading
import strategies.oracle_delay_arb    # noqa: F401 — registriert oracle_delay_arb
from utils.logger import setup_logger

app = FastAPI(title="Polymarket Latency Arb")

# ═══════════════════════════════════════════════════════════════════
# STRATEGY STATE PERSISTENCE — Überlebt Server-Restarts
# ═══════════════════════════════════════════════════════════════════

STRATEGY_STATE_FILE = Path(__file__).parent.parent / "data" / "strategy_state.json"


def _save_strategy_state() -> None:
    """Speichert aktive Strategien + Live-Mode persistent (atomic write)."""
    try:
        STRATEGY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "active": list(active_strategies.keys()),
            "live_trading": settings.live_trading,
            "saved_at": time.time(),
        }
        tmp = STRATEGY_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f)
        import os as _os
        _os.replace(str(tmp), str(STRATEGY_STATE_FILE))
    except Exception as e:
        logger.error(f"Strategy state save error: {e}")


def _load_strategy_state() -> dict | None:
    """Lädt gespeicherten Strategie-State. Returns None bei Fehler."""
    try:
        if STRATEGY_STATE_FILE.exists():
            with open(STRATEGY_STATE_FILE) as f:
                state = json.load(f)
            if isinstance(state, dict) and "active" in state:
                return state
    except Exception as e:
        logger.warning(f"Strategy state load error: {e}")
    return None

# ═══════════════════════════════════════════════════════════════════
# SHARED ASYNC HTTP CLIENT (replaces blocking urlopen)
# ═══════════════════════════════════════════════════════════════════
_http_session: aiohttp.ClientSession | None = None
_HTTP_HEADERS = {"User-Agent": "polymarket-arb/2.0"}


async def _get_http_session() -> aiohttp.ClientSession:
    """Lazy-init shared aiohttp session für Dashboard API calls."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12),
            headers=_HTTP_HEADERS,
        )
    return _http_session


async def _async_fetch_json(url: str, timeout: int = 10) -> list | dict:
    """Async JSON fetch — ersetzt blockierendes urlopen."""
    try:
        session = await _get_http_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                return await resp.json()
            return []
    except Exception as e:
        logger.debug(f"Async fetch error ({url[:60]}...): {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# SESSION AUTH — Cookie-basiert, 1x Login, 30 Tage gültig
# Set DASHBOARD_PASSWORD env var to enable (disabled if not set)
# ═══════════════════════════════════════════════════════════════════

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
_SESSION_SECRET = secrets.token_hex(32)  # Random pro Server-Start
_SESSION_MAX_AGE = 30 * 24 * 3600       # 30 Tage Cookie-Lebensdauer


def _make_session_token(password: str) -> str:
    """Generiert einen Session-Token aus Passwort + Server-Secret."""
    return hashlib.sha256(f"{password}:{_SESSION_SECRET}".encode()).hexdigest()[:48]


# Login Page (inline HTML — kein separates File nötig)
_LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — Polymarket Bot</title>
<style>
  :root{--bg:#0a0c10;--card:#13151c;--border:#1e2230;--text:#e1e4eb;--accent:#3b82f6;--red:#ef4444;--font:'SF Mono',monospace}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:var(--font);display:flex;align-items:center;justify-content:center;min-height:100vh}
  .box{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:32px;width:320px;text-align:center}
  h2{font-size:14px;letter-spacing:1.5px;margin-bottom:20px;color:var(--accent)}
  input{width:100%;padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--font);font-size:13px;margin-bottom:12px}
  input:focus{outline:none;border-color:var(--accent)}
  button{width:100%;padding:10px;background:var(--accent);color:#fff;border:none;border-radius:4px;font-family:var(--font);font-size:12px;font-weight:600;cursor:pointer;text-transform:uppercase;letter-spacing:1px}
  button:hover{opacity:0.9}
  .err{color:var(--red);font-size:11px;margin-bottom:8px;display:none}
</style></head><body>
<div class="box">
  <h2>POLYMARKET BOT</h2>
  <div class="err" id="err">Falsches Passwort</div>
  <form method="POST" action="/auth/login">
    <input type="password" name="password" placeholder="Dashboard Passwort" autofocus>
    <button type="submit">LOGIN</button>
  </form>
</div>
<script>if(location.search.includes('fail'))document.getElementById('err').style.display='block'</script>
</body></html>"""


class AuthMiddleware(BaseHTTPMiddleware):
    """Cookie-basierte Session Auth — 1x einloggen, 30 Tage gültig."""

    # Paths die OHNE Auth erreichbar sein müssen
    PUBLIC_PATHS = {"/auth/login", "/auth/logout", "/docs", "/openapi.json"}

    async def dispatch(self, request: Request, call_next):
        # Auth deaktiviert wenn kein Passwort gesetzt
        if not DASHBOARD_PASSWORD:
            return await call_next(request)

        path = request.url.path

        # Public paths durchlassen
        if path in self.PUBLIC_PATHS:
            return await call_next(request)

        # Session-Cookie prüfen
        session = request.cookies.get("session")
        valid_token = _make_session_token(DASHBOARD_PASSWORD)

        if session == valid_token:
            return await call_next(request)

        # WebSocket: Token als Query-Param (Cookies gehen nicht immer bei WS)
        if path == "/ws":
            token = request.query_params.get("token", "")
            if token == valid_token:
                return await call_next(request)
            # WS ohne Auth → Connection refused (stille Ablehnung)
            return Response(status_code=403)

        # API Requests: JSON 401
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "login": "/auth/login"},
            )

        # HTML Pages: Redirect zu Login
        return RedirectResponse(url="/auth/login", status_code=302)


# Middleware NUR aktivieren wenn Passwort gesetzt
if DASHBOARD_PASSWORD:
    app.add_middleware(AuthMiddleware)
    logger.info(f"Dashboard Auth AKTIV — Passwort gesetzt ({len(DASHBOARD_PASSWORD)} Zeichen)")
else:
    logger.warning("Dashboard Auth DEAKTIVIERT — kein DASHBOARD_PASSWORD gesetzt!")

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

    # Strategie-State laden (persistent über Restarts)
    saved = _load_strategy_state()
    available = list_strategies()

    if saved:
        # Restore: Live-Mode + alle gespeicherten Strategien
        settings.live_trading = saved.get("live_trading", settings.live_trading)
        names_to_start = saved.get("active", [settings.strategy_name])
        logger.info(f"Strategy State geladen: {names_to_start} | live={settings.live_trading}")
    else:
        # Fallback: Default aus config.py
        names_to_start = [settings.strategy_name]
        logger.info(f"Kein Strategy State — Default: {names_to_start}")

    for name in names_to_start:
        if name not in available:
            logger.warning(f"Strategie '{name}' nicht in Registry — skip")
            continue
        try:
            strat = create_strategy(name, settings)
            active_strategies[name] = strat
            _strategy_tasks[name] = asyncio.create_task(strat.run())
            if strategy is None or name in {"momentum_latency_v2", "hmsf_decision_engine"}:
                strategy = strat  # Momentum bevorzugt als Primär
            logger.info(f"Strategie geladen: {name} — {strat.description}")
        except Exception as e:
            logger.error(f"Strategie '{name}' Start FAILED: {e}")

    # Dashboard broadcastet Strategy-Status an WebSocket-Clients (1Hz)
    _broadcast_task = asyncio.create_task(_broadcast_loop())

    # Self-Ping Keep-Alive (nur auf Render, verhindert Sleep nach 15 Min)
    asyncio.create_task(_keep_alive_loop())

    # Auto-Collect: Redeem + Merge alle 60 Sekunden
    asyncio.create_task(_auto_collect_loop())


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
    # Shared HTTP Session schliessen
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None
    logger.info("Dashboard beendet.")


_last_collect_result: dict = {}


async def _auto_collect_loop() -> None:
    """Alle 60 Sekunden: Redeem gewonnene Positionen + Merge beide-Seiten-Positionen."""
    global _last_collect_result
    await asyncio.sleep(15)  # Warte auf Strategy-Start

    while True:
        try:
            # Finde einen Redeemer in den aktiven Strategien
            redeemer = None
            for name, strat in active_strategies.items():
                if hasattr(strat, "redeemer"):
                    redeemer = strat.redeemer
                    break

            if redeemer:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, redeemer.check_and_collect)
                _last_collect_result = result

                if result.get("redeemed", 0) > 0 or result.get("merged", 0) > 0:
                    from utils import telegram
                    await telegram.send_alert(
                        f"💰 <b>AUTO-COLLECT</b>\n"
                        f"Redeemed: {result.get('redeemed', 0)} (${result.get('redeem_usd', 0):.2f})\n"
                        f"Merged: {result.get('merged', 0)} (${result.get('merge_usd', 0):.2f})\n"
                        f"Stuck: ${result.get('stuck_usd', 0):.2f}\n"
                        f"Pending Merge: ${result.get('pending_merge_usd', 0):.2f}"
                    )
        except Exception as e:
            logger.error(f"Auto-Collect Loop Error: {e}")

        await asyncio.sleep(60)  # Alle 60 Sekunden


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

# ═══════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.get("/auth/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    """Login Page — zeigt Passwort-Feld."""
    if not DASHBOARD_PASSWORD:
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(_LOGIN_HTML)


@app.post("/auth/login")
async def login_submit(request: Request) -> Response:
    """Prüft Passwort, setzt Session-Cookie (30 Tage)."""
    form = await request.form()
    password = form.get("password", "")

    if password == DASHBOARD_PASSWORD:
        token = _make_session_token(DASHBOARD_PASSWORD)
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key="session",
            value=token,
            max_age=_SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        logger.info(f"Dashboard Login OK — IP: {request.client.host}")
        return response
    else:
        logger.warning(f"Dashboard Login FAILED — IP: {request.client.host}")
        return RedirectResponse(url="/auth/login?fail=1", status_code=302)


@app.get("/auth/logout")
async def logout() -> Response:
    """Löscht Session-Cookie."""
    response = RedirectResponse(url="/auth/login", status_code=302)
    response.delete_cookie("session")
    return response


# ═══════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════
# JOURNAL READER — Single Source of Truth für Dashboard Stats
# ═══════════════════════════════════════════════════════════════════

_journal_stats_cache: dict = {"data": {}, "ts": 0}
JOURNAL_PATH = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"


def _read_journal_stats() -> dict:
    """Liest trade_journal.jsonl und berechnet Stats pro Strategie.

    Cached für 30 Sekunden. Gibt aggregierte Daten zurück die
    das Dashboard als Single Source of Truth nutzt.
    """
    import json
    from collections import defaultdict

    now = time.time()
    if now - _journal_stats_cache["ts"] < 30 and _journal_stats_cache["data"]:
        return _journal_stats_cache["data"]

    entries = []
    # Try multiple paths (CWD may differ from __file__ location)
    paths_to_try = [
        JOURNAL_PATH,
        Path("data/trade_journal.jsonl"),
        Path("/home/ubuntu/polymarket-arb/data/trade_journal.jsonl"),
    ]
    journal_file = None
    for p in paths_to_try:
        if p.exists():
            journal_file = p
            break

    if journal_file:
        try:
            with open(journal_file) as f:
                for line in f:
                    if line.strip():
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"Journal read error ({journal_file}): {e}")

    # Gruppiere nach trade_id
    by_trade = defaultdict(list)
    for e in entries:
        tid = e.get("trade_id", "")
        if tid:
            by_trade[tid].append(e)

    # Strategie-Zuordnung via Prefix + order_type
    def get_strategy(tid: str, order_type: str) -> str:
        if tid.startswith("CT-"):
            return "copy_trade"
        if tid.startswith("ODA"):
            return "oda"
        if tid.startswith("LT-"):
            return "momentum_live"
        if tid.startswith("PT-"):
            return "momentum_paper"
        if order_type in ("copy_trade", "copy_hedge", "copy_sell"):
            return "copy_trade"
        if order_type == "oracle_delay_arb":
            return "oda"
        return "other"

    # Stats pro Strategie
    strat_stats = defaultdict(lambda: {
        "opens": 0, "wins": 0, "losses": 0, "pending": 0,
        "total_pnl": 0.0, "total_invested": 0.0, "total_payout": 0.0,
    })
    resolved_cids = set()  # condition_ids die resolved sind
    all_trades_list = []   # Für Trade History

    for tid, events in by_trade.items():
        open_ev = next((e for e in events if e.get("event") == "open"), None)
        close_ev = next((e for e in events if e.get("event") in ("redeemed", "resolved_loss", "close")), None)

        if not open_ev:
            continue

        order_type = open_ev.get("order_type", "")
        strat = get_strategy(tid, order_type)
        strat_stats[strat]["opens"] += 1
        strat_stats[strat]["total_invested"] += open_ev.get("size_usd", 0)

        trade_record = {
            "trade_id": tid,
            "strategy": strat,
            "order_type": order_type,
            "market_question": open_ev.get("market_question", ""),
            "direction": open_ev.get("direction", ""),
            "executed_price": open_ev.get("executed_price", 0),
            "size_usd": open_ev.get("size_usd", 0),
            "entry_ts": open_ev.get("entry_ts", 0),
            "source_wallet_name": open_ev.get("source_wallet_name", ""),
            "condition_id": open_ev.get("condition_id", ""),
            "live_order_success": open_ev.get("live_order_success", False),
        }

        if close_ev:
            event_type = close_ev.get("event", "")
            pnl = close_ev.get("pnl_usd", 0)
            payout = close_ev.get("payout_usd", 0)
            cid = close_ev.get("condition_id", "") or open_ev.get("condition_id", "")

            if event_type == "redeemed":
                strat_stats[strat]["wins"] += 1
                strat_stats[strat]["total_pnl"] += pnl
                strat_stats[strat]["total_payout"] += payout
                trade_record["status"] = "redeemed"
                trade_record["pnl_usd"] = pnl
                trade_record["payout_usd"] = payout
                trade_record["outcome_correct"] = True
            elif event_type == "resolved_loss":
                strat_stats[strat]["losses"] += 1
                strat_stats[strat]["total_pnl"] += pnl
                trade_record["status"] = "lost"
                trade_record["pnl_usd"] = pnl
                trade_record["outcome_correct"] = False
            elif event_type == "close":
                if close_ev.get("outcome_correct"):
                    strat_stats[strat]["wins"] += 1
                else:
                    strat_stats[strat]["losses"] += 1
                pnl = close_ev.get("pnl_usd", 0)
                strat_stats[strat]["total_pnl"] += pnl
                trade_record["status"] = "won" if close_ev.get("outcome_correct") else "lost"
                trade_record["pnl_usd"] = pnl
                trade_record["outcome_correct"] = close_ev.get("outcome_correct", False)

            if cid:
                resolved_cids.add(cid)
            trade_record["exit_ts"] = close_ev.get("exit_ts", 0)
        else:
            strat_stats[strat]["pending"] += 1
            trade_record["status"] = "open"
            trade_record["pnl_usd"] = 0
            trade_record["outcome_correct"] = None

        all_trades_list.append(trade_record)

    # Aggregiere Live-Stats (alles ausser Paper)
    live_stats = {"count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
    for strat_name in ("copy_trade", "oda", "momentum_live"):
        s = strat_stats.get(strat_name, {})
        live_stats["count"] += s.get("opens", 0)
        live_stats["wins"] += s.get("wins", 0)
        live_stats["losses"] += s.get("losses", 0)
        live_stats["total_pnl"] += s.get("total_pnl", 0)
    resolved_count = live_stats["wins"] + live_stats["losses"]
    live_stats["win_rate"] = round(live_stats["wins"] / max(1, resolved_count) * 100, 1)

    result = {
        "live": live_stats,
        "paper": dict(strat_stats.get("momentum_paper", {})),
        "by_strategy": dict(strat_stats),
        "resolved_condition_ids": resolved_cids,
        "trades": sorted(all_trades_list, key=lambda x: x.get("entry_ts", 0), reverse=True),
        "total_entries": len(entries),
    }

    _journal_stats_cache["data"] = result
    _journal_stats_cache["ts"] = now
    return result


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


def _fetch_activity_lookup(wallet: str) -> dict:
    """Activity API → Lookup: (conditionId, outcomeIndex) → {txHash, tradeCount, firstTx, usdcTotal}."""
    import json as _json
    from urllib.request import urlopen as _urlopen, Request as _Req
    from collections import defaultdict

    try:
        url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=200&type=TRADE"
        req = _Req(url, headers={"User-Agent": "polymarket-arb/2.0"})
        with _urlopen(req, timeout=8) as resp:
            trades = _json.loads(resp.read())
    except Exception:
        return {}

    # Gruppieren nach (conditionId, outcomeIndex) → alle Trades für diese Position
    groups: dict = defaultdict(list)
    for t in trades:
        key = (t.get("conditionId", ""), t.get("outcomeIndex", 0))
        groups[key].append(t)

    lookup = {}
    for key, tlist in groups.items():
        # Sortiert nach timestamp ascending (ältester zuerst)
        tlist.sort(key=lambda x: x.get("timestamp", 0))
        first = tlist[0]
        latest = tlist[-1]
        total_usdc = sum(t.get("usdcSize", 0) for t in tlist)
        buys = [t for t in tlist if t.get("side") == "BUY"]
        lookup[key] = {
            "firstTxHash": first.get("transactionHash", ""),
            "latestTxHash": latest.get("transactionHash", ""),
            "tradeCount": len(tlist),
            "buyCount": len(buys),
            "totalUsdc": round(total_usdc, 2),
            "firstTs": first.get("timestamp", 0),
        }
    return lookup


_copy_source_cache: dict = {"data": {}, "ts": 0}


def _fetch_copy_source_lookup() -> dict:
    """Tracked Wallets Activity → Lookup: conditionId → {source_name, source_tx_hash, source_wallet}.

    Checkt welche unserer Positionen von tracked Wallets kopiert wurden.
    Cached für 5 Minuten (5 API Calls pro Refresh sind zu viel bei 30s Interval).
    """
    import json as _json
    import time as _time
    from urllib.request import urlopen as _urlopen, Request as _Req

    # Cache: 5 Minuten
    if _time.time() - _copy_source_cache["ts"] < 300 and _copy_source_cache["data"]:
        return _copy_source_cache["data"]

    # Import tracked wallets config
    try:
        from strategies.copy_trading import DEFAULT_TRACKED_WALLETS
        wallets = DEFAULT_TRACKED_WALLETS
    except Exception:
        return {}

    lookup: dict = {}  # conditionId → {source_name, source_tx_hash, ...}

    for w in wallets:
        addr = w["address"]
        name = w["name"]
        try:
            url = f"https://data-api.polymarket.com/activity?user={addr}&limit=100&type=TRADE"
            req = _Req(url, headers={"User-Agent": "polymarket-arb/2.0"})
            with _urlopen(req, timeout=6) as resp:
                trades = _json.loads(resp.read())
        except Exception:
            continue

        for t in trades:
            cid = t.get("conditionId", "")
            if cid and cid not in lookup:
                # Erster Match gewinnt (ältester Trade = wahrscheinlich der kopierte)
                lookup[cid] = {
                    "source_name": name,
                    "source_wallet": addr,
                    "source_tx_hash": t.get("transactionHash", ""),
                    "source_price": t.get("price", 0),
                    "source_size": t.get("usdcSize", 0),
                    "source_side": t.get("side", ""),
                }

    _copy_source_cache["data"] = lookup
    _copy_source_cache["ts"] = _time.time()
    return lookup


def _enrich_position(p: dict, activity_lookup: dict, copy_lookup: dict | None = None) -> dict:
    """Position um Activity-Daten + Copy-Source anreichern."""
    cid = p.get("conditionId", "")
    oi = p.get("outcomeIndex", 0)
    act = activity_lookup.get((cid, oi), {})
    p["txHash"] = act.get("firstTxHash", "")
    p["latestTxHash"] = act.get("latestTxHash", "")
    p["tradeCount"] = act.get("tradeCount", 0)
    p["totalUsdc"] = act.get("totalUsdc", 0)
    p["positionId"] = cid[:10] if cid else "?"

    # Copy Source Zuordnung
    if copy_lookup and cid in copy_lookup:
        src = copy_lookup[cid]
        p["copySource"] = src["source_name"]
        p["sourceTxHash"] = src["source_tx_hash"]
        p["sourcePrice"] = src["source_price"]
        p["sourceSize"] = src["source_size"]
    else:
        p["copySource"] = ""

    return p


@app.get("/api/positions")
async def api_positions() -> dict:
    """Echte Polymarket Positionen — direkt von der Data API + Activity für Tx-Hashes."""
    import asyncio

    wallet = settings.polymarket_funder
    if not wallet:
        return {"positions": [], "error": "no wallet configured"}

    try:
        # ASYNC: Alle 3 Fetches parallel (statt sequentiell blocking)
        positions, activity, copy_sources = await asyncio.gather(
            _async_fetch_json(f"https://data-api.polymarket.com/positions?user={wallet}&limit=50"),
            asyncio.to_thread(_fetch_activity_lookup, wallet),
            asyncio.to_thread(_fetch_copy_source_lookup),
        )

        # Alle Positionen anreichern
        for p in positions:
            _enrich_position(p, activity, copy_sources)

        # Ghost-Position Filter: Journal als Source of Truth
        # Positionen die im Journal als redeemed/resolved_loss stehen
        # werden NICHT mehr als "aktiv" oder "redeemable" gezeigt
        journal = _read_journal_stats()
        resolved_cids = journal.get("resolved_condition_ids", set())

        active = [
            p for p in positions
            if p.get("currentValue", 0) > 0.01
            and p.get("conditionId", "") not in resolved_cids
        ]
        resolved = [
            p for p in positions
            if p.get("currentValue", 0) <= 0.01
            or p.get("conditionId", "") in resolved_cids
        ]

        total_value = sum(p.get("currentValue", 0) for p in active)
        redeemable = sum(p.get("currentValue", 0) for p in active if p.get("redeemable") and p.get("currentValue", 0) > 0)
        total_invested = sum(p.get("initialValue", 0) for p in active)
        total_pnl = sum(p.get("cashPnl", 0) for p in active)

        # Resolved Positionen für Trade History aufbereiten
        resolved_data = []
        for p in resolved:
            resolved_data.append({
                "title": p.get("title", "?"),
                "outcome": p.get("outcome", "?"),
                "avgPrice": p.get("avgPrice", 0),
                "curPrice": p.get("curPrice", 0),
                "initialValue": p.get("initialValue", 0),
                "currentValue": p.get("currentValue", 0),
                "cashPnl": p.get("cashPnl", 0),
                "eventSlug": p.get("eventSlug", ""),
                "endDate": p.get("endDate", ""),
                "redeemable": p.get("redeemable", False),
                "resolved": True,
                "txHash": p.get("txHash", ""),
                "latestTxHash": p.get("latestTxHash", ""),
                "tradeCount": p.get("tradeCount", 0),
                "positionId": p.get("positionId", "?"),
                "copySource": p.get("copySource", ""),
                "sourceTxHash": p.get("sourceTxHash", ""),
            })

        return {
            "positions": active,  # Nur aktive/redeemable Positionen (inkl. txHash etc.)
            "resolved": resolved_data,  # LOST Positionen → für Trade History
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

    _save_strategy_state()  # Persistent speichern
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
    _save_strategy_state()  # Persistent speichern
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
    _save_strategy_state()  # Persistent speichern
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


# ═══════════════════════════════════════════════════════════════════
# COPY TRADING DASHBOARD — /copytrading
# ═══════════════════════════════════════════════════════════════════

@app.get("/copytrading", response_class=HTMLResponse)
async def copytrading_page() -> HTMLResponse:
    html_path = Path(__file__).parent / "copytrading.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/copy/status")
async def api_copy_status() -> dict:
    """Status der Copy Trading Strategie — Wallets, Performance, Config.

    Merge: RAM-Daten (aktuelle Session) + Journal (historisch persistent).
    """
    strat = active_strategies.get("copy_trading")
    if strat and hasattr(strat, "get_status"):
        result = strat.get_status()
        # Journal-basierte KPIs einmischen (persistent über Restarts)
        journal = _read_journal_stats()
        copy_journal = journal.get("by_strategy", {}).get("copy_trade", {})
        if copy_journal:
            result["journal_stats"] = {
                "total_opens": copy_journal.get("opens", 0),
                "wins": copy_journal.get("wins", 0),
                "losses": copy_journal.get("losses", 0),
                "pending": copy_journal.get("pending", 0),
                "total_pnl": round(copy_journal.get("total_pnl", 0), 2),
                "total_invested": round(copy_journal.get("total_invested", 0), 2),
                "total_payout": round(copy_journal.get("total_payout", 0), 2),
                "win_rate": round(
                    copy_journal.get("wins", 0) /
                    max(1, copy_journal.get("wins", 0) + copy_journal.get("losses", 0)) * 100, 1
                ),
            }
        return result
    # Fallback: static defaults
    try:
        from strategies.copy_trading import DEFAULT_TRACKED_WALLETS
        return {
            "running": False,
            "tracked_wallets": [
                {"name": w["name"], "address": w["address"], "pnl": w.get("pnl", 0),
                 "category": w.get("category", "mixed"), "status": "inactive"}
                for w in DEFAULT_TRACKED_WALLETS
            ],
            "copies_total": 0, "copies_active": 0, "recent_copies": [],
            "per_wallet_stats": {},
            "config": {},
        }
    except Exception:
        return {"running": False, "tracked_wallets": [], "error": "not loaded"}


_leaderboard_cache: dict = {"data": [], "ts": 0}


@app.get("/api/copy/leaderboard")
async def api_copy_leaderboard(limit: int = 50) -> dict:
    """Polymarket Top Traders Leaderboard (cached 10 min)."""
    now = time.time()
    if now - _leaderboard_cache["ts"] < 600 and _leaderboard_cache["data"]:
        data = _leaderboard_cache["data"]
    else:
        try:
            url = f"https://data-api.polymarket.com/v1/leaderboard?limit={min(limit, 100)}"
            data = await _async_fetch_json(url, timeout=15)
            if data:
                _leaderboard_cache["data"] = data
                _leaderboard_cache["ts"] = now
        except Exception as e:
            logger.error(f"Leaderboard fetch error: {e}")
            data = _leaderboard_cache.get("data", [])

    # Enrich: mark already-tracked wallets
    tracked_addrs = set()
    strat = active_strategies.get("copy_trading")
    if strat and hasattr(strat, "tracked_wallets"):
        tracked_addrs = {w["address"].lower() for w in strat.tracked_wallets}

    traders = []
    for t in data[:limit]:
        addr = t.get("proxyWallet", "")
        traders.append({
            "rank": int(t.get("rank", 0)),
            "address": addr,
            "name": t.get("userName", addr[:10] + "..."),
            "pnl": round(t.get("pnl", 0), 2),
            "volume": round(t.get("vol", 0), 2),
            "profile_image": t.get("profileImage", ""),
            "verified": t.get("verifiedBadge", False),
            "already_tracked": addr.lower() in tracked_addrs,
            "profile_url": f"https://polymarket.com/profile/{addr}",
        })

    # AI Engine Score — Recalibriert für Polymarket-Skala
    # PnL: $100K = 100pts (Polymarket Top-Trader Range)
    # Volume: $1M = 100pts
    # Efficiency: PnL/Volume Ratio (Skill vs Luck)
    # Negative PnL = negative Score (Penalty)
    for t in traders:
        pnl = t["pnl"]
        vol = max(1, t["volume"])
        pnl_score = min(100, max(-50, pnl / 1000))       # $100K = 100, -$50K = -50
        vol_score = min(100, max(0, vol / 10000))          # $1M vol = 100
        efficiency = min(50, max(0, (pnl / vol) * 100)) if vol > 100 else 0  # PnL/Vol ratio
        t["ai_score"] = round(pnl_score * 0.4 + vol_score * 0.2 + efficiency * 0.4, 1)
        t["efficiency_pct"] = round(pnl / vol * 100, 2) if vol > 100 else 0

    return {"traders": traders, "count": len(traders), "cached": now - _leaderboard_cache["ts"] < 5}


_analyze_cache: dict = {}  # wallet -> {data, ts}


@app.get("/api/copy/analyze")
async def api_copy_analyze(wallet: str = "") -> dict:
    """Deep-Dive Analyse einer Wallet: Trades, Positionen, Kategorien."""
    import asyncio
    from collections import Counter

    if not wallet or len(wallet) < 10:
        return {"error": "wallet address required"}

    # Cache 2 min
    now = time.time()
    cached = _analyze_cache.get(wallet)
    if cached and now - cached["ts"] < 120:
        return cached["data"]

    result = {"address": wallet, "recent_trades": [], "positions": [], "summary": {}}

    # Fetch activity + positions PARALLEL (async!)
    activity_url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=100&type=TRADE"
    positions_url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=50"
    trades_raw, positions_raw = await asyncio.gather(
        _async_fetch_json(activity_url, timeout=10),
        _async_fetch_json(positions_url, timeout=10),
    )

    # Fetch activity
    try:
        trades = trades_raw if isinstance(trades_raw, list) else []

        categories = Counter()
        total_usd = 0
        for t in trades:
            title = t.get("title", "")
            total_usd += t.get("usdcSize", 0)
            # Simple category detection
            tl = title.lower()
            if any(w in tl for w in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana"]):
                categories["crypto"] += 1
            elif any(w in tl for w in ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "hockey", "tennis"]):
                categories["sports"] += 1
            elif any(w in tl for w in ["trump", "biden", "election", "president", "congress"]):
                categories["politics"] += 1
            else:
                categories["other"] += 1

        result["recent_trades"] = [
            {
                "timestamp": t.get("timestamp", 0),
                "side": t.get("side", ""),
                "outcome": t.get("outcome", ""),
                "price": t.get("price", 0),
                "size_usd": round(t.get("usdcSize", 0), 2),
                "title": t.get("title", "")[:60],
                "tx_hash": t.get("transactionHash", ""),
            }
            for t in trades[:20]
        ]

        last_ts = trades[0]["timestamp"] if trades else 0
        age_s = now - last_ts if last_ts > 0 else 999999
        if age_s < 3600:
            last_active = f"{int(age_s/60)}m ago"
        elif age_s < 86400:
            last_active = f"{int(age_s/3600)}h ago"
        else:
            last_active = f"{int(age_s/86400)}d ago"

        result["summary"] = {
            "total_trades": len(trades),
            "last_active": last_active,
            "last_active_ts": last_ts,
            "categories": dict(categories),
            "avg_trade_size_usd": round(total_usd / max(1, len(trades)), 2),
            "buys": sum(1 for t in trades if t.get("side") == "BUY"),
            "sells": sum(1 for t in trades if t.get("side") == "SELL"),
        }
    except Exception as e:
        result["summary"]["error"] = str(e)

    # Process positions (already fetched in parallel above)
    try:
        positions = positions_raw if isinstance(positions_raw, list) else []
        active_pos = [p for p in positions if p.get("currentValue", 0) > 0.01]
        total_value = sum(p.get("currentValue", 0) for p in active_pos)
        total_invested = sum(p.get("initialValue", 0) for p in active_pos)
        total_pnl = sum(p.get("cashPnl", 0) for p in active_pos)

        result["positions"] = [
            {
                "title": p.get("title", "")[:50],
                "outcome": p.get("outcome", ""),
                "size": round(p.get("initialValue", 0), 2),
                "current_value": round(p.get("currentValue", 0), 2),
                "pnl": round(p.get("cashPnl", 0), 2),
                "avg_price": round(p.get("avgPrice", 0), 3),
            }
            for p in active_pos[:15]
        ]
        result["summary"]["active_positions"] = len(active_pos)
        result["summary"]["portfolio_value"] = round(total_value, 2)
        result["summary"]["portfolio_invested"] = round(total_invested, 2)
        result["summary"]["portfolio_pnl"] = round(total_pnl, 2)
    except Exception as e:
        result["summary"]["positions_error"] = str(e)

    _analyze_cache[wallet] = {"data": result, "ts": now}
    return result


@app.post("/api/copy/add-wallet")
async def api_copy_add_wallet(body: dict) -> dict:
    """Fügt einen Trader zur Tracking-Liste hinzu."""
    strat = active_strategies.get("copy_trading")
    if not strat or not hasattr(strat, "add_wallet"):
        return {"error": "copy trading strategy not running"}

    wallet = {
        "address": body.get("address", ""),
        "name": body.get("name", body.get("address", "")[:10]),
        "pnl": body.get("pnl", 0),
        "category": body.get("category", "mixed"),
        "notes": body.get("notes", "Added via dashboard"),
    }

    if strat.add_wallet(wallet):
        return {"status": "added", "wallet": wallet["name"], "total": len(strat.tracked_wallets)}
    return {"error": "failed (duplicate or invalid address)"}


@app.post("/api/copy/remove-wallet")
async def api_copy_remove_wallet(body: dict) -> dict:
    """Entfernt einen Trader aus der Tracking-Liste."""
    strat = active_strategies.get("copy_trading")
    if not strat or not hasattr(strat, "remove_wallet"):
        return {"error": "copy trading strategy not running"}

    if strat.remove_wallet(body.get("address", "")):
        return {"status": "removed", "total": len(strat.tracked_wallets)}
    return {"error": "wallet not found"}


@app.post("/api/copy/pause-wallet")
async def api_copy_pause_wallet(body: dict) -> dict:
    """Pausiert einen Trader (keine neuen Copies)."""
    strat = active_strategies.get("copy_trading")
    if not strat:
        return {"error": "copy trading not running"}
    if strat.pause_wallet(body.get("address", "")):
        return {"status": "paused"}
    return {"error": "wallet not found"}


@app.post("/api/copy/resume-wallet")
async def api_copy_resume_wallet(body: dict) -> dict:
    """Reaktiviert einen pausierten Trader."""
    strat = active_strategies.get("copy_trading")
    if not strat:
        return {"error": "copy trading not running"}
    if strat.resume_wallet(body.get("address", "")):
        return {"status": "resumed"}
    return {"error": "wallet not found or not paused"}


@app.get("/api/collect/status")
async def api_collect_status() -> dict:
    """Auto-Collect Status: Redeem + Merge + Stuck Positionen."""
    result = dict(_last_collect_result) if _last_collect_result else {}

    # Redeemer stats
    for name, strat in active_strategies.items():
        if hasattr(strat, "redeemer"):
            result["redeemer_stats"] = strat.redeemer.stats()
            break

    # Mergeable pairs
    for name, strat in active_strategies.items():
        if hasattr(strat, "redeemer"):
            try:
                pairs = strat.redeemer.get_mergeable_pairs()
                result["mergeable_pairs"] = pairs
                result["mergeable_total_usd"] = round(sum(p["merge_value_usd"] for p in pairs), 2)
            except Exception:
                pass
            break

    return result


@app.get("/api/journal/stats")
async def api_journal_stats() -> dict:
    """Journal-basierte Stats — Single Source of Truth für das Dashboard.

    Liest trade_journal.jsonl und gibt aggregierte Stats pro Strategie zurück.
    Ersetzt die fehlerhafte API/RAM-basierte PnL-Berechnung.
    """
    stats = _read_journal_stats()
    # resolved_condition_ids ist ein set — nicht JSON-serialisierbar
    result = {k: v for k, v in stats.items() if k != "resolved_condition_ids"}
    result["resolved_count"] = len(stats.get("resolved_condition_ids", set()))
    return result


@app.get("/api/journal/chart-data")
async def api_journal_chart_data() -> dict:
    """Chronologische P&L-Daten für den Performance-Chart.

    Gibt jeden resolved Trade als Datenpunkt zurück mit kumuliertem PnL.
    Frontend gruppiert nach Timeframe (1M, 1W, 1D, 1H, 1m).
    """
    import json as _json
    from pathlib import Path as _Path

    journal_file = None
    for p in [
        _Path(__file__).parent.parent / "data" / "trade_journal.jsonl",
        _Path("data/trade_journal.jsonl"),
        _Path("/home/ubuntu/polymarket-arb/data/trade_journal.jsonl"),
    ]:
        if p.exists():
            journal_file = p
            break

    if not journal_file:
        return {"points": [], "error": "journal not found"}

    # Sammle alle resolved Events chronologisch
    resolved_events = []
    try:
        with open(journal_file) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue

                event = e.get("event", "")
                if event not in ("redeemed", "resolved_loss", "close"):
                    continue

                ts = e.get("exit_ts", 0) or e.get("entry_ts", 0)
                pnl = e.get("pnl_usd", 0)
                tid = e.get("trade_id", "")
                ot = e.get("order_type", "")

                # Strategie bestimmen
                if tid.startswith("CT-"):
                    strat = "Copy"
                elif tid.startswith("ODA"):
                    strat = "ODA"
                elif tid.startswith("LT-"):
                    strat = "Momentum"
                elif tid.startswith("PT-"):
                    strat = "Paper"
                else:
                    strat = ot or "Other"

                # Paper-Trades nicht im echten PnL-Chart
                if strat == "Paper":
                    continue

                resolved_events.append({
                    "ts": ts,
                    "pnl": round(pnl, 4),
                    "trade_id": tid,
                    "strategy": strat,
                    "market": (e.get("market_question", "") or "")[:40],
                    "event": event,
                })
    except Exception:
        return {"points": [], "error": "read error"}

    # Sortiere chronologisch
    resolved_events.sort(key=lambda x: x["ts"])

    # Berechne kumulierten PnL
    cumulative = 0
    points = []
    for ev in resolved_events:
        cumulative += ev["pnl"]
        points.append({
            "time": int(ev["ts"]),
            "value": round(cumulative, 2),
            "pnl": ev["pnl"],
            "trade_id": ev["trade_id"],
            "strategy": ev["strategy"],
            "market": ev["market"],
            "event": ev["event"],
        })

    return {
        "points": points,
        "total_pnl": round(cumulative, 2),
        "count": len(points),
    }


@app.post("/api/collect/now")
async def api_collect_now() -> dict:
    """Manueller Trigger: Sofort Redeem + Merge ausführen."""
    for name, strat in active_strategies.items():
        if hasattr(strat, "redeemer"):
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, strat.redeemer.check_and_collect)
            global _last_collect_result
            _last_collect_result = result
            return result
    return {"error": "no redeemer available"}


@app.post("/api/copy/config")
async def api_copy_config(body: dict) -> dict:
    """Aktualisiert Copy Trading Konfiguration zur Laufzeit."""
    strat = active_strategies.get("copy_trading")
    if not strat:
        return {"error": "copy trading not running"}

    updated = {}
    if "max_copy_size_usd" in body:
        v = max(1.0, min(100.0, float(body["max_copy_size_usd"])))
        strat.max_copy_size_usd = v
        updated["max_copy_size_usd"] = v
    if "max_concurrent" in body:
        v = max(1, min(50, int(body["max_concurrent"])))
        strat.max_concurrent = v
        updated["max_concurrent"] = v
    if "poll_interval_s" in body:
        v = max(1.0, min(60.0, float(body["poll_interval_s"])))
        strat.poll_interval_s = v
        updated["poll_interval_s"] = v
    if "min_copy_price" in body:
        v = max(0.01, min(0.99, float(body["min_copy_price"])))
        strat.min_copy_price = v
        updated["min_copy_price"] = v
    if "max_copy_price" in body:
        v = max(0.01, min(0.99, float(body["max_copy_price"])))
        strat.max_copy_price = v
        updated["max_copy_price"] = v

    return {"status": "updated", "config": updated}


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
