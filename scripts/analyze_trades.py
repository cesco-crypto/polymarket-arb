#!/usr/bin/env python3
"""
Trade Journal Analyzer — Comprehensive Forensic Analysis.

Reads data/trade_journal.jsonl (open + close + live_update events),
merges them into complete trade records, then produces a full
statistical report covering calibration, markout, latency, entry
price, direction/asset, time-of-day, regime, advanced metrics,
aggregate stats, and statistical tests.

Output goes to stdout AND data/analysis_report.txt.

Dependencies: numpy, scipy (for statistical tests).
All other code uses only the standard library.
"""

from __future__ import annotations

import json
import math
import os
import sys
import textwrap
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

# ---------------------------------------------------------------------------
# Optional heavy imports — graceful degradation
# ---------------------------------------------------------------------------
try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from scipy import stats as sp_stats

    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = PROJECT_ROOT / "data" / "trade_journal.jsonl"
REPORT_PATH = PROJECT_ROOT / "data" / "analysis_report.txt"

# Width of the horizontal separator lines
LINE_W = 72


# ===========================================================================
# 1. DATA LOADING & MERGING
# ===========================================================================

def load_and_merge_trades(journal_path: Path) -> list[dict]:
    """Read JSONL, merge open + live_update + close into unified records.

    For each trade_id we produce ONE merged dict that contains every field
    from open, close, and live_update events.  If a field is present in
    multiple events the priority is: live_update > close > open (so the
    most-recent value wins for overlapping keys).

    Returns only *closed* trades (those that have a close event).
    """
    opens: dict[str, dict] = {}
    closes: dict[str, dict] = {}
    live_updates: dict[str, dict] = {}

    if not journal_path.exists():
        print(f"ERROR: Journal file not found at {journal_path}")
        sys.exit(1)

    with open(journal_path) as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"  WARNING: Skipping malformed JSON on line {lineno}: {exc}")
                continue

            tid = rec.get("trade_id", "")
            event = rec.get("event", "")
            if event == "open":
                opens[tid] = rec
            elif event == "close":
                closes[tid] = rec
            elif event == "live_update":
                live_updates[tid] = rec

    # Merge: start from open, overlay close, overlay live_update
    merged: list[dict] = []
    for tid, close_rec in closes.items():
        base = {}
        if tid in opens:
            base.update(opens[tid])
        base.update(close_rec)  # close fields overwrite open fields
        if tid in live_updates:
            # live_update carries live_order_success, live_order_id, etc.
            upd = live_updates[tid]
            for k, v in upd.items():
                if k == "event":
                    continue  # keep event="close"
                # Only overwrite if the update value is non-default
                if v not in (0, 0.0, "", False, None):
                    base[k] = v
        merged.append(base)

    # Sort by exit timestamp
    merged.sort(key=lambda t: t.get("exit_ts", 0))
    return merged


# ===========================================================================
# HELPERS
# ===========================================================================

def safe_float(d: dict, key: str, default: float = 0.0) -> float:
    """Extract a float from dict, defaulting on missing/None/non-numeric."""
    v = d.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def safe_int(d: dict, key: str, default: int = 0) -> int:
    v = d.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def safe_str(d: dict, key: str, default: str = "") -> str:
    v = d.get(key)
    return str(v) if v is not None else default


def safe_bool(d: dict, key: str, default: bool = False) -> bool:
    v = d.get(key)
    if v is None:
        return default
    return bool(v)


def pct_fmt(v: float, width: int = 7) -> str:
    """Format a percentage with sign, right-aligned."""
    return f"{v:+.2f}%".rjust(width)


def usd_fmt(v: float, width: int = 9) -> str:
    return f"${v:+.4f}".rjust(width)


def bucket_label(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{hi:.2f}"


def make_buckets(values: list[float], edges: list[float]) -> dict[str, list[int]]:
    """Assign indices to buckets defined by edges.

    Returns {label: [indices]} where label = "lo-hi".
    Values below the first edge go into the first bucket,
    values >= last edge go into the last bucket.
    """
    buckets: dict[str, list[int]] = {}
    for i in range(len(edges) - 1):
        buckets[bucket_label(edges[i], edges[i + 1])] = []

    for idx, v in enumerate(values):
        placed = False
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                buckets[bucket_label(edges[i], edges[i + 1])].append(idx)
                placed = True
                break
        if not placed:
            # Overflow: put in last bucket
            last_key = bucket_label(edges[-2], edges[-1])
            buckets[last_key].append(idx)

    return buckets


# ---------------------------------------------------------------------------
# ASCII table printer
# ---------------------------------------------------------------------------

def print_table(headers: list[str], rows: list[list[str]], out: TextIO) -> None:
    """Print a nicely aligned ASCII table."""
    if not rows:
        out.write("  (no data)\n")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    # Header
    hdr = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    out.write(f"  {hdr}\n")
    sep = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    out.write(f"  {sep}\n")

    # Rows
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            w = col_widths[i] if i < len(col_widths) else 10
            cells.append(str(cell).rjust(w) if i > 0 else str(cell).ljust(w))
        out.write(f"  {' | '.join(cells)}\n")


def section_header(title: str, out: TextIO) -> None:
    out.write(f"\n{'=' * LINE_W}\n")
    out.write(f"  {title}\n")
    out.write(f"{'=' * LINE_W}\n\n")


def sub_header(title: str, out: TextIO) -> None:
    out.write(f"\n  --- {title} ---\n\n")


# ===========================================================================
# 2. ANALYSIS SECTIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# 2.1 CALIBRATION ANALYSIS
# ---------------------------------------------------------------------------

def analyze_calibration(trades: list[dict], out: TextIO) -> None:
    section_header("1. CALIBRATION ANALYSIS", out)
    out.write("  p_true bucket  |  predicted  |  actual WR  |  trades  |  delta\n")
    out.write("  " + "-" * 64 + "\n")

    # Build edges from 0.50 to 1.00 in 0.01 steps (relevant buckets only)
    edges = [round(0.50 + i * 0.01, 2) for i in range(51)]  # 0.50 .. 1.00

    p_values = [safe_float(t, "p_true") for t in trades]
    outcomes = [safe_bool(t, "outcome_correct") for t in trades]

    # Group into buckets (merge thin buckets into 0.02 wide)
    coarse_edges = [round(0.50 + i * 0.02, 2) for i in range(26)]  # 0.50..1.00

    rows: list[list[str]] = []
    for i in range(len(coarse_edges) - 1):
        lo, hi = coarse_edges[i], coarse_edges[i + 1]
        idx_in = [j for j, p in enumerate(p_values) if lo <= p < hi]
        if not idx_in:
            continue
        n = len(idx_in)
        pred_avg = sum(p_values[j] for j in idx_in) / n
        actual_wr = sum(1 for j in idx_in if outcomes[j]) / n
        delta = actual_wr - pred_avg
        rows.append([
            f"  {lo:.2f} - {hi:.2f}",
            f"   {pred_avg:.4f}   ",
            f"   {actual_wr:.4f}   ",
            f"   {n:>4}   ",
            f"  {delta:+.4f}",
        ])

    if rows:
        for r in rows:
            out.write(" | ".join(r) + "\n")
    else:
        out.write("  (no trades with p_true data)\n")

    # Summary
    valid = [(p_values[i], outcomes[i]) for i in range(len(trades)) if p_values[i] > 0]
    if valid:
        avg_pred = sum(p for p, _ in valid) / len(valid)
        avg_actual = sum(1 for _, o in valid if o) / len(valid)
        out.write(f"\n  Overall:  avg predicted = {avg_pred:.4f}  |  avg actual = {avg_actual:.4f}  |  gap = {avg_actual - avg_pred:+.4f}\n")
        if avg_pred > avg_actual:
            out.write("  Interpretation: Model is OVER-CONFIDENT (predicts higher than reality)\n")
        else:
            out.write("  Interpretation: Model is UNDER-CONFIDENT (reality beats prediction)\n")


# ---------------------------------------------------------------------------
# 2.2 MARKOUT ANALYSIS
# ---------------------------------------------------------------------------

def analyze_markout(trades: list[dict], out: TextIO) -> None:
    section_header("2. MARKOUT ANALYSIS", out)

    horizons = ["markout_1s", "markout_5s", "markout_10s", "markout_30s", "markout_60s"]
    labels = ["T+1s", "T+5s", "T+10s", "T+30s", "T+60s"]

    wins = [t for t in trades if safe_bool(t, "outcome_correct")]
    losses = [t for t in trades if not safe_bool(t, "outcome_correct")]

    def avg_markout(group: list[dict], field: str) -> float:
        vals = [safe_float(t, field) for t in group]
        # Filter out zeros (missing data)
        non_zero = [v for v in vals if v != 0.0]
        return sum(non_zero) / len(non_zero) if non_zero else 0.0

    headers = ["Horizon", "All Trades", "Wins", "Losses", "Win-Loss Gap"]
    rows = []
    for h, lbl in zip(horizons, labels):
        all_avg = avg_markout(trades, h)
        win_avg = avg_markout(wins, h)
        loss_avg = avg_markout(losses, h)
        gap = win_avg - loss_avg
        rows.append([
            lbl,
            pct_fmt(all_avg),
            pct_fmt(win_avg),
            pct_fmt(loss_avg),
            pct_fmt(gap),
        ])

    print_table(headers, rows, out)

    # Signal persistence assessment
    m5_all = avg_markout(trades, "markout_5s")
    m60_all = avg_markout(trades, "markout_60s")
    out.write(f"\n  Key metric:  avg markout_5s = {m5_all:+.4f}%\n")
    if m5_all > 0:
        out.write("  Signal: POSITIVE at T+5s -> genuine predictive signal detected\n")
    elif m5_all < 0:
        out.write("  Signal: NEGATIVE at T+5s -> signal may be noise or adverse selection\n")
    else:
        out.write("  Signal: ZERO at T+5s -> insufficient markout data\n")

    if m60_all != 0:
        decay = (1 - abs(m60_all) / abs(m5_all)) * 100 if m5_all != 0 else 0
        out.write(f"  Decay:  T+5s -> T+60s = {decay:.1f}% decay\n")


# ---------------------------------------------------------------------------
# 2.3 LATENCY SEGMENTATION
# ---------------------------------------------------------------------------

def analyze_latency(trades: list[dict], out: TextIO) -> None:
    section_header("3. LATENCY SEGMENTATION", out)

    edges_ms = [0, 50, 100, 200, 500, 1000, float("inf")]
    edge_labels = ["0-50ms", "50-100ms", "100-200ms", "200-500ms", "500ms-1s", "1s+"]

    headers = ["Latency Bucket", "Trades", "Win Rate", "Avg PnL", "Avg Markout 5s"]
    rows = []

    for i in range(len(edges_ms) - 1):
        lo, hi = edges_ms[i], edges_ms[i + 1]
        group = [
            t for t in trades
            if lo <= safe_float(t, "transit_latency_ms") < hi
        ]
        if not group:
            continue
        n = len(group)
        wr = sum(1 for t in group if safe_bool(t, "outcome_correct")) / n * 100
        avg_pnl = sum(safe_float(t, "pnl_usd") for t in group) / n
        avg_m5 = sum(safe_float(t, "markout_5s") for t in group) / n
        rows.append([
            edge_labels[i],
            str(n),
            f"{wr:.1f}%",
            usd_fmt(avg_pnl),
            pct_fmt(avg_m5),
        ])

    # Also include trades with 0 latency (missing data)
    zero_lat = [t for t in trades if safe_float(t, "transit_latency_ms") == 0]
    if zero_lat and len(zero_lat) != len(trades):
        n = len(zero_lat)
        wr = sum(1 for t in zero_lat if safe_bool(t, "outcome_correct")) / n * 100
        avg_pnl = sum(safe_float(t, "pnl_usd") for t in zero_lat) / n
        rows.append(["(no data)", str(n), f"{wr:.1f}%", usd_fmt(avg_pnl), "  n/a  "])

    print_table(headers, rows, out)

    # Correlation note
    valid_lat = [
        (safe_float(t, "transit_latency_ms"), safe_bool(t, "outcome_correct"))
        for t in trades if safe_float(t, "transit_latency_ms") > 0
    ]
    if HAS_NUMPY and len(valid_lat) >= 5:
        lats = np.array([v[0] for v in valid_lat])
        outs = np.array([1.0 if v[1] else 0.0 for v in valid_lat])
        corr = np.corrcoef(lats, outs)[0, 1]
        out.write(f"\n  Latency-Win correlation: r = {corr:.4f}")
        if corr < -0.05:
            out.write(" (faster = more wins)\n")
        elif corr > 0.05:
            out.write(" (slower = more wins — unusual)\n")
        else:
            out.write(" (no significant relationship)\n")


# ---------------------------------------------------------------------------
# 2.4 ENTRY PRICE ANALYSIS
# ---------------------------------------------------------------------------

def analyze_entry_price(trades: list[dict], out: TextIO) -> None:
    section_header("4. ENTRY PRICE ANALYSIS", out)

    # Use executed_price, fall back to polymarket_ask
    def entry_price(t: dict) -> float:
        ep = safe_float(t, "executed_price")
        if ep > 0:
            return ep
        return safe_float(t, "polymarket_ask")

    edges = [0.0, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 1.01]
    edge_labels = [
        "<0.30", "0.30-0.35", "0.35-0.40", "0.40-0.45", "0.45-0.50",
        "0.50-0.55", "0.55-0.60", "0.60-0.65", "0.65-0.70", "0.70-0.80", "0.80+"
    ]

    headers = ["Entry Price", "Trades", "Win Rate", "Avg PnL", "Total PnL"]
    rows = []

    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        group = [t for t in trades if lo <= entry_price(t) < hi]
        if not group:
            continue
        n = len(group)
        wr = sum(1 for t in group if safe_bool(t, "outcome_correct")) / n * 100
        avg_pnl = sum(safe_float(t, "pnl_usd") for t in group) / n
        total_pnl = sum(safe_float(t, "pnl_usd") for t in group)
        rows.append([
            edge_labels[i],
            str(n),
            f"{wr:.1f}%",
            usd_fmt(avg_pnl),
            usd_fmt(total_pnl),
        ])

    print_table(headers, rows, out)

    # Optimal range
    if rows:
        best = max(rows, key=lambda r: float(r[3].replace("$", "").replace("+", "")))
        out.write(f"\n  Optimal entry range: {best[0]} (avg PnL = {best[3].strip()})\n")


# ---------------------------------------------------------------------------
# 2.5 DIRECTION + ASSET BREAKDOWN
# ---------------------------------------------------------------------------

def analyze_direction_asset(trades: list[dict], out: TextIO) -> None:
    section_header("5. DIRECTION + ASSET BREAKDOWN", out)

    combos: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        key = f"{safe_str(t, 'asset', 'UNK')} {safe_str(t, 'direction', 'UNK')}"
        combos[key].append(t)

    headers = ["Combo", "Trades", "Win Rate", "Total PnL", "Avg PnL", "Profit Factor"]
    rows = []

    for key in sorted(combos):
        group = combos[key]
        n = len(group)
        wins_n = sum(1 for t in group if safe_bool(t, "outcome_correct"))
        wr = wins_n / n * 100
        total_pnl = sum(safe_float(t, "pnl_usd") for t in group)
        avg_pnl = total_pnl / n
        gross_win = sum(safe_float(t, "pnl_usd") for t in group if safe_float(t, "pnl_usd") > 0)
        gross_loss = abs(sum(safe_float(t, "pnl_usd") for t in group if safe_float(t, "pnl_usd") < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        rows.append([key, str(n), f"{wr:.1f}%", usd_fmt(total_pnl), usd_fmt(avg_pnl), pf_str])

    print_table(headers, rows, out)

    # Per-asset subtotals
    sub_header("Per-Asset Subtotals", out)
    assets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        assets[safe_str(t, "asset", "UNK")].append(t)

    for asset in sorted(assets):
        group = assets[asset]
        n = len(group)
        wr = sum(1 for t in group if safe_bool(t, "outcome_correct")) / n * 100
        total_pnl = sum(safe_float(t, "pnl_usd") for t in group)
        out.write(f"  {asset}: {n} trades | WR {wr:.1f}% | PnL {usd_fmt(total_pnl)}\n")


# ---------------------------------------------------------------------------
# 2.6 TIME-OF-DAY ANALYSIS
# ---------------------------------------------------------------------------

def analyze_time_of_day(trades: list[dict], out: TextIO) -> None:
    section_header("6. TIME-OF-DAY ANALYSIS (UTC)", out)

    hourly: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        ts = safe_float(t, "entry_ts") or safe_float(t, "signal_ts")
        if ts > 0:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            hourly[dt.hour].append(t)

    if not hourly:
        out.write("  (no timestamp data available)\n")
        return

    headers = ["Hour (UTC)", "Trades", "Win Rate", "Total PnL", "Avg PnL"]
    rows = []

    for hour in range(24):
        group = hourly.get(hour, [])
        if not group:
            continue
        n = len(group)
        wr = sum(1 for t in group if safe_bool(t, "outcome_correct")) / n * 100
        total_pnl = sum(safe_float(t, "pnl_usd") for t in group)
        avg_pnl = total_pnl / n
        rows.append([f"{hour:02d}:00", str(n), f"{wr:.1f}%", usd_fmt(total_pnl), usd_fmt(avg_pnl)])

    print_table(headers, rows, out)

    # Best/worst hours
    if len(rows) >= 2:
        best = max(rows, key=lambda r: float(r[2].replace("%", "")))
        worst = min(rows, key=lambda r: float(r[2].replace("%", "")))
        out.write(f"\n  Best hour:  {best[0]} ({best[2]} WR, {best[1]} trades)\n")
        out.write(f"  Worst hour: {worst[0]} ({worst[2]} WR, {worst[1]} trades)\n")


# ---------------------------------------------------------------------------
# 2.7 REGIME ANALYSIS
# ---------------------------------------------------------------------------

def analyze_regime(trades: list[dict], out: TextIO) -> None:
    section_header("7. REGIME ANALYSIS", out)

    regimes: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        tag = safe_str(t, "regime_tag")
        if tag:
            regimes[tag].append(t)

    if not regimes:
        out.write("  (no regime_tag data available in current trades)\n")
        return

    headers = ["Regime", "Trades", "Win Rate", "Total PnL", "Avg PnL", "Avg Markout 5s"]
    rows = []

    for regime in sorted(regimes):
        group = regimes[regime]
        n = len(group)
        wr = sum(1 for t in group if safe_bool(t, "outcome_correct")) / n * 100
        total_pnl = sum(safe_float(t, "pnl_usd") for t in group)
        avg_pnl = total_pnl / n
        avg_m5 = sum(safe_float(t, "markout_5s") for t in group) / n
        rows.append([regime, str(n), f"{wr:.1f}%", usd_fmt(total_pnl), usd_fmt(avg_pnl), pct_fmt(avg_m5)])

    print_table(headers, rows, out)


# ---------------------------------------------------------------------------
# 2.8 ADVANCED METRICS
# ---------------------------------------------------------------------------

def analyze_advanced(trades: list[dict], out: TextIO) -> None:
    section_header("8. ADVANCED METRICS", out)

    # --- 8a. Orderbook Imbalance ---
    sub_header("8a. Orderbook Imbalance", out)
    imb_trades = [t for t in trades if safe_float(t, "orderbook_imbalance_pct") != 0]
    if imb_trades:
        bullish = [t for t in imb_trades if safe_float(t, "orderbook_imbalance_pct") > 0]
        bearish = [t for t in imb_trades if safe_float(t, "orderbook_imbalance_pct") < 0]
        headers = ["Imbalance", "Trades", "Win Rate", "Avg PnL"]
        rows = []
        for label, grp in [("Bullish", bullish), ("Bearish", bearish)]:
            if grp:
                n = len(grp)
                wr = sum(1 for t in grp if safe_bool(t, "outcome_correct")) / n * 100
                avg_pnl = sum(safe_float(t, "pnl_usd") for t in grp) / n
                rows.append([label, str(n), f"{wr:.1f}%", usd_fmt(avg_pnl)])
        print_table(headers, rows, out)
    else:
        out.write("  (no orderbook_imbalance_pct data)\n")

    # --- 8b. Confidence Score ---
    sub_header("8b. Confidence Score", out)
    conf_trades = [t for t in trades if safe_float(t, "confidence_score") > 0]
    if conf_trades:
        edges = [0, 20, 40, 60, 80, 101]
        labels = ["0-20", "20-40", "40-60", "60-80", "80-100"]
        headers = ["Confidence", "Trades", "Win Rate", "Avg PnL"]
        rows = []
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            grp = [t for t in conf_trades if lo <= safe_float(t, "confidence_score") < hi]
            if not grp:
                continue
            n = len(grp)
            wr = sum(1 for t in grp if safe_bool(t, "outcome_correct")) / n * 100
            avg_pnl = sum(safe_float(t, "pnl_usd") for t in grp) / n
            rows.append([labels[i], str(n), f"{wr:.1f}%", usd_fmt(avg_pnl)])
        print_table(headers, rows, out)
    else:
        out.write("  (no confidence_score data)\n")

    # --- 8c. Signal Confluence ---
    sub_header("8c. Signal Confluence Count", out)
    confl_trades = [t for t in trades if safe_int(t, "signal_confluence_count") > 0]
    if confl_trades:
        headers = ["Confluence", "Trades", "Win Rate", "Avg PnL"]
        rows = []
        for cnt in range(1, 6):
            grp = [t for t in confl_trades if safe_int(t, "signal_confluence_count") == cnt]
            if not grp:
                continue
            n = len(grp)
            wr = sum(1 for t in grp if safe_bool(t, "outcome_correct")) / n * 100
            avg_pnl = sum(safe_float(t, "pnl_usd") for t in grp) / n
            rows.append([str(cnt), str(n), f"{wr:.1f}%", usd_fmt(avg_pnl)])
        print_table(headers, rows, out)
    else:
        out.write("  (no signal_confluence_count data)\n")

    # --- 8d. Correlation Matrix ---
    sub_header("8d. Feature-Win Correlation Matrix", out)
    if not HAS_NUMPY:
        out.write("  (numpy not installed — skipping correlation matrix)\n")
        return

    feature_keys = [
        ("p_true", "p_true"),
        ("momentum_pct", "momentum"),
        ("net_ev_pct", "net_EV"),
        ("transit_latency_ms", "latency"),
        ("confidence_score", "confidence"),
        ("orderbook_imbalance_pct", "OB_imbalance"),
        ("spread_pct", "spread"),
        ("market_liquidity_usd", "liquidity"),
        ("signal_confluence_count", "confluence"),
        ("markout_5s", "markout_5s"),
    ]

    outcomes = np.array([1.0 if safe_bool(t, "outcome_correct") else 0.0 for t in trades])
    out.write(f"  {'Feature':<20}  {'r(win)':<10}  {'non-zero N':<10}\n")
    out.write(f"  {'-'*20}  {'-'*10}  {'-'*10}\n")

    for key, label in feature_keys:
        vals = np.array([safe_float(t, key) for t in trades])
        mask = vals != 0
        if mask.sum() < 5:
            out.write(f"  {label:<20}  {'n/a':>10}  {int(mask.sum()):>10}\n")
            continue
        r = np.corrcoef(vals[mask], outcomes[mask])[0, 1]
        out.write(f"  {label:<20}  {r:+.4f}      {int(mask.sum()):>10}\n")


# ---------------------------------------------------------------------------
# 2.9 AGGREGATE STATISTICS
# ---------------------------------------------------------------------------

def analyze_aggregate(trades: list[dict], out: TextIO) -> None:
    section_header("9. AGGREGATE STATISTICS", out)

    n = len(trades)
    wins = [t for t in trades if safe_bool(t, "outcome_correct")]
    losses = [t for t in trades if not safe_bool(t, "outcome_correct")]
    n_wins = len(wins)
    n_losses = len(losses)
    wr = n_wins / n * 100 if n > 0 else 0

    pnls = [safe_float(t, "pnl_usd") for t in trades]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / n if n > 0 else 0

    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0

    avg_win_pnl = sum(safe_float(t, "pnl_usd") for t in wins) / n_wins if n_wins else 0
    avg_loss_pnl = sum(safe_float(t, "pnl_usd") for t in losses) / n_losses if n_losses else 0

    # Sizes
    total_volume = sum(safe_float(t, "size_usd") for t in trades)
    avg_size = total_volume / n if n > 0 else 0
    total_fees = sum(safe_float(t, "fee_usd") for t in trades)

    out.write(f"  Total trades:         {n}\n")
    out.write(f"  Wins / Losses:        {n_wins} / {n_losses}\n")
    out.write(f"  Win Rate:             {wr:.2f}%\n")
    out.write(f"  Total PnL:            ${total_pnl:+.4f}\n")
    out.write(f"  Avg PnL per trade:    ${avg_pnl:+.4f}\n")
    out.write(f"  Avg Win PnL:          ${avg_win_pnl:+.4f}\n")
    out.write(f"  Avg Loss PnL:         ${avg_loss_pnl:+.4f}\n")
    out.write(f"  Profit Factor:        {profit_factor:.2f}\n")
    out.write(f"  Total Volume:         ${total_volume:,.2f}\n")
    out.write(f"  Avg Trade Size:       ${avg_size:.2f}\n")
    out.write(f"  Total Fees:           ${total_fees:.4f}\n")

    # --- Sharpe Ratio Estimate ---
    sub_header("Sharpe Ratio (trade-level)", out)
    if HAS_NUMPY and n >= 2:
        pnl_arr = np.array(pnls)
        mean_pnl = pnl_arr.mean()
        std_pnl = pnl_arr.std(ddof=1)
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
        # Annualize: assume ~50 trades/day, 365 days
        trades_per_day = n  # rough; use actual if timeline available
        first_ts = min(safe_float(t, "entry_ts") or safe_float(t, "signal_ts") for t in trades)
        last_ts = max(safe_float(t, "exit_ts") or safe_float(t, "entry_ts") for t in trades)
        elapsed_days = (last_ts - first_ts) / 86400 if last_ts > first_ts else 1
        tpd = n / elapsed_days if elapsed_days > 0 else n
        ann_sharpe = sharpe * math.sqrt(tpd * 365) if tpd > 0 else 0

        out.write(f"  Mean PnL / trade:     ${mean_pnl:+.4f}\n")
        out.write(f"  Std PnL / trade:      ${std_pnl:.4f}\n")
        out.write(f"  Sharpe (per trade):   {sharpe:.4f}\n")
        out.write(f"  Trades / day:         {tpd:.1f}\n")
        out.write(f"  Annualized Sharpe:    {ann_sharpe:.2f}\n")
    else:
        out.write("  (need numpy + >=2 trades for Sharpe)\n")

    # --- Max Drawdown ---
    sub_header("Drawdown", out)
    if pnls:
        cum_pnl = []
        running = 0.0
        for p in pnls:
            running += p
            cum_pnl.append(running)

        peak = cum_pnl[0]
        max_dd = 0.0
        max_dd_idx = 0
        for i, cp in enumerate(cum_pnl):
            if cp > peak:
                peak = cp
            dd = peak - cp
            if dd > max_dd:
                max_dd = dd
                max_dd_idx = i

        out.write(f"  Max Drawdown:         ${max_dd:.4f}\n")
        out.write(f"  Max DD occurred at:   trade #{max_dd_idx + 1}\n")
        out.write(f"  Final cum PnL:        ${cum_pnl[-1]:+.4f}\n")
    else:
        out.write("  (no PnL data)\n")

    # --- Holding Time ---
    sub_header("Holding Time", out)
    hold_times = []
    for t in trades:
        entry = safe_float(t, "entry_ts") or safe_float(t, "signal_ts")
        exit_ = safe_float(t, "exit_ts")
        if entry > 0 and exit_ > 0:
            hold_times.append(exit_ - entry)

    if hold_times:
        avg_hold = sum(hold_times) / len(hold_times)
        min_hold = min(hold_times)
        max_hold = max(hold_times)
        out.write(f"  Avg holding time:     {avg_hold:.1f}s ({avg_hold/60:.1f}min)\n")
        out.write(f"  Min holding time:     {min_hold:.1f}s\n")
        out.write(f"  Max holding time:     {max_hold:.1f}s ({max_hold/60:.1f}min)\n")
    else:
        out.write("  (no entry/exit timestamp data)\n")

    # --- Win/Loss Streaks ---
    sub_header("Streak Analysis", out)
    if trades:
        outcomes = [safe_bool(t, "outcome_correct") for t in trades]
        max_win_streak = 0
        max_loss_streak = 0
        cur_win = 0
        cur_loss = 0
        for o in outcomes:
            if o:
                cur_win += 1
                cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)

        out.write(f"  Max win streak:       {max_win_streak}\n")
        out.write(f"  Max loss streak:      {max_loss_streak}\n")

        # Current streak
        if outcomes:
            cur = 1
            last = outcomes[-1]
            for o in reversed(outcomes[:-1]):
                if o == last:
                    cur += 1
                else:
                    break
            streak_type = "WIN" if last else "LOSS"
            out.write(f"  Current streak:       {cur} {streak_type}\n")


# ---------------------------------------------------------------------------
# 2.10 STATISTICAL TESTS
# ---------------------------------------------------------------------------

def analyze_statistics(trades: list[dict], out: TextIO) -> None:
    section_header("10. STATISTICAL TESTS", out)

    n = len(trades)
    k = sum(1 for t in trades if safe_bool(t, "outcome_correct"))  # wins

    # --- 10a. Binomial Test ---
    sub_header("10a. Binomial Test: WR vs 50%", out)
    out.write(f"  H0: true win rate = 50%\n")
    out.write(f"  H1: true win rate != 50%\n")
    out.write(f"  Observed: {k} wins in {n} trades ({k/n*100:.2f}%)\n\n")

    if HAS_SCIPY:
        result = sp_stats.binomtest(k, n, 0.5, alternative="two-sided")
        out.write(f"  p-value (two-sided):  {result.pvalue:.6f}\n")
        if result.pvalue < 0.01:
            out.write(f"  Conclusion: HIGHLY SIGNIFICANT (p < 0.01) — WR is NOT 50%\n")
        elif result.pvalue < 0.05:
            out.write(f"  Conclusion: SIGNIFICANT (p < 0.05) — WR is likely NOT 50%\n")
        elif result.pvalue < 0.10:
            out.write(f"  Conclusion: MARGINALLY significant (p < 0.10)\n")
        else:
            out.write(f"  Conclusion: NOT significant (p >= 0.10) — cannot reject H0\n")
    else:
        # Manual z-test approximation
        p0 = 0.5
        p_hat = k / n if n > 0 else 0
        se = math.sqrt(p0 * (1 - p0) / n) if n > 0 else 1
        z = (p_hat - p0) / se if se > 0 else 0
        # Two-sided approximate p-value using normal CDF approximation
        p_approx = 2 * (1 - _norm_cdf(abs(z)))
        out.write(f"  z-statistic:          {z:.4f}\n")
        out.write(f"  p-value (approx):     {p_approx:.6f}\n")
        out.write("  (install scipy for exact binomial test)\n")

    # --- 10b. Wilson Confidence Interval ---
    sub_header("10b. Wilson 95% Confidence Interval for True WR", out)
    p_hat = k / n if n > 0 else 0
    z95 = 1.96
    denom = 1 + z95**2 / n
    center = (p_hat + z95**2 / (2 * n)) / denom
    half_w = z95 * math.sqrt((p_hat * (1 - p_hat) + z95**2 / (4 * n)) / n) / denom
    wilson_lo = center - half_w
    wilson_hi = center + half_w

    out.write(f"  Point estimate:       {p_hat:.4f} ({p_hat*100:.2f}%)\n")
    out.write(f"  Wilson 95% CI:        [{wilson_lo:.4f}, {wilson_hi:.4f}]\n")
    out.write(f"                        [{wilson_lo*100:.2f}%, {wilson_hi*100:.2f}%]\n")
    if wilson_lo > 0.50:
        out.write(f"  Interpretation:       CI entirely above 50% — EDGE IS REAL\n")
    elif wilson_hi < 0.50:
        out.write(f"  Interpretation:       CI entirely below 50% — NEGATIVE edge\n")
    else:
        out.write(f"  Interpretation:       CI spans 50% — edge not yet confirmed\n")

    # --- 10c. Bootstrap 95% CI for Total PnL ---
    sub_header("10c. Bootstrap 95% CI for Total PnL", out)
    if HAS_NUMPY:
        pnls = np.array([safe_float(t, "pnl_usd") for t in trades])
        n_boot = 10_000
        rng = np.random.default_rng(42)
        boot_pnls = np.zeros(n_boot)
        for b in range(n_boot):
            sample = rng.choice(pnls, size=n, replace=True)
            boot_pnls[b] = sample.sum()

        lo, hi = np.percentile(boot_pnls, [2.5, 97.5])
        median_boot = np.median(boot_pnls)

        out.write(f"  Bootstrap iterations: {n_boot}\n")
        out.write(f"  Observed total PnL:   ${pnls.sum():+.4f}\n")
        out.write(f"  Bootstrap median:     ${median_boot:+.4f}\n")
        out.write(f"  95% CI:               [${lo:+.4f}, ${hi:+.4f}]\n")
        if lo > 0:
            out.write(f"  Interpretation:       CI entirely positive — PROFITABLE strategy\n")
        elif hi < 0:
            out.write(f"  Interpretation:       CI entirely negative — LOSING strategy\n")
        else:
            out.write(f"  Interpretation:       CI spans zero — profitability uncertain\n")

        # Probability of positive PnL
        prob_pos = (boot_pnls > 0).mean() * 100
        out.write(f"  P(total PnL > 0):     {prob_pos:.1f}%\n")
    else:
        out.write("  (numpy required for bootstrap — skipping)\n")

    # --- 10d. Expected Value Significance ---
    sub_header("10d. Per-Trade Expected Value", out)
    pnls_list = [safe_float(t, "pnl_usd") for t in trades]
    mean_pnl = sum(pnls_list) / n if n > 0 else 0
    if n >= 2:
        var_pnl = sum((p - mean_pnl)**2 for p in pnls_list) / (n - 1)
        se_pnl = math.sqrt(var_pnl / n) if n > 0 else 0
        t_stat = mean_pnl / se_pnl if se_pnl > 0 else 0
        out.write(f"  Mean PnL:             ${mean_pnl:+.6f}\n")
        out.write(f"  SE(mean):             ${se_pnl:.6f}\n")
        out.write(f"  t-statistic:          {t_stat:.4f}\n")

        if HAS_SCIPY and n > 1:
            p_val = 2 * sp_stats.t.sf(abs(t_stat), df=n - 1)
            out.write(f"  p-value (two-sided):  {p_val:.6f}\n")
            if p_val < 0.05:
                out.write(f"  Conclusion:           Mean PnL significantly different from $0\n")
            else:
                out.write(f"  Conclusion:           Cannot reject that mean PnL = $0\n")
        else:
            out.write("  (install scipy for exact p-value on t-stat)\n")
    else:
        out.write("  (need >=2 trades for t-test)\n")


def _norm_cdf(x: float) -> float:
    """Approximate standard normal CDF (Abramowitz & Stegun 26.2.17)."""
    if x < 0:
        return 1 - _norm_cdf(-x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-x * x / 2.0)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 1.0 - p * poly


# ===========================================================================
# BONUS: LIVE vs PAPER SPLIT
# ===========================================================================

def analyze_live_vs_paper(trades: list[dict], out: TextIO) -> None:
    section_header("BONUS: LIVE vs PAPER TRADES", out)

    live = [t for t in trades if safe_bool(t, "live_order_success")]
    paper = [t for t in trades if not safe_bool(t, "live_order_success")]

    for label, group in [("LIVE (order_success=True)", live), ("PAPER / REJECTED", paper)]:
        n = len(group)
        if n == 0:
            out.write(f"  {label}: 0 trades\n\n")
            continue
        wins_n = sum(1 for t in group if safe_bool(t, "outcome_correct"))
        wr = wins_n / n * 100
        total_pnl = sum(safe_float(t, "pnl_usd") for t in group)
        avg_pnl = total_pnl / n
        out.write(f"  {label}:\n")
        out.write(f"    Trades: {n} | Wins: {wins_n} | WR: {wr:.1f}%\n")
        out.write(f"    Total PnL: ${total_pnl:+.4f} | Avg PnL: ${avg_pnl:+.4f}\n\n")


# ===========================================================================
# BONUS: RECENT TRADE LIST
# ===========================================================================

def print_recent_trades(trades: list[dict], out: TextIO, limit: int = 30) -> None:
    section_header(f"RECENT TRADES (last {limit})", out)

    recent = trades[-limit:]
    headers = ["#", "Trade ID", "Asset", "Dir", "Entry", "PnL $", "W/L", "Markout 5s"]
    rows = []
    for i, t in enumerate(recent, 1):
        tid = safe_str(t, "trade_id")
        asset = safe_str(t, "asset")
        direction = safe_str(t, "direction")
        ep = safe_float(t, "executed_price") or safe_float(t, "polymarket_ask")
        pnl = safe_float(t, "pnl_usd")
        wl = "W" if safe_bool(t, "outcome_correct") else "L"
        m5 = safe_float(t, "markout_5s")
        rows.append([
            str(i), tid[:20], asset, direction,
            f"{ep:.3f}", usd_fmt(pnl), wl, pct_fmt(m5),
        ])

    print_table(headers, rows, out)


# ===========================================================================
# BONUS: CUMULATIVE PNL CURVE (ASCII)
# ===========================================================================

def print_ascii_pnl_curve(trades: list[dict], out: TextIO, width: int = 60, height: int = 15) -> None:
    section_header("CUMULATIVE PNL CURVE (ASCII)", out)

    pnls = [safe_float(t, "pnl_usd") for t in trades]
    if not pnls:
        out.write("  (no PnL data)\n")
        return

    cum = []
    running = 0.0
    for p in pnls:
        running += p
        cum.append(running)

    n = len(cum)
    if n == 0:
        return

    # Resample to 'width' points if needed
    if n > width:
        indices = [int(i * (n - 1) / (width - 1)) for i in range(width)]
        sampled = [cum[i] for i in indices]
    else:
        sampled = cum
        width = n

    min_val = min(sampled)
    max_val = max(sampled)
    val_range = max_val - min_val
    if val_range == 0:
        val_range = 1.0

    # Build grid
    grid = [[" "] * width for _ in range(height)]

    for col, val in enumerate(sampled):
        row = int((max_val - val) / val_range * (height - 1))
        row = max(0, min(height - 1, row))
        grid[row][col] = "*"

    # Add zero line if it's in range
    if min_val <= 0 <= max_val:
        zero_row = int((max_val - 0) / val_range * (height - 1))
        zero_row = max(0, min(height - 1, zero_row))
        for col in range(width):
            if grid[zero_row][col] == " ":
                grid[zero_row][col] = "-"

    # Print
    for row_idx, row in enumerate(grid):
        val_at_row = max_val - row_idx * val_range / (height - 1)
        label = f"${val_at_row:+.3f}"
        out.write(f"  {label:>10} |{''.join(row)}|\n")

    out.write(f"  {'':>10} +{'-' * width}+\n")
    out.write(f"  {'':>10}  {'Trade #1':<{width // 2}}{'#' + str(n):>{width - width // 2}}\n")


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> None:
    print(f"Trade Journal Analyzer")
    print(f"{'=' * LINE_W}")
    print(f"Journal:  {JOURNAL_PATH}")
    print(f"Report:   {REPORT_PATH}")
    print()

    # Load data
    trades = load_and_merge_trades(JOURNAL_PATH)
    print(f"Loaded {len(trades)} closed trades from journal.\n")

    if not trades:
        print("No closed trades found. Nothing to analyze.")
        sys.exit(0)

    # Date range
    first_ts = min(
        safe_float(t, "entry_ts") or safe_float(t, "signal_ts") or safe_float(t, "exit_ts")
        for t in trades
    )
    last_ts = max(
        safe_float(t, "exit_ts") or safe_float(t, "entry_ts") or safe_float(t, "signal_ts")
        for t in trades
    )
    first_dt = datetime.fromtimestamp(first_ts, tz=timezone.utc) if first_ts > 0 else None
    last_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc) if last_ts > 0 else None

    # Tee output to both stdout and file
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report_file = open(REPORT_PATH, "w")

    class TeeWriter:
        """Write to both stdout and a file simultaneously."""
        def __init__(self, *targets: TextIO):
            self.targets = targets

        def write(self, text: str) -> None:
            for t in self.targets:
                t.write(text)

        def flush(self) -> None:
            for t in self.targets:
                t.flush()

    out = TeeWriter(sys.stdout, report_file)

    # --- Report Header ---
    out.write(f"\n{'#' * LINE_W}\n")
    out.write(f"  TRADE JOURNAL ANALYSIS REPORT\n")
    out.write(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    out.write(f"{'#' * LINE_W}\n\n")
    out.write(f"  Source:       {JOURNAL_PATH}\n")
    out.write(f"  Trades:       {len(trades)}\n")
    if first_dt:
        out.write(f"  First trade:  {first_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    if last_dt:
        out.write(f"  Last trade:   {last_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
    if first_dt and last_dt:
        span = (last_ts - first_ts) / 86400
        out.write(f"  Time span:    {span:.2f} days\n")
    out.write(f"  numpy:        {'yes' if HAS_NUMPY else 'NO (some features disabled)'}\n")
    out.write(f"  scipy:        {'yes' if HAS_SCIPY else 'NO (some features disabled)'}\n")

    # --- Run All Sections ---
    analyze_calibration(trades, out)
    analyze_markout(trades, out)
    analyze_latency(trades, out)
    analyze_entry_price(trades, out)
    analyze_direction_asset(trades, out)
    analyze_time_of_day(trades, out)
    analyze_regime(trades, out)
    analyze_advanced(trades, out)
    analyze_aggregate(trades, out)
    analyze_statistics(trades, out)
    analyze_live_vs_paper(trades, out)
    print_recent_trades(trades, out)
    print_ascii_pnl_curve(trades, out)

    # --- Footer ---
    out.write(f"\n{'#' * LINE_W}\n")
    out.write(f"  END OF REPORT\n")
    out.write(f"{'#' * LINE_W}\n")

    report_file.close()
    print(f"\nReport saved to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
