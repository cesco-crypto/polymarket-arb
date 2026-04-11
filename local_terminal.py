"""Local Terminal Monitor — SSH-basiertes Read-Only Dashboard.

Laeuft auf deinem MacBook. Verbindet sich per SSH mit AWS Dublin
und zeigt Net Edge, Trades, ODA Stats, Deadzone Cancels.

Usage:
  python local_terminal.py
  python local_terminal.py --host 34.249.110.17
  python local_terminal.py --refresh 15
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("pip install rich")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

DEFAULT_HOST = "34.249.110.17"
DEFAULT_USER = "ubuntu"
DEFAULT_KEY = str(Path.home() / ".ssh" / "polymarket-dublin.pem")
BOT_DIR = "/home/ubuntu/polymarket-arb"


# ═══════════════════════════════════════════════════════════════════
# SSH REMOTE READER
# ═══════════════════════════════════════════════════════════════════

class RemoteReader:
    """Liest Bot-Daten von AWS Dublin via SSH."""

    def __init__(self, host: str, user: str, key: str):
        self.host = host
        self.user = user
        self.key = key

    def _ssh(self, cmd: str, timeout: int = 10) -> str:
        """Fuehrt Befehl auf Remote aus, gibt stdout zurueck."""
        try:
            r = subprocess.run(
                ["ssh", "-i", self.key, "-o", "ConnectTimeout=5",
                 "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                 f"{self.user}@{self.host}", cmd],
                capture_output=True, text=True, timeout=timeout,
            )
            return r.stdout
        except (subprocess.TimeoutExpired, Exception) as e:
            return f"SSH_ERROR: {e}"

    def fetch_journal(self) -> list[dict]:
        """Liest nur ODA/CT/LT Trades vom Server (filtert serverseitig)."""
        raw = self._ssh(
            f"grep -E '\"trade_id\": \"(ODA|CT-|LT-)' {BOT_DIR}/data/trade_journal.jsonl 2>/dev/null",
            timeout=20,
        )
        if raw.startswith("SSH_ERROR"):
            return []
        records = []
        for line in raw.strip().splitlines():
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, Exception):
                continue
        return records

    def fetch_log_tail(self, lines: int = 40) -> list[str]:
        """Letzte N Zeilen von bot.log, gefiltert auf wichtige Events."""
        raw = self._ssh(f"tail -n 500 /home/ubuntu/bot.log 2>/dev/null")
        if raw.startswith("SSH_ERROR"):
            return [raw]
        keywords = ("SNIPE", "BURST", "CANCEL", "Shot#", "FILLED", "ERROR",
                    "REDEEM", "BLOCKED", "ODA", "Kill-Switch", "HEADLESS",
                    "Strategy started", "AUTO-COLLECT", "gestartet")
        all_lines = raw.strip().splitlines()
        filtered = [l for l in all_lines if any(k in l for k in keywords)]
        return list(reversed(filtered[-lines:]))

    def fetch_deadzone_cancels(self) -> int:
        """Zaehlt ODA CANCEL Eintraege im Log (Deadzone Filter)."""
        raw = self._ssh(f"grep -c 'ODA CANCEL' /home/ubuntu/bot.log 2>/dev/null")
        try:
            return int(raw.strip())
        except (ValueError, Exception):
            return 0

    def fetch_process(self) -> str:
        """Prueft ob Bot-Prozess laeuft."""
        raw = self._ssh("pgrep -c -f 'python3 main.py' 2>/dev/null || echo '0'")
        try:
            count = int(raw.strip())
            return "RUNNING" if count > 0 else "NOT_RUNNING"
        except ValueError:
            return raw.strip()

    def fetch_uptime(self) -> str:
        """Server-Uptime."""
        raw = self._ssh("uptime -p 2>/dev/null || uptime")
        return raw.strip()[:60]


# ═══════════════════════════════════════════════════════════════════
# STATS COMPUTATION
# ═══════════════════════════════════════════════════════════════════

def compute_stats(records: list[dict]) -> dict:
    """Berechnet P&L, Win Rate, ODA Stats aus Journal-Eintraegen."""
    by_trade: dict[str, list] = defaultdict(list)
    for r in records:
        tid = r.get("trade_id", "")
        if tid and not tid.startswith("REDEEM-CLEANUP"):
            by_trade[tid].append(r)

    strat_stats: dict[str, dict] = defaultdict(lambda: {
        "opens": 0, "wins": 0, "losses": 0, "pending": 0,
        "total_pnl": 0.0, "total_invested": 0.0,
    })
    recent_trades: list[dict] = []

    for tid, events in by_trade.items():
        open_ev = next((e for e in events if e.get("event") == "open"), None)
        close_ev = next((e for e in events if e.get("event") in ("redeemed", "resolved_loss", "close") and e.get("trade_id") == tid), None)
        live_upd = next((e for e in events if e.get("event") == "live_update"), None)

        # Wenn kein open_ev: nutze close oder skip
        if not open_ev:
            if close_ev:
                open_ev = close_ev  # standalone redeemed = nutze als Basis
            else:
                continue

        # Merge live_update
        if live_upd and open_ev:
            if live_upd.get("live_order_success"):
                open_ev["live_order_success"] = True

        # Strategie bestimmen
        if tid.startswith("CT-"):
            strat = "copy"
        elif tid.startswith("ODA"):
            strat = "oda"
        elif tid.startswith("LT-"):
            strat = "momentum" if open_ev.get("live_order_success") else "paper"
        elif tid.startswith("PT-"):
            strat = "paper"
        elif tid.startswith("REDEEM-CLEANUP"):
            continue
        else:
            strat = "other"

        strat_stats[strat]["opens"] += 1
        strat_stats[strat]["total_invested"] += open_ev.get("size_usd", 0)

        trade_info = {
            "trade_id": tid, "strategy": strat,
            "asset": open_ev.get("asset", ""),
            "direction": open_ev.get("direction", ""),
            "entry_price": open_ev.get("executed_price", 0),
            "size_usd": open_ev.get("size_usd", 0),
            "entry_ts": open_ev.get("entry_ts", 0),
            "live": bool(open_ev.get("live_order_success")),
        }

        if close_ev and close_ev is not open_ev:
            pnl = close_ev.get("pnl_usd", 0)
            event_type = close_ev.get("event", "")
            is_win = close_ev.get("outcome_correct", False) or pnl > 0
            if event_type == "redeemed" and is_win:
                strat_stats[strat]["wins"] += 1
                trade_info["status"] = "WIN"
                trade_info["pnl"] = pnl
            elif event_type in ("redeemed", "resolved_loss") and not is_win:
                strat_stats[strat]["losses"] += 1
                trade_info["status"] = "LOSS"
                trade_info["pnl"] = pnl
            else:
                if close_ev.get("outcome_correct"):
                    strat_stats[strat]["wins"] += 1
                    trade_info["status"] = "WIN"
                else:
                    strat_stats[strat]["losses"] += 1
                    trade_info["status"] = "LOSS"
                trade_info["pnl"] = pnl
            strat_stats[strat]["total_pnl"] += pnl
        else:
            strat_stats[strat]["pending"] += 1
            trade_info["status"] = "PENDING"
            trade_info["pnl"] = 0

        if strat != "paper":
            recent_trades.append(trade_info)

    # Aggregierte Live-Stats
    live = {"wins": 0, "losses": 0, "pnl": 0.0, "invested": 0.0}
    for sn in ("copy", "oda", "momentum"):
        s = strat_stats.get(sn, {})
        live["wins"] += s.get("wins", 0)
        live["losses"] += s.get("losses", 0)
        live["pnl"] += s.get("total_pnl", 0)
        live["invested"] += s.get("total_invested", 0)

    resolved = live["wins"] + live["losses"]
    live["win_rate"] = round(live["wins"] / max(1, resolved) * 100, 1)

    return {
        "live": live,
        "by_strategy": dict(strat_stats),
        "recent": sorted(recent_trades, key=lambda x: x.get("entry_ts", 0), reverse=True)[:15],
    }


# ═══════════════════════════════════════════════════════════════════
# RICH TERMINAL UI
# ═══════════════════════════════════════════════════════════════════

def build_display(stats: dict, log_lines: list[str], deadzone_cancels: int,
                  process_info: str, uptime: str) -> Layout:
    """Baut das Rich-Layout fuer das Terminal."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=2),
    )
    layout["left"].split_column(
        Layout(name="portfolio", size=14),
        Layout(name="trades"),
    )
    layout["right"].split_column(
        Layout(name="oda_stats", size=16),
        Layout(name="logs"),
    )

    # Header
    is_running = "RUNNING" in process_info and "NOT_RUNNING" not in process_info
    status = "[bold green]LIVE[/]" if is_running else "[bold red]STOPPED[/]"
    now = datetime.now().strftime("%H:%M:%S")
    layout["header"].update(
        Panel(f"[bold cyan]POLYMARKET HEADLESS TERMINAL[/]  {status}  |  {now}  |  {uptime}",
              style="cyan")
    )

    # Portfolio Panel
    live = stats.get("live", {})
    pnl = live.get("pnl", 0)
    pnl_color = "green" if pnl >= 0 else "red"
    wr = live.get("win_rate", 0)
    wr_color = "green" if wr >= 50 else "red"

    by_strat = stats.get("by_strategy", {})
    oda = by_strat.get("oda", {})
    copy = by_strat.get("copy", {})
    mom = by_strat.get("momentum", {})

    port = Table(show_header=False, box=None, padding=(0, 2))
    port.add_column("Label", style="dim")
    port.add_column("Value", justify="right")
    port.add_row("Net P&L (Live)", f"[bold {pnl_color}]${pnl:+.2f}[/]")
    port.add_row("Win Rate", f"[{wr_color}]{wr:.1f}%[/]")
    port.add_row("Wins / Losses", f"[green]{live.get('wins', 0)}[/] / [red]{live.get('losses', 0)}[/]")
    port.add_row("Total Invested", f"${live.get('invested', 0):.2f}")
    port.add_row("", "")
    oda_pnl = oda.get("total_pnl", 0)
    port.add_row("[purple]ODA PnL[/]", f"[{'green' if oda_pnl >= 0 else 'red'}]${oda_pnl:+.2f}[/]  ({oda.get('wins', 0)}W/{oda.get('losses', 0)}L)")
    ct_pnl = copy.get("total_pnl", 0)
    port.add_row("[yellow]Copy PnL[/]", f"[{'green' if ct_pnl >= 0 else 'red'}]${ct_pnl:+.2f}[/]  ({copy.get('wins', 0)}W/{copy.get('losses', 0)}L)")
    mom_pnl = mom.get("total_pnl", 0)
    port.add_row("[blue]Mom PnL[/]", f"[{'green' if mom_pnl >= 0 else 'red'}]${mom_pnl:+.2f}[/]  ({mom.get('wins', 0)}W/{mom.get('losses', 0)}L)")

    layout["portfolio"].update(Panel(port, title="[bold]PORTFOLIO[/]", border_style="green"))

    # ODA Stats
    oda_stats = Table(show_header=False, box=None, padding=(0, 2))
    oda_stats.add_column("Label", style="dim")
    oda_stats.add_column("Value", justify="right")
    oda_stats.add_row("ODA Snipes", f"[bold]{oda.get('opens', 0)}[/]")
    oda_stats.add_row("ODA Wins", f"[green]{oda.get('wins', 0)}[/]")
    oda_stats.add_row("ODA Losses", f"[red]{oda.get('losses', 0)}[/]")
    oda_stats.add_row("ODA Pending", f"[yellow]{oda.get('pending', 0)}[/]")
    oda_wr = round(oda.get("wins", 0) / max(1, oda.get("wins", 0) + oda.get("losses", 0)) * 100, 1)
    oda_stats.add_row("ODA Win Rate", f"[{'green' if oda_wr >= 50 else 'red'}]{oda_wr:.1f}%[/]")
    oda_stats.add_row("", "")
    oda_stats.add_row("[cyan]Deadzone Cancels[/]", f"[cyan]{deadzone_cancels}[/]")
    fill_attempts = oda.get("opens", 0) + deadzone_cancels
    fill_rate = round(oda.get("opens", 0) / max(1, fill_attempts) * 100, 1) if fill_attempts > 0 else 0
    oda_stats.add_row("Fill Rate", f"{fill_rate:.1f}%")
    oda_stats.add_row("Filter Rate", f"{100 - fill_rate:.1f}%")

    layout["oda_stats"].update(Panel(oda_stats, title="[bold purple]ODA STATS[/]", border_style="purple"))

    # Recent Trades Table
    trades_table = Table(box=None, padding=(0, 1), show_edge=False)
    trades_table.add_column("Time", style="dim", width=8)
    trades_table.add_column("Strat", width=5)
    trades_table.add_column("Asset", width=4)
    trades_table.add_column("Dir", width=4)
    trades_table.add_column("Entry", justify="right", width=6)
    trades_table.add_column("Size", justify="right", width=6)
    trades_table.add_column("PnL", justify="right", width=8)
    trades_table.add_column("Status", width=7)

    strat_colors = {"oda": "purple", "copy": "yellow", "momentum": "blue", "other": "dim"}
    for t in stats.get("recent", [])[:12]:
        ts = datetime.fromtimestamp(t.get("entry_ts", 0)).strftime("%H:%M") if t.get("entry_ts") else "?"
        sc = strat_colors.get(t["strategy"], "dim")
        pnl_val = t.get("pnl", 0)
        pnl_str = f"[{'green' if pnl_val >= 0 else 'red'}]${pnl_val:+.2f}[/]" if t["status"] != "PENDING" else "[yellow]---[/]"
        status_str = f"[green]{t['status']}[/]" if t["status"] == "WIN" else (
            f"[red]{t['status']}[/]" if t["status"] == "LOSS" else f"[yellow]{t['status']}[/]"
        )
        trades_table.add_row(
            ts, f"[{sc}]{t['strategy'][:3].upper()}[/]",
            t.get("asset", "?"), t.get("direction", "?"),
            f"${t.get('entry_price', 0):.2f}", f"${t.get('size_usd', 0):.1f}",
            pnl_str, status_str,
        )

    layout["trades"].update(Panel(trades_table, title="[bold]RECENT TRADES[/]", border_style="cyan"))

    # Activity Log
    log_text = Text()
    for line in log_lines[:20]:
        if "ERROR" in line:
            log_text.append(line[:120] + "\n", style="red")
        elif "CANCEL" in line:
            log_text.append(line[:120] + "\n", style="cyan")
        elif "SNIPE" in line or "Shot#" in line:
            log_text.append(line[:120] + "\n", style="green")
        elif "BURST" in line:
            log_text.append(line[:120] + "\n", style="yellow")
        else:
            log_text.append(line[:120] + "\n", style="dim")

    layout["logs"].update(Panel(log_text, title="[bold]ACTIVITY LOG[/]", border_style="dim"))

    # Footer
    layout["footer"].update(
        Panel(f"[dim]SSH → {DEFAULT_HOST} | Refresh: auto | Press Ctrl+C to exit[/]", style="dim")
    )

    return layout


# ═══════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Local Terminal Monitor for Polymarket Bot")
    parser.add_argument("--host", default=DEFAULT_HOST, help="AWS EC2 IP")
    parser.add_argument("--user", default=DEFAULT_USER, help="SSH user")
    parser.add_argument("--key", default=DEFAULT_KEY, help="SSH key path")
    parser.add_argument("--refresh", type=int, default=30, help="Refresh interval (seconds)")
    args = parser.parse_args()

    reader = RemoteReader(args.host, args.user, args.key)
    console = Console()

    console.print("[bold cyan]Connecting to AWS Dublin...[/]")

    try:
        with Live(console=console, refresh_per_second=0.2, screen=True) as live:
            while True:
                try:
                    records = reader.fetch_journal()
                    stats = compute_stats(records)
                    log_lines = reader.fetch_log_tail(30)
                    deadzone = reader.fetch_deadzone_cancels()
                    process = reader.fetch_process()
                    uptime = reader.fetch_uptime()
                    display = build_display(stats, log_lines, deadzone, process, uptime)
                    live.update(display)
                except Exception as e:
                    live.update(Panel(f"[red]Error: {e}[/]", title="ERROR"))

                time.sleep(args.refresh)

    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/]")


if __name__ == "__main__":
    main()
