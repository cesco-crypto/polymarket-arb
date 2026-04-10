"""Headless Engine — Polymarket ODA Trading Bot.

Pure asyncio, kein Webserver. Laedt Strategy-State, startet Strategien,
AutoRedeemer, Telegram Alerts, graceful Shutdown.

Usage:
  python main.py              → Headless Engine (Produktion)
  python main.py --dashboard  → Legacy Web-Dashboard (Debugging)
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from config import settings
from utils.logger import setup_logger


# ═══════════════════════════════════════════════════════════════════
# STRATEGY STATE PERSISTENCE — Identisch mit web_ui.py
# ═══════════════════════════════════════════════════════════════════

STRATEGY_STATE_FILE = Path(__file__).parent / "data" / "strategy_state.json"


def _load_strategy_state() -> dict | None:
    """Laedt gespeicherten Strategie-State. Returns None bei Fehler."""
    try:
        if not STRATEGY_STATE_FILE.exists():
            return None
        with open(STRATEGY_STATE_FILE) as f:
            state = json.load(f)
        if isinstance(state, dict) and "active" in state:
            return state
        return None
    except Exception as e:
        logger.warning(f"Strategy state load error (using fallback): {e}")
        return None


def _save_strategy_state(active: dict) -> None:
    """Speichert aktive Strategien + Live-Mode persistent (atomic write)."""
    try:
        STRATEGY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "active": list(active.keys()),
            "live_trading": settings.live_trading,
            "saved_at": time.time(),
        }
        tmp = STRATEGY_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(str(tmp), str(STRATEGY_STATE_FILE))
    except Exception as e:
        logger.error(f"Strategy state save FAILED: {e}")


# ═══════════════════════════════════════════════════════════════════
# GLOBAL AUTO-REDEEMER — Identisch mit web_ui.py
# ═══════════════════════════════════════════════════════════════════

_global_redeemer = None


def _get_global_redeemer():
    """Lazy-Init: Erstellt den globalen Redeemer einmalig beim ersten Aufruf."""
    global _global_redeemer
    if _global_redeemer is None:
        pk = settings.polymarket_private_key
        wallet = settings.polymarket_funder
        safe = getattr(settings, 'polymarket_safe_address', '')
        if pk and wallet:
            from core.redeemer import AutoRedeemer
            _global_redeemer = AutoRedeemer(
                private_key=pk,
                wallet_address=safe or wallet,  # Safe hat Vorrang
                safe_address=safe,
                builder_api_key=os.environ.get('POLY_BUILDER_API_KEY', ''),
                builder_secret=os.environ.get('POLY_BUILDER_SECRET', ''),
                builder_passphrase=os.environ.get('POLY_BUILDER_PASSPHRASE', ''),
            )
            mode = "GASLESS (Safe Relayer)" if safe else "EOA (Gas)"
            logger.info(f"Global AutoRedeemer initialisiert ({mode})")
    return _global_redeemer


async def _auto_collect_loop() -> None:
    """Alle 60 Sekunden: Redeem gewonnene Positionen — global, strategie-unabhaengig."""
    await asyncio.sleep(15)  # Warte auf System-Start

    while True:
        try:
            redeemer = _get_global_redeemer()

            if redeemer:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, redeemer.check_and_collect)

                if result.get("redeemed", 0) > 0 or result.get("merged", 0) > 0:
                    from utils import telegram
                    await telegram.send_alert(
                        f"\U0001f4b0 <b>AUTO-COLLECT</b>\n"
                        f"Redeemed: {result.get('redeemed', 0)} (${result.get('redeem_usd', 0):.2f})\n"
                        f"Merged: {result.get('merged', 0)} (${result.get('merge_usd', 0):.2f})\n"
                        f"Stuck: ${result.get('stuck_usd', 0):.2f}\n"
                        f"Pending Merge: ${result.get('pending_merge_usd', 0):.2f}"
                    )
        except Exception as e:
            logger.error(f"Auto-Collect Loop Error: {e}")

        await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════════════════
# HEADLESS ENGINE
# ═══════════════════════════════════════════════════════════════════

active_strategies: dict = {}
_strategy_tasks: dict = {}


async def _run_headless() -> None:
    """Main Headless Engine — Pure asyncio, kein Webserver."""
    global active_strategies, _strategy_tasks

    setup_logger(level=settings.log_level, data_dir=settings.data_dir)
    logger.info("=" * 60)
    logger.info("OPERATION HEADLESS — Pure Async Engine")
    logger.info(f"PID: {os.getpid()} | Instance: {os.environ.get('INSTANCE_LABEL', 'LOCAL')}")
    logger.info("=" * 60)

    # 1. Strategy-Module importieren (registriert sie in der Registry)
    import strategies.oracle_delay_arb     # noqa: F401
    import strategies.polymarket_latency   # noqa: F401
    import strategies.hmsf_strategy        # noqa: F401
    import strategies.copy_trading         # noqa: F401

    from strategies.registry import create_strategy, list_strategies

    # 2. Strategy-State laden (persistent ueber Restarts)
    saved = _load_strategy_state()
    available = list_strategies()

    if saved:
        settings.live_trading = saved.get("live_trading", settings.live_trading)
        names_to_start = saved.get("active", [settings.strategy_name])
        saved_ts = saved.get("saved_at", 0)
        logger.info(
            f"STATE LOADED | active={len(names_to_start)} | "
            f"strategies={names_to_start} | live={settings.live_trading} | "
            f"saved_at={datetime.fromtimestamp(saved_ts).strftime('%H:%M:%S') if saved_ts else '?'}"
        )
    else:
        names_to_start = [settings.strategy_name]
        logger.warning("NO STATE FILE — using config default")

    # 3. Strategien erstellen + starten
    for name in names_to_start:
        if name not in available:
            logger.warning(f"Strategy '{name}' not in registry — skip")
            continue
        try:
            strat = create_strategy(name, settings)
            active_strategies[name] = strat
            _strategy_tasks[name] = asyncio.create_task(strat.run())
            logger.info(f"Strategy started: {name} — {strat.description}")
        except Exception as e:
            logger.error(f"Strategy '{name}' FAILED: {e}")

    if not active_strategies:
        logger.error("KEINE Strategie gestartet — Exit")
        return

    # 4. Config Tracker
    try:
        from core.config_tracker import track_config_changes
        track_config_changes(settings, active_strategies)
    except Exception as e:
        logger.error(f"Config tracker: {e}")

    # 5. Telegram Startup-Alert
    try:
        from utils import telegram
        telegram.configure(settings)
        strat_names = ", ".join(active_strategies.keys())
        mode = "LIVE" if settings.live_trading else "PAPER"
        await telegram.send_alert(
            f"\U0001f680 <b>HEADLESS ENGINE STARTED</b>\n"
            f"Mode: {mode}\n"
            f"Strategies: {strat_names}\n"
            f"Instance: {os.environ.get('INSTANCE_LABEL', 'LOCAL')}\n"
            f"PID: {os.getpid()}"
        )
    except Exception as e:
        logger.warning(f"Telegram startup alert failed: {e}")

    # 6. Auto-Collect Loop (Background-Task)
    collect_task = asyncio.create_task(_auto_collect_loop())

    # 7. Graceful Shutdown via Signal
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    logger.info(
        f"Engine running | {len(active_strategies)} strategies | "
        f"{'LIVE' if settings.live_trading else 'PAPER'} | Ctrl+C to stop"
    )

    # 8. Blockieren bis Shutdown-Signal
    await shutdown_event.wait()

    # 9. Graceful Shutdown
    logger.info("Shutdown signal received...")

    collect_task.cancel()
    try:
        await collect_task
    except (asyncio.CancelledError, Exception):
        pass

    for name, task in _strategy_tasks.items():
        task.cancel()
    await asyncio.gather(*_strategy_tasks.values(), return_exceptions=True)

    for name, strat in active_strategies.items():
        try:
            await strat.shutdown()
        except Exception as e:
            logger.error(f"Shutdown {name}: {e}")

    # 10. Telegram Shutdown-Alert
    try:
        from utils import telegram
        await telegram.send_alert(
            f"\U0001f6d1 <b>HEADLESS ENGINE STOPPED</b>\n"
            f"PID: {os.getpid()}\n"
            f"Strategies: {', '.join(active_strategies.keys())}"
        )
        await telegram.close()
    except Exception:
        pass

    logger.info("Engine stopped.")


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--dashboard" in sys.argv:
        # Legacy: Web-Dashboard (fuer Debugging)
        import uvicorn
        port = int(os.environ.get("PORT", 8000))
        uvicorn.run("dashboard.web_ui:app", host="0.0.0.0", port=port, log_level="info")
    else:
        # Default: Headless Engine (Produktion)
        asyncio.run(_run_headless())
