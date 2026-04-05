"""Config Tracker — Automatischer Snapshot & Diff aller Trading-Parameter.

Bei jedem Bot-Start:
1. Sammelt ALLE trading-relevanten Parameter aus allen Modulen
2. Vergleicht mit dem letzten gespeicherten Snapshot
3. Loggt Änderungen als Events in data/config_changes.jsonl
4. Speichert neuen Snapshot

Kein AI, kein ML — einfacher JSON-Diff. Aber erfasst JEDE Änderung.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

SNAPSHOT_FILE = Path("data/config_snapshot.json")
CHANGES_FILE = Path("data/config_changes.jsonl")


def collect_current_config(settings, active_strategies: dict) -> dict:
    """Sammelt alle trading-relevanten Parameter aus dem laufenden System."""
    config = {
        "_timestamp": time.time(),
        "_version": "1.0",

        # Global Settings
        "live_trading": settings.live_trading,
        "strategy_name": settings.strategy_name,
        "max_live_position_usd": settings.max_live_position_usd,
        "kelly_fraction": settings.kelly_fraction,
        "min_momentum_pct": settings.min_momentum_pct,
        "min_edge_pct": settings.min_edge_pct,
        "polymarket_max_fee_pct": settings.polymarket_max_fee_pct,
        "order_type": settings.order_type,
        "min_seconds_to_expiry": settings.min_seconds_to_expiry,
        "max_seconds_to_expiry": settings.max_seconds_to_expiry,
        "paper_capital_usd": settings.paper_capital_usd,
        "max_position_pct": settings.max_position_pct,

        # Active Strategies
        "active_strategies": sorted(active_strategies.keys()),
    }

    # Copy Trading Parameters
    ct = active_strategies.get("copy_trading")
    if ct:
        config["ct_poll_interval_s"] = ct.poll_interval_s
        config["ct_max_copy_size_usd"] = ct.max_copy_size_usd
        config["ct_max_concurrent"] = ct.max_concurrent
        config["ct_min_copy_price"] = ct.min_copy_price
        config["ct_max_copy_price"] = ct.max_copy_price
        config["ct_tracked_wallets"] = len(ct.tracked_wallets)
        config["ct_tracked_names"] = [w.get("name", "?") for w in ct.tracked_wallets]
        if hasattr(ct, "guards"):
            config["ct_slippage_max_pct"] = ct.guards.max_slippage_pct
            config["ct_max_market_exposure"] = ct.guards.max_market_exposure
        if hasattr(ct, "risk"):
            config["ct_max_daily_loss"] = ct.risk.max_daily_loss_usd
            config["ct_max_total_loss"] = ct.risk.max_total_loss_usd

    # ODA Parameters
    oda = active_strategies.get("oracle_delay_arb")
    if oda:
        config["oda_trade_size_usd"] = oda.trade_size_usd
        config["oda_min_entry_price"] = oda.min_entry_price
        config["oda_max_entry_price"] = oda.max_entry_price
        config["oda_delay_after_close_s"] = oda.delay_after_close_s
        config["oda_max_delay_s"] = oda.max_delay_s

    # Executor Parameters (from the first strategy that has one)
    for name, strat in active_strategies.items():
        if hasattr(strat, "executor"):
            config["executor_min_order_usd"] = strat.executor.MIN_ORDER_SIZE_USD
            config["executor_min_shares"] = strat.executor.MIN_SHARES
            break

    return config


def load_last_snapshot() -> dict | None:
    """Lädt den letzten Config-Snapshot."""
    try:
        if SNAPSHOT_FILE.exists():
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def save_snapshot(config: dict) -> None:
    """Speichert den aktuellen Config-Snapshot (atomic write)."""
    try:
        SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
        import os
        os.replace(str(tmp), str(SNAPSHOT_FILE))
    except Exception as e:
        logger.error(f"Config snapshot save error: {e}")


def compute_diff(old: dict, new: dict) -> list[dict]:
    """Vergleicht zwei Config-Snapshots und gibt die Änderungen zurück."""
    changes = []
    skip_keys = {"_timestamp", "_version"}

    all_keys = set(old.keys()) | set(new.keys())
    for key in sorted(all_keys):
        if key in skip_keys:
            continue
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val != new_val:
            changes.append({
                "parameter": key,
                "old_value": old_val,
                "new_value": new_val,
            })
    return changes


def log_changes(changes: list[dict]) -> None:
    """Schreibt Änderungen ins Config-Changes-Journal."""
    if not changes:
        return
    try:
        CHANGES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CHANGES_FILE, "a") as f:
            event = {
                "timestamp": time.time(),
                "changes": changes,
                "count": len(changes),
            }
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        logger.error(f"Config changes log error: {e}")


def track_config_changes(settings, active_strategies: dict) -> list[dict]:
    """Hauptfunktion: Snapshot → Diff → Log → Save.

    Aufrufen beim Bot-Start (in startup()).
    Returns: Liste der Änderungen (leer wenn keine).
    """
    current = collect_current_config(settings, active_strategies)
    previous = load_last_snapshot()

    if previous is None:
        # Erster Start — nur speichern, kein Diff
        save_snapshot(current)
        logger.info(
            f"CONFIG TRACKER | First snapshot saved | "
            f"{len(current)} parameters tracked"
        )
        return []

    changes = compute_diff(previous, current)

    if changes:
        log_changes(changes)
        save_snapshot(current)
        for c in changes:
            logger.info(
                f"CONFIG CHANGE | {c['parameter']}: "
                f"{c['old_value']} → {c['new_value']}"
            )
        logger.info(
            f"CONFIG TRACKER | {len(changes)} changes detected and logged"
        )
    else:
        # Kein Diff — aber Snapshot trotzdem aktualisieren (Timestamp)
        save_snapshot(current)
        logger.info("CONFIG TRACKER | No changes since last deploy")

    return changes


def get_change_history() -> list[dict]:
    """Liest die komplette Change-History für das Dashboard."""
    history = []
    try:
        if CHANGES_FILE.exists():
            with open(CHANGES_FILE) as f:
                for line in f:
                    if line.strip():
                        try:
                            history.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
    except Exception:
        pass
    return history
