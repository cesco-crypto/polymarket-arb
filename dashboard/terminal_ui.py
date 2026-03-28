"""Terminal-Dashboard mit Rich — zeigt Top-Arbitrage-Opportunitäten in Echtzeit."""

from __future__ import annotations

import time
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import Settings
from core.risk_manager import RiskManager, BotState
from core.scanner import Opportunity, Scanner


class TerminalDashboard:
    """Rich-basiertes Terminal-Dashboard für den Arbitrage Scanner."""

    def __init__(
        self,
        settings: Settings,
        scanner: Scanner,
        risk_manager: RiskManager,
    ) -> None:
        self.settings = settings
        self.scanner = scanner
        self.risk_manager = risk_manager
        self.console = Console()
        self._start_time = time.time()

    def build_layout(self) -> Layout:
        """Erstellt das Dashboard-Layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=5),
        )
        layout["body"].split_row(
            Layout(name="opportunities", ratio=3),
            Layout(name="stats", ratio=1),
        )

        layout["header"].update(self._build_header())
        layout["opportunities"].update(self._build_opportunities_table())
        layout["stats"].update(self._build_stats_panel())
        layout["footer"].update(self._build_footer())

        return layout

    def _build_header(self) -> Panel:
        """Header mit Bot-Status und Uhrzeit."""
        state = self.risk_manager.state
        if state == BotState.RUNNING:
            status = Text("● SCANNING", style="bold green")
        elif state == BotState.PAUSED:
            status = Text("● PAUSED", style="bold yellow")
        else:
            status = Text("● SHUTDOWN", style="bold red")

        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        header = Text()
        header.append("  ARBITRAGE SCANNER  ", style="bold white on blue")
        header.append("  ")
        header.append_text(status)
        header.append(f"  |  Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
        header.append(f"  |  {datetime.now().strftime('%H:%M:%S')}")
        header.append(f"  |  Scan #{self.scanner.scan_count}")

        return Panel(header, style="blue")

    def _build_opportunities_table(self) -> Panel:
        """Tabelle der Top-Arbitrage-Opportunitäten."""
        table = Table(
            title=f"Top {self.settings.top_opportunities} Arbitrage Opportunities",
            expand=True,
            show_lines=False,
            header_style="bold cyan",
        )

        table.add_column("#", style="dim", width=3, justify="right")
        table.add_column("Symbol", style="bold white", min_width=12)
        table.add_column("Buy On", style="green", min_width=8)
        table.add_column("Sell On", style="red", min_width=8)
        table.add_column("Ask", justify="right", min_width=12)
        table.add_column("Bid", justify="right", min_width=12)
        table.add_column("Gross %", justify="right", min_width=8)
        table.add_column("Net %", justify="right", min_width=8)
        table.add_column("Est. $", justify="right", min_width=8)
        table.add_column("Vol Buy", justify="right", min_width=10)
        table.add_column("Vol Sell", justify="right", min_width=10)

        for i, opp in enumerate(self.scanner.opportunities, 1):
            # Farbe basierend auf Netto-Spread
            if opp.net_spread_pct >= 1.0:
                spread_style = "bold green"
            elif opp.net_spread_pct >= 0.5:
                spread_style = "green"
            else:
                spread_style = "yellow"

            # Preis-Formatierung (dynamische Dezimalstellen)
            ask_str = self._format_price(opp.ask_price)
            bid_str = self._format_price(opp.bid_price)

            table.add_row(
                str(i),
                opp.symbol.replace("/USDT", ""),
                opp.buy_exchange,
                opp.sell_exchange,
                ask_str,
                bid_str,
                f"{opp.gross_spread_pct:.3f}%",
                Text(f"{opp.net_spread_pct:.3f}%", style=spread_style),
                f"${opp.estimated_profit_usd:.2f}",
                self._format_volume(opp.buy_volume_24h),
                self._format_volume(opp.sell_volume_24h),
            )

        if not self.scanner.opportunities:
            table.add_row(
                "-", "Keine profitablen Spreads gefunden", "", "", "", "",
                "", "", "", "", "",
            )

        return Panel(table, border_style="cyan")

    def _build_stats_panel(self) -> Panel:
        """Statistik-Panel rechts."""
        stats = self.risk_manager.daily_stats

        text = Text()
        text.append("Scanner Stats\n", style="bold underline")
        text.append(f"\nScans: {self.scanner.scan_count}")
        text.append(f"\nLetzte Dauer: {self.scanner.last_scan_duration:.1f}s")
        text.append(f"\nOpps: {len(self.scanner.opportunities)}")

        text.append("\n\nRisiko\n", style="bold underline")
        text.append(f"\nTages-PnL: ", style="dim")
        pnl = stats.total_pnl_usd
        text.append(
            f"${pnl:+.2f}",
            style="green" if pnl >= 0 else "red",
        )
        text.append(f"\nTrades heute: {stats.trades}")

        if stats.trades > 0:
            wr = stats.wins / stats.trades * 100
            text.append(f"\nWin Rate: {wr:.0f}%")

        text.append("\n\nKonfiguration\n", style="bold underline")
        text.append(f"\nMin Spread: {self.settings.min_profitable_spread}%")
        text.append(f"\nMin Vol: ${self.settings.min_volume_usd:,.0f}")
        text.append(f"\nMax Order: ${self.settings.max_order_size_usd:,.0f}")
        text.append(f"\nModus: {self.settings.mode}")

        return Panel(text, title="Stats", border_style="green")

    def _build_footer(self) -> Panel:
        """Footer mit Börsen-Status."""
        exchanges = list(self.scanner.market_data.exchanges.keys())
        common = len(self.scanner.market_data.common_symbols)

        text = Text()
        text.append("Börsen: ", style="bold")
        for ex in exchanges:
            text.append(f"● {ex}  ", style="green")

        text.append(f"\nGemeinsame Paare: {common}")
        text.append(f"  |  Quote: {self.settings.quote_currency}")
        text.append(f"  |  Refresh: {self.settings.scan_interval_seconds}s")
        text.append("\n\nDrücke Ctrl+C zum Beenden", style="dim italic")

        return Panel(text, border_style="dim")

    def get_live(self) -> Live:
        """Erstellt ein Rich Live-Display."""
        return Live(
            self.build_layout(),
            console=self.console,
            refresh_per_second=1,
            screen=True,
        )

    @staticmethod
    def _format_price(price: float) -> str:
        """Formatiert Preise mit sinnvollen Dezimalstellen."""
        if price >= 1000:
            return f"{price:,.2f}"
        elif price >= 1:
            return f"{price:.4f}"
        elif price >= 0.01:
            return f"{price:.6f}"
        else:
            return f"{price:.8f}"

    @staticmethod
    def _format_volume(volume_usd: float) -> str:
        """Formatiert Volumen leserlich (K, M, B)."""
        if volume_usd >= 1_000_000_000:
            return f"${volume_usd / 1_000_000_000:.1f}B"
        elif volume_usd >= 1_000_000:
            return f"${volume_usd / 1_000_000:.1f}M"
        elif volume_usd >= 1_000:
            return f"${volume_usd / 1_000:.0f}K"
        else:
            return f"${volume_usd:.0f}"
