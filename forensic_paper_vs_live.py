#!/usr/bin/env python3
"""Forensic Analysis: Paper Trades vs Live Trades.

Reads trade_journal.jsonl, matches each live trade (LT-*) to its
corresponding paper trade (PT-*) on the same window/signal, then
compares entry price, PnL, markouts, and latency.

Run on the server:
    python3 forensic_paper_vs_live.py

Or point to a different journal file:
    python3 forensic_paper_vs_live.py /path/to/trade_journal.jsonl
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# 1. LOAD DATA
# ---------------------------------------------------------------------------

journal_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/ubuntu/polymarket-arb/data/trade_journal.jsonl")

if not journal_path.exists():
    print(f"ERROR: Journal file not found at {journal_path}")
    sys.exit(1)

# Parse all lines -- keep open, close, live_update keyed by trade_id
opens: dict[str, dict] = {}
closes: dict[str, dict] = {}
live_updates: dict[str, dict] = {}

line_count = 0
parse_errors = 0

with open(journal_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        line_count += 1
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue

        event = data.get("event", "")
        tid = data.get("trade_id", "")
        if not tid:
            continue

        if event == "open":
            opens[tid] = data
        elif event == "close":
            closes[tid] = data
        elif event == "live_update":
            live_updates[tid] = data

print(f"{'='*80}")
print(f"FORENSIC ANALYSIS: Paper vs Live Trade Comparison")
print(f"{'='*80}")
print(f"Journal: {journal_path}")
print(f"Lines parsed: {line_count} (errors: {parse_errors})")
print(f"Open records: {len(opens)}")
print(f"Close records: {len(closes)}")
print(f"Live updates: {len(live_updates)}")
print()

# ---------------------------------------------------------------------------
# 2. MERGE into self-contained close records
# ---------------------------------------------------------------------------

_open_fields = (
    "signal_ts", "window_slug", "market_question", "timeframe",
    "polymarket_bid", "polymarket_ask",
    "p_market", "raw_edge_pct", "fee_pct", "net_ev_pct",
    "shares", "kelly_fraction",
    "signal_to_order_ms", "transit_latency_ms", "tick_age_ms",
    "order_type", "seconds_to_expiry", "market_liquidity_usd", "spread_pct",
    "orderbook_imbalance_pct", "cex_lag_ms", "implied_prob",
    "regime_tag", "signal_confluence_count", "confidence_score",
    "gas_fee_usd", "fill_type", "position_size_usd",
    "entry_timestamp_ms", "market_id",
    "condition_id", "source_wallet", "source_wallet_name", "source_tx_hash",
)

merged_close: dict[str, dict] = {}

for tid, data in closes.items():
    rec = dict(data)

    # merge open-time fields
    if tid in opens:
        opn = opens[tid]
        for fld in _open_fields:
            if not rec.get(fld):
                val = opn.get(fld)
                if val:
                    rec[fld] = val

    # merge live_update fields
    if tid in live_updates:
        upd = live_updates[tid]
        rec["live_order_success"] = upd.get("live_order_success", False)
        rec["live_order_id"] = upd.get("live_order_id", "")
        rec["live_error"] = upd.get("live_error", "")
        rec["order_post_ts"] = upd.get("order_post_ts", 0)
        if upd.get("slippage_pct"):
            rec["slippage_pct"] = upd["slippage_pct"]
        if upd.get("fill_type"):
            rec["fill_type"] = upd["fill_type"]
        if upd.get("position_size_usd"):
            rec["position_size_usd"] = upd["position_size_usd"]

    merged_close[tid] = rec

# ---------------------------------------------------------------------------
# 3. SEPARATE live vs paper trades
# ---------------------------------------------------------------------------

live_trades: list[dict] = []
paper_trades: list[dict] = []

for tid, rec in merged_close.items():
    if tid.startswith("LT-"):
        live_trades.append(rec)
    elif tid.startswith("PT-"):
        paper_trades.append(rec)

print(f"Closed live trades (LT-*):  {len(live_trades)}")
print(f"Closed paper trades (PT-*): {len(paper_trades)}")
print()

if not live_trades:
    print("NO LIVE TRADES FOUND. Nothing to compare.")
    print()
    print("Dumping paper trade summary instead...")
    if paper_trades:
        wins = sum(1 for t in paper_trades if t.get("outcome_correct"))
        total_pnl = sum(t.get("pnl_usd", 0) for t in paper_trades)
        print(f"  Paper trades: {len(paper_trades)}")
        print(f"  Paper wins: {wins} ({100*wins/len(paper_trades):.1f}%)")
        print(f"  Paper total PnL: ${total_pnl:.4f}")
    sys.exit(0)

# ---------------------------------------------------------------------------
# 4. MATCH live trades to paper trades
# ---------------------------------------------------------------------------

def match_key(rec: dict) -> str:
    """Generate a matching key from window_slug or signal_ts + asset + direction."""
    slug = rec.get("window_slug", "")
    if slug:
        return slug
    # Fallback: round signal_ts to nearest second + asset + direction
    sig = rec.get("signal_ts", 0)
    asset = rec.get("asset", "")
    direction = rec.get("direction", "")
    return f"{round(sig)}_{asset}_{direction}"

# Build paper index by match key
paper_by_key: dict[str, list[dict]] = defaultdict(list)
for pt in paper_trades:
    key = match_key(pt)
    paper_by_key[key].append(pt)

# Also build a secondary index by signal_ts for fuzzy matching
paper_by_signal: list[tuple[float, dict]] = []
for pt in paper_trades:
    sig = pt.get("signal_ts", 0)
    if sig > 0:
        paper_by_signal.append((sig, pt))
paper_by_signal.sort(key=lambda x: x[0])

def find_nearest_paper(live_rec: dict) -> dict | None:
    """Find the best matching paper trade for a live trade."""
    # 1. Exact match by window_slug
    key = match_key(live_rec)
    candidates = paper_by_key.get(key, [])
    if candidates:
        # If multiple, pick the one closest in signal_ts
        live_sig = live_rec.get("signal_ts", 0)
        best = min(candidates, key=lambda c: abs(c.get("signal_ts", 0) - live_sig))
        return best

    # 2. Fuzzy match by signal_ts (within 5 seconds) + same asset + direction
    live_sig = live_rec.get("signal_ts", 0)
    live_asset = live_rec.get("asset", "")
    live_dir = live_rec.get("direction", "")

    best_match = None
    best_dt = float("inf")
    for sig_ts, pt in paper_by_signal:
        dt = abs(sig_ts - live_sig)
        if dt > 5.0:  # more than 5 seconds apart = not the same signal
            if sig_ts > live_sig + 5:
                break
            continue
        if pt.get("asset") == live_asset and pt.get("direction") == live_dir:
            if dt < best_dt:
                best_dt = dt
                best_match = pt
    return best_match

# ---------------------------------------------------------------------------
# 5. COMPARE matched pairs
# ---------------------------------------------------------------------------

matched_pairs: list[dict] = []
unmatched_live: list[dict] = []

for lt in live_trades:
    pt = find_nearest_paper(lt)
    if pt:
        matched_pairs.append({"live": lt, "paper": pt})
    else:
        unmatched_live.append(lt)

print(f"{'='*80}")
print(f"MATCHING RESULTS")
print(f"{'='*80}")
print(f"Matched pairs:     {len(matched_pairs)}")
print(f"Unmatched live:    {len(unmatched_live)}")
print()

# ---------------------------------------------------------------------------
# 6. DETAILED PAIR COMPARISON
# ---------------------------------------------------------------------------

def ts_str(ts: float) -> str:
    if ts <= 0:
        return "N/A"
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def safe_float(d: dict, key: str) -> float:
    v = d.get(key, 0)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

print(f"{'='*80}")
print(f"TRADE-BY-TRADE COMPARISON")
print(f"{'='*80}")

# Accumulators for averages
price_diffs = []       # live executed_price - paper executed_price
pnl_diffs = []         # live pnl_usd - paper pnl_usd
markout_1s_diffs = []
markout_5s_diffs = []
markout_10s_diffs = []
markout_30s_diffs = []
markout_60s_diffs = []
live_latencies = []    # signal_to_order_ms for live trades
paper_latencies = []
slippage_values = []
live_fill_failures = []

for i, pair in enumerate(matched_pairs, 1):
    lt = pair["live"]
    pt = pair["paper"]

    lt_price = safe_float(lt, "executed_price")
    pt_price = safe_float(pt, "executed_price")
    price_diff = lt_price - pt_price

    lt_pnl = safe_float(lt, "pnl_usd")
    pt_pnl = safe_float(pt, "pnl_usd")
    pnl_diff = lt_pnl - pt_pnl

    lt_m1 = safe_float(lt, "markout_1s")
    pt_m1 = safe_float(pt, "markout_1s")
    lt_m5 = safe_float(lt, "markout_5s")
    pt_m5 = safe_float(pt, "markout_5s")
    lt_m10 = safe_float(lt, "markout_10s")
    pt_m10 = safe_float(pt, "markout_10s")
    lt_m30 = safe_float(lt, "markout_30s")
    pt_m30 = safe_float(pt, "markout_30s")
    lt_m60 = safe_float(lt, "markout_60s")
    pt_m60 = safe_float(pt, "markout_60s")

    lt_latency = safe_float(lt, "signal_to_order_ms")
    pt_latency = safe_float(pt, "signal_to_order_ms")

    lt_slip = safe_float(lt, "slippage_pct")
    lt_success = lt.get("live_order_success", False)
    lt_error = lt.get("live_error", "")
    lt_fill_type = lt.get("fill_type", "")

    # Accumulate for averages
    if lt_price > 0 and pt_price > 0:
        price_diffs.append(price_diff)
    pnl_diffs.append(pnl_diff)
    if lt_m1 != 0 or pt_m1 != 0:
        markout_1s_diffs.append(lt_m1 - pt_m1)
    if lt_m5 != 0 or pt_m5 != 0:
        markout_5s_diffs.append(lt_m5 - pt_m5)
    if lt_m10 != 0 or pt_m10 != 0:
        markout_10s_diffs.append(lt_m10 - pt_m10)
    if lt_m30 != 0 or pt_m30 != 0:
        markout_30s_diffs.append(lt_m30 - pt_m30)
    if lt_m60 != 0 or pt_m60 != 0:
        markout_60s_diffs.append(lt_m60 - pt_m60)
    if lt_latency > 0:
        live_latencies.append(lt_latency)
    if pt_latency > 0:
        paper_latencies.append(pt_latency)
    if lt_slip != 0:
        slippage_values.append(lt_slip)
    if not lt_success:
        live_fill_failures.append(lt)

    # Print individual comparison
    print(f"\n--- Pair #{i} ---")
    print(f"  Live:  {lt.get('trade_id','?')}  |  Paper: {pt.get('trade_id','?')}")
    print(f"  Asset: {lt.get('asset','?')} {lt.get('direction','?')} | Window: {lt.get('window_slug','?')}")
    print(f"  Signal time: {ts_str(safe_float(lt, 'signal_ts'))}")
    print(f"  Entry price:   Live={lt_price:.4f}  Paper={pt_price:.4f}  Diff={price_diff:+.4f}")
    print(f"  PnL (USD):     Live=${lt_pnl:+.4f}  Paper=${pt_pnl:+.4f}  Diff=${pnl_diff:+.4f}")
    print(f"  Outcome:       Live={'WIN' if lt.get('outcome_correct') else 'LOSS'}  Paper={'WIN' if pt.get('outcome_correct') else 'LOSS'}")
    print(f"  Size:          Live=${safe_float(lt,'size_usd'):.2f}  Paper=${safe_float(pt,'size_usd'):.2f}")
    print(f"  Markout 1s:    Live={lt_m1:+.4f}%  Paper={pt_m1:+.4f}%  Diff={lt_m1-pt_m1:+.4f}%")
    print(f"  Markout 5s:    Live={lt_m5:+.4f}%  Paper={pt_m5:+.4f}%  Diff={lt_m5-pt_m5:+.4f}%")
    print(f"  Markout 10s:   Live={lt_m10:+.4f}%  Paper={pt_m10:+.4f}%  Diff={lt_m10-pt_m10:+.4f}%")
    print(f"  Markout 30s:   Live={lt_m30:+.4f}%  Paper={pt_m30:+.4f}%  Diff={lt_m30-pt_m30:+.4f}%")
    print(f"  Markout 60s:   Live={lt_m60:+.4f}%  Paper={pt_m60:+.4f}%  Diff={lt_m60-pt_m60:+.4f}%")
    print(f"  Latency:       Live={lt_latency:.0f}ms  Paper={pt_latency:.0f}ms")
    print(f"  Live fill:     Success={lt_success}  Type={lt_fill_type}  Slippage={lt_slip:+.4f}%")
    if lt_error:
        print(f"  Live error:    {lt_error}")

# ---------------------------------------------------------------------------
# 7. AGGREGATE STATISTICS
# ---------------------------------------------------------------------------

def avg(lst: list[float]) -> float:
    return sum(lst) / len(lst) if lst else 0.0

def median(lst: list[float]) -> float:
    if not lst:
        return 0.0
    s = sorted(lst)
    n = len(s)
    if n % 2 == 0:
        return (s[n//2 - 1] + s[n//2]) / 2
    return s[n//2]

def stdev(lst: list[float]) -> float:
    if len(lst) < 2:
        return 0.0
    m = avg(lst)
    return (sum((x - m)**2 for x in lst) / (len(lst) - 1)) ** 0.5

print(f"\n{'='*80}")
print(f"AGGREGATE STATISTICS ({len(matched_pairs)} matched pairs)")
print(f"{'='*80}")

print(f"\n--- Price Difference (live - paper executed_price) ---")
if price_diffs:
    print(f"  Mean:   {avg(price_diffs):+.6f}")
    print(f"  Median: {median(price_diffs):+.6f}")
    print(f"  StdDev: {stdev(price_diffs):.6f}")
    print(f"  Min:    {min(price_diffs):+.6f}")
    print(f"  Max:    {max(price_diffs):+.6f}")
    print(f"  N:      {len(price_diffs)}")
    worse = sum(1 for d in price_diffs if d > 0)
    print(f"  Live pays more: {worse}/{len(price_diffs)} ({100*worse/len(price_diffs):.0f}%)")
else:
    print(f"  No valid price pairs.")

print(f"\n--- PnL Difference (live - paper) ---")
if pnl_diffs:
    print(f"  Mean:   ${avg(pnl_diffs):+.6f}")
    print(f"  Median: ${median(pnl_diffs):+.6f}")
    print(f"  StdDev: ${stdev(pnl_diffs):.6f}")
    print(f"  Min:    ${min(pnl_diffs):+.6f}")
    print(f"  Max:    ${max(pnl_diffs):+.6f}")
    print(f"  Total PnL gap: ${sum(pnl_diffs):+.6f}")
    live_total = sum(safe_float(p["live"], "pnl_usd") for p in matched_pairs)
    paper_total = sum(safe_float(p["paper"], "pnl_usd") for p in matched_pairs)
    print(f"  Live total PnL:  ${live_total:+.6f}")
    print(f"  Paper total PnL: ${paper_total:+.6f}")

print(f"\n--- Markout Differences (live - paper) ---")
for label, diffs in [
    ("1s",  markout_1s_diffs),
    ("5s",  markout_5s_diffs),
    ("10s", markout_10s_diffs),
    ("30s", markout_30s_diffs),
    ("60s", markout_60s_diffs),
]:
    if diffs:
        print(f"  T+{label:>3s}: mean={avg(diffs):+.4f}%  median={median(diffs):+.4f}%  (N={len(diffs)})")
    else:
        print(f"  T+{label:>3s}: no data")

print(f"\n--- Latency ---")
if live_latencies:
    print(f"  Live  signal_to_order_ms:  mean={avg(live_latencies):.1f}  median={median(live_latencies):.1f}  max={max(live_latencies):.1f}  (N={len(live_latencies)})")
if paper_latencies:
    print(f"  Paper signal_to_order_ms:  mean={avg(paper_latencies):.1f}  median={median(paper_latencies):.1f}  max={max(paper_latencies):.1f}  (N={len(paper_latencies)})")
if live_latencies and paper_latencies:
    print(f"  Latency overhead (live-paper): {avg(live_latencies) - avg(paper_latencies):+.1f}ms mean")

print(f"\n--- Slippage (live trades only) ---")
if slippage_values:
    print(f"  Mean:   {avg(slippage_values):+.4f}%")
    print(f"  Median: {median(slippage_values):+.4f}%")
    print(f"  Max:    {max(slippage_values):+.4f}%")
    print(f"  N:      {len(slippage_values)}")
else:
    print(f"  No slippage data recorded.")

print(f"\n--- Live Order Failures ---")
print(f"  Failed fills: {len(live_fill_failures)}/{len(matched_pairs)}")
if live_fill_failures:
    for lf in live_fill_failures:
        print(f"    {lf.get('trade_id','?')}: {lf.get('live_error','unknown')[:80]}")

# ---------------------------------------------------------------------------
# 8. OUTCOME DIVERGENCE: where live and paper disagreed on win/loss
# ---------------------------------------------------------------------------

print(f"\n{'='*80}")
print(f"OUTCOME DIVERGENCE (live won but paper lost, or vice versa)")
print(f"{'='*80}")

diverged = [
    p for p in matched_pairs
    if p["live"].get("outcome_correct") != p["paper"].get("outcome_correct")
]
print(f"Divergent outcomes: {len(diverged)}/{len(matched_pairs)}")
for p in diverged:
    lt, pt = p["live"], p["paper"]
    print(f"  {lt.get('trade_id','?')} vs {pt.get('trade_id','?')}: "
          f"Live={'WIN' if lt.get('outcome_correct') else 'LOSS'} "
          f"Paper={'WIN' if pt.get('outcome_correct') else 'LOSS'} "
          f"| PnL diff: ${safe_float(lt,'pnl_usd') - safe_float(pt,'pnl_usd'):+.4f}")

# ---------------------------------------------------------------------------
# 9. WORST LIVE TRADES (biggest negative PnL divergence from paper)
# ---------------------------------------------------------------------------

print(f"\n{'='*80}")
print(f"WORST LIVE TRADES (largest negative PnL divergence vs paper)")
print(f"{'='*80}")

ranked = sorted(matched_pairs, key=lambda p: safe_float(p["live"], "pnl_usd") - safe_float(p["paper"], "pnl_usd"))
top_n = min(10, len(ranked))

for i, pair in enumerate(ranked[:top_n], 1):
    lt = pair["live"]
    pt = pair["paper"]
    lt_pnl = safe_float(lt, "pnl_usd")
    pt_pnl = safe_float(pt, "pnl_usd")
    diff = lt_pnl - pt_pnl

    print(f"\n  #{i}  PnL gap: ${diff:+.4f}")
    print(f"    Live {lt.get('trade_id','?')}: PnL=${lt_pnl:+.4f} price={safe_float(lt,'executed_price'):.4f} "
          f"{'WIN' if lt.get('outcome_correct') else 'LOSS'} "
          f"slip={safe_float(lt,'slippage_pct'):+.4f}% "
          f"latency={safe_float(lt,'signal_to_order_ms'):.0f}ms")
    print(f"    Paper {pt.get('trade_id','?')}: PnL=${pt_pnl:+.4f} price={safe_float(pt,'executed_price'):.4f} "
          f"{'WIN' if pt.get('outcome_correct') else 'LOSS'}")
    print(f"    Window: {lt.get('window_slug','?')} | {lt.get('asset','?')} {lt.get('direction','?')}")
    if lt.get("live_error"):
        print(f"    ERROR: {lt['live_error'][:100]}")
    if not lt.get("live_order_success"):
        print(f"    ** FILL FAILED **")

# ---------------------------------------------------------------------------
# 10. UNMATCHED LIVE TRADES (no paper counterpart)
# ---------------------------------------------------------------------------

if unmatched_live:
    print(f"\n{'='*80}")
    print(f"UNMATCHED LIVE TRADES (no paper counterpart found)")
    print(f"{'='*80}")
    for lt in unmatched_live:
        print(f"  {lt.get('trade_id','?')} | {lt.get('asset','?')} {lt.get('direction','?')} "
              f"| Signal: {ts_str(safe_float(lt,'signal_ts'))} "
              f"| PnL: ${safe_float(lt,'pnl_usd'):+.4f} "
              f"| Slug: {lt.get('window_slug','?')}")

# ---------------------------------------------------------------------------
# 11. SUMMARY VERDICTS
# ---------------------------------------------------------------------------

print(f"\n{'='*80}")
print(f"EXECUTIVE SUMMARY")
print(f"{'='*80}")

if matched_pairs:
    live_wins = sum(1 for p in matched_pairs if p["live"].get("outcome_correct"))
    paper_wins = sum(1 for p in matched_pairs if p["paper"].get("outcome_correct"))
    live_wr = 100 * live_wins / len(matched_pairs) if matched_pairs else 0
    paper_wr = 100 * paper_wins / len(matched_pairs) if matched_pairs else 0

    live_total_pnl = sum(safe_float(p["live"], "pnl_usd") for p in matched_pairs)
    paper_total_pnl = sum(safe_float(p["paper"], "pnl_usd") for p in matched_pairs)

    print(f"  Matched pairs:        {len(matched_pairs)}")
    print(f"  Live win rate:        {live_wr:.1f}%  ({live_wins}/{len(matched_pairs)})")
    print(f"  Paper win rate:       {paper_wr:.1f}%  ({paper_wins}/{len(matched_pairs)})")
    print(f"  Win rate gap:         {live_wr - paper_wr:+.1f}pp")
    print(f"  Live total PnL:       ${live_total_pnl:+.4f}")
    print(f"  Paper total PnL:      ${paper_total_pnl:+.4f}")
    print(f"  PnL gap (live-paper): ${live_total_pnl - paper_total_pnl:+.4f}")
    print(f"  Outcome divergences:  {len(diverged)}")
    if price_diffs:
        print(f"  Avg price paid more:  {avg(price_diffs):+.6f}")
    if live_latencies:
        print(f"  Avg live latency:     {avg(live_latencies):.1f}ms")
    if slippage_values:
        print(f"  Avg live slippage:    {avg(slippage_values):+.4f}%")
    print(f"  Live fill failures:   {len(live_fill_failures)}")

    # Verdict
    print(f"\n  VERDICT:")
    if live_total_pnl >= paper_total_pnl:
        print(f"  >> Live execution MATCHES or BEATS paper. Execution is clean.")
    elif abs(live_total_pnl - paper_total_pnl) < 0.05:
        print(f"  >> Negligible gap. Execution cost is within noise.")
    else:
        gap_pct = 100 * (paper_total_pnl - live_total_pnl) / abs(paper_total_pnl) if paper_total_pnl != 0 else 0
        print(f"  >> Live UNDERPERFORMS paper by ${paper_total_pnl - live_total_pnl:.4f} ({gap_pct:.1f}% of paper PnL)")
        if live_fill_failures:
            print(f"  >> PRIMARY CAUSE: {len(live_fill_failures)} fill failures")
        if slippage_values and avg(slippage_values) > 0.1:
            print(f"  >> CONTRIBUTING: avg slippage {avg(slippage_values):+.4f}%")
        if live_latencies and avg(live_latencies) > 500:
            print(f"  >> CONTRIBUTING: high latency {avg(live_latencies):.0f}ms")

print(f"\n{'='*80}")
print(f"END OF FORENSIC REPORT")
print(f"{'='*80}")
