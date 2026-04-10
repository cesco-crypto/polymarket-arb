#!/usr/bin/env python3
"""Signal-Tier Forensics — Auswertung nach 300+ Trades.

Liest trade_journal.jsonl, gruppiert nach Signal-Tier (confidence_score)
und berechnet EV, Fill-Rate, Latency pro Tier.

Usage:
    python3 scripts/signal_tier_forensics.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

JOURNAL_PATHS = [
    Path("data/trade_journal.jsonl"),
    Path("data/trade_journal_archive_20260408.jsonl"),
]

TIER_MAP = {3: "STRONG", 2: "MID", 1: "LOW", 0: "UNKNOWN"}


def load_trades():
    opens = {}
    redeems = {}
    for jp in JOURNAL_PATHS:
        if not jp.exists():
            continue
        with open(jp) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    tid = d.get("trade_id", "")
                    event = d.get("event", "")
                    if event == "open":
                        opens[tid] = d
                    elif event == "redeemed":
                        redeems[tid] = d
                except Exception:
                    pass
    return opens, redeems


def main():
    opens, redeems = load_trades()
    print(f"Loaded: {len(opens)} opens, {len(redeems)} redeems\n")

    # Filter: nur Trades mit confidence_score > 0 (nach Instrumentierung)
    instrumented = {tid: o for tid, o in opens.items() if o.get("confidence_score", 0) > 0}
    print(f"Instrumented (confidence_score > 0): {len(instrumented)}")

    if len(instrumented) < 10:
        print("\nNicht genug instrumentierte Trades. Warte auf mehr Daten.")
        print("(Trades vor der Instrumentierung haben confidence_score=0)")

    # === SECTION 1: EV PER TIER ===
    print(f"\n{'='*80}")
    print("SECTION 1: EV PER SIGNAL-TIER")
    print(f"{'='*80}")

    tiers = defaultdict(lambda: {"bursts": 0, "fills": 0, "exec_fail": 0, "wins": 0, "losses": 0,
                                  "pnl_sum": 0.0, "win_pnl": 0.0, "loss_pnl": 0.0})

    for tid, o in instrumented.items():
        tier = TIER_MAP.get(o.get("confidence_score", 0), "UNKNOWN")
        tiers[tier]["bursts"] += 1

        r = redeems.get(tid)
        if o.get("live_order_success"):
            tiers[tier]["fills"] += 1
        elif o.get("fill_type") == "EXECUTION_FAIL" or not o.get("live_order_id"):
            tiers[tier]["exec_fail"] += 1

        if r:
            pnl = r.get("pnl_usd", 0)
            tiers[tier]["pnl_sum"] += pnl
            if pnl > 0:
                tiers[tier]["wins"] += 1
                tiers[tier]["win_pnl"] += pnl
            else:
                tiers[tier]["losses"] += 1
                tiers[tier]["loss_pnl"] += pnl

    print(f"\n{'Tier':<8} {'Bursts':>7} {'Fills':>6} {'ExFail':>7} {'FillR':>6} {'WR':>5} {'AvgWin':>8} {'AvgLoss':>8} {'EV/Burst':>9}")
    print("-" * 75)
    for tier in ["STRONG", "MID", "LOW", "UNKNOWN"]:
        d = tiers[tier]
        if d["bursts"] == 0:
            continue
        fill_rate = d["fills"] / d["bursts"] * 100 if d["bursts"] > 0 else 0
        resolved = d["wins"] + d["losses"]
        wr = d["wins"] / resolved * 100 if resolved > 0 else 0
        avg_win = d["win_pnl"] / d["wins"] if d["wins"] > 0 else 0
        avg_loss = d["loss_pnl"] / d["losses"] if d["losses"] > 0 else 0
        ev = (fill_rate / 100) * (wr / 100 * avg_win + (1 - wr / 100) * avg_loss) if resolved > 0 else 0
        n_tag = "" if d["bursts"] >= 20 else " *"
        print(f"{tier:<8} {d['bursts']:>7} {d['fills']:>6} {d['exec_fail']:>7} {fill_rate:>5.0f}% {wr:>4.0f}% {avg_win:>+7.2f} {avg_loss:>+7.2f} {ev:>+8.3f}{n_tag}")

    print("\n* = N<20, statistisch nicht belastbar")
    print("EV/Burst = FillRate × (WR × AvgWin + (1-WR) × AvgLoss)")

    # === SECTION 2: PREDICTIVE POWER ===
    print(f"\n{'='*80}")
    print("SECTION 2: MOMENTUM als PREDICTOR")
    print(f"{'='*80}")

    momentum_buckets = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0})
    for tid, o in opens.items():
        r = redeems.get(tid)
        if not r:
            continue
        mom = abs(o.get("momentum_pct", 0))
        if mom < 0.05:
            b = "<0.05%"
        elif mom < 0.10:
            b = "0.05-0.10%"
        elif mom < 0.20:
            b = "0.10-0.20%"
        else:
            b = ">0.20%"
        momentum_buckets[b]["n"] += 1
        if r.get("pnl_usd", 0) > 0:
            momentum_buckets[b]["wins"] += 1
        else:
            momentum_buckets[b]["losses"] += 1

    print(f"\n{'|Δ| Bucket':<14} {'N':>5} {'Wins':>5} {'WR':>6}")
    print("-" * 35)
    for b in ["<0.05%", "0.05-0.10%", "0.10-0.20%", ">0.20%"]:
        d = momentum_buckets[b]
        wr = d["wins"] / d["n"] * 100 if d["n"] > 0 else 0
        n_tag = "" if d["n"] >= 20 else " *"
        print(f"{b:<14} {d['n']:>5} {d['wins']:>5} {wr:>5.0f}%{n_tag}")

    print("\nMonoton steigend? → Momentum ist ein valider Predictor")

    # === SECTION 3: LATENCY vs OUTCOME ===
    print(f"\n{'='*80}")
    print("SECTION 3: LATENCY vs OUTCOME (breite Buckets, ±drift)")
    print(f"{'='*80}")

    latency_buckets = defaultdict(lambda: {"fills": 0, "exec_fail": 0})
    for tid, o in instrumented.items():
        cex_lag = o.get("cex_lag_ms", 0)
        if cex_lag <= 0:
            continue
        if cex_lag < 75:
            b = "<75ms"
        elif cex_lag < 175:
            b = "75-175ms"
        else:
            b = ">175ms"
        if o.get("live_order_success"):
            latency_buckets[b]["fills"] += 1
        else:
            latency_buckets[b]["exec_fail"] += 1

    if latency_buckets:
        print(f"\n{'Bucket':<14} {'Fills':>6} {'ExFail':>7} {'ExFail%':>8}")
        print("-" * 40)
        for b in ["<75ms", "75-175ms", ">175ms"]:
            d = latency_buckets[b]
            total = d["fills"] + d["exec_fail"]
            rate = d["exec_fail"] / total * 100 if total > 0 else 0
            print(f"{b:<14} {d['fills']:>6} {d['exec_fail']:>7} {rate:>7.0f}%")
    else:
        print("\nKeine Latency-Daten (cex_lag_ms). Braucht instrumentierte Trades.")

    # === SECTION 4: SPREAD vs FILL ===
    print(f"\n{'='*80}")
    print("SECTION 4: SPREAD vs FILL")
    print(f"{'='*80}")

    spread_buckets = defaultdict(lambda: {"fills": 0, "exec_fail": 0})
    for tid, o in instrumented.items():
        sp = o.get("spread_pct", 0)
        if sp <= 0:
            continue
        if sp < 0.5:
            b = "<0.5%"
        elif sp < 1.0:
            b = "0.5-1.0%"
        else:
            b = ">1.0%"
        if o.get("live_order_success"):
            spread_buckets[b]["fills"] += 1
        else:
            spread_buckets[b]["exec_fail"] += 1

    if spread_buckets:
        print(f"\n{'Spread':<14} {'Fills':>6} {'ExFail':>7} {'FillRate':>9}")
        print("-" * 40)
        for b in ["<0.5%", "0.5-1.0%", ">1.0%"]:
            d = spread_buckets[b]
            total = d["fills"] + d["exec_fail"]
            rate = d["fills"] / total * 100 if total > 0 else 0
            print(f"{b:<14} {d['fills']:>6} {d['exec_fail']:>7} {rate:>8.0f}%")
    else:
        print("\nKeine Spread-Daten. Braucht instrumentierte Trades.")

    # === SECTION 5: REGIME ===
    print(f"\n{'='*80}")
    print("SECTION 5: REGIME CONTEXT")
    print(f"{'='*80}")

    regime_stats = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for tid, o in instrumented.items():
        regime = o.get("regime_tag", "")
        if not regime:
            continue
        regime_stats[regime]["n"] += 1
        r = redeems.get(tid)
        if r:
            pnl = r.get("pnl_usd", 0)
            regime_stats[regime]["pnl"] += pnl
            if pnl > 0:
                regime_stats[regime]["wins"] += 1
            else:
                regime_stats[regime]["losses"] += 1

    if regime_stats:
        print(f"\n{'Regime':<10} {'N':>5} {'WR':>6} {'PnL':>9}")
        print("-" * 35)
        for reg in ["HIGH_VOL", "MID_VOL", "LOW_VOL"]:
            d = regime_stats[reg]
            resolved = d["wins"] + d["losses"]
            wr = d["wins"] / resolved * 100 if resolved > 0 else 0
            print(f"{reg:<10} {d['n']:>5} {wr:>5.0f}% {d['pnl']:>+8.2f}")
    else:
        print("\nKeine Regime-Daten. Braucht instrumentierte Trades.")

    print(f"\n{'='*80}")
    print(f"Total: {len(opens)} trades, {len(instrumented)} instrumented, {len(redeems)} resolved")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
