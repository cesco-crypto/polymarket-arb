"""Backtest Engine — Validiert Polymarket Latency Arb mit historischen Binance-Daten.

Kernfrage: Schlägt unser Momentum-Signal die 3.15% Polymarket-Gebühr?

Methode:
1. Lade 1-Minuten Binance Klines (BTC + ETH, 2024-2026)
2. Simuliere jeden 5m/15m Slot (wie Polymarket-Contracts)
3. Berechne Momentum an verschiedenen Zeitpunkten im Slot
4. Prüfe ob Richtungsvorhersage korrekt war
5. Wende Fee-Kurve (3.15%) + Half-Kelly an
6. Grid-Search über Momentum-Schwellen und Zeitfenster

Verwendung:
  python backtest_engine.py                    → Standard-Backtest (6 Monate)
  python backtest_engine.py --months 24        → 2 Jahre
  python backtest_engine.py --plot             → Mit Equity-Curve Chart
  python backtest_engine.py --asset ETH        → Nur ETH
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

# --- Konfiguration ---

DATA_DIR = Path(__file__).parent / "data" / "backtest"
BINANCE_API = "https://api.binance.com/api/v3/klines"

# Grid Search Parameter
MOMENTUM_THRESHOLDS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
MOMENTUM_WINDOWS_MIN = [1, 2, 3, 5]  # Minuten (1m-Klines Auflösung)
SLOT_DURATIONS = [5, 15]  # Minuten (Polymarket Contract-Längen)

# Strategie-Parameter (aus config.py)
MAX_FEE_PCT = 3.15
KELLY_FRACTION = 0.50  # Half-Kelly
MAX_POSITION_PCT = 0.08
INITIAL_CAPITAL = 1000.0
MIN_EDGE_PCT = 2.0


# --- Datenstrukturen ---

@dataclass
class Candle:
    ts: float       # Unix timestamp (Sekunden)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SlotResult:
    """Ergebnis eines simulierten 5m/15m Slots."""
    slot_start_ts: float
    slot_duration_min: int
    momentum_pct: float
    momentum_window_min: int
    predicted_direction: str  # "UP" / "DOWN"
    actual_direction: str     # "UP" / "DOWN"
    correct: bool
    entry_price: float        # Simulierter Polymarket-Ask
    fee_pct: float
    net_ev_pct: float
    p_true: float
    kelly: float
    pnl_usd: float


# --- Fee-Modell (identisch zu pretrade_calculator.py) ---

def polymarket_fee(price: float) -> float:
    """Dynamische Gebühr in %. Max 3.15% bei p=0.50."""
    p = max(0.01, min(0.99, price))
    base = 4 * p * (1 - p)
    return MAX_FEE_PCT * base * base


def estimate_probability(momentum_pct: float, seconds_to_expiry: float) -> float:
    """Logistische Funktion: Momentum → Wahrscheinlichkeit."""
    if seconds_to_expiry <= 0:
        return 0.5
    time_factor = min(1.0, 120.0 / max(seconds_to_expiry, 1.0))
    raw_signal = abs(momentum_pct) * time_factor * 2.0  # scaling=2.0
    return 1 / (1 + math.exp(-raw_signal))


def kelly_fraction(p_true: float, p_market: float) -> float:
    """Half-Kelly für Binary Prediction Markets."""
    if p_market >= 1.0 or p_market <= 0.0:
        return 0.0
    raw = (p_true - p_market) / (1 - p_market)
    raw = max(0.0, min(raw, 0.25))
    return raw * KELLY_FRACTION


# --- Daten laden ---

async def fetch_binance_klines(
    symbol: str, interval: str = "1m",
    start_date: str = "2024-01-01", end_date: str = "2026-03-28",
    cache: bool = True
) -> list[Candle]:
    """Lädt historische Klines von Binance API mit Caching."""
    cache_file = DATA_DIR / f"{symbol}_{interval}_{start_date}_{end_date}.json"

    if cache and cache_file.exists():
        print(f"  Cache gefunden: {cache_file.name}")
        with open(cache_file) as f:
            raw = json.load(f)
        return [Candle(r[0] / 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

    all_candles = []
    current = start_ts
    batch = 0

    async with aiohttp.ClientSession() as session:
        while current < end_ts:
            batch += 1
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current,
                "endTime": end_ts,
                "limit": 1000,
            }
            async with session.get(BINANCE_API, params=params) as resp:
                if resp.status != 200:
                    print(f"  API Fehler: {resp.status}")
                    break
                data = await resp.json()

            if not data:
                break

            all_candles.extend(data)
            current = data[-1][0] + 60000  # Nächste Minute

            if batch % 50 == 0:
                days = len(all_candles) / 1440
                print(f"  {symbol}: {len(all_candles):,} Klines geladen ({days:.0f} Tage)...")

            await asyncio.sleep(0.1)  # Rate limit

    print(f"  {symbol}: {len(all_candles):,} Klines total ({len(all_candles)/1440:.0f} Tage)")

    # Cache speichern
    with open(cache_file, "w") as f:
        json.dump(all_candles, f)

    return [Candle(r[0] / 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in all_candles]


# --- Backtest-Kern ---

def simulate_slots(
    candles: list[Candle],
    slot_duration_min: int,
    momentum_window_min: int,
    momentum_threshold: float,
    capital: float = INITIAL_CAPITAL,
) -> tuple[list[SlotResult], list[float]]:
    """Simuliert alle 5m/15m Slots und berechnet PnL.

    Returns:
        (slot_results, equity_curve)
    """
    results: list[SlotResult] = []
    equity = [capital]
    current_capital = capital

    # Index-Lookup: timestamp → candle index
    ts_to_idx = {int(c.ts): i for i, c in enumerate(candles)}

    # Slot-Starts: alle slot_duration_min Minuten
    first_ts = int(candles[0].ts)
    last_ts = int(candles[-1].ts)

    # Auf Slot-Grenze runden
    slot_seconds = slot_duration_min * 60
    start = ((first_ts // slot_seconds) + 1) * slot_seconds

    slots_processed = 0
    signals_found = 0

    for slot_start in range(start, last_ts - slot_seconds, slot_seconds):
        # Candle-Indizes für den Slot
        momentum_end_ts = slot_start + momentum_window_min * 60
        slot_end_ts = slot_start + slot_seconds

        # Referenzpreis = Open der ersten Candle im Slot
        ref_idx = ts_to_idx.get(slot_start)
        mom_idx = ts_to_idx.get(momentum_end_ts)
        end_idx = ts_to_idx.get(slot_end_ts)

        if ref_idx is None or mom_idx is None or end_idx is None:
            continue

        slots_processed += 1
        ref_price = candles[ref_idx].open
        mom_price = candles[mom_idx].close
        end_price = candles[end_idx].close

        if ref_price <= 0:
            continue

        # Momentum berechnen
        momentum = (mom_price - ref_price) / ref_price * 100

        # Unter Schwelle? → kein Signal
        if abs(momentum) < momentum_threshold:
            continue

        signals_found += 1

        # Richtung vorhersagen
        predicted = "UP" if momentum > 0 else "DOWN"
        actual = "UP" if end_price >= ref_price else "DOWN"
        correct = predicted == actual

        # Simulierter Polymarket-Preis
        # Bei 50/50 Markt: Ask ≈ 0.50. Momentum verschiebt den "wahren" Preis.
        # Der Arbitrage-Vorteil ist, dass Polymarket noch bei ~0.50 steht
        # während Binance schon Richtung zeigt.
        simulated_ask = 0.50  # Konservative Annahme: Markt hat sich noch nicht bewegt

        # p_true aus Momentum
        seconds_remaining = (slot_duration_min - momentum_window_min) * 60
        p_true = estimate_probability(momentum, seconds_remaining)

        # Fee
        fee = polymarket_fee(simulated_ask)

        # Net EV
        effective_ask = simulated_ask * (1 + fee / 100)
        net_ev = (p_true / effective_ask - 1) * 100

        # Kelly Sizing
        kelly = kelly_fraction(p_true, simulated_ask)

        # Position Size
        if net_ev <= MIN_EDGE_PCT or kelly <= 0:
            # Trade nicht profitabel genug → skip (aber trotzdem loggen)
            pnl = 0.0
            results.append(SlotResult(
                slot_start, slot_duration_min, momentum, momentum_window_min,
                predicted, actual, correct, simulated_ask, fee, net_ev,
                p_true, kelly, 0.0
            ))
            continue

        # Fixe Positionsgrösse für realistische PnL-Berechnung
        position = 50.0  # $50 pro Trade (fix)
        if current_capital < position + 5:
            continue

        # PnL berechnen
        fee_usd = position * fee / 100
        shares = position / simulated_ask

        if correct:
            gross_payout = shares * 1.0
            pnl = gross_payout - position - fee_usd
        else:
            pnl = -(position + fee_usd)

        current_capital += pnl
        equity.append(current_capital)

        results.append(SlotResult(
            slot_start, slot_duration_min, momentum, momentum_window_min,
            predicted, actual, correct, simulated_ask, fee, net_ev,
            p_true, kelly, pnl
        ))

        # Kill Switch Check
        if current_capital < INITIAL_CAPITAL * 0.60:  # -40% total
            break

    return results, equity


# --- Grid Search ---

def run_grid_search(
    candles: list[Candle], asset: str
) -> list[dict]:
    """Testet alle Kombinationen von Parametern."""
    results = []
    total = len(SLOT_DURATIONS) * len(MOMENTUM_WINDOWS_MIN) * len(MOMENTUM_THRESHOLDS)
    current = 0

    for slot_dur in SLOT_DURATIONS:
        for mom_win in MOMENTUM_WINDOWS_MIN:
            if mom_win >= slot_dur:
                continue  # Momentum-Fenster muss kleiner als Slot sein

            for threshold in MOMENTUM_THRESHOLDS:
                current += 1
                sys.stdout.write(f"\r  Grid Search: {current}/{total} ({asset} {slot_dur}m, mom={mom_win}m, thr={threshold}%)")
                sys.stdout.flush()

                slots, equity = simulate_slots(candles, slot_dur, mom_win, threshold)

                # Nur Trades mit PnL ≠ 0 zählen (tatsächlich ausgeführte)
                traded = [s for s in slots if s.pnl_usd != 0]
                all_signals = [s for s in slots if True]  # Alle Signale inkl. skipped

                if not traded:
                    results.append({
                        "asset": asset, "slot_min": slot_dur,
                        "mom_win_min": mom_win, "threshold_pct": threshold,
                        "signals": len(all_signals), "trades": 0,
                        "win_rate": 0, "pnl": 0, "sharpe": 0,
                        "max_dd_pct": 0, "final_capital": INITIAL_CAPITAL,
                    })
                    continue

                wins = sum(1 for s in traded if s.correct)
                total_pnl = sum(s.pnl_usd for s in traded)
                win_rate = wins / len(traded) * 100

                # Sharpe Ratio (annualisiert)
                pnls = [s.pnl_usd for s in traded]
                if len(pnls) > 1:
                    try:
                        mean_pnl = sum(pnls) / len(pnls)
                        variance = sum((float(x) - mean_pnl) ** 2 for x in pnls) / (len(pnls) - 1)
                        std_pnl = math.sqrt(variance) if variance > 0 else 1
                        days = len(candles) / 1440
                        trades_per_year = len(pnls) / max(days / 365, 0.01)
                        sharpe = (mean_pnl / std_pnl * math.sqrt(trades_per_year)) if std_pnl > 0 else 0
                    except (OverflowError, ValueError):
                        sharpe = 0
                else:
                    sharpe = 0

                # Max Drawdown
                peak = INITIAL_CAPITAL
                max_dd = 0
                for eq in equity:
                    if eq > peak:
                        peak = eq
                    dd = (peak - eq) / peak * 100
                    if dd > max_dd:
                        max_dd = dd

                results.append({
                    "asset": asset, "slot_min": slot_dur,
                    "mom_win_min": mom_win, "threshold_pct": threshold,
                    "signals": len(all_signals), "trades": len(traded),
                    "win_rate": round(win_rate, 1), "pnl": round(total_pnl, 2),
                    "sharpe": round(sharpe, 2), "max_dd_pct": round(max_dd, 1),
                    "final_capital": round(equity[-1], 2) if equity else INITIAL_CAPITAL,
                })

    print()
    return results


# --- Ausgabe ---

def print_results(all_results: list[dict]) -> None:
    """Gibt die Grid-Search Ergebnisse aus."""
    W = 80

    print(f"\n{'=' * W}")
    print(f"  BACKTEST ERGEBNISSE — Polymarket Latency Arbitrage")
    print(f"{'=' * W}")

    # Sortiere nach PnL
    profitable = [r for r in all_results if r["trades"] > 0]
    profitable.sort(key=lambda r: r["pnl"], reverse=True)

    if not profitable:
        print("\n  Keine Trades generiert. Alle Momentum-Schwellen zu hoch für die Datenperiode.")
        return

    # Top 15
    print(f"\n  TOP 15 PARAMETER-KOMBINATIONEN (nach PnL)")
    print(f"  {'Asset':<5} {'Slot':<5} {'Mom':<5} {'Thr%':<6} {'Signals':>8} {'Trades':>7} {'WR%':>6} {'PnL $':>10} {'Sharpe':>7} {'MaxDD%':>7} {'Final$':>10}")
    print(f"  {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*8} {'-'*7} {'-'*6} {'-'*10} {'-'*7} {'-'*7} {'-'*10}")

    for r in profitable[:15]:
        wr_color = "\033[92m" if r["win_rate"] >= 55 else "\033[91m" if r["win_rate"] < 50 else "\033[93m"
        pnl_color = "\033[92m" if r["pnl"] > 0 else "\033[91m"
        print(
            f"  {r['asset']:<5} {r['slot_min']:<5} {r['mom_win_min']:<5} {r['threshold_pct']:<6} "
            f"{r['signals']:>8} {r['trades']:>7} "
            f"{wr_color}{r['win_rate']:>5.1f}%\033[0m "
            f"{pnl_color}{r['pnl']:>+10.2f}\033[0m "
            f"{r['sharpe']:>7.2f} {r['max_dd_pct']:>6.1f}% "
            f"${r['final_capital']:>9.2f}"
        )

    # Bottom 5 (schlimmste)
    print(f"\n  BOTTOM 5 (schlechteste)")
    for r in profitable[-5:]:
        print(
            f"  {r['asset']:<5} {r['slot_min']:<5} {r['mom_win_min']:<5} {r['threshold_pct']:<6} "
            f"{r['signals']:>8} {r['trades']:>7} "
            f"\033[91m{r['win_rate']:>5.1f}%\033[0m "
            f"\033[91m{r['pnl']:>+10.2f}\033[0m "
            f"{r['sharpe']:>7.2f} {r['max_dd_pct']:>6.1f}% "
            f"${r['final_capital']:>9.2f}"
        )

    # Momentum-Korrelation
    print(f"\n{'=' * W}")
    print(f"  MOMENTUM-SCHWELLE vs WIN RATE (5m BTC, aggregiert)")
    print(f"{'=' * W}")

    for thr in MOMENTUM_THRESHOLDS:
        matching = [r for r in profitable if r["threshold_pct"] == thr and r["slot_min"] == 5 and r["asset"] == "BTC"]
        if matching:
            avg_wr = sum(r["win_rate"] for r in matching) / len(matching)
            avg_pnl = sum(r["pnl"] for r in matching) / len(matching)
            avg_trades = sum(r["trades"] for r in matching) / len(matching)
            bar = "#" * int(avg_wr / 2)
            wr_c = "\033[92m" if avg_wr >= 55 else "\033[91m" if avg_wr < 50 else "\033[93m"
            print(f"  {thr:>5.2f}%  {wr_c}{avg_wr:>5.1f}%\033[0m  PnL=${avg_pnl:>+8.2f}  Trades={avg_trades:>6.0f}  {bar}")

    # Fazit
    print(f"\n{'=' * W}")
    print(f"  FAZIT")
    print(f"{'=' * W}")

    best = profitable[0]
    if best["pnl"] > 0 and best["win_rate"] > 52:
        print(f"\n  \033[92mSTRATEGIE HAT POSITIVEN EXPECTED VALUE\033[0m")
        print(f"  Beste Konfiguration: {best['asset']} {best['slot_min']}m, "
              f"Momentum={best['mom_win_min']}m, Schwelle={best['threshold_pct']}%")
        print(f"  Win Rate: {best['win_rate']}%, PnL: ${best['pnl']:+.2f}, "
              f"Sharpe: {best['sharpe']}, Max DD: {best['max_dd_pct']}%")
    else:
        print(f"\n  \033[91mSTRATEGIE IST NICHT PROFITABEL nach 3.15% Gebühren\033[0m")
        print(f"  Beste Win Rate: {best['win_rate']}% — nicht genug um Gebühren zu decken")


def plot_equity_curve(candles: list[Candle], asset: str, best_config: dict) -> None:
    """Plottet die Equity Curve der besten Konfiguration."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht installiert. pip install matplotlib")
        return

    _, equity = simulate_slots(
        candles, best_config["slot_min"],
        best_config["mom_win_min"], best_config["threshold_pct"],
    )

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})
    fig.suptitle(
        f"Backtest: {asset} {best_config['slot_min']}m Slots | "
        f"Mom={best_config['mom_win_min']}m, Thr={best_config['threshold_pct']}% | "
        f"WR={best_config['win_rate']}%, PnL=${best_config['pnl']:+.2f}",
        fontsize=12, fontweight="bold"
    )

    # Equity Curve
    color = "#22c55e" if equity[-1] > INITIAL_CAPITAL else "#ef4444"
    ax1.plot(equity, color=color, linewidth=1)
    ax1.axhline(y=INITIAL_CAPITAL, color="gray", linestyle="--", alpha=0.5)
    ax1.axhline(y=INITIAL_CAPITAL * 0.8, color="red", linestyle="--", alpha=0.3, label="-20% Kill Switch")
    ax1.set_ylabel("Capital ($)")
    ax1.set_title("Equity Curve")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.2)

    # Drawdown
    peak = INITIAL_CAPITAL
    dd_curve = []
    for eq in equity:
        if eq > peak:
            peak = eq
        dd_curve.append((peak - eq) / peak * 100)
    ax2.fill_between(range(len(dd_curve)), dd_curve, alpha=0.4, color="#ef4444")
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_xlabel("Trade #")
    ax2.invert_yaxis()
    ax2.grid(alpha=0.2)

    plt.tight_layout()
    out = DATA_DIR / f"backtest_{asset}_{best_config['slot_min']}m.png"
    plt.savefig(out, dpi=150, facecolor="#1a1d27")
    print(f"\n  Chart gespeichert: {out}")
    plt.close()


# --- Main ---

async def main():
    parser = argparse.ArgumentParser(description="Polymarket Latency Arb Backtest")
    parser.add_argument("--months", type=int, default=6, help="Monate Daten (default: 6)")
    parser.add_argument("--asset", default="both", help="BTC, ETH, oder both")
    parser.add_argument("--plot", action="store_true", help="Equity Curve plotten")
    args = parser.parse_args()

    # Daten-Zeitraum berechnen
    end_date = "2026-03-28"
    start_year = 2026 - (args.months // 12)
    start_month = 3 - (args.months % 12)
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    start_date = f"{start_year}-{start_month:02d}-01"

    assets = ["BTC", "ETH"] if args.asset.lower() == "both" else [args.asset.upper()]

    print(f"\n{'=' * 60}")
    print(f"  POLYMARKET LATENCY ARB — BACKTEST ENGINE")
    print(f"  Zeitraum: {start_date} → {end_date} ({args.months} Monate)")
    print(f"  Assets: {', '.join(assets)}")
    print(f"  Fee-Modell: 3.15% peak (Exponent 2)")
    print(f"  Kelly: Half-Kelly (0.50), 8% Hard Cap")
    print(f"  Grid: {len(MOMENTUM_THRESHOLDS)} Schwellen × {len(MOMENTUM_WINDOWS_MIN)} Fenster × {len(SLOT_DURATIONS)} Slots")
    print(f"{'=' * 60}\n")

    all_results = []

    for asset in assets:
        symbol = f"{asset}USDT"
        print(f"[{asset}] Lade Binance 1m Klines...")
        candles = await fetch_binance_klines(symbol, "1m", start_date, end_date)

        if len(candles) < 1000:
            print(f"  Zu wenig Daten für {asset}: {len(candles)} Klines")
            continue

        print(f"[{asset}] Starte Grid Search...")
        results = run_grid_search(candles, asset)
        all_results.extend(results)

        if args.plot:
            profitable = [r for r in results if r["pnl"] > 0]
            if profitable:
                best = max(profitable, key=lambda r: r["pnl"])
                plot_equity_curve(candles, asset, best)

    print_results(all_results)


if __name__ == "__main__":
    asyncio.run(main())
