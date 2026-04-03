"""HMSF Decision Engine — Das Gehirn des Multi-Strategy Frameworks.

Zentrale Steuerungseinheit die bei JEDEM Binance-Tick:
1. STRATEGY_CONFIG ausliest (welche Module aktiv?)
2. Globale Filter prüft (Regime, Risiko)
3. Alle aktiven Module abfragt
4. Konflikt-Resolution durchführt (Modul 2 vs 3 Exklusion)
5. Die endgültige Order-Entscheidung generiert

Logik-Regeln:
- Modul 2 + 3 schliessen sich gegenseitig aus (nie beide für gleichen Token)
- Konservativ-Modus: Min 1 primäres Modul + globale Bestätigung, ODER Modul 3 autark
- Aggressive-Modus: Ein primäres Modul reicht, Filter relaxter
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from strategies.hmsf_core import (
    hmsf_config,
    global_risk_filter,
    regime_detector,
    anti_whipsaw,
    OpenPosition,
)
from strategies.hmsf_modules import (
    LegacyMomentumModule,
    EnhancedLatencyModule,
    LastSecondsSnipingModule,
    BothSidesArbModule,
    TradeSignal,
    ArbSignal,
    WindowData,
)


# ═══════════════════════════════════════════════════════════════════════
# Decision Result
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class DecisionResult:
    """Das Ergebnis der Decision Engine für einen Tick."""
    action: str = "HOLD"         # "HOLD" / "TRADE" / "ARB" / "EXIT"
    signals: list = field(default_factory=list)  # Liste von TradeSignal
    arb_signal: Optional[ArbSignal] = None       # Für Both-Sides-Arb
    exit_signals: list = field(default_factory=list)  # Positionen zum Schliessen
    source_modules: list = field(default_factory=list)  # Welche Module gefeuert haben
    reason: str = ""
    timestamp: float = 0.0


# ═══════════════════════════════════════════════════════════════════════
# Decision Engine
# ═══════════════════════════════════════════════════════════════════════

class DecisionEngine:
    """Zentrales Gehirn — verknüpft alle Module und Filter.

    Wird bei JEDEM Binance-Tick aufgerufen.
    Entscheidet ob getradet wird, welches Modul, und wie.
    """

    def __init__(self, settings) -> None:
        self.settings = settings

        # Module initialisieren
        self.mod0_legacy = LegacyMomentumModule(settings)
        self.mod1_enhanced = EnhancedLatencyModule(settings)
        self.mod2_snipe = LastSecondsSnipingModule(settings)
        self.mod3_arb = BothSidesArbModule(settings)

        # Tracking
        self._decisions_made: int = 0
        self._trades_triggered: int = 0
        self._arbs_triggered: int = 0
        self._exits_triggered: int = 0

    # ─────────────────────────────────────────────────────────
    # MAIN ENTRY POINT: Wird bei jedem Tick aufgerufen
    # ─────────────────────────────────────────────────────────

    def evaluate_tick(
        self,
        *,
        asset: str,
        direction: str,
        momentum_pct: float,
        binance_price: float,
        window: WindowData,
        calculator,
        capital_usd: float,
        transit_latency_ms: float = 0,
        open_positions: list[OpenPosition] | None = None,
    ) -> DecisionResult:
        """Hauptmethode: Evaluiert einen Tick durch alle Module.

        Args:
            asset: "BTC" / "ETH"
            direction: "UP" / "DOWN" (vom Momentum bestimmt)
            momentum_pct: Binance-Momentum in %
            binance_price: Aktueller Binance-Preis
            window: Aufbereitetes Polymarket-Fenster
            calculator: PreTradeCalculator Instanz
            capital_usd: Verfügbares Kapital
            transit_latency_ms: Binance→Bot Latenz
            open_positions: Offene Positionen für Exit-Check

        Returns:
            DecisionResult mit Aktion und Signalen
        """
        self._decisions_made += 1
        result = DecisionResult(timestamp=time.time())

        # ── Regime-Detektor aktualisieren ──
        regime_detector.record_price(asset, binance_price)
        if self._decisions_made % 100 == 0:  # Nicht bei jedem Tick (Performance)
            regime_detector.update()

        # ── Phase 1: Anti-Whipsaw Exit-Check — DEAKTIVIERT ──
        # STRESSTEST FIX (04.04.2026): Stop-Loss bei 5-Min Binary Options
        # ist mathematisch nutzlos (EV Verkaufen = EV Halten) und der
        # Executor hat keinen SELL. Code bleibt erhalten für spätere
        # SELL-Implementierung. Hold-to-Resolution ist strikt besser.
        # Siehe: Microstructure Agent + Liquidity Agent Stresstest-Ergebnisse.

        # ── Phase 2: Modul 3 (Both-Sides-Arb) — prüfe IMMER, unabhängig von Momentum ──
        arb_signal = self.mod3_arb.evaluate(
            asset=asset,
            window=window,
            capital_usd=capital_usd,
        )

        if arb_signal:
            result.action = "ARB"
            result.arb_signal = arb_signal
            result.source_modules = ["both_sides_arb"]
            result.reason = (
                f"ARB: {asset} UP@{arb_signal.up_ask_price:.3f} + DOWN@{arb_signal.down_ask_price:.3f} "
                f"= {arb_signal.combined_cost:.3f} → Profit {arb_signal.guaranteed_profit_pct:.2f}%"
            )
            self._arbs_triggered += 1
            logger.info(f"DECISION: {result.reason}")
            return result

        # ── Phase 3: Primäre Module evaluieren (0, 1, 2) ──
        signals: list[TradeSignal] = []

        # Modul 0: Legacy Momentum
        sig0 = self.mod0_legacy.evaluate(
            asset=asset, direction=direction, momentum_pct=momentum_pct,
            binance_price=binance_price, window=window,
            calculator=calculator, capital_usd=capital_usd,
            transit_latency_ms=transit_latency_ms,
        )
        if sig0:
            signals.append(sig0)

        # Modul 1: Enhanced Latency
        sig1 = self.mod1_enhanced.evaluate(
            asset=asset, direction=direction, momentum_pct=momentum_pct,
            binance_price=binance_price, window=window,
            calculator=calculator, capital_usd=capital_usd,
            transit_latency_ms=transit_latency_ms,
        )
        if sig1:
            signals.append(sig1)

        # Modul 2: Last-Seconds-Sniping
        # WICHTIG: Modul 2 und 3 schliessen sich aus.
        # Da Modul 3 (Arb) oben schon geprüft wurde und NICHT getriggert hat,
        # darf Modul 2 jetzt feuern.
        sig2 = self.mod2_snipe.evaluate(
            asset=asset, direction=direction, momentum_pct=momentum_pct,
            binance_price=binance_price, window=window,
            calculator=calculator, capital_usd=capital_usd,
            transit_latency_ms=transit_latency_ms,
        )
        if sig2:
            signals.append(sig2)

        # ── Phase 4: Decision Logic ──
        if not signals:
            result.action = "HOLD"
            result.reason = "Kein Modul hat gefeuert"
            return result

        is_conservative = hmsf_config.get("conservative_mode", True)
        is_aggressive = hmsf_config.get("aggressive_mode", False)

        if is_aggressive:
            # ── AGGRESSIVE: Ein Modul reicht ──
            # Wähle das Signal mit dem höchsten Net-EV
            best_signal = max(signals, key=lambda s: s.net_ev_pct)
            result.action = "TRADE"
            result.signals = [best_signal]
            result.source_modules = [best_signal.module]
            result.reason = (
                f"AGGRESSIVE: {best_signal.module} | {asset} {best_signal.direction} "
                f"@ {best_signal.ask_price:.3f} | EV: {best_signal.net_ev_pct:+.2f}%"
            )

        elif is_conservative:
            # ── KONSERVATIV: Braucht Bestätigung ──
            # Option A: Mindestens EIN primäres Modul (0, 1, oder 2) feuert
            #           UND globale Filter bestätigen
            # Option B: Mehrere Module feuern gleichzeitig (stärkere Konfidenz)

            primary_signals = [s for s in signals if s.module in (
                "legacy_momentum", "enhanced_latency", "last_seconds_sniping"
            )]

            if not primary_signals:
                result.action = "HOLD"
                result.reason = "Konservativ: Kein primäres Modul hat gefeuert"
                return result

            # Im konservativen Modus: Wähle das Signal mit dem höchsten Net-EV
            best_signal = max(primary_signals, key=lambda s: s.net_ev_pct)

            # Globaler HMSF-Filter (nur für Legacy und Enhanced, nicht für Snipe)
            if best_signal.module != "last_seconds_sniping":
                filter_result = global_risk_filter.check(
                    capital_usd=capital_usd,
                    size_usd=best_signal.position_usd,
                    net_ev_pct=best_signal.net_ev_pct,
                    fee_pct=best_signal.fee_pct,
                    spread_pct=best_signal.spread_pct,
                    liquidity_usd=best_signal.liquidity_usd,
                    transit_latency_ms=transit_latency_ms,
                    shares=best_signal.shares,
                    asset=asset,
                    direction=best_signal.direction,
                )

                if not filter_result.passed:
                    result.action = "HOLD"
                    result.reason = f"Konservativ: HMSF Filter blockiert — {filter_result.reason}"
                    return result

                # Size ggf. anpassen (Quarter-Kelly)
                best_signal.position_usd = filter_result.adjusted_size_usd
                if best_signal.ask_price > 0:
                    best_signal.shares = round(filter_result.adjusted_size_usd / best_signal.ask_price, 2)

            # Confluence-Bonus loggen
            # STRESSTEST FIX (04.04.2026): M0+M1 zählen als EINE Familie
            # (gleicher Binance-Momentum Stamm, nicht unabhängig)
            module_names = [s.module for s in signals]
            MOMENTUM_FAMILY = {"legacy_momentum", "enhanced_latency"}
            unique_families = set()
            for s in signals:
                if s.module in MOMENTUM_FAMILY:
                    unique_families.add("momentum_family")
                else:
                    unique_families.add(s.module)
            confluence = len(unique_families)

            result.action = "TRADE"
            result.signals = [best_signal]
            result.source_modules = module_names
            result.reason = (
                f"KONSERVATIV: {best_signal.module} | {asset} {best_signal.direction} "
                f"@ {best_signal.ask_price:.3f} | EV: {best_signal.net_ev_pct:+.2f}% | "
                f"Confluence: {confluence} Familien ({', '.join(module_names)})"
            )

        else:
            # Weder konservativ noch aggressiv — Fallback auf konservativ
            result.action = "HOLD"
            result.reason = "Kein Modus aktiv"
            return result

        self._trades_triggered += 1
        logger.info(f"DECISION: {result.reason}")
        return result

    # ─────────────────────────────────────────────────────────
    # Helper
    # ─────────────────────────────────────────────────────────

    def _get_position_prices(self, direction: str, window: WindowData) -> tuple[float, float]:
        """Gibt (bid, ask) für eine Richtung zurück."""
        if direction == "UP":
            return window.up_best_bid, window.up_best_ask
        else:
            return window.down_best_bid, window.down_best_ask

    def discard_window(self, slug: str, direction: str) -> None:
        """Entfernt ein Fenster aus ALLEN Module-Trackings."""
        self.mod0_legacy.discard_window(slug, direction)
        self.mod1_enhanced.discard_window(slug, direction)
        self.mod2_snipe.discard_window(slug, direction)
        self.mod3_arb.discard_window(slug)

    def get_status(self) -> dict:
        """Status für Dashboard / Telegram."""
        return {
            "decisions_made": self._decisions_made,
            "trades_triggered": self._trades_triggered,
            "arbs_triggered": self._arbs_triggered,
            "exits_triggered": self._exits_triggered,
            "modules": {
                "legacy_momentum": self.mod0_legacy.is_enabled(),
                "enhanced_latency": self.mod1_enhanced.is_enabled(),
                "last_seconds_sniping": self.mod2_snipe.is_enabled(),
                "both_sides_arb": self.mod3_arb.is_enabled(),
            },
            "mode": "aggressive" if hmsf_config.get("aggressive_mode") else "conservative",
            "regime": regime_detector.get_status(),
            "config": hmsf_config.get_all(),
        }
