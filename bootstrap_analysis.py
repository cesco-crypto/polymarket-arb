#!/usr/bin/env python3
"""
Monte Carlo Bootstrap Analysis for Polymarket Latency Arb Bot
=============================================================
17 real trades -> 10,000 synthetic paths per simulation.
5 scenarios from conservative to catastrophe.
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Tuple

np.random.seed(42)

# ─── REAL TRADE DATA ─────────────────────────────────────────────────────────
REAL_PNLS = np.array([
    +8.81,   # 1. ETH DOWN WIN
    -1.91,   # 2. ETH UP LOSS
    +5.42,   # 3. ETH DOWN WIN
    -2.27,   # 4. ETH DOWN LOSS (correction: was UP)
    +4.66,   # 5. ETH DOWN WIN
    -2.25,   # 6. ETH UP LOSS
    +5.31,   # 7. BTC DOWN WIN
    -5.09,   # 8. ETH UP LOSS
    +8.44,   # 9. BTC UP WIN
    +2.35,   # 10. ETH DOWN WIN
    -2.97,   # 11. ETH DOWN LOSS
    +6.82,   # 12. ETH DOWN WIN
    +2.00,   # 13. ETH DOWN WIN
    -1.24,   # 14. ETH DOWN LOSS
    +1.97,   # 15. ETH DOWN WIN
    -5.08,   # 16. ETH DOWN LOSS
    -5.02,   # 17. ETH UP LOSS
])

STARTING_CAPITAL = 87.61
FEE_PER_TRADE = 0.06
N_PATHS = 10_000

# ─── DERIVED STATS FROM REAL DATA ────────────────────────────────────────────
wins = REAL_PNLS[REAL_PNLS > 0]
losses = REAL_PNLS[REAL_PNLS < 0]
observed_wr = len(wins) / len(REAL_PNLS)
avg_win = np.mean(wins)
avg_loss = np.mean(np.abs(losses))

print("=" * 72)
print("REAL TRADE STATISTICS (17 trades)")
print("=" * 72)
print(f"  Win Rate:         {observed_wr:.1%} ({len(wins)}W / {len(losses)}L)")
print(f"  Avg Win:          ${avg_win:.2f}")
print(f"  Avg Loss:         ${avg_loss:.2f}")
print(f"  Win/Loss Ratio:   {avg_win/avg_loss:.2f}x")
print(f"  Total PnL:        ${REAL_PNLS.sum():.2f}")
print(f"  Starting Capital: ${STARTING_CAPITAL:.2f}")
print(f"  Current Capital:  ${STARTING_CAPITAL + REAL_PNLS.sum():.2f}")
print(f"  Fee per trade:    ${FEE_PER_TRADE:.2f}")
print(f"  Expected PnL/trade (observed): ${np.mean(REAL_PNLS):.2f}")
print()


@dataclass
class SimResult:
    name: str
    terminal_capitals: np.ndarray
    max_drawdowns: np.ndarray  # as fractions (0.0 to 1.0)
    win_rates: np.ndarray
    n_trades: int


def run_bootstrap_sim(n_paths: int, n_trades: int, pnls: np.ndarray,
                      start_cap: float, fee: float) -> SimResult:
    """Bootstrap from real PnL distribution (sample with replacement)."""
    terminal_caps = np.zeros(n_paths)
    max_dds = np.zeros(n_paths)
    wrs = np.zeros(n_paths)

    for i in range(n_paths):
        sampled = np.random.choice(pnls, size=n_trades, replace=True)
        # Apply fees
        net_pnls = sampled - fee
        equity_curve = start_cap + np.cumsum(net_pnls)
        equity_with_start = np.concatenate([[start_cap], equity_curve])

        # Clamp at zero (can't go negative in real life)
        equity_with_start = np.maximum(equity_with_start, 0.0)

        running_max = np.maximum.accumulate(equity_with_start)
        drawdowns = (running_max - equity_with_start) / np.maximum(running_max, 1e-10)

        terminal_caps[i] = equity_with_start[-1]
        max_dds[i] = np.max(drawdowns)
        wrs[i] = np.mean(sampled > 0)

    return SimResult(
        name="bootstrap",
        terminal_capitals=terminal_caps,
        max_drawdowns=max_dds,
        win_rates=wrs,
        n_trades=n_trades,
    )


def run_parametric_sim(n_paths: int, n_trades: int, win_rate: float,
                       avg_win: float, avg_loss: float,
                       start_cap: float, fee: float) -> SimResult:
    """Parametric simulation: flip coin at given WR, pay avg_win or avg_loss."""
    terminal_caps = np.zeros(n_paths)
    max_dds = np.zeros(n_paths)
    wrs = np.zeros(n_paths)

    for i in range(n_paths):
        flips = np.random.random(n_trades)
        pnls = np.where(flips < win_rate, avg_win, -avg_loss) - fee
        equity_curve = start_cap + np.cumsum(pnls)
        equity_with_start = np.concatenate([[start_cap], equity_curve])
        equity_with_start = np.maximum(equity_with_start, 0.0)

        running_max = np.maximum.accumulate(equity_with_start)
        drawdowns = (running_max - equity_with_start) / np.maximum(running_max, 1e-10)

        terminal_caps[i] = equity_with_start[-1]
        max_dds[i] = np.max(drawdowns)
        actual_wins = np.sum(flips < win_rate)
        wrs[i] = actual_wins / n_trades

    return SimResult(
        name="parametric",
        terminal_capitals=terminal_caps,
        max_drawdowns=max_dds,
        win_rates=wrs,
        n_trades=n_trades,
    )


def report(sim: SimResult, title: str, start_cap: float, trades_per_day: float = 2.5):
    """Print comprehensive report for a simulation."""
    tc = sim.terminal_capitals
    dd = sim.max_drawdowns

    # Percentiles
    p5, p25, p50, p75, p95 = np.percentile(tc, [5, 25, 50, 75, 95])

    # Probabilities
    p_profit = np.mean(tc > start_cap) * 100
    p_gt200 = np.mean(tc > 200) * 100
    p_lt50 = np.mean(tc < 50) * 100
    p_lt10 = np.mean(tc < 10) * 100

    # Drawdowns
    dd_median = np.median(dd) * 100
    dd_p95 = np.percentile(dd, 95) * 100

    # Expected value per trade
    avg_pnl_per_trade = (np.mean(tc) - start_cap) / sim.n_trades

    # Days to $1000
    if avg_pnl_per_trade > 0:
        trades_to_1000 = (1000 - start_cap) / avg_pnl_per_trade
        days_to_1000 = trades_to_1000 / trades_per_day
    else:
        trades_to_1000 = float('inf')
        days_to_1000 = float('inf')

    print("=" * 72)
    print(f"  {title}")
    print(f"  ({N_PATHS:,} paths x {sim.n_trades} trades)")
    print("=" * 72)
    print()
    print(f"  TERMINAL CAPITAL DISTRIBUTION")
    print(f"  {'─' * 40}")
    print(f"  P5  (worst 5%):     ${p5:>10.2f}")
    print(f"  P25 (lower quartile):${p25:>9.2f}")
    print(f"  P50 (median):        ${p50:>9.2f}")
    print(f"  P75 (upper quartile):${p75:>9.2f}")
    print(f"  P95 (best 5%):      ${p95:>10.2f}")
    print(f"  Mean:                ${np.mean(tc):>9.2f}")
    print(f"  Std Dev:             ${np.std(tc):>9.2f}")
    print()
    print(f"  PROBABILITIES")
    print(f"  {'─' * 40}")
    print(f"  P(profit > 0):       {p_profit:>8.1f}%")
    print(f"  P(capital > $200):   {p_gt200:>8.1f}%")
    print(f"  P(capital < $50):    {p_lt50:>8.1f}%  [DANGER]")
    print(f"  P(capital < $10):    {p_lt10:>8.1f}%  [RUIN]")
    print()
    print(f"  DRAWDOWN")
    print(f"  {'─' * 40}")
    print(f"  Median max drawdown:  {dd_median:>7.1f}%")
    print(f"  95th pctl drawdown:   {dd_p95:>7.1f}%")
    print()
    print(f"  SCALING")
    print(f"  {'─' * 40}")
    print(f"  Avg PnL per trade:   ${avg_pnl_per_trade:>9.3f}")
    if days_to_1000 < float('inf') and days_to_1000 > 0:
        print(f"  Est. trades to $1K:   {trades_to_1000:>8.0f}")
        print(f"  Est. days to $1K:     {days_to_1000:>8.0f}  (at {trades_per_day:.1f} trades/day)")
    else:
        print(f"  Est. days to $1K:     NEVER (negative EV)")
    print()
    print()


# ═════════════════════════════════════════════════════════════════════════════
# SIMULATION 1: Bootstrap from Real Data
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "█" * 72)
print("█  SIMULATION 1: BOOTSTRAP FROM REAL DATA (17 trades, resampled)")
print("█" * 72)

sim1 = run_bootstrap_sim(
    n_paths=N_PATHS,
    n_trades=100,
    pnls=REAL_PNLS,
    start_cap=STARTING_CAPITAL,
    fee=FEE_PER_TRADE,
)
report(sim1, "SIM 1: BOOTSTRAP FROM REAL DATA", STARTING_CAPITAL)


# ═════════════════════════════════════════════════════════════════════════════
# SIMULATION 2: Stress Test — True Edge 2pp Lower (WR = 50.9%)
# ═════════════════════════════════════════════════════════════════════════════
print("█" * 72)
print("█  SIMULATION 2: STRESS TEST — TRUE EDGE 2pp LOWER (WR=50.9%)")
print("█" * 72)

sim2 = run_parametric_sim(
    n_paths=N_PATHS,
    n_trades=100,
    win_rate=0.509,
    avg_win=avg_win,
    avg_loss=avg_loss,
    start_cap=STARTING_CAPITAL,
    fee=FEE_PER_TRADE,
)
report(sim2, "SIM 2: STRESS TEST — WR=50.9% (2pp below observed)", STARTING_CAPITAL)


# ═════════════════════════════════════════════════════════════════════════════
# SIMULATION 3: No Skill — Pure Coin Flip (WR = 50.0%)
# ═════════════════════════════════════════════════════════════════════════════
print("█" * 72)
print("█  SIMULATION 3: NO SKILL — PURE COIN FLIP (WR=50.0%)")
print("█" * 72)

sim3 = run_parametric_sim(
    n_paths=N_PATHS,
    n_trades=100,
    win_rate=0.500,
    avg_win=avg_win,
    avg_loss=avg_loss,
    start_cap=STARTING_CAPITAL,
    fee=FEE_PER_TRADE,
)
report(sim3, "SIM 3: NO SKILL (WR=50.0%, pure payout asymmetry)", STARTING_CAPITAL)


# ═════════════════════════════════════════════════════════════════════════════
# SIMULATION 4: Increased Frequency (250 trades, WR drops 2pp)
# ═════════════════════════════════════════════════════════════════════════════
print("█" * 72)
print("█  SIMULATION 4: HIGH FREQUENCY — 250 TRADES, WR=50.9%")
print("█" * 72)

sim4 = run_parametric_sim(
    n_paths=N_PATHS,
    n_trades=250,
    win_rate=0.509,
    avg_win=avg_win,
    avg_loss=avg_loss,
    start_cap=STARTING_CAPITAL,
    fee=FEE_PER_TRADE,
)
report(sim4, "SIM 4: HIGH FREQUENCY (250 trades, WR=50.9%)", STARTING_CAPITAL, trades_per_day=6.25)


# ═════════════════════════════════════════════════════════════════════════════
# SIMULATION 5: Catastrophe — Strategy is Actually Losing (WR = 45%)
# ═════════════════════════════════════════════════════════════════════════════
print("█" * 72)
print("█  SIMULATION 5: CATASTROPHE — STRATEGY IS LOSING (WR=45%)")
print("█" * 72)

sim5 = run_parametric_sim(
    n_paths=N_PATHS,
    n_trades=100,
    win_rate=0.450,
    avg_win=avg_win,
    avg_loss=avg_loss,
    start_cap=STARTING_CAPITAL,
    fee=FEE_PER_TRADE,
)
report(sim5, "SIM 5: CATASTROPHE (WR=45%, strategy is broken)", STARTING_CAPITAL)


# ═════════════════════════════════════════════════════════════════════════════
# CROSS-SIMULATION COMPARISON TABLE
# ═════════════════════════════════════════════════════════════════════════════
print("=" * 72)
print("  CROSS-SIMULATION COMPARISON")
print("=" * 72)
print()

header = f"  {'Scenario':<35} {'Median$':>8} {'P(profit)':>9} {'P(ruin)':>8} {'MedDD%':>7} {'$/trade':>8}"
print(header)
print(f"  {'─' * 75}")

sims = [
    ("1: Bootstrap (real data, 100t)", sim1),
    ("2: Edge -2pp (WR=50.9%, 100t)", sim2),
    ("3: No Skill (WR=50.0%, 100t)", sim3),
    ("4: High Freq (WR=50.9%, 250t)", sim4),
    ("5: Catastrophe (WR=45%, 100t)", sim5),
]

for label, s in sims:
    med = np.median(s.terminal_capitals)
    p_profit = np.mean(s.terminal_capitals > STARTING_CAPITAL) * 100
    p_ruin = np.mean(s.terminal_capitals < 10) * 100
    med_dd = np.median(s.max_drawdowns) * 100
    pnl_per_trade = (np.mean(s.terminal_capitals) - STARTING_CAPITAL) / s.n_trades
    print(f"  {label:<35} ${med:>7.2f} {p_profit:>8.1f}% {p_ruin:>7.1f}% {med_dd:>6.1f}% ${pnl_per_trade:>7.3f}")

print()
print()


# ═════════════════════════════════════════════════════════════════════════════
# KEY INSIGHTS
# ═════════════════════════════════════════════════════════════════════════════
print("=" * 72)
print("  KEY INSIGHTS")
print("=" * 72)
print()

# Payout asymmetry analysis
ev_at_50 = 0.50 * avg_win - 0.50 * avg_loss - FEE_PER_TRADE
ev_at_509 = 0.509 * avg_win - 0.491 * avg_loss - FEE_PER_TRADE
ev_at_529 = observed_wr * avg_win - (1 - observed_wr) * avg_loss - FEE_PER_TRADE

print(f"  Payout Asymmetry: avg_win=${avg_win:.2f} vs avg_loss=${avg_loss:.2f}")
print(f"  Win/Loss ratio: {avg_win/avg_loss:.2f}x")
print()
print(f"  Expected Value per Trade:")
print(f"    At WR=50.0%: ${ev_at_50:+.3f}  {'PROFITABLE' if ev_at_50 > 0 else 'LOSING'}")
print(f"    At WR=50.9%: ${ev_at_509:+.3f}  {'PROFITABLE' if ev_at_509 > 0 else 'LOSING'}")
print(f"    At WR=52.9%: ${ev_at_529:+.3f}  {'PROFITABLE' if ev_at_529 > 0 else 'LOSING'}")
print()

# Break-even win rate
# EV = WR * avg_win - (1-WR) * avg_loss - fee = 0
# WR * avg_win - avg_loss + WR * avg_loss = fee
# WR * (avg_win + avg_loss) = avg_loss + fee
be_wr = (avg_loss + FEE_PER_TRADE) / (avg_win + avg_loss)
print(f"  Break-even Win Rate: {be_wr:.1%}")
print(f"  Observed Win Rate:   {observed_wr:.1%}")
print(f"  Edge above break-even: {(observed_wr - be_wr)*100:+.1f}pp")
print()

# Confidence interval on true win rate (Wilson interval)
n = len(REAL_PNLS)
z = 1.96  # 95% CI
p_hat = observed_wr
denom = 1 + z**2 / n
center = (p_hat + z**2 / (2*n)) / denom
margin = z * np.sqrt((p_hat * (1 - p_hat) + z**2 / (4*n)) / n) / denom
ci_lower = center - margin
ci_upper = center + margin

print(f"  95% Wilson CI on True Win Rate: [{ci_lower:.1%}, {ci_upper:.1%}]")
print(f"  (With only 17 trades, massive uncertainty!)")
print(f"  Break-even WR ({be_wr:.1%}) is {'INSIDE' if ci_lower <= be_wr <= ci_upper else 'OUTSIDE'} the CI")
print()

# How many trades to establish edge
# For WR significance at p<0.05, need: n > (z/delta)^2 * p*(1-p)
# where delta = observed_wr - 0.5
delta = observed_wr - 0.5
if delta > 0:
    n_needed = (1.96 / delta) ** 2 * observed_wr * (1 - observed_wr)
    print(f"  Trades needed for statistical significance:")
    print(f"    To confirm WR > 50% at 95%: ~{n_needed:.0f} trades")
    print(f"    Currently: {n} trades ({n/n_needed*100:.1f}% of required)")
print()
print("=" * 72)
