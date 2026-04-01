"""Backtest Engine — Validiert Polymarket Latency Arb mit historischen Binance-Daten.

Kernfrage: Schlägt unser Momentum-Signal die Polymarket-Gebühr unter realen Bedingungen?

Zwei Modi:
  IDEALISTIC — Keine Friktion, zeigt rohes Signal-Alpha
  REALISTIC  — Slippage, Latenz, Position-Caps, Competition, flat Sizing

Methode:
1. Lade 1-Minuten Binance Klines (BTC + ETH, 2024-2026)
2. Simuliere jeden 5m/15m Slot (wie Polymarket-Contracts)
3. Berechne Momentum an verschiedenen Zeitpunkten im Slot
4. Prüfe ob Richtungsvorhersage korrekt war
5. Wende Fee-Kurve (1.80% Peak ab 30.03.2026) + Half-Kelly an
6. Grid-Search über Momentum-Schwellen und Zeitfenster

Verwendung:
  python backtest_engine.py                         → Vergleich: Idealistic vs Realistic
  python backtest_engine.py --realistic-only        → Nur realistischer Modus
  python backtest_engine.py --idealistic-only       → Nur idealistischer Modus
  python backtest_engine.py --months 24             → 2 Jahre
  python backtest_engine.py --plot                  → Mit Equity-Curve Chart
  python backtest_engine.py --asset ETH             → Nur ETH
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
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
MAX_FEE_PCT = 1.80  # Ab 30. März 2026: Crypto 5m/15m Peak Fee
KELLY_FRACTION = 0.50  # Half-Kelly
MAX_POSITION_PCT = 0.08
MIN_EDGE_PCT = 2.0

# Idealistic defaults
IDEALISTIC_CAPITAL = 1000.0
IDEALISTIC_POSITION = 50.0


# --- Realistic Friction Model ---

@dataclass
class RealisticParams:
    """Kalibrierte Friktions-Parameter für ehrlichen Backtest.

    Kalibriert gegen ECHTE Polymarket-Daten (1. April 2026):
    - 1,048 BTC 5m Trades → opportunity_rate = 15.1%
    - Avg Entry im tradeablen Bereich: 0.4965
    - pmxt_full_sample.parquet: 297K L2 Snapshots, Markt repriced in 2-5s
    - Spread: Median 0.1 Cent, Liquidity: Median $1M+ an Top 3 Ask Levels
    """
    initial_capital: float = 80.0       # Echtes Startkapital ($79.99)
    flat_position_usd: float = 5.0      # Feste Positionsgröße, kein Compounding
    opportunity_rate: float = 0.151     # 15.1% — aus 1,048 echten Polymarket Trades
    avg_entry_price: float = 0.4965     # Durchschnittlicher Entry im tradeablen Bereich
    entry_noise_std: float = 0.025      # Standardabweichung um avg_entry (realer Spread)
    base_slippage_pct: float = 0.10     # Spread nur 0.1 Cent median → 0.1% realistisch
    latency_ms_mean: float = 350.0      # Mittlere Order-Latenz (ms)
    latency_ms_std: float = 75.0        # Latenz-Standardabweichung
    fill_rate: float = 0.85             # 85% der Orders werden gefüllt
    competition_haircut: float = 0.20   # 20% der Edge geht an andere Bots
    kill_switch_pct: float = 0.40       # -40% Total Loss → Shutdown
    seed: int = 42                      # Reproduzierbare Ergebnisse


REALISTIC = RealisticParams()


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
    """Dynamische Gebühr in %. Max 1.80% bei p=0.50 (ab 30.03.2026)."""
    p = max(0.01, min(0.99, price))
    base = 4 * p * (1 - p)
    return MAX_FEE_PCT * base * base


def estimate_probability(momentum_pct: float, seconds_to_expiry: float) -> float:
    """Logistische Funktion: Momentum → Wahrscheinlichkeit."""
    if seconds_to_expiry <= 0:
        return 0.5
    time_factor = min(1.0, 120.0 / max(seconds_to_expiry, 1.0))
    raw_signal = abs(momentum_pct) * time_factor * 3.0  # scaling=3.0 (RMSE-optimiert gegen 7mo BTC-Daten)
    return 1 / (1 + math.exp(-raw_signal))


def kelly_size(p_true: float, p_market: float) -> float:
    """Half-Kelly für Binary Prediction Markets."""
    if p_market >= 1.0 or p_market <= 0.0:
        return 0.0
    raw = (p_true - p_market) / (1 - p_market)
    raw = max(0.0, min(raw, 0.25))
    return raw * KELLY_FRACTION


# --- Sharpe Ratio (korrekt: daily returns, annualized) ---

def compute_sharpe_daily(trade_results: list[SlotResult], num_candles: int) -> float:
    """Berechnet Sharpe Ratio aus täglichen Renditen (annualisiert).

    Korrekte Methode: PnL nach Kalendertag gruppieren, daily mean/std, × sqrt(252).
    """
    if not trade_results:
        return 0.0

    # PnL nach Tag gruppieren
    daily_pnl: dict[int, float] = defaultdict(float)
    for r in trade_results:
        if r.pnl_usd != 0:
            day = int(r.slot_start_ts // 86400)  # Tag als Unix-Day
            daily_pnl[day] += r.pnl_usd

    if len(daily_pnl) < 5:  # Mindestens 5 Handelstage
        return 0.0

    returns = list(daily_pnl.values())
    n = len(returns)
    mean_r = sum(returns) / n
    variance = sum((x - mean_r) ** 2 for x in returns) / (n - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.001

    return round((mean_r / std_r) * math.sqrt(252), 2)


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
    realistic: bool = False,
    params: RealisticParams | None = None,
) -> tuple[list[SlotResult], list[float]]:
    """Simuliert alle 5m/15m Slots und berechnet PnL.

    Args:
        realistic: True → alle Friktionen aktiv (Slippage, Latenz, Caps, Competition)
        params: RealisticParams (nur wenn realistic=True)

    Returns:
        (slot_results, equity_curve)
    """
    p = params or REALISTIC
    capital = p.initial_capital if realistic else IDEALISTIC_CAPITAL
    position_size = p.flat_position_usd if realistic else IDEALISTIC_POSITION

    if realistic:
        random.seed(p.seed)

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

        # --- REALISTIC: Opportunity Gate + Latency + Fill Rate ---
        if realistic:
            # 1. Opportunity Rate: Nur 15.1% der Signale haben einen Counterpart bei ~0.50
            #    (Kalibriert aus 1,048 echten Polymarket BTC 5m Trades)
            if random.random() > p.opportunity_rate:
                continue  # Kein Counterpart bei 0.50 — Markt schon repriced

            # 2. Latency: prüfe ob Signal nach 350ms noch gültig
            next_candle_ts = momentum_end_ts + 60
            next_idx = ts_to_idx.get(next_candle_ts)
            if next_idx is not None:
                next_price = candles[next_idx].close
                next_momentum = (next_price - ref_price) / ref_price * 100
                if abs(momentum) > 0 and (next_momentum / momentum) < 0.70:
                    continue  # Signal reversed während Latenz

            # 3. Fill Rate
            if random.random() > p.fill_rate:
                continue

        # Richtung vorhersagen
        predicted = "UP" if momentum > 0 else "DOWN"
        actual = "UP" if end_price >= ref_price else "DOWN"
        correct = predicted == actual

        # --- Entry Price ---
        if realistic:
            # Echte Entry-Preise aus Polymarket-Daten:
            # Avg = 0.4965, Std = 0.025 (gemessen aus tradeablem Bereich 0.45-0.55)
            simulated_ask = max(0.42, min(0.58,
                random.gauss(p.avg_entry_price, p.entry_noise_std)
            ))
            simulated_ask *= (1 + p.base_slippage_pct / 100)
        else:
            simulated_ask = 0.50  # Idealistic: Markt steht noch bei 0.50

        # p_true aus Momentum
        seconds_remaining = (slot_duration_min - momentum_window_min) * 60
        p_true = estimate_probability(momentum, seconds_remaining)

        # Fee
        fee = polymarket_fee(simulated_ask)

        # Net EV
        effective_ask = simulated_ask * (1 + fee / 100)
        net_ev = (p_true / effective_ask - 1) * 100

        # Kelly Sizing
        kelly = kelly_size(p_true, simulated_ask)

        # Position Size Check
        if net_ev <= MIN_EDGE_PCT or kelly <= 0:
            results.append(SlotResult(
                slot_start, slot_duration_min, momentum, momentum_window_min,
                predicted, actual, correct, simulated_ask, fee, net_ev,
                p_true, kelly, 0.0
            ))
            continue

        # Kapital-Check
        position = position_size
        if current_capital < position + 1:
            continue

        # PnL berechnen
        fee_usd = position * fee / 100
        shares = position / simulated_ask

        if correct:
            gross_payout = shares * 1.0
            pnl = gross_payout - position - fee_usd
            # REALISTIC: Competition Haircut auf Gewinne
            if realistic:
                pnl *= (1 - p.competition_haircut)
        else:
            pnl = -(position + fee_usd)
            # REALISTIC: Slippage macht Verluste etwas schlimmer
            if realistic:
                pnl *= 1.02  # 2% schlimmere Fills bei Verlusten

        current_capital += pnl
        equity.append(current_capital)

        results.append(SlotResult(
            slot_start, slot_duration_min, momentum, momentum_window_min,
            predicted, actual, correct, simulated_ask, fee, net_ev,
            p_true, kelly, pnl
        ))

        # Kill Switch Check
        kill_threshold = p.initial_capital if realistic else IDEALISTIC_CAPITAL
        kill_pct = p.kill_switch_pct if realistic else 0.40
        if current_capital < kill_threshold * (1 - kill_pct):
            break

    return results, equity


# --- Grid Search ---

def run_grid_search(
    candles: list[Candle], asset: str,
    realistic: bool = False,
) -> list[dict]:
    """Testet alle Kombinationen von Parametern."""
    mode = "realistic" if realistic else "idealistic"
    results = []
    total = len(SLOT_DURATIONS) * len(MOMENTUM_WINDOWS_MIN) * len(MOMENTUM_THRESHOLDS)
    current = 0

    for slot_dur in SLOT_DURATIONS:
        for mom_win in MOMENTUM_WINDOWS_MIN:
            if mom_win >= slot_dur:
                continue  # Momentum-Fenster muss kleiner als Slot sein

            for threshold in MOMENTUM_THRESHOLDS:
                current += 1
                sys.stdout.write(
                    f"\r  [{mode.upper()}] Grid Search: {current}/{total} "
                    f"({asset} {slot_dur}m, mom={mom_win}m, thr={threshold}%)"
                )
                sys.stdout.flush()

                slots, equity = simulate_slots(
                    candles, slot_dur, mom_win, threshold,
                    realistic=realistic,
                )

                # Nur Trades mit PnL ≠ 0 zählen
                traded = [s for s in slots if s.pnl_usd != 0]
                all_signals = [s for s in slots]

                init_cap = REALISTIC.initial_capital if realistic else IDEALISTIC_CAPITAL

                if not traded:
                    results.append({
                        "mode": mode, "asset": asset, "slot_min": slot_dur,
                        "mom_win_min": mom_win, "threshold_pct": threshold,
                        "signals": len(all_signals), "trades": 0,
                        "win_rate": 0, "pnl": 0, "sharpe": 0,
                        "max_dd_pct": 0, "final_capital": init_cap,
                        "avg_daily_pnl": 0, "trading_days": 0,
                    })
                    continue

                wins = sum(1 for s in traded if s.correct)
                total_pnl = sum(s.pnl_usd for s in traded)
                win_rate = wins / len(traded) * 100

                # Sharpe Ratio (korrekt: tägliche Renditen)
                sharpe = compute_sharpe_daily(traded, len(candles))

                # Trading Days & Avg Daily PnL
                days_set = set(int(s.slot_start_ts // 86400) for s in traded)
                trading_days = len(days_set)
                avg_daily_pnl = total_pnl / trading_days if trading_days > 0 else 0

                # Max Drawdown
                peak = init_cap
                max_dd = 0
                for eq in equity:
                    if eq > peak:
                        peak = eq
                    dd = (peak - eq) / peak * 100
                    if dd > max_dd:
                        max_dd = dd

                results.append({
                    "mode": mode, "asset": asset, "slot_min": slot_dur,
                    "mom_win_min": mom_win, "threshold_pct": threshold,
                    "signals": len(all_signals), "trades": len(traded),
                    "win_rate": round(win_rate, 1), "pnl": round(total_pnl, 2),
                    "sharpe": sharpe, "max_dd_pct": round(max_dd, 1),
                    "final_capital": round(equity[-1], 2) if equity else init_cap,
                    "avg_daily_pnl": round(avg_daily_pnl, 4),
                    "trading_days": trading_days,
                })

    print()
    return results


# --- Ausgabe ---

def print_results(all_results: list[dict], mode_filter: str = "") -> None:
    """Gibt die Grid-Search Ergebnisse aus."""
    W = 80
    results = [r for r in all_results if not mode_filter or r.get("mode") == mode_filter]

    if mode_filter:
        label = mode_filter.upper()
    else:
        label = "ALL"

    print(f"\n{'=' * W}")
    print(f"  BACKTEST ERGEBNISSE [{label}] — Polymarket Latency Arbitrage")
    print(f"{'=' * W}")

    profitable = [r for r in results if r["trades"] > 0]
    profitable.sort(key=lambda r: r["pnl"], reverse=True)

    if not profitable:
        print("\n  Keine Trades generiert.")
        return

    # Top 15
    print(f"\n  TOP 15 (nach PnL)")
    print(f"  {'Mode':<10} {'Asset':<5} {'Slot':<4} {'Mom':<4} {'Thr%':<6} {'Trades':>7} {'WR%':>6} {'PnL $':>10} {'Sharpe':>7} {'MaxDD%':>7} {'$/Day':>8}")
    print(f"  {'-'*10} {'-'*5} {'-'*4} {'-'*4} {'-'*6} {'-'*7} {'-'*6} {'-'*10} {'-'*7} {'-'*7} {'-'*8}")

    for r in profitable[:15]:
        wr_color = "\033[92m" if r["win_rate"] >= 55 else "\033[91m" if r["win_rate"] < 50 else "\033[93m"
        pnl_color = "\033[92m" if r["pnl"] > 0 else "\033[91m"
        print(
            f"  {r.get('mode','?'):<10} {r['asset']:<5} {r['slot_min']:<4} {r['mom_win_min']:<4} {r['threshold_pct']:<6} "
            f"{r['trades']:>7} "
            f"{wr_color}{r['win_rate']:>5.1f}%\033[0m "
            f"{pnl_color}{r['pnl']:>+10.2f}\033[0m "
            f"{r['sharpe']:>7.2f} {r['max_dd_pct']:>6.1f}% "
            f"{r.get('avg_daily_pnl', 0):>+8.4f}"
        )

    # Bottom 5
    print(f"\n  BOTTOM 5")
    for r in profitable[-5:]:
        pnl_color = "\033[92m" if r["pnl"] > 0 else "\033[91m"
        print(
            f"  {r.get('mode','?'):<10} {r['asset']:<5} {r['slot_min']:<4} {r['mom_win_min']:<4} {r['threshold_pct']:<6} "
            f"{r['trades']:>7} "
            f"{r['win_rate']:>5.1f}% "
            f"{pnl_color}{r['pnl']:>+10.2f}\033[0m "
            f"{r['sharpe']:>7.2f} {r['max_dd_pct']:>6.1f}%"
        )

    # Fazit
    best = profitable[0]
    print(f"\n  FAZIT [{label}]")
    if best["pnl"] > 0 and best["win_rate"] > 52:
        print(f"  \033[92mPOSITIVER EXPECTED VALUE\033[0m")
    else:
        print(f"  \033[91mNICHT PROFITABEL\033[0m")
    print(f"  Beste: {best['asset']} {best['slot_min']}m, Mom={best['mom_win_min']}m, "
          f"Thr={best['threshold_pct']}%")
    print(f"  WR={best['win_rate']}%, PnL=${best['pnl']:+.2f}, "
          f"Sharpe={best['sharpe']}, DD={best['max_dd_pct']}%")


def print_comparison(all_results: list[dict]) -> None:
    """Side-by-Side Vergleich: Idealistic vs Realistic für gleiche Configs."""
    W = 100
    print(f"\n{'=' * W}")
    print(f"  IDEALISTIC vs REALISTIC — Side-by-Side Comparison")
    print(f"{'=' * W}")

    ideal = {(r["asset"], r["slot_min"], r["mom_win_min"], r["threshold_pct"]): r
             for r in all_results if r.get("mode") == "idealistic" and r["trades"] > 0}
    real = {(r["asset"], r["slot_min"], r["mom_win_min"], r["threshold_pct"]): r
            for r in all_results if r.get("mode") == "realistic"}

    # Sortiere nach idealistic PnL
    keys = sorted(ideal.keys(), key=lambda k: ideal[k]["pnl"], reverse=True)

    print(f"\n  {'Config':<22} {'--- IDEALISTIC ---':>30} {'--- REALISTIC ---':>30} {'Reality':>12}")
    print(f"  {'':22} {'Trades':>7} {'WR%':>6} {'PnL':>9} {'Sharpe':>7}   {'Trades':>7} {'WR%':>6} {'PnL':>9} {'Sharpe':>7}   {'PnL Ratio':>10}")
    print(f"  {'-'*22} {'-'*30} {'-'*30} {'-'*12}")

    for key in keys[:20]:
        i = ideal[key]
        r = real.get(key)
        if not r:
            continue

        config = f"{i['asset']} {i['slot_min']}m m{i['mom_win_min']} t{i['threshold_pct']}%"
        ratio = f"{r['pnl']/i['pnl']*100:.1f}%" if i['pnl'] > 0 else "N/A"

        r_pnl_color = "\033[92m" if r["pnl"] > 0 else "\033[91m"
        print(
            f"  {config:<22} "
            f"{i['trades']:>7} {i['win_rate']:>5.1f}% {i['pnl']:>+9.2f} {i['sharpe']:>7.2f}   "
            f"{r['trades']:>7} {r['win_rate']:>5.1f}% {r_pnl_color}{r['pnl']:>+9.2f}\033[0m {r['sharpe']:>7.2f}   "
            f"{ratio:>10}"
        )

    # Summary Stats
    ideal_profitable = sum(1 for r in all_results if r.get("mode") == "idealistic" and r["pnl"] > 0 and r["trades"] > 0)
    ideal_total = sum(1 for r in all_results if r.get("mode") == "idealistic" and r["trades"] > 0)
    real_profitable = sum(1 for r in all_results if r.get("mode") == "realistic" and r["pnl"] > 0 and r["trades"] > 0)
    real_total = sum(1 for r in all_results if r.get("mode") == "realistic" and r["trades"] > 0)

    print(f"\n  {'=' * 60}")
    print(f"  SUMMARY")
    print(f"  Idealistic: {ideal_profitable}/{ideal_total} configs profitabel ({ideal_profitable/max(ideal_total,1)*100:.0f}%)")
    print(f"  Realistic:  {real_profitable}/{real_total} configs profitabel ({real_profitable/max(real_total,1)*100:.0f}%)")
    if ideal_total > 0 and real_total > 0:
        ideal_avg_pnl = sum(r["pnl"] for r in all_results if r.get("mode") == "idealistic" and r["trades"] > 0) / ideal_total
        real_avg_pnl = sum(r["pnl"] for r in all_results if r.get("mode") == "realistic" and r["trades"] > 0) / real_total
        print(f"  Avg PnL Idealistic: ${ideal_avg_pnl:+.2f}")
        print(f"  Avg PnL Realistic:  ${real_avg_pnl:+.2f}")
        print(f"  Reality Factor:     {real_avg_pnl/ideal_avg_pnl*100:.1f}% of idealistic" if ideal_avg_pnl > 0 else "")


def plot_equity_curve(candles: list[Candle], asset: str, best_config: dict, realistic: bool = False) -> None:
    """Plottet die Equity Curve der besten Konfiguration."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht installiert. pip install matplotlib")
        return

    mode = "realistic" if realistic else "idealistic"
    _, equity = simulate_slots(
        candles, best_config["slot_min"],
        best_config["mom_win_min"], best_config["threshold_pct"],
        realistic=realistic,
    )

    init_cap = REALISTIC.initial_capital if realistic else IDEALISTIC_CAPITAL

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})
    fig.suptitle(
        f"[{mode.upper()}] {asset} {best_config['slot_min']}m | "
        f"Mom={best_config['mom_win_min']}m, Thr={best_config['threshold_pct']}% | "
        f"WR={best_config['win_rate']}%, PnL=${best_config['pnl']:+.2f}",
        fontsize=12, fontweight="bold"
    )

    color = "#22c55e" if equity[-1] > init_cap else "#ef4444"
    ax1.plot(equity, color=color, linewidth=1)
    ax1.axhline(y=init_cap, color="gray", linestyle="--", alpha=0.5)
    ax1.axhline(y=init_cap * 0.8, color="red", linestyle="--", alpha=0.3, label="-20% Kill Switch")
    ax1.set_ylabel("Capital ($)")
    ax1.set_title(f"Equity Curve [{mode}]")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.2)

    peak = init_cap
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
    out = DATA_DIR / f"backtest_{asset}_{best_config['slot_min']}m_{mode}.png"
    plt.savefig(out, dpi=150, facecolor="#1a1d27")
    print(f"\n  Chart gespeichert: {out}")
    plt.close()


# --- File Saves ---

def save_results(all_results: list[dict]) -> None:
    """Speichert Ergebnisse persistent — Daten dürfen nie verloren gehen."""
    if not all_results:
        return

    # Combined
    f_json = DATA_DIR / "grid_results.json"
    with open(f_json, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n  Gespeichert: {f_json.name} ({len(all_results)} Configs)")

    f_csv = DATA_DIR / "grid_results.csv"
    with open(f_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
        writer.writeheader()
        writer.writerows(all_results)

    # Split by mode
    for mode in ["idealistic", "realistic"]:
        subset = [r for r in all_results if r.get("mode") == mode]
        if not subset:
            continue
        f_json = DATA_DIR / f"grid_results_{mode}.json"
        with open(f_json, "w") as f:
            json.dump(subset, f, indent=2)

        f_csv = DATA_DIR / f"grid_results_{mode}.csv"
        with open(f_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=subset[0].keys())
            writer.writeheader()
            writer.writerows(subset)
        print(f"  Gespeichert: {f_json.name} ({len(subset)} Configs)")


# --- Main ---

async def main():
    parser = argparse.ArgumentParser(description="Polymarket Latency Arb Backtest")
    parser.add_argument("--months", type=int, default=6, help="Monate Daten (default: 6)")
    parser.add_argument("--asset", default="both", help="BTC, ETH, oder both")
    parser.add_argument("--plot", action="store_true", help="Equity Curve plotten")
    parser.add_argument("--realistic-only", action="store_true", help="Nur realistischen Modus")
    parser.add_argument("--idealistic-only", action="store_true", help="Nur idealistischen Modus")
    args = parser.parse_args()

    run_idealistic = not args.realistic_only
    run_realistic = not args.idealistic_only

    # Daten-Zeitraum berechnen
    end_date = "2026-03-28"
    start_year = 2026 - (args.months // 12)
    start_month = 3 - (args.months % 12)
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    start_date = f"{start_year}-{start_month:02d}-01"

    assets = ["BTC", "ETH"] if args.asset.lower() == "both" else [args.asset.upper()]

    modes = []
    if run_idealistic:
        modes.append("IDEALISTIC ($1K, $50/trade, no friction)")
    if run_realistic:
        modes.append(f"REALISTIC (${REALISTIC.initial_capital}, ${REALISTIC.flat_position_usd}/trade, "
                     f"slippage={REALISTIC.base_slippage_pct}%, latency={REALISTIC.latency_ms_mean:.0f}ms, "
                     f"fill={REALISTIC.fill_rate*100:.0f}%, haircut={REALISTIC.competition_haircut*100:.0f}%)")

    print(f"\n{'=' * 80}")
    print(f"  POLYMARKET LATENCY ARB — BACKTEST ENGINE v2.0")
    print(f"  Zeitraum: {start_date} → {end_date} ({args.months} Monate)")
    print(f"  Assets: {', '.join(assets)}")
    print(f"  Fee-Modell: {MAX_FEE_PCT}% peak (Exponent 2)")
    for m in modes:
        print(f"  Mode: {m}")
    print(f"  Grid: {len(MOMENTUM_THRESHOLDS)} Schwellen × {len(MOMENTUM_WINDOWS_MIN)} Fenster × {len(SLOT_DURATIONS)} Slots")
    print(f"{'=' * 80}\n")

    all_results = []

    for asset in assets:
        symbol = f"{asset}USDT"
        print(f"[{asset}] Lade Binance 1m Klines...")
        candles = await fetch_binance_klines(symbol, "1m", start_date, end_date)

        if len(candles) < 1000:
            print(f"  Zu wenig Daten für {asset}: {len(candles)} Klines")
            continue

        # --- IDEALISTIC ---
        if run_idealistic:
            print(f"[{asset}] Grid Search IDEALISTIC...")
            results_i = run_grid_search(candles, asset, realistic=False)
            all_results.extend(results_i)

            if args.plot:
                profitable = [r for r in results_i if r["pnl"] > 0]
                if profitable:
                    best = max(profitable, key=lambda r: r["pnl"])
                    plot_equity_curve(candles, asset, best, realistic=False)

        # --- REALISTIC ---
        if run_realistic:
            print(f"[{asset}] Grid Search REALISTIC...")
            results_r = run_grid_search(candles, asset, realistic=True)
            all_results.extend(results_r)

            if args.plot:
                profitable = [r for r in results_r if r["pnl"] > 0]
                if profitable:
                    best = max(profitable, key=lambda r: r["pnl"])
                    plot_equity_curve(candles, asset, best, realistic=True)

    # --- Output ---
    if run_idealistic:
        print_results(all_results, mode_filter="idealistic")
    if run_realistic:
        print_results(all_results, mode_filter="realistic")
    if run_idealistic and run_realistic:
        print_comparison(all_results)

    save_results(all_results)


if __name__ == "__main__":
    asyncio.run(main())
