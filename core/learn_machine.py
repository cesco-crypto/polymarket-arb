"""Timing Learn Machine — Datengetriebenes FIRE/SKIP System.

Phase 1: Sammlung (200 Bursts) — alles loggen, nichts ändern
Phase 2: Training — Logistic Regression, Shadow-Predictions loggen
Phase 3: Shadow Mode — Modell empfiehlt, Bot ignoriert, Vergleich
Phase 4: Live — Modell entscheidet 90%, 10% Exploration

Prinzip: Lieber kein Trade als falscher Trade.
Kein Code im Hot-Path. Nur Logging + Analyse.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

BURST_LOG = Path("data/burst_log.jsonl")
DAILY_REPORT_HOUR = 22  # UTC


class LearnMachine:
    """FIRE/SKIP Lernmaschine fuer Oracle Delay Arbitrage."""

    def __init__(self):
        self._burst_count = 0
        self._daily_pnl = 0.0
        self._daily_win_pnl = 0.0   # Brutto-Gewinne (nur positive PnL)
        self._daily_loss_pnl = 0.0  # Brutto-Verluste (nur negative PnL, als negativer Wert)
        self._daily_wins = 0
        self._daily_losses = 0
        self._daily_fires = 0
        self._daily_skips = 0
        self._daily_cap_exceeded = 0
        self._daily_no_liq = 0
        self._daily_fills = 0
        self._consecutive_losses = 0
        self._skip_only_until = 0.0  # Unix timestamp bis wann Skip-Only
        self._last_report_day = ""
        self._phase = 1  # 1=Sammlung, 2=Training, 3=Shadow, 4=Live

        # Lade bestehende Burst-Daten fuer Zähler
        self._load_burst_count()

    def _load_burst_count(self):
        """Zaehle bestehende Eintraege in burst_log.jsonl."""
        try:
            if BURST_LOG.exists():
                with open(BURST_LOG) as f:
                    self._burst_count = sum(1 for line in f if line.strip())
                logger.info(f"LearnMachine: {self._burst_count} bestehende Bursts geladen (Phase {self._phase})")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════
    # GUARDRAILS
    # ══════════════════════════════════════════════════════

    def should_skip(self) -> tuple[bool, str]:
        """Pruefe ob Guardrails einen Skip erzwingen.

        Returns: (should_skip, reason)
        """
        now = time.time()

        # Skip-Only Modus aktiv?
        if now < self._skip_only_until:
            remaining = int(self._skip_only_until - now)
            return True, f"skip_only_mode ({remaining}s remaining)"

        # Tagesverlust > $25?
        if self._daily_pnl < -25.0:
            self._skip_only_until = now + 6 * 3600  # 6h Pause
            return True, f"daily_loss_limit (${self._daily_pnl:.2f})"

        # 5 konsekutive Losses?
        if self._consecutive_losses >= 5:
            self._skip_only_until = now + 3600  # 1h Pause
            self._consecutive_losses = 0  # Reset nach Pause
            return True, f"consecutive_losses (5)"

        return False, ""

    # ══════════════════════════════════════════════════════
    # RESOLVE CALLBACK (vom Redeemer)
    # ══════════════════════════════════════════════════════

    def on_resolve(self, trade_id: str, pnl_usd: float, is_win: bool) -> None:
        """Callback vom Redeemer — aktualisiert Live-Counter sofort.

        Wird direkt aus _log_redemption_to_journal() aufgerufen.
        is_win basiert auf pnl_usd > 0 (oekonomisches Ergebnis).
        """
        # Live-Counter sofort aktualisieren
        self._daily_pnl += pnl_usd
        self._daily_fills += 1

        if is_win:
            self._daily_wins += 1
            self._daily_win_pnl += pnl_usd
            self._consecutive_losses = 0
        else:
            self._daily_losses += 1
            self._daily_loss_pnl += pnl_usd  # Negativer Wert
            self._consecutive_losses += 1

        # Guardrail: Loss-Streak Warning bei 3
        if self._consecutive_losses == 3:
            self._send_loss_streak_warning()

        # RESOLVE in burst_log.jsonl fuer Offline-Analyse
        entry = {
            "ts": round(time.time(), 3),
            "event": "RESOLVE",
            "trade_id": trade_id,
            "pnl_usd": round(pnl_usd, 4),
            "is_win": is_win,
            "daily_pnl": round(self._daily_pnl, 4),
            "consecutive_losses": self._consecutive_losses,
        }
        try:
            BURST_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(BURST_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

        logger.info(
            f"LearnMachine RESOLVE: {trade_id} {'WIN' if is_win else 'LOSS'} "
            f"${pnl_usd:+.2f} | daily=${self._daily_pnl:+.2f} "
            f"streak={self._consecutive_losses}"
        )

    # ══════════════════════════════════════════════════════
    # BURST LOGGING
    # ══════════════════════════════════════════════════════

    def log_burst(
        self,
        slug: str,
        asset: str,
        action: str,  # "FIRE" oder "SKIP"
        abs_delta: float = 0.0,
        tick_age_ms: float = 0.0,
        best_ask: float = 0.0,
        spread_pct: float = 0.0,
        outcome: str = "",  # CAP_EXCEEDED, NO_LIQUIDITY, FILLED_WIN, FILLED_LOSS
        pnl_usd: float = 0.0,
        fill_price: float = 0.0,
        skip_reason: str = "",
    ) -> None:
        """Logge einen Burst mit Features + Outcome in burst_log.jsonl."""
        now = time.time()
        is_15m = "15m" in slug
        hour_utc = datetime.fromtimestamp(now, tz=timezone.utc).hour

        entry = {
            "ts": round(now, 3),
            "slug": slug,
            "asset": asset,
            "is_15m": is_15m,
            "abs_delta": round(abs_delta, 4),
            "tick_age_ms": round(tick_age_ms, 1),
            "best_ask": round(best_ask, 3),
            "spread_pct": round(spread_pct, 2),
            "hour_utc": hour_utc,
            "action": action,
            "outcome": outcome,
            "pnl_usd": round(pnl_usd, 4),
            "fill_price": round(fill_price, 3),
            "skip_reason": skip_reason,
            "phase": self._phase,
        }

        try:
            BURST_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(BURST_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"LearnMachine: Burst log write error: {e}")

        self._burst_count += 1

        # Update daily stats
        # WICHTIG: _daily_fills, _daily_wins, _daily_losses, _daily_pnl
        # werden NICHT hier gezaehlt — nur in on_resolve() beim RESOLVE-Callback.
        # log_burst() zaehlt nur FIRE/SKIP und strukturelle Outcomes (CAP, NO_LIQ).
        if action == "FIRE":
            self._daily_fires += 1
            if outcome == "CAP_EXCEEDED":
                self._daily_cap_exceeded += 1
            elif outcome == "NO_LIQUIDITY":
                self._daily_no_liq += 1
            # FILLED: wird hier nur geloggt, Fill/Win/Loss-Counter kommen via on_resolve()
        elif action == "SKIP":
            self._daily_skips += 1

    # ══════════════════════════════════════════════════════
    # REPORTS
    # ══════════════════════════════════════════════════════

    def get_terminal_report(self) -> str:
        """Kompakter Terminal-Report (alle 50 Bursts)."""
        total = self._daily_fires + self._daily_skips
        if total == 0:
            return ""

        p_fill = self._daily_fills / max(1, self._daily_fires) * 100
        p_win = self._daily_wins / max(1, self._daily_fills) * 100
        ev = self._daily_pnl / max(1, total)

        lines = [
            f"{'═'*50}",
            f"TIMING LEARN REPORT (Phase {self._phase}, N={self._burst_count})",
            f"{'═'*50}",
            f"Windows: {total} | Fired: {self._daily_fires} | Skipped: {self._daily_skips}",
            f"",
            f"FIRE Outcomes:",
            f"  ⬜ CAP_EXCEEDED:  {self._daily_cap_exceeded}",
            f"  🔲 NO_LIQUIDITY:  {self._daily_no_liq}",
            f"  ✅ WIN:  {self._daily_wins}  (+${self._daily_win_pnl:.2f})" if self._daily_wins else f"  ✅ WIN:  0",
            f"  ❌ LOSS: {self._daily_losses}  (${self._daily_loss_pnl:.2f})" if self._daily_losses else f"  ❌ LOSS: 0",
            f"",
            f"P(fill|fire): {p_fill:.0f}% | P(win|fill): {p_win:.0f}%",
            f"EV/Burst: ${ev:.2f} | Net PnL: ${self._daily_pnl:.2f}",
            f"Phase: {self._phase} | {self._burst_count}/200 Bursts",
            f"{'═'*50}",
        ]
        return "\n".join(lines)

    async def send_daily_telegram(self) -> None:
        """Taegliche Telegram-Zusammenfassung."""
        from utils import telegram

        total = self._daily_fires + self._daily_skips
        if total == 0:
            return

        p_fill = self._daily_fills / max(1, self._daily_fires) * 100
        p_win = self._daily_wins / max(1, self._daily_fills) * 100
        ev = self._daily_pnl / max(1, total)

        # Gruen wenn positiv, Rot wenn negativ
        if self._daily_pnl >= 0:
            header = "🟢 DAILY REPORT ✅"
        else:
            header = "🔴 DAILY REPORT ⚠️"

        msg = (
            f"<b>{header}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 Windows: {total}\n"
            f"🎯 Fired: {self._daily_fires} → Filled: {self._daily_fills} ({p_fill:.0f}%)\n"
            f"  ✅ WIN:  {self._daily_wins}  (+${self._daily_win_pnl:.2f})\n"
            f"  ❌ LOSS: {self._daily_losses}  (${self._daily_loss_pnl:.2f})\n"
            f"  ⬜ CAP:  {self._daily_cap_exceeded} | 🔲 NoLiq: {self._daily_no_liq}\n"
            f"🚫 Skipped: {self._daily_skips}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Net PnL: ${self._daily_pnl:+.2f}\n"
            f"📈 P(fill): {p_fill:.0f}% | P(win|fill): {p_win:.0f}%\n"
            f"📐 EV/Burst: ${ev:+.2f}\n"
            f"🔬 Phase: {self._phase} | {self._burst_count} total Bursts"
        )

        await telegram.send_alert(msg)

        # Reset daily stats
        self._reset_daily()

    def _reset_daily(self):
        self._daily_pnl = 0.0
        self._daily_win_pnl = 0.0
        self._daily_loss_pnl = 0.0
        self._daily_wins = 0
        self._daily_losses = 0
        self._daily_fires = 0
        self._daily_skips = 0
        self._daily_cap_exceeded = 0
        self._daily_no_liq = 0
        self._daily_fills = 0

    def _send_loss_streak_warning(self):
        """Telegram-Warnung bei 3 konsekutiven Losses."""
        import asyncio
        from utils import telegram

        msg = (
            f"<b>🔴🔴🔴 LOSS STREAK WARNING</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"❌ {self._consecutive_losses} konsekutive Losses\n"
            f"💸 Tages-PnL: ${self._daily_pnl:+.2f}\n"
            f"⚡ Guardrail: {5 - self._consecutive_losses} weitere → Skip-Only 1h"
        )

        try:
            loop = asyncio.get_event_loop()
            loop.create_task(telegram.send_alert(msg))
        except Exception:
            pass

    async def check_daily_report(self) -> None:
        """Pruefe ob es Zeit fuer den Daily Report ist (22:00 UTC)."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        if now.hour == DAILY_REPORT_HOUR and today != self._last_report_day:
            self._last_report_day = today
            await self.send_daily_telegram()

    def get_phase_info(self) -> str:
        """Kurzer Status fuer Logs."""
        return f"Phase {self._phase} | {self._burst_count} Bursts"
