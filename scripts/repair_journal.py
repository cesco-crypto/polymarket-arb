"""Einmaliges Reparatur-Script: Verknüpft historische Redemptions mit dem Trade Journal.

NICHT in den Bot integriert. Standalone-Ausführung:
    python3 scripts/repair_journal.py

Was es tut:
1. Liest trade_journal.jsonl (alle open events)
2. Matcht die 9 bekannten Redemptions per Titel
3. Holt aktuelle Positions-Daten von der Polymarket API
4. Schreibt fehlende 'redeemed' close-events ins Journal
5. Validiert die JSONL-Integrität nach dem Schreiben

SICHERHEIT:
- Append-only: Bestehende Einträge werden NIE geändert
- Dry-Run Modus: --dry-run Flag zeigt was passieren würde ohne zu schreiben
- Backup: Erstellt automatisch eine Kopie vor dem Schreiben
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen, Request

JOURNAL_PATH = Path("data/trade_journal.jsonl")
WALLET = "0xc0135e6D18031ea2Eb5A171d8eD24308682E4105"

# ══════════════════════════════════════════════════════════════
# Die 9 bekannten, verifizierten Redemptions (aus Auto-Collect Logs)
# ══════════════════════════════════════════════════════════════
KNOWN_REDEMPTIONS = [
    {"title_match": "Counter-Strike: 3DMAX vs FOKUS", "payout": 11.36},
    {"title_match": "Milwaukee Brewers vs. Kansas City", "payout": 10.00},
    {"title_match": "Club Atlético de Madrid", "payout": 9.66},
    {"title_match": "FC Bayern München", "payout": 9.26},
    {"title_match": "Barletta: Kimmer Coppejans vs Vitaliy", "payout": 9.27},
    {"title_match": "SS Lazio vs. Parma Calcio", "payout": 8.66},
    {"title_match": "Kasımpaşa SK vs. Kayserispor", "payout": 6.94},
    {"title_match": "FC DAC 1904 Dunajská Streda", "payout": 6.41},
    {"title_match": "Hibernian FC", "payout": 5.00},
]


def load_journal() -> list[dict]:
    """Lädt alle Journal-Einträge."""
    entries = []
    if not JOURNAL_PATH.exists():
        print(f"FEHLER: {JOURNAL_PATH} nicht gefunden")
        sys.exit(1)

    with open(JOURNAL_PATH) as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"WARNUNG: Zeile {i} ist kein valides JSON")
    return entries


def fetch_positions() -> list[dict]:
    """Holt aktuelle Positionen von der Polymarket API."""
    try:
        url = f"https://data-api.polymarket.com/positions?user={WALLET}&limit=500"
        req = Request(url, headers={"User-Agent": "polymarket-arb/2.0"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"WARNUNG: Positions-API Fehler: {e}")
        return []


def find_open_for_condition(opens: list[dict], condition_id: str) -> dict | None:
    """Findet den passenden Open-Event für eine condition_id."""
    for e in opens:
        if e.get("condition_id") == condition_id:
            return e
    return None


def find_open_for_title(opens: list[dict], title_fragment: str) -> dict | None:
    """Fuzzy-Match: Findet Open-Event über Titel-Fragment."""
    fragment_lower = title_fragment[:20].lower()
    for e in opens:
        q = e.get("market_question", "").lower()
        if fragment_lower in q:
            return e
    return None


def already_redeemed(entries: list[dict], trade_id: str) -> bool:
    """Prüft ob für diesen trade_id bereits ein redeemed-Event existiert."""
    for e in entries:
        if e.get("trade_id") == trade_id and e.get("event") == "redeemed":
            return True
    return False


def validate_journal() -> tuple[int, int]:
    """Validiert die gesamte JSONL-Datei. Returns (valid, invalid)."""
    valid = 0
    invalid = 0
    with open(JOURNAL_PATH) as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    invalid += 1
                    print(f"  FEHLER Zeile {i}: Kein dict")
                elif "trade_id" not in entry or "event" not in entry:
                    invalid += 1
                    print(f"  FEHLER Zeile {i}: Fehlende Pflichtfelder")
                else:
                    valid += 1
            except json.JSONDecodeError:
                invalid += 1
                print(f"  FEHLER Zeile {i}: Invalides JSON")
    return valid, invalid


def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("JOURNAL REPARATUR-SCRIPT")
    print("Modus: %s" % ("DRY-RUN (keine Änderungen)" if dry_run else "LIVE (schreibt ins Journal)"))
    print("=" * 60)
    print()

    # 1. Journal laden
    entries = load_journal()
    opens = [e for e in entries if e.get("event") == "open"]
    existing_redeemed = [e for e in entries if e.get("event") == "redeemed"]

    print(f"Journal: {len(entries)} Einträge, {len(opens)} opens, {len(existing_redeemed)} redeemed")
    print()

    # 2. Positions API laden
    positions = fetch_positions()
    pos_by_cid = {}
    for p in positions:
        cid = p.get("conditionId", "")
        if cid:
            pos_by_cid[cid] = p
    print(f"Positions API: {len(positions)} Positionen geladen")
    print()

    # ══════════════════════════════════════════════════════════
    # TEIL A: Die 9 bekannten Redemptions
    # ══════════════════════════════════════════════════════════
    new_events = []

    print("-" * 60)
    print("TEIL A: 9 bekannte Redemptions matchen")
    print("-" * 60)

    for redeem in KNOWN_REDEMPTIONS:
        title_match = redeem["title_match"]
        payout = redeem["payout"]

        # Match im Journal
        match = find_open_for_title(opens, title_match)
        if not match:
            print(f"  ✗ KEIN MATCH: {title_match[:40]}")
            continue

        trade_id = match.get("trade_id", "UNKNOWN")
        condition_id = match.get("condition_id", "")
        invested = match.get("size_usd", 0)
        order_type = match.get("order_type", "unknown")
        source = match.get("source_wallet_name", "")

        # Bereits redeemed?
        if already_redeemed(entries, trade_id):
            print(f"  ⏭ SKIP (bereits redeemed): {trade_id} — {title_match[:30]}")
            continue

        pnl = payout - invested
        close_event = {
            "trade_id": trade_id,
            "event": "redeemed",
            "exit_ts": time.time(),
            "condition_id": condition_id,
            "market_question": match.get("market_question", ""),
            "direction": match.get("direction", ""),
            "order_type": order_type,
            "source_wallet_name": source,
            "executed_price": match.get("executed_price", 0),
            "size_usd": invested,
            "pnl_usd": round(pnl, 4),
            "pnl_pct": round(pnl / max(0.01, invested) * 100, 2),
            "outcome_correct": True,
            "payout_usd": payout,
            "redeem_tx_hash": "",  # Nicht verfügbar für historische
            "repair_source": "known_redemption",
        }

        new_events.append(close_event)
        print(f"  ✓ MATCH: {trade_id} ({order_type}) — inv ${invested:.2f} → ${payout:.2f} = ${pnl:+.2f} | {title_match[:30]}")

    # ══════════════════════════════════════════════════════════
    # TEIL B: Positions API Abgleich (condition_id Match)
    # ══════════════════════════════════════════════════════════
    print()
    print("-" * 60)
    print("TEIL B: Positions API Abgleich via condition_id")
    print("-" * 60)

    for e in opens:
        cid = e.get("condition_id", "")
        trade_id = e.get("trade_id", "")

        if not cid or not trade_id:
            continue

        # Bereits redeemed?
        if already_redeemed(entries + new_events, trade_id):
            continue

        # Position in API finden
        pos = pos_by_cid.get(cid)
        if not pos:
            continue

        cur_price = pos.get("curPrice", 0)
        current_value = pos.get("currentValue", 0)
        initial_value = pos.get("initialValue", 0)
        redeemable = pos.get("redeemable", False)
        outcome = pos.get("outcome", "")

        # Status bestimmen
        if cur_price <= 0.05 and current_value <= 0.01:
            # Verloren
            pnl = -e.get("size_usd", 0)
            close_event = {
                "trade_id": trade_id,
                "event": "resolved_loss",
                "exit_ts": time.time(),
                "condition_id": cid,
                "market_question": e.get("market_question", ""),
                "direction": e.get("direction", ""),
                "order_type": e.get("order_type", "unknown"),
                "source_wallet_name": e.get("source_wallet_name", ""),
                "executed_price": e.get("executed_price", 0),
                "size_usd": e.get("size_usd", 0),
                "pnl_usd": round(pnl, 4),
                "pnl_pct": -100.0,
                "outcome_correct": False,
                "payout_usd": 0,
                "repair_source": "positions_api_match",
            }
            new_events.append(close_event)
            print(f"  ✗ LOSS: {trade_id} — ${e.get('size_usd', 0):.2f} lost | {e.get('market_question', '')[:35]}")

        elif cur_price >= 0.95 and current_value > 0.01:
            # Gewonnen, noch nicht redeemed (pending)
            print(f"  ⏳ PENDING: {trade_id} — ${current_value:.2f} val, redeem={redeemable} | {e.get('market_question', '')[:35]}")

    # ══════════════════════════════════════════════════════════
    # TEIL C: Schreiben
    # ══════════════════════════════════════════════════════════
    print()
    print("-" * 60)
    print(f"ERGEBNIS: {len(new_events)} neue Events zum Schreiben")
    print("-" * 60)

    wins = [e for e in new_events if e.get("event") == "redeemed"]
    losses = [e for e in new_events if e.get("event") == "resolved_loss"]
    win_pnl = sum(e.get("pnl_usd", 0) for e in wins)
    loss_pnl = sum(e.get("pnl_usd", 0) for e in losses)

    print(f"  Redeemed (Wins):  {len(wins)} — PnL: ${win_pnl:+.2f}")
    print(f"  Resolved (Losses): {len(losses)} — PnL: ${loss_pnl:+.2f}")
    print(f"  Net PnL:           ${win_pnl + loss_pnl:+.2f}")

    if dry_run:
        print()
        print("  DRY-RUN — Keine Änderungen geschrieben.")
        return

    if not new_events:
        print("  Nichts zu schreiben.")
        return

    # Backup
    backup_path = JOURNAL_PATH.with_suffix(".jsonl.backup")
    shutil.copy2(JOURNAL_PATH, backup_path)
    print(f"\n  Backup erstellt: {backup_path}")

    # Append
    with open(JOURNAL_PATH, "a") as f:
        for event in new_events:
            f.write(json.dumps(event) + "\n")
    print(f"  {len(new_events)} Events ins Journal geschrieben.")

    # Validierung
    print()
    print("VALIDIERUNG:")
    valid, invalid = validate_journal()
    print(f"  Valide Zeilen: {valid}")
    print(f"  Invalide Zeilen: {invalid}")
    if invalid == 0:
        print("  ✅ Journal ist strukturell korrekt.")
    else:
        print("  ❌ Journal hat Fehler! Backup wiederherstellen:")
        print(f"     cp {backup_path} {JOURNAL_PATH}")


if __name__ == "__main__":
    main()
