"""PreTrade Cost Calculator — Polymarket Gebühren + Binary Kelly Criterion.

Berechnet für jeden potenziellen Polymarket-Trade:
- Dynamische Taker-Gebühr (parabolische Kurve, max bei p=0.50)
- Erwartete Edge nach Gebühren
- Optimale Positionsgröße via Fractional Kelly
- ABORT-Signal wenn Net-EV ≤ 0
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from loguru import logger

from config import Settings


class TradeDecision(Enum):
    EXECUTE = "execute"
    ABORT = "abort"


@dataclass
class PreTradeResult:
    """Ergebnis der Pre-Trade-Analyse."""

    # --- Signal ---
    asset: str
    direction: str          # "UP" / "DOWN"
    p_true: float           # Unsere geschätzte Gewinnwahrscheinlichkeit
    p_market: float         # Polymarket-Preis (Markt-implied Probability)
    raw_edge_pct: float     # p_true - p_market (in %)

    # --- Kosten ---
    fee_pct: float          # Polymarket-Gebühr in %
    net_ev_pct: float       # Erwarteter Wert nach Gebühren in %

    # --- Sizing ---
    kelly_fraction: float   # Berechneter Kelly-Anteil (nach Capping)
    position_usd: float     # Empfohlene Positionsgröße in USD

    # --- Entscheidung ---
    decision: TradeDecision
    abort_reason: str = ""

    def __str__(self) -> str:
        if self.decision == TradeDecision.ABORT:
            return (
                f"ABORT {self.asset} {self.direction}: {self.abort_reason} "
                f"(edge={self.raw_edge_pct:.2f}%, fee={self.fee_pct:.2f}%, ev={self.net_ev_pct:.2f}%)"
            )
        return (
            f"EXECUTE {self.asset} {self.direction} @ {self.p_market:.3f}: "
            f"p_true={self.p_true:.3f}, edge={self.raw_edge_pct:.2f}%, "
            f"fee={self.fee_pct:.2f}%, ev={self.net_ev_pct:.2f}%, "
            f"size=${self.position_usd:.2f}"
        )


class PreTradeCalculator:
    """Berechnet ob und wie groß ein Polymarket-Trade sein soll."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # --- Gebühren ---

    def polymarket_fee_pct(self, price: float) -> float:
        """Dynamische Polymarket-Gebühr in %.

        Offizielle Formel: fee_pct = feeRate × (p × (1-p))^exponent × 100
        Für 15-Min Krypto-Märkte (aktuell): feeRate=0.50, exponent=2
        → Max 3.15% bei p=0.50, steilerer Abfall als einfache Parabel
        Bei p=0.50: 0.50 × 0.0625 × 100 = 3.125%
        Bei p=0.80: 0.50 × 0.0256 × 100 = 1.28%

        Wir nutzen polymarket_max_fee_pct als Peak-Gebühr und Exponent 2:
        fee = MAX_FEE × (4 × p × (1-p))^2
        """
        p = max(0.01, min(0.99, price))
        base = 4 * p * (1 - p)  # Normalisiert: 1.0 bei p=0.50
        return self.settings.polymarket_max_fee_pct * base * base  # Exponent 2

    # --- Wahrscheinlichkeitsschätzung ---

    def estimate_probability(
        self,
        momentum_pct: float,
        seconds_to_expiry: float,
        direction: str,
    ) -> float:
        """Schätzt Gewinnwahrscheinlichkeit aus Binance-Momentum.

        Args:
            momentum_pct: Binance-Preisänderung in % (+ = Preis gestiegen)
            seconds_to_expiry: Verbleibende Sekunden bis Marktablauf
            direction: "UP" oder "DOWN"

        Returns:
            Geschätzte Wahrscheinlichkeit (0–1)
        """
        if seconds_to_expiry <= 0:
            return 0.5  # Kein Signal bei abgelaufenem Markt

        # Zeit-Dämpfung: Signal ist stärker wenn weniger Zeit verbleibt
        # (in letzten 60s bleibt Momentum-Information vollständig relevant)
        time_factor = min(1.0, 120.0 / max(seconds_to_expiry, 1.0))
        effective_momentum = momentum_pct * time_factor

        # Logistische Funktion: Signal → Wahrscheinlichkeit
        # Kalibriert (konservativ):
        #   +0.15% → ~57%  (Schwellenwert)
        #   +0.30% → ~65%
        #   +0.50% → ~73%
        #   +1.00% → ~88%
        scaling = 2.0
        raw_signal = effective_momentum * scaling

        p_up = 1 / (1 + math.exp(-raw_signal))  # P(BTC steigt)

        if direction == "UP":
            return p_up
        else:  # DOWN
            return 1 - p_up

    # --- Kelly Criterion ---

    def kelly_fraction(self, p_true: float, p_market: float) -> float:
        """Fractional Kelly für Binary Prediction Markets.

        Für einen Kauf bei Preis p_market:
        - Gewinn wenn korrekt: (1 - p_market) USD pro USD eingesetzt
        - Verlust wenn falsch: p_market USD pro USD eingesetzt

        Kelly: f = (p_true - p_market) / (1 - p_market)
        (Abgeleitet aus b = (1-p)/p als Auszahlungs-Odds)
        """
        if p_market >= 1.0 or p_market <= 0.0:
            return 0.0

        raw_kelly = (p_true - p_market) / (1 - p_market)
        raw_kelly = max(0.0, raw_kelly)          # Kein negativer Einsatz
        raw_kelly = min(raw_kelly, 0.25)         # Hard cap bei 25% (Kelly-Limit)

        return raw_kelly * self.settings.kelly_fraction  # Fractional Kelly

    # --- Hauptfunktion ---

    def evaluate(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        polymarket_ask: float,
        seconds_to_expiry: float,
        available_capital_usd: float,
    ) -> PreTradeResult:
        """Vollständige Pre-Trade-Analyse. Gibt EXECUTE oder ABORT zurück.

        Args:
            asset: "BTC" / "ETH"
            direction: "UP" / "DOWN"
            momentum_pct: Binance-Momentum in % (letzten N Sekunden)
            polymarket_ask: Bester Ask-Preis auf Polymarket (0–1)
            seconds_to_expiry: Verbleibende Sekunden bis Marktablauf
            available_capital_usd: Verfügbares Kapital
        """

        def abort(reason: str) -> PreTradeResult:
            return PreTradeResult(
                asset=asset, direction=direction,
                p_true=0, p_market=polymarket_ask,
                raw_edge_pct=0, fee_pct=0, net_ev_pct=0,
                kelly_fraction=0, position_usd=0,
                decision=TradeDecision.ABORT,
                abort_reason=reason,
            )

        # --- Guard Checks ---
        if seconds_to_expiry < self.settings.min_seconds_to_expiry:
            return abort(f"Zu wenig Zeit bis Ablauf ({seconds_to_expiry:.0f}s < {self.settings.min_seconds_to_expiry}s)")

        if seconds_to_expiry > self.settings.max_seconds_to_expiry:
            return abort(f"Zu früh vor Ablauf ({seconds_to_expiry:.0f}s > {self.settings.max_seconds_to_expiry}s)")

        if polymarket_ask <= 0 or polymarket_ask >= 1:
            return abort(f"Ungültiger Polymarket-Preis: {polymarket_ask}")

        if available_capital_usd < 10:
            return abort("Kapital < $10")

        # --- Wahrscheinlichkeit schätzen ---
        p_true = self.estimate_probability(momentum_pct, seconds_to_expiry, direction)

        # --- Edge berechnen (ROI-Basis: % Rendite pro investiertem Dollar) ---
        raw_edge_pct = (p_true - polymarket_ask) / polymarket_ask * 100

        if raw_edge_pct <= 0:
            return abort(f"Keine Edge (p_true={p_true:.3f} ≤ p_market={polymarket_ask:.3f})")

        # --- Gebühren ---
        fee_pct = self.polymarket_fee_pct(polymarket_ask)

        # --- Net Expected Value (exakt) ---
        # Effektiver Ask = ask × (1 + fee/100)  (Fee wird auf Position aufgeschlagen)
        # Net ROI = p_true / effective_ask - 1   (Erwartete Rendite nach Gebühren)
        effective_ask = polymarket_ask * (1 + fee_pct / 100)
        net_ev_pct = (p_true / effective_ask - 1) * 100
        gross_ev_pct = raw_edge_pct  # Brutto-Edge = ROI ohne Gebühren

        if net_ev_pct <= self.settings.min_edge_pct:
            return abort(
                f"Net-EV {net_ev_pct:.2f}% < Mindest-Edge {self.settings.min_edge_pct}% "
                f"(gross={gross_ev_pct:.2f}%, fee={fee_pct:.2f}%)"
            )

        # --- Kelly Sizing ---
        kelly = self.kelly_fraction(p_true, polymarket_ask)

        if kelly <= 0:
            return abort("Kelly-Fraction = 0 (keine profitable Wette)")

        # Positionsgröße
        max_by_kelly = available_capital_usd * kelly
        max_by_pct = available_capital_usd * self.settings.max_position_pct
        position_usd = min(max_by_kelly, max_by_pct)

        if position_usd < 1.0:
            return abort(f"Positionsgröße zu klein: ${position_usd:.2f}")

        result = PreTradeResult(
            asset=asset,
            direction=direction,
            p_true=p_true,
            p_market=polymarket_ask,
            raw_edge_pct=raw_edge_pct,
            fee_pct=fee_pct,
            net_ev_pct=net_ev_pct,
            kelly_fraction=kelly,
            position_usd=round(position_usd, 2),
            decision=TradeDecision.EXECUTE,
        )

        logger.info(f"PreTrade: {result}")
        return result
