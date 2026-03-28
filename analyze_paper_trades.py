"""Paper Trade Analyse — Validiert Polymarket Latency Arb Profitabilität.

Liest paper_trades.csv + bot.log aus und berechnet:
1. Win Rate & EV vs. PreTradeCalculator-Erwartung
2. Netto-PnL nach 3.15% dynamischer Taker-Gebühr
3. Momentum-Korrelation (höheres Momentum → höhere Win Rate?)
4. Drawdown & Kill-Switch Tracking

Verwendung:
  python analyze_paper_trades.py              → Standardpfad data/paper_trades.csv
  python analyze_paper_trades.py --csv path   → Benutzerdefinierter Pfad
  python analyze_paper_trades.py --plot       → Matplotlib-Charts generieren
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClosedTrade:
    """Ein abgeschlossener Paper Trade."""

    trade_id: str
    timestamp: str
    asset: str
    direction: str
    question: str
    entry_price: float
    size_usd: float
    fee_usd: float
    shares: float
    p_true: float
    momentum_pct: float
    seconds_to_expiry: float
    exit_price: float
    pnl_usd: float
    outcome_correct: bool


def load_trades(csv_path: Path) -> tuple[list[ClosedTrade], int]:
    """Lädt Trades aus CSV. Gibt (closed_trades, open_count) zurück."""
    if not csv_path.exists():
        print(f"Fehler: {csv_path} nicht gefunden.")
        print("Starte zuerst Paper Trading: python main.py --polymarket")
        sys.exit(1)

    closed: list[ClosedTrade] = []
    open_count = 0

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            event = row.get("event", "")
            if event == "open":
                open_count += 1
            elif event == "close":
                open_count -= 1  # war vorher als open gezählt
                try:
                    closed.append(ClosedTrade(
                        trade_id=row["trade_id"],
                        timestamp=row["timestamp"],
                        asset=row["asset"],
                        direction=row["direction"],
                        question=row.get("question", "")[:60],
                        entry_price=float(row["entry_price"]),
                        size_usd=float(row["size_usd"]),
                        fee_usd=float(row["fee_usd"]),
                        shares=float(row["shares"]),
                        p_true=float(row["p_true"]),
                        momentum_pct=float(row["momentum_pct"]),
                        seconds_to_expiry=float(row["seconds_to_expiry"]),
                        exit_price=float(row["exit_price"]),
                        pnl_usd=float(row["pnl_usd"]),
                        outcome_correct=row["outcome_correct"].strip().lower() == "true",
                    ))
                except (KeyError, ValueError) as e:
                    print(f"  Warnung: Trade-Zeile übersprungen ({e})")

    return closed, max(open_count, 0)


def analyze(trades: list[ClosedTrade], open_count: int) -> dict:
    """Berechnet alle Analyse-Metriken."""
    if not trades:
        return {}

    n = len(trades)
    wins = [t for t in trades if t.outcome_correct]
    losses = [t for t in trades if not t.outcome_correct]
    win_rate = len(wins) / n * 100

    # --- PnL ---
    total_pnl = sum(t.pnl_usd for t in trades)
    total_fees = sum(t.fee_usd for t in trades)
    total_volume = sum(t.size_usd for t in trades)
    avg_pnl = total_pnl / n
    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0

    # Profit Factor
    gross_profit = sum(t.pnl_usd for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_usd for t in losses)) if losses else 0.01
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # --- EV Vergleich ---
    avg_p_true = sum(t.p_true for t in trades) / n
    avg_entry = sum(t.entry_price for t in trades) / n
    expected_wr = avg_p_true * 100  # Durchschnittliche geschätzte Gewinnwahrscheinlichkeit

    # --- Momentum-Korrelation ---
    momentum_bins = {
        "0.15-0.25%": {"trades": [], "label": "0.15-0.25%"},
        "0.25-0.50%": {"trades": [], "label": "0.25-0.50%"},
        "0.50-1.00%": {"trades": [], "label": "0.50-1.00%"},
        ">1.00%": {"trades": [], "label": ">1.00%"},
    }
    for t in trades:
        m = abs(t.momentum_pct)
        if m < 0.25:
            momentum_bins["0.15-0.25%"]["trades"].append(t)
        elif m < 0.50:
            momentum_bins["0.25-0.50%"]["trades"].append(t)
        elif m < 1.00:
            momentum_bins["0.50-1.00%"]["trades"].append(t)
        else:
            momentum_bins[">1.00%"]["trades"].append(t)

    momentum_analysis = []
    for key, data in momentum_bins.items():
        bin_trades = data["trades"]
        if not bin_trades:
            momentum_analysis.append({"range": key, "n": 0, "win_rate": 0, "avg_pnl": 0})
            continue
        bin_wins = sum(1 for t in bin_trades if t.outcome_correct)
        bin_pnl = sum(t.pnl_usd for t in bin_trades)
        momentum_analysis.append({
            "range": key,
            "n": len(bin_trades),
            "win_rate": bin_wins / len(bin_trades) * 100,
            "avg_pnl": bin_pnl / len(bin_trades),
        })

    # --- Asset-Breakdown ---
    assets = {}
    for t in trades:
        if t.asset not in assets:
            assets[t.asset] = {"wins": 0, "total": 0, "pnl": 0.0}
        assets[t.asset]["total"] += 1
        assets[t.asset]["pnl"] += t.pnl_usd
        if t.outcome_correct:
            assets[t.asset]["wins"] += 1

    # --- Timeframe Breakdown (5m vs 15m) ---
    tf_data = {"5m": {"wins": 0, "total": 0, "pnl": 0.0}, "15m": {"wins": 0, "total": 0, "pnl": 0.0}}
    for t in trades:
        tf = "5m" if t.seconds_to_expiry <= 300 else "15m"
        tf_data[tf]["total"] += 1
        tf_data[tf]["pnl"] += t.pnl_usd
        if t.outcome_correct:
            tf_data[tf]["wins"] += 1

    # --- Drawdown ---
    equity_curve = []
    running_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_pct = 0.0
    initial_capital = 1000.0  # aus config

    for t in trades:
        running_pnl += t.pnl_usd
        equity = initial_capital + running_pnl
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = peak - equity
        dd_pct = dd / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

    kill_switch_proximity = max_dd_pct / 20 * 100  # 20% = Kill-Switch-Level

    return {
        "n": n,
        "open_count": open_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "expected_wr": expected_wr,
        "total_pnl": total_pnl,
        "total_fees": total_fees,
        "total_volume": total_volume,
        "avg_pnl": avg_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "avg_p_true": avg_p_true,
        "avg_entry": avg_entry,
        "momentum_analysis": momentum_analysis,
        "assets": assets,
        "timeframes": tf_data,
        "max_drawdown_usd": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "kill_switch_proximity": kill_switch_proximity,
        "equity_curve": equity_curve,
        "final_capital": equity_curve[-1] if equity_curve else initial_capital,
    }


def print_report(stats: dict) -> None:
    """Gibt den Analysebericht im Terminal aus."""
    W = 60

    def header(title: str) -> str:
        return f"\n{'=' * W}\n  {title}\n{'=' * W}"

    def bar(pct: float, width: int = 30) -> str:
        filled = int(pct / 100 * width)
        return f"[{'#' * filled}{'.' * (width - filled)}]"

    print(header("POLYMARKET PAPER TRADING — ANALYSE"))
    print(f"  Abgeschlossene Trades:  {stats['n']}")
    print(f"  Offene Positionen:      {stats['open_count']}")
    print(f"  Ziel für Live-Trading:  200 Trades mit >70% Win Rate")

    progress = min(stats["n"] / 200 * 100, 100)
    print(f"  Fortschritt:            {bar(progress)} {stats['n']}/200 ({progress:.0f}%)")

    # --- Win Rate & EV ---
    print(header("WIN RATE & EXPECTED VALUE"))
    wr = stats["win_rate"]
    ewr = stats["expected_wr"]
    wr_color = "\033[92m" if wr >= 70 else "\033[93m" if wr >= 55 else "\033[91m"
    print(f"  Tatsächliche Win Rate:  {wr_color}{wr:.1f}%\033[0m  ({stats['wins']}W / {stats['losses']}L)")
    print(f"  Erwartete Win Rate:     {ewr:.1f}%  (Durchschnitt p_true)")
    diff = wr - ewr
    print(f"  Differenz:              {diff:+.1f}pp  {'(Modell unterschätzt)' if diff > 0 else '(Modell überschätzt)' if diff < -5 else '(im Rahmen)'}")
    print(f"  Avg Entry Price:        {stats['avg_entry']:.3f}")
    print(f"  Avg p_true:             {stats['avg_p_true']:.3f}")

    # --- PnL ---
    print(header("NETTO-PROFITABILITÄT (nach 3.15% Gebühren)"))
    pnl = stats["total_pnl"]
    pnl_color = "\033[92m" if pnl > 0 else "\033[91m"
    print(f"  Total PnL:              {pnl_color}${pnl:+.2f}\033[0m")
    print(f"  Total Gebühren:         ${stats['total_fees']:.2f}")
    print(f"  Total Volumen:          ${stats['total_volume']:.2f}")
    print(f"  Avg PnL/Trade:          ${stats['avg_pnl']:+.4f}")
    print(f"  Avg Win:                ${stats['avg_win']:+.4f}")
    print(f"  Avg Loss:               ${stats['avg_loss']:+.4f}")
    print(f"  Profit Factor:          {stats['profit_factor']:.2f}x")
    ret = (stats["final_capital"] / 1000 - 1) * 100
    print(f"  Return on Capital:      {ret:+.2f}%")
    print(f"  Endkapital:             ${stats['final_capital']:.2f}")

    # --- Momentum-Korrelation ---
    print(header("MOMENTUM-KORRELATION"))
    print(f"  {'Momentum':<14} {'Trades':>7} {'Win Rate':>10} {'Avg PnL':>10}")
    print(f"  {'-'*14} {'-'*7} {'-'*10} {'-'*10}")
    for m in stats["momentum_analysis"]:
        if m["n"] == 0:
            print(f"  {m['range']:<14} {'—':>7}")
            continue
        wr_c = "\033[92m" if m["win_rate"] >= 70 else "\033[93m" if m["win_rate"] >= 55 else "\033[91m"
        print(f"  {m['range']:<14} {m['n']:>7} {wr_c}{m['win_rate']:>9.1f}%\033[0m {m['avg_pnl']:>+10.4f}")

    # --- Asset Breakdown ---
    print(header("ASSET-BREAKDOWN"))
    for asset, data in stats["assets"].items():
        wr_a = data["wins"] / data["total"] * 100 if data["total"] > 0 else 0
        print(f"  {asset:6s}  {data['total']:3d} trades  WR={wr_a:.1f}%  PnL=${data['pnl']:+.2f}")

    # --- Timeframe Breakdown ---
    print(header("TIMEFRAME-BREAKDOWN"))
    for tf, data in stats["timeframes"].items():
        if data["total"] == 0:
            print(f"  {tf:6s}  — keine Trades")
            continue
        wr_t = data["wins"] / data["total"] * 100
        print(f"  {tf:6s}  {data['total']:3d} trades  WR={wr_t:.1f}%  PnL=${data['pnl']:+.2f}")

    # --- Drawdown ---
    print(header("DRAWDOWN & KILL-SWITCH"))
    print(f"  Max Drawdown:           ${stats['max_drawdown_usd']:.2f}  ({stats['max_drawdown_pct']:.2f}%)")
    print(f"  Kill-Switch Level:      -20.00%  ($200.00)")
    prox = stats["kill_switch_proximity"]
    prox_color = "\033[91m" if prox > 50 else "\033[93m" if prox > 25 else "\033[92m"
    print(f"  Proximity:              {prox_color}{prox:.1f}%\033[0m  {'GEFAHR' if prox > 75 else 'OK' if prox < 25 else 'Beobachten'}")
    print(f"  Kill-Switch ausgelöst:  {'JA' if prox >= 100 else 'Nein'}")

    # --- Fazit ---
    print(header("FAZIT"))
    ready = stats["n"] >= 200 and stats["win_rate"] >= 70 and stats["total_pnl"] > 0
    if stats["n"] < 10:
        print("  Zu wenige Trades für eine Bewertung. Weiter laufen lassen.")
    elif ready:
        print("  \033[92mSTRATEGIE VALIDIERT — Bereit für Live-Trading-Evaluation\033[0m")
    elif stats["win_rate"] >= 70 and stats["total_pnl"] > 0:
        print(f"  Profitabel, aber noch {200 - stats['n']} Trades bis zur Validierung.")
    elif stats["win_rate"] >= 55:
        print("  Win Rate akzeptabel, aber unter dem 70%-Ziel. Parameter prüfen.")
    else:
        print("  \033[91mWin Rate zu niedrig. Strategie-Parameter überarbeiten.\033[0m")
    print()


def plot_equity_curve(stats: dict, output_path: Path | None = None) -> None:
    """Generiert Equity-Curve und Momentum-Korrelation Charts."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib nicht installiert. Überspringe Charts.")
        print("  pip install matplotlib")
        return

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Polymarket Paper Trading Analysis", fontsize=14, fontweight="bold")

    # 1. Equity Curve
    ax = axes[0, 0]
    curve = stats["equity_curve"]
    ax.plot(range(len(curve)), curve, color="#3b82f6", linewidth=1.5)
    ax.axhline(y=1000, color="gray", linestyle="--", alpha=0.5, label="Start $1000")
    ax.axhline(y=800, color="red", linestyle="--", alpha=0.5, label="Kill-Switch $800")
    ax.set_title("Equity Curve")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Capital ($)")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)

    # 2. Win Rate per Momentum Bin
    ax = axes[0, 1]
    bins = stats["momentum_analysis"]
    labels = [b["range"] for b in bins if b["n"] > 0]
    rates = [b["win_rate"] for b in bins if b["n"] > 0]
    counts = [b["n"] for b in bins if b["n"] > 0]
    if labels:
        colors = ["#22c55e" if r >= 70 else "#eab308" if r >= 55 else "#ef4444" for r in rates]
        bars = ax.bar(labels, rates, color=colors)
        ax.axhline(y=70, color="green", linestyle="--", alpha=0.5, label="70% Ziel")
        ax.axhline(y=50, color="red", linestyle="--", alpha=0.5, label="50% Break-even")
        for bar, count in zip(bars, counts):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                    f"n={count}", ha="center", fontsize=8)
        ax.set_title("Win Rate vs. Momentum")
        ax.set_ylabel("Win Rate (%)")
        ax.set_ylim(0, 100)
        ax.legend(fontsize=8)

    # 3. PnL Distribution
    ax = axes[1, 0]
    pnls = [t.pnl_usd for t in _last_trades] if "_last_trades" in dir() else stats.get("_pnls", [])
    if not pnls:
        pnls = stats["equity_curve"]
        pnls = [pnls[i] - pnls[i - 1] if i > 0 else pnls[0] - 1000 for i in range(len(pnls))]
    if pnls:
        colors_pnl = ["#22c55e" if p > 0 else "#ef4444" for p in pnls]
        ax.bar(range(len(pnls)), pnls, color=colors_pnl, width=1.0)
        ax.set_title("PnL per Trade")
        ax.set_xlabel("Trade #")
        ax.set_ylabel("PnL ($)")
        ax.axhline(y=0, color="white", linewidth=0.5)
        ax.grid(alpha=0.3)

    # 4. Cumulative PnL
    ax = axes[1, 1]
    cum_pnl = [e - 1000 for e in stats["equity_curve"]]
    color_cum = "#22c55e" if cum_pnl and cum_pnl[-1] > 0 else "#ef4444"
    ax.fill_between(range(len(cum_pnl)), cum_pnl, alpha=0.3, color=color_cum)
    ax.plot(range(len(cum_pnl)), cum_pnl, color=color_cum, linewidth=1.5)
    ax.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
    ax.set_title("Cumulative PnL")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("PnL ($)")
    ax.grid(alpha=0.3)

    plt.tight_layout()
    save_path = output_path or Path("data/paper_analysis.png")
    plt.savefig(save_path, dpi=150, bbox_inches="tight", facecolor="#1a1d27")
    print(f"\n  Chart gespeichert: {save_path}")
    plt.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trade Analyse")
    parser.add_argument("--csv", default="data/paper_trades.csv", help="Pfad zur CSV")
    parser.add_argument("--plot", action="store_true", help="Matplotlib-Charts generieren")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    trades, open_count = load_trades(csv_path)

    if not trades:
        print(f"\nKeine abgeschlossenen Trades in {csv_path}.")
        print(f"Offene Positionen: {open_count}")
        print("\nBot läuft? Prüfe mit: tail -f data/bot.log | grep SIGNAL")
        print("Warte bis Trades abgeschlossen werden (5-15 Min pro Trade).")
        return

    stats = analyze(trades, open_count)
    print_report(stats)

    if args.plot:
        plot_equity_curve(stats)


if __name__ == "__main__":
    main()
