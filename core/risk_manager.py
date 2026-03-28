"""Kill-Switches und Circuit Breaker — Risikomanagement vom ersten Tag."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

from config import Settings


class BotState(Enum):
    RUNNING = "running"
    PAUSED = "paused"       # Temporärer Stop (z.B. tägliches Limit)
    SHUTDOWN = "shutdown"   # Finaler Kill-Switch ausgelöst


@dataclass
class DailyStats:
    """Tages-Performance-Tracking."""

    date: str = ""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    peak_pnl_usd: float = 0.0


class RiskManager:
    """Überwacht Risiko und erzwingt Kill-Switches.

    Kill-Switches:
    1. Max 8% des Portfolios pro Trade
    2. -20% Tagesverlust → Pause
    3. -40% Gesamtverlust → Shutdown
    4. Min. Balance < 100 USD auf einer Börse → kein Trading auf dieser Börse
    """

    def __init__(self, settings: Settings, initial_portfolio_usd: float = 0) -> None:
        self.settings = settings
        self.initial_portfolio_usd = initial_portfolio_usd
        self.state = BotState.RUNNING
        self._total_pnl_usd: float = 0.0
        self._daily_stats = DailyStats(date=self._today())
        self._cooldown_until: float = 0  # Unix timestamp
        self._trade_count: int = 0

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _reset_daily_if_needed(self) -> None:
        today = self._today()
        if self._daily_stats.date != today:
            logger.info(f"Neuer Tag: Reset der Tagesstatistiken (gestern: PnL={self._daily_stats.total_pnl_usd:.2f})")
            self._daily_stats = DailyStats(date=today)
            if self.state == BotState.PAUSED:
                self.state = BotState.RUNNING
                logger.info("Bot nach täglichem Reset wieder aktiv")

    # --- Pre-Trade Checks ---

    def can_trade(self) -> tuple[bool, str]:
        """Prüft ob ein neuer Trade erlaubt ist."""
        self._reset_daily_if_needed()

        if self.state == BotState.SHUTDOWN:
            return False, "SHUTDOWN: Gesamtverlust-Limit erreicht"

        if self.state == BotState.PAUSED:
            return False, "PAUSED: Tagesverlust-Limit erreicht"

        if time.time() < self._cooldown_until:
            remaining = int(self._cooldown_until - time.time())
            return False, f"Cooldown aktiv: noch {remaining}s"

        return True, "OK"

    def check_order_size(self, order_size_usd: float, portfolio_value_usd: float) -> tuple[bool, str]:
        """Prüft ob Ordergröße innerhalb der Limits liegt."""
        if portfolio_value_usd <= 0:
            return False, "Portfolio-Wert unbekannt oder 0"

        max_size = portfolio_value_usd * self.settings.max_portfolio_pct_per_trade
        if order_size_usd > max_size:
            return False, f"Ordergröße {order_size_usd:.2f} > Max {max_size:.2f} (8% von {portfolio_value_usd:.2f})"

        if order_size_usd > self.settings.max_order_size_usd:
            return False, f"Ordergröße {order_size_usd:.2f} > Hard Limit {self.settings.max_order_size_usd:.2f}"

        return True, "OK"

    def check_balance(self, balance_usd: float, exchange_id: str) -> tuple[bool, str]:
        """Prüft ob genug Balance auf einer Börse vorhanden ist."""
        if balance_usd < self.settings.min_balance_usd:
            return False, f"{exchange_id}: Balance {balance_usd:.2f} < Min {self.settings.min_balance_usd:.2f}"
        return True, "OK"

    # --- Post-Trade Updates ---

    def record_trade(self, pnl_usd: float) -> None:
        """Aktualisiert Statistiken nach einem Trade."""
        self._reset_daily_if_needed()
        self._trade_count += 1
        self._total_pnl_usd += pnl_usd

        self._daily_stats.trades += 1
        self._daily_stats.total_pnl_usd += pnl_usd

        if pnl_usd >= 0:
            self._daily_stats.wins += 1
        else:
            self._daily_stats.losses += 1

        # Peak/Drawdown tracken
        if self._daily_stats.total_pnl_usd > self._daily_stats.peak_pnl_usd:
            self._daily_stats.peak_pnl_usd = self._daily_stats.total_pnl_usd

        drawdown = self._daily_stats.peak_pnl_usd - self._daily_stats.total_pnl_usd
        if drawdown > self._daily_stats.max_drawdown_usd:
            self._daily_stats.max_drawdown_usd = drawdown

        # Kill-Switch-Prüfung
        self._check_kill_switches()

    def _check_kill_switches(self) -> None:
        """Prüft und aktiviert Kill-Switches."""
        portfolio = self.initial_portfolio_usd
        if portfolio <= 0:
            return

        # Kill-Switch 2: Tagesverlust
        daily_loss_pct = abs(self._daily_stats.total_pnl_usd) / portfolio
        if self._daily_stats.total_pnl_usd < 0 and daily_loss_pct >= self.settings.max_daily_loss_pct:
            self.state = BotState.PAUSED
            logger.critical(
                f"KILL-SWITCH: Tagesverlust {daily_loss_pct:.1%} >= {self.settings.max_daily_loss_pct:.0%} "
                f"→ Trading PAUSIERT bis morgen"
            )

        # Kill-Switch 3: Gesamtverlust
        total_loss_pct = abs(self._total_pnl_usd) / portfolio
        if self._total_pnl_usd < 0 and total_loss_pct >= self.settings.max_total_loss_pct:
            self.state = BotState.SHUTDOWN
            logger.critical(
                f"KILL-SWITCH: Gesamtverlust {total_loss_pct:.1%} >= {self.settings.max_total_loss_pct:.0%} "
                f"→ Bot SHUTDOWN"
            )

    def activate_cooldown(self, seconds: int = 60) -> None:
        """Aktiviert einen Cooldown (z.B. nach Failure Unwind)."""
        self._cooldown_until = time.time() + seconds
        logger.warning(f"Cooldown aktiviert: {seconds}s")

    # --- Status ---

    @property
    def daily_stats(self) -> DailyStats:
        self._reset_daily_if_needed()
        return self._daily_stats

    @property
    def total_pnl_usd(self) -> float:
        return self._total_pnl_usd

    @property
    def trade_count(self) -> int:
        return self._trade_count
