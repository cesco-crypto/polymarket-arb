"""Paper Trader — Simuliert Polymarket-Trades und trackt PnL ohne echtes Kapital."""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path

import asyncio

from loguru import logger

from config import Settings
from core.pretrade_calculator import PreTradeResult, TradeDecision
from utils import telegram


@dataclass
class PaperPosition:
    """Eine offene Paper-Position auf Polymarket."""

    trade_id: str
    asset: str
    direction: str            # "UP" / "DOWN"
    market_condition_id: str
    market_question: str

    # Einstieg
    entry_price: float        # Polymarket-Ask bei Einstieg
    size_usd: float           # Investiertes Kapital
    shares: float             # Gekaufte Shares (size_usd / entry_price)
    fee_usd: float            # Bezahlte Gebühr
    entered_at: float         # Unix timestamp

    # Markt-Ablauf
    market_end_timestamp: float

    # Schätzung
    p_true_at_entry: float
    momentum_at_entry: float
    seconds_to_expiry_at_entry: float
    oracle_price_at_entry: float = 0.0   # Binance-Preis bei Einstieg (für Resolution)

    # Mark-out: Binance-Preis N Sekunden nach Einstieg (für Signal-Qualitäts-Analyse)
    markout_prices: dict = field(default_factory=dict)  # {seconds: price}

    # Auflösung (wird gesetzt wenn Markt abläuft)
    resolved: bool = False
    outcome_correct: bool = False
    exit_price: float = 0.0   # 1.0 wenn gewonnen, 0.0 wenn verloren
    pnl_usd: float = 0.0
    resolved_at: float = 0.0

    @property
    def age_s(self) -> float:
        return time.time() - self.entered_at

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.market_end_timestamp - time.time())

    @property
    def is_expired(self) -> bool:
        return time.time() > self.market_end_timestamp


class PaperTrader:
    """Verwaltet Paper-Trades und trackt Gesamt-PnL.

    Kill-Switch: Stoppt bei -20% Tages-Drawdown (aus risk_manager übernommen,
    aber hier nochmals direkt für die Polymarket-Strategie).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.capital_usd = settings.paper_capital_usd
        self.initial_capital_usd = settings.paper_capital_usd

        self._positions: dict[str, PaperPosition] = {}    # trade_id → Position
        self._closed_positions: list[PaperPosition] = []
        self._trade_counter = self._load_last_trade_counter(settings)
        self._session_start = time.time()
        self._peak_capital_usd = settings.paper_capital_usd

        # CSV-Logging
        self._csv_path = settings.data_dir / "paper_trades.csv"
        self._csv_initialized = False
        settings.data_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_last_trade_counter(settings: Settings) -> int:
        """Liest den höchsten Trade-Counter aus dem JSONL Journal.

        Verhindert ID-Duplikate nach Bot-Neustarts.
        """
        import json
        journal_path = settings.data_dir / "trade_journal.jsonl"
        max_counter = 0
        if journal_path.exists():
            try:
                with open(journal_path) as f:
                    for line in f:
                        try:
                            rec = json.loads(line.strip())
                            tid = rec.get("trade_id", "")
                            # Parse "PT-0014" oder "LT-0014" → 14
                            if tid and "-" in tid:
                                num = int(tid.split("-")[1])
                                max_counter = max(max_counter, num)
                        except (ValueError, json.JSONDecodeError):
                            pass
                if max_counter > 0:
                    logger.info(f"PaperTrader: Trade-Counter bei {max_counter} fortgesetzt (aus JSONL)")
            except Exception as e:
                logger.warning(f"PaperTrader: Konnte Counter nicht laden: {e}")
        return max_counter

    # --- Trade Entry ---

    def open_position(
        self,
        result: PreTradeResult,
        condition_id: str,
        question: str,
        market_end_timestamp: float,
        momentum_pct: float,
        seconds_to_expiry: float,
        oracle_price: float = 0.0,
    ) -> PaperPosition | None:
        """Eröffnet eine neue Paper-Position. Gibt None zurück wenn Kill-Switch aktiv."""

        # Kill-Switch Check
        if not self.can_trade():
            logger.warning("KILL-SWITCH aktiv — kein Paper-Trade")
            return None

        if result.decision != TradeDecision.EXECUTE:
            return None

        # Verfügbares Kapital prüfen
        if self.capital_usd < result.position_usd:
            logger.warning(f"Zu wenig Kapital: ${self.capital_usd:.2f} < ${result.position_usd:.2f}")
            return None

        self._trade_counter += 1
        prefix = "LT" if self.settings.live_trading else "PT"
        trade_id = f"{prefix}-{self._trade_counter:04d}"

        fee_usd = result.position_usd * (result.fee_pct / 100)
        shares = result.position_usd / result.p_market  # Shares = USD / Preis pro Share

        position = PaperPosition(
            trade_id=trade_id,
            asset=result.asset,
            direction=result.direction,
            market_condition_id=condition_id,
            market_question=question[:80],
            entry_price=result.p_market,
            size_usd=result.position_usd,
            shares=shares,
            fee_usd=fee_usd,
            entered_at=time.time(),
            market_end_timestamp=market_end_timestamp,
            p_true_at_entry=result.p_true,
            momentum_at_entry=momentum_pct,
            seconds_to_expiry_at_entry=seconds_to_expiry,
            oracle_price_at_entry=oracle_price,
        )

        # Kapital reservieren (inkl. Gebühr)
        self.capital_usd -= result.position_usd + fee_usd
        self._positions[trade_id] = position

        logger.info(
            f"📝 PAPER TRADE #{trade_id}: {result.asset} {result.direction} "
            f"@ {result.p_market:.3f} | ${result.position_usd:.2f} | "
            f"Edge: {result.net_ev_pct:.2f}% | "
            f"Ablauf in {seconds_to_expiry:.0f}s"
        )
        self._log_to_csv(position, event="open")

        # Telegram Backup (Persistence für Render ephemeral FS)
        asyncio.create_task(telegram.alert_trade_log(
            "open", trade_id, result.asset, result.direction,
            result.p_market, result.position_usd, fee_usd,
            momentum_pct, result.p_true, oracle_entry=oracle_price,
        ))

        return position

    # --- Trade Resolution ---

    def resolve_position(self, trade_id: str, oracle_price_now: float) -> None:
        """Löst eine Position auf wenn der Markt abläuft.

        Nutzt den gespeicherten oracle_price_at_entry der Position als Referenz.
        - Wenn BTC-Preis gestiegen seit Entry und direction=UP: WIN
        - Wenn BTC-Preis gefallen seit Entry und direction=DOWN: WIN
        """
        position = self._positions.get(trade_id)
        if not position or position.resolved:
            return

        if not position.is_expired:
            return  # Noch nicht abgelaufen

        # Auflösung: Referenzpreis ist der Binance-Preis bei Positionseröffnung
        ref_price = position.oracle_price_at_entry
        if ref_price <= 0:
            ref_price = oracle_price_now  # Fallback wenn kein Referenzpreis gespeichert

        actual_up = oracle_price_now > ref_price

        if position.direction == "UP":
            outcome_correct = actual_up
        else:  # DOWN
            outcome_correct = not actual_up

        if outcome_correct:
            # Gewinn: Shares × $1 (Polymarket zahlt $1 pro Share)
            gross_payout = position.shares * 1.0
            pnl_usd = gross_payout - position.size_usd - position.fee_usd
            exit_price = 1.0
        else:
            # Verlust: Alles verloren
            pnl_usd = -(position.size_usd + position.fee_usd)
            exit_price = 0.0

        position.resolved = True
        position.outcome_correct = outcome_correct
        position.exit_price = exit_price
        position.pnl_usd = round(pnl_usd, 4)
        position.resolved_at = time.time()

        # Kapital zurückbuchen
        self.capital_usd += position.size_usd + position.fee_usd + pnl_usd
        self._peak_capital_usd = max(self._peak_capital_usd, self.capital_usd)

        result_emoji = "✅" if outcome_correct else "❌"
        logger.info(
            f"{result_emoji} RESOLVED #{trade_id}: {position.asset} {position.direction} "
            f"→ {'WIN' if outcome_correct else 'LOSS'} | "
            f"PnL: ${pnl_usd:+.2f} | Kapital: ${self.capital_usd:.2f}"
        )

        self._log_to_csv(position, event="close")
        self._closed_positions.append(position)

        # Telegram Backup
        asyncio.create_task(telegram.alert_trade_log(
            "close", trade_id, position.asset, position.direction,
            position.entry_price, position.size_usd, position.fee_usd,
            position.momentum_at_entry, position.p_true_at_entry,
            pnl=pnl_usd, correct=outcome_correct, capital=self.capital_usd,
            oracle_entry=position.oracle_price_at_entry, oracle_now=oracle_price_now,
        ))

        del self._positions[trade_id]

    def record_markout(self, oracle_price: float, asset_filter: str = "") -> None:
        """Zeichnet Mark-out Preise auf — NUR für Positionen des richtigen Assets.

        KRITISCH: BTC-Preis darf nur BTC-Mark-outs aufzeichnen, nicht ETH!
        """
        now = time.time()
        for pos in self._positions.values():
            if asset_filter and pos.asset != asset_filter:
                continue
            age_s = now - pos.entered_at
            for target_s in [1, 5, 10, 30, 60]:
                if target_s not in pos.markout_prices and abs(age_s - target_s) < 1.5:
                    pos.markout_prices[target_s] = oracle_price

    def check_and_resolve_expired(
        self, oracle_price_now: float, asset_filter: str = ""
    ) -> list[PaperPosition]:
        """Prüft und löst abgelaufene Positionen auf.

        KRITISCH: asset_filter verhindert dass BTC-Preis ETH-Positionen resolvet!
        """
        resolved = []
        for trade_id in list(self._positions.keys()):
            pos = self._positions[trade_id]
            if asset_filter and pos.asset != asset_filter:
                continue
            if pos.is_expired:
                self.resolve_position(trade_id, oracle_price_now)
                if trade_id not in self._positions:
                    resolved.append(pos)
        return resolved

    # --- Kill-Switch ---

    def can_trade(self) -> bool:
        """Kill-Switch: False wenn Tages-Drawdown ≥ 20%."""
        daily_pnl = self.daily_pnl_usd()
        daily_loss_pct = abs(daily_pnl) / self.initial_capital_usd

        if daily_pnl < 0 and daily_loss_pct >= self.settings.max_daily_loss_pct:
            logger.critical(
                f"KILL-SWITCH AKTIV: Tages-Drawdown {daily_loss_pct:.1%} ≥ "
                f"{self.settings.max_daily_loss_pct:.0%} — Keine weiteren Trades"
            )
            return False
        return True

    # --- Statistiken ---

    def daily_pnl_usd(self) -> float:
        """Tages-PnL: Änderung des Kapitals seit Sessionstart."""
        return self.capital_usd - self.initial_capital_usd

    def total_return_pct(self) -> float:
        return (self.capital_usd / self.initial_capital_usd - 1) * 100

    def win_rate(self) -> float:
        if not self._closed_positions:
            return 0.0
        wins = sum(1 for p in self._closed_positions if p.outcome_correct)
        return wins / len(self._closed_positions) * 100

    def avg_pnl_per_trade(self) -> float:
        if not self._closed_positions:
            return 0.0
        return sum(p.pnl_usd for p in self._closed_positions) / len(self._closed_positions)

    def drawdown_pct(self) -> float:
        """Aktueller Drawdown vom Peak in %."""
        if self._peak_capital_usd <= 0:
            return 0.0
        return max(0.0, (self._peak_capital_usd - self.capital_usd) / self._peak_capital_usd * 100)

    def stats(self) -> dict:
        closed = self._closed_positions
        open_count = len(self._positions)
        return {
            "capital_usd": round(self.capital_usd, 2),
            "daily_pnl_usd": round(self.daily_pnl_usd(), 2),
            "total_return_pct": round(self.total_return_pct(), 2),
            "trades_total": len(closed) + open_count,
            "trades_closed": len(closed),
            "trades_open": open_count,
            "wins": sum(1 for p in closed if p.outcome_correct),
            "losses": sum(1 for p in closed if not p.outcome_correct),
            "win_rate_pct": round(self.win_rate(), 1),
            "avg_pnl_usd": round(self.avg_pnl_per_trade(), 4),
            "kill_switch_active": not self.can_trade(),
            "peak_capital_usd": round(self._peak_capital_usd, 2),
            "drawdown_pct": round(self.drawdown_pct(), 2),
        }

    def closed_positions_summary(self, limit: int = 50) -> list[dict]:
        """Letzte N geschlossene Positionen für Dashboard."""
        return [
            {
                "trade_id": p.trade_id,
                "asset": p.asset,
                "direction": p.direction,
                "entry_price": round(p.entry_price, 4),
                "size_usd": round(p.size_usd, 2),
                "fee_usd": round(p.fee_usd, 4),
                "pnl_usd": round(p.pnl_usd, 4),
                "outcome_correct": p.outcome_correct,
                "resolved_at": time.strftime("%H:%M:%S", time.localtime(p.resolved_at)) if p.resolved_at else "",
                "markout": {
                    str(k): round((v - p.oracle_price_at_entry) / p.oracle_price_at_entry * 100, 4)
                    if p.oracle_price_at_entry > 0 else 0
                    for k, v in p.markout_prices.items()
                },
            }
            for p in reversed(self._closed_positions[-limit:])
        ]

    def open_positions_summary(self) -> list[dict]:
        return [
            {
                "trade_id": p.trade_id,
                "asset": p.asset,
                "direction": p.direction,
                "entry_price": p.entry_price,
                "size_usd": p.size_usd,
                "p_true": round(p.p_true_at_entry, 3),
                "seconds_remaining": round(p.seconds_remaining, 0),
            }
            for p in self._positions.values()
        ]

    # --- CSV Logging ---

    def _log_to_csv(self, pos: PaperPosition, event: str) -> None:
        write_header = not self._csv_initialized or not self._csv_path.exists()
        try:
            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow([
                        "event", "trade_id", "timestamp", "asset", "direction",
                        "question", "entry_price", "size_usd", "fee_usd", "shares",
                        "p_true", "momentum_pct", "seconds_to_expiry",
                        "exit_price", "pnl_usd", "outcome_correct",
                    ])
                    self._csv_initialized = True
                writer.writerow([
                    event,
                    pos.trade_id,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    pos.asset,
                    pos.direction,
                    pos.market_question,
                    f"{pos.entry_price:.4f}",
                    f"{pos.size_usd:.2f}",
                    f"{pos.fee_usd:.4f}",
                    f"{pos.shares:.4f}",
                    f"{pos.p_true_at_entry:.4f}",
                    f"{pos.momentum_at_entry:.4f}",
                    f"{pos.seconds_to_expiry_at_entry:.1f}",
                    f"{pos.exit_price:.2f}" if pos.resolved else "",
                    f"{pos.pnl_usd:.4f}" if pos.resolved else "",
                    pos.outcome_correct if pos.resolved else "",
                ])
        except OSError as e:
            logger.warning(f"Paper Trade CSV-Logging Fehler: {e}")
