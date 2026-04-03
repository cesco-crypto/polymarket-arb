"""HMSF Strategy Modules — 4 Module für Multi-Strategy Trading.

Modul 0: Legacy Momentum Strategy
    Exakte Kopie der bestehenden v2.0 Logik. ISOLIERT, UNVERÄNDERT.
    Trigger: Binance-Momentum >= 0.05% in 15s + 9 Basis-Filter.

Modul 1: Enhanced Latency-Momentum
    Smartere Version mit 4 gleichzeitigen Triggern:
    Momentum + Price-Gap + Orderbook-Imbalance + Ask 0.35-0.65.

Modul 2: Last-Seconds-Sniping
    Hoch-Wahrscheinlichkeits-Trades nahe Expiry (15-60s).
    Eine Seite bei >= 0.72, strenge Imbalance >= 15%.
    Max 1.2% Kapital, KEIN Early-Exit.

Modul 3: Both-Sides-Arbitrage (Delta Neutral)
    Kaufe BEIDE Seiten wenn UP+DOWN < $0.98 (garantierter Profit).
    Risikofrei wenn beide Seiten gefüllt werden.

Alle Module nutzen das HMSF-Fundament (hmsf_core.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from strategies.hmsf_core import (
    hmsf_config,
    global_risk_filter,
    regime_detector,
    FilterResult,
)


# ═══════════════════════════════════════════════════════════════════════
# Shared Data Types
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TradeSignal:
    """Ein generiertes Trade-Signal von einem Modul."""
    module: str              # "legacy_momentum" / "enhanced_latency"
    asset: str               # "BTC" / "ETH"
    direction: str           # "UP" / "DOWN"
    momentum_pct: float
    ask_price: float
    bid_price: float
    binance_price: float
    seconds_to_expiry: float
    window_slug: str
    market_question: str
    timeframe: str

    # Modul-spezifische Metriken
    net_ev_pct: float = 0.0
    fee_pct: float = 0.0
    p_true: float = 0.0
    kelly_fraction: float = 0.0
    position_usd: float = 0.0
    spread_pct: float = 0.0
    liquidity_usd: float = 0.0
    transit_latency_ms: float = 0.0

    # Enhanced-spezifisch
    price_gap_pct: float = 0.0         # Abstand zum Price-to-Beat
    orderbook_imbalance_pct: float = 0.0  # Bid/Ask Volume Imbalance
    confidence_tag: str = ""           # "legacy" / "enhanced"

    # Timestamps
    signal_time: float = 0.0
    shares: float = 0.0


@dataclass
class WindowData:
    """Aufbereitete Fenster-Daten die beide Module nutzen."""
    slug: str
    asset: str
    timeframe: str
    condition_id: str
    question: str
    window_end_ts: float
    seconds_to_expiry: float
    up_best_bid: float
    up_best_ask: float
    down_best_bid: float
    down_best_ask: float
    up_bid_size: float
    up_ask_size: float
    up_token_id: str
    down_token_id: str
    liquidity_usd: float
    orderbook_fresh: bool
    orderbook_ts: float


# ═══════════════════════════════════════════════════════════════════════
# MODUL 0: Legacy Momentum Strategy (UNBERÜHRT, ISOLIERT)
# ═══════════════════════════════════════════════════════════════════════

class LegacyMomentumModule:
    """Exakte Kopie der bestehenden v2.0 Momentum-Logik.

    WICHTIG: Diese Klasse darf NICHT verändert werden.
    Sie ist ein isolierter, unberührter Code-Pfad der die
    originale Strategie 1:1 abbildet.

    Die 9 Basis-Filter (identisch zu polymarket_latency.py):
    1. Tick frisch (max 5s)
    2. Transit < 300ms
    3. |Momentum| >= 0.05%
    4. 15s <= Expiry <= 180s
    5. Orderbook frisch
    6. Ask 0.35-0.65 (Price-First)
    7. Spread < 3%
    8. Participation < 1% der Liquidität
    9. Duplicate Prevention (1x pro Fenster/Richtung)
    """

    MODULE_NAME = "legacy_momentum"
    CONFIG_KEY = "legacy_momentum_enabled"

    def __init__(self, settings) -> None:
        self.settings = settings
        self._traded_windows: set = set()

    def is_enabled(self) -> bool:
        return hmsf_config.is_strategy_enabled(self.CONFIG_KEY)

    def reset_window_tracking(self) -> None:
        """Wird aufgerufen wenn ein Fenster abläuft."""
        pass  # _traded_windows wird per discard in der Strategie bereinigt

    def evaluate(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        binance_price: float,
        window: WindowData,
        calculator,
        capital_usd: float,
        transit_latency_ms: float = 0,
    ) -> Optional[TradeSignal]:
        """Evaluiert ein Fenster mit der ORIGINALEN v2.0 Logik.

        Returns: TradeSignal wenn Trade ausgelöst, None sonst.
        """
        if not self.is_enabled():
            return None

        seconds_to_expiry = window.seconds_to_expiry

        # === FILTER 4: Zeitfenster ===
        if not (self.settings.min_seconds_to_expiry <= seconds_to_expiry <= self.settings.max_seconds_to_expiry):
            return None

        # === FILTER 5: Orderbook frisch ===
        if not window.orderbook_fresh:
            return None

        # === Ask-Preis bestimmen ===
        if direction == "UP":
            ask_price = window.up_best_ask
            bid_price = window.up_best_bid
        else:
            ask_price = window.down_best_ask
            bid_price = window.down_best_bid

        if ask_price <= 0 or ask_price >= 1:
            return None

        # === FILTER 6: Price-First (0.35-0.65) ===
        if abs(ask_price - 0.50) > 0.15:
            return None

        # === FILTER 7: Spread < 3% ===
        spread_pct = 0.0
        if bid_price > 0 and ask_price > 0:
            spread_pct = (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 100
            if spread_pct > 3.0:
                return None

        # === FILTER 8: Participation < 1% ===
        market_liq = window.liquidity_usd
        if market_liq > 0:
            max_order = self.settings.max_live_position_usd
            if max_order > market_liq * 0.01:
                return None

        # === FILTER 9: Duplicate Prevention ===
        trade_key = f"{window.slug}_{direction}"
        if trade_key in self._traded_windows:
            return None

        # === PreTrade-Analyse (identisch zu v2.0) ===
        result = calculator.evaluate(
            asset=asset,
            direction=direction,
            momentum_pct=momentum_pct,
            polymarket_ask=ask_price,
            seconds_to_expiry=seconds_to_expiry,
            available_capital_usd=capital_usd,
        )

        if result.decision.value != "EXECUTE":
            return None

        # Duplicate markieren
        self._traded_windows.add(trade_key)

        # Shares berechnen
        shares = result.position_usd / ask_price if ask_price > 0 else 0

        return TradeSignal(
            module=self.MODULE_NAME,
            asset=asset,
            direction=direction,
            momentum_pct=momentum_pct,
            ask_price=ask_price,
            bid_price=bid_price,
            binance_price=binance_price,
            seconds_to_expiry=seconds_to_expiry,
            window_slug=window.slug,
            market_question=window.question,
            timeframe=window.timeframe,
            net_ev_pct=result.net_ev_pct,
            fee_pct=result.fee_pct,
            p_true=result.p_true,
            kelly_fraction=result.kelly_fraction,
            position_usd=result.position_usd,
            spread_pct=round(spread_pct, 2),
            liquidity_usd=market_liq,
            transit_latency_ms=transit_latency_ms,
            confidence_tag="legacy",
            signal_time=time.time(),
            shares=round(shares, 2),
        )

    def discard_window(self, slug: str, direction: str) -> None:
        """Entfernt ein Fenster aus dem Duplicate-Tracking."""
        self._traded_windows.discard(f"{slug}_{direction}")


# ═══════════════════════════════════════════════════════════════════════
# MODUL 1: Enhanced Latency-Momentum
# ═══════════════════════════════════════════════════════════════════════

class EnhancedLatencyModule:
    """Smartere Momentum-Strategie mit zusätzlichen Confirmations.

    ALLE folgenden Trigger müssen GLEICHZEITIG erfüllt sein:
    1. Binance Momentum >= 0.05% in 15s
    2. Price-to-Beat Gap >= 0.08% in gleiche Richtung
    3. Orderbook-Imbalance >= +12% in gleiche Richtung
    4. Ask-Preis zwischen 0.35 und 0.65

    Plus: HMSF globale Filter (Quarter-Kelly, Fee-Check, Liquidity, Spread, Latency)
    Plus: Regime-Malus (ETH UP in Downtrend: -30% Net-EV)
    """

    MODULE_NAME = "enhanced_latency"
    CONFIG_KEY = "enhanced_latency_momentum_enabled"

    # Modul-spezifische Schwellenwerte
    MIN_PRICE_GAP_PCT = 0.08     # Mindest-Abstand zum Price-to-Beat
    MIN_OB_IMBALANCE_PCT = 12.0  # Mindest-Orderbook-Imbalance
    MIN_ASK = 0.35
    MAX_ASK = 0.65

    def __init__(self, settings) -> None:
        self.settings = settings
        self._traded_windows: set = set()

    def is_enabled(self) -> bool:
        return hmsf_config.is_strategy_enabled(self.CONFIG_KEY)

    def evaluate(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        binance_price: float,
        window: WindowData,
        calculator,
        capital_usd: float,
        transit_latency_ms: float = 0,
    ) -> Optional[TradeSignal]:
        """Evaluiert ein Fenster mit der Enhanced Latency-Logik.

        Returns: TradeSignal wenn ALLE Bedingungen erfüllt, None sonst.
        """
        if not self.is_enabled():
            return None

        seconds_to_expiry = window.seconds_to_expiry

        # === Zeitfenster ===
        if not (self.settings.min_seconds_to_expiry <= seconds_to_expiry <= self.settings.max_seconds_to_expiry):
            return None

        if not window.orderbook_fresh:
            return None

        # === Ask/Bid bestimmen ===
        if direction == "UP":
            ask_price = window.up_best_ask
            bid_price = window.up_best_bid
            bid_size = window.up_bid_size
            ask_size = window.up_ask_size
        else:
            ask_price = window.down_best_ask
            bid_price = window.down_best_bid
            # DOWN-Seite: nutze UP bid/ask sizes als Proxy (invertiert)
            bid_size = window.up_ask_size  # Wer UP verkauft = DOWN-Käufer
            ask_size = window.up_bid_size  # Wer UP kauft = DOWN-Verkäufer

        if ask_price <= 0 or ask_price >= 1:
            return None

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 1: Binance Momentum >= 0.05% in 15s           │
        # │  (bereits geprüft bevor dieses Modul aufgerufen wird)  │
        # └─────────────────────────────────────────────────────────┘

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 2: Price-to-Beat Gap >= 0.08%                 │
        # │  Der Polymarket-Preis muss HINTER dem Binance-Signal   │
        # │  zurückliegen — das ist unsere Latency Edge.           │
        # └─────────────────────────────────────────────────────────┘

        # Price-to-Beat: Wie weit ist der aktuelle Ask vom "fairen" Preis entfernt?
        # Wenn BTC steigt (UP Signal), sollte der UP-Ask noch niedrig sein (< 0.50).
        # Gap = Differenz zwischen dem was der Markt denkt und dem was Binance zeigt.
        #
        # Für UP: ask_price sollte < 0.50 sein → gap = (0.50 - ask_price) / 0.50
        #         Grösser = mehr Edge (Markt hat Binance-Move noch nicht eingepreist)
        # Für DOWN: ask_price sollte < 0.50 sein → gap = (0.50 - ask_price) / 0.50
        #
        # Einfacher: Der Gap ist proportional zum Abstand von 0.50 IN unserer Richtung.
        # Wir kaufen bei 0.42 → gap = (0.50 - 0.42) / 0.50 = 16% → GUT
        # Wir kaufen bei 0.48 → gap = (0.50 - 0.48) / 0.50 = 4%  → zu klein

        if ask_price <= 0.50:
            price_gap_pct = (0.50 - ask_price) / 0.50 * 100
        else:
            # Ask über 0.50 — der Markt hat möglicherweise schon repriced
            price_gap_pct = -((ask_price - 0.50) / 0.50 * 100)

        if price_gap_pct < self.MIN_PRICE_GAP_PCT:
            return None  # Zu wenig Preis-Gap → Markt hat schon repriced

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 3: Orderbook-Imbalance >= +12%                │
        # │  Bid-Volumen vs Ask-Volumen bestätigt die Richtung.    │
        # └─────────────────────────────────────────────────────────┘

        # Imbalance = (bid_vol / ask_vol) - 1
        # Positiv = mehr Käufer als Verkäufer (bullish)
        # Für UP: Imbalance sollte positiv sein (mehr Bids = Kaufdruck)
        # Für DOWN: Imbalance sollte NEGATIV sein (mehr Asks = Verkaufsdruck)

        if ask_size > 0 and bid_size > 0:
            raw_imbalance = (bid_size / ask_size - 1) * 100
        else:
            raw_imbalance = 0.0

        # Richtungsanpassung: Für DOWN invertieren wir das Vorzeichen
        if direction == "UP":
            directional_imbalance = raw_imbalance  # Positiv = gut für UP
        else:
            directional_imbalance = -raw_imbalance  # Negiert: Positiv = gut für DOWN

        if directional_imbalance < self.MIN_OB_IMBALANCE_PCT:
            return None  # Orderbook bestätigt die Richtung nicht

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 4: Ask-Preis 0.35 - 0.65                     │
        # └─────────────────────────────────────────────────────────┘

        if ask_price < self.MIN_ASK or ask_price > self.MAX_ASK:
            return None

        # === Duplicate Prevention ===
        trade_key = f"{window.slug}_{direction}_enhanced"
        if trade_key in self._traded_windows:
            return None

        # === PreTrade-Analyse ===
        result = calculator.evaluate(
            asset=asset,
            direction=direction,
            momentum_pct=momentum_pct,
            polymarket_ask=ask_price,
            seconds_to_expiry=seconds_to_expiry,
            available_capital_usd=capital_usd,
        )

        if result.decision.value != "EXECUTE":
            return None

        # === Regime-Anpassung (ETH UP Malus in Downtrend) ===
        adjusted_ev, regime_reason = regime_detector.apply_regime_adjustment(
            asset, direction, result.net_ev_pct
        )

        # === HMSF Globale Filter ===
        shares = result.position_usd / ask_price if ask_price > 0 else 0
        spread_pct = (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 100 if bid_price > 0 else 0

        filter_result = global_risk_filter.check(
            capital_usd=capital_usd,
            size_usd=result.position_usd,
            net_ev_pct=adjusted_ev,
            fee_pct=result.fee_pct,
            spread_pct=spread_pct,
            liquidity_usd=window.liquidity_usd,
            transit_latency_ms=transit_latency_ms,
            shares=shares,
            asset=asset,
            direction=direction,
        )

        if not filter_result.passed:
            logger.debug(f"Enhanced [{asset} {direction}]: HMSF Filter blockiert — {filter_result.reason}")
            return None

        # Duplicate markieren
        self._traded_windows.add(trade_key)

        return TradeSignal(
            module=self.MODULE_NAME,
            asset=asset,
            direction=direction,
            momentum_pct=momentum_pct,
            ask_price=ask_price,
            bid_price=bid_price,
            binance_price=binance_price,
            seconds_to_expiry=seconds_to_expiry,
            window_slug=window.slug,
            market_question=window.question,
            timeframe=window.timeframe,
            net_ev_pct=adjusted_ev,
            fee_pct=result.fee_pct,
            p_true=result.p_true,
            kelly_fraction=result.kelly_fraction,
            position_usd=filter_result.adjusted_size_usd,  # Quarter-Kelly adjusted
            spread_pct=round(spread_pct, 2),
            liquidity_usd=window.liquidity_usd,
            transit_latency_ms=transit_latency_ms,
            price_gap_pct=round(price_gap_pct, 2),
            orderbook_imbalance_pct=round(directional_imbalance, 2),
            confidence_tag="enhanced" + (f" ({regime_reason})" if regime_reason else ""),
            signal_time=time.time(),
            shares=round(filter_result.adjusted_size_usd / ask_price, 2) if ask_price > 0 else 0,
        )

    def discard_window(self, slug: str, direction: str) -> None:
        """Entfernt ein Fenster aus dem Duplicate-Tracking."""
        self._traded_windows.discard(f"{slug}_{direction}_enhanced")


# ═══════════════════════════════════════════════════════════════════════
# MODUL 2: Last-Seconds-Sniping
# ═══════════════════════════════════════════════════════════════════════

class LastSecondsSnipingModule:
    """Hoch-Wahrscheinlichkeits-Trades kurz vor Expiry.

    Logik: Kurz vor Ablauf (15-60s) ist die Richtung fast entschieden.
    Eine Seite steht bei >= 0.72 (Markt ist sich 72%+ sicher).
    Wir kaufen die teure Seite — niedrigerer Profit pro Trade,
    aber extrem hohe Win Rate.

    Trigger (ALLE gleichzeitig):
    1. 15-60 Sekunden bis Expiry
    2. Eine Seite Ask >= 0.72
    3. Price-to-Beat Gap >= 0.10%
    4. Orderbook-Imbalance >= +15% (strenger als Modul 1)

    Sonderregeln:
    - Max Position: 1.2% des Kapitals (statt 2.5%)
    - Early-Exit: KOMPLETT DEAKTIVIERT (Trade läuft bis Resolution)
    """

    MODULE_NAME = "last_seconds_sniping"
    CONFIG_KEY = "last_seconds_sniping_enabled"

    # Modul-spezifische Schwellenwerte
    MIN_EXPIRY_S = 15.0
    MAX_EXPIRY_S = 60.0
    MIN_ASK_PRICE = 0.72        # Mindest-Ask (Markt muss sich sicher sein)
    MIN_PRICE_GAP_PCT = 0.10    # Strenger als Modul 1
    MIN_OB_IMBALANCE_PCT = 15.0  # Strenger als Modul 1
    MAX_RISK_PCT = 1.2           # Nur 1.2% des Kapitals (konservativ)
    EARLY_EXIT_DISABLED = True   # KEIN Anti-Whipsaw für dieses Modul

    def __init__(self, settings) -> None:
        self.settings = settings
        self._traded_windows: set = set()

    def is_enabled(self) -> bool:
        return hmsf_config.is_strategy_enabled(self.CONFIG_KEY)

    def evaluate(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        binance_price: float,
        window: WindowData,
        calculator,
        capital_usd: float,
        transit_latency_ms: float = 0,
    ) -> Optional[TradeSignal]:
        """Evaluiert ein Fenster für Last-Seconds-Sniping.

        WICHTIG: Dieses Modul braucht KEIN Momentum-Signal.
        Es triggert rein auf Basis von Expiry + Preis + Orderbook.
        Der momentum_pct Parameter wird als Kontext mitgegeben aber
        ist NICHT Teil der Trigger-Bedingung.
        """
        if not self.is_enabled():
            return None

        seconds_to_expiry = window.seconds_to_expiry

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 1: 15-60 Sekunden bis Expiry                  │
        # └─────────────────────────────────────────────────────────┘
        if not (self.MIN_EXPIRY_S <= seconds_to_expiry <= self.MAX_EXPIRY_S):
            return None

        if not window.orderbook_fresh:
            return None

        # Bestimme welche Seite "gewinnt" (die mit dem höheren Ask)
        up_ask = window.up_best_ask
        down_ask = window.down_best_ask

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 2: Eine Seite Ask >= 0.72                     │
        # │  Wir kaufen die TEURE Seite (die wahrscheinlich gewinnt)│
        # └─────────────────────────────────────────────────────────┘
        if up_ask >= self.MIN_ASK_PRICE and up_ask > down_ask:
            snipe_direction = "UP"
            ask_price = up_ask
            bid_price = window.up_best_bid
            bid_size = window.up_bid_size
            ask_size = window.up_ask_size
        elif down_ask >= self.MIN_ASK_PRICE and down_ask > up_ask:
            snipe_direction = "DOWN"
            ask_price = down_ask
            bid_price = window.down_best_bid
            bid_size = window.up_ask_size   # Invertiert (siehe Modul 1)
            ask_size = window.up_bid_size
        else:
            return None  # Keine Seite bei >= 0.72

        if ask_price <= 0 or ask_price >= 1:
            return None

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 3: Price-to-Beat Gap >= 0.10%                 │
        # │  Der Binance-Preis bestätigt die Richtung.             │
        # └─────────────────────────────────────────────────────────┘
        # Beim Sniping ist der "Gap" der Abstand zwischen Polymarket-Preis
        # und dem was Binance zeigt. Wenn UP bei 0.80 steht und Binance
        # bestätigt einen Aufwärtstrend, ist der Gap die Differenz
        # zwischen dem implied probability und der tatsächlichen.
        #
        # Einfacher: Nutze den absoluten Momentum als Proxy für den Gap.
        # Wenn |momentum| >= 0.10%, bestätigt Binance die Richtung.
        price_gap_pct = abs(momentum_pct)
        if price_gap_pct < self.MIN_PRICE_GAP_PCT:
            return None

        # Richtungs-Konsistenz: Momentum muss zur Snipe-Richtung passen
        if snipe_direction == "UP" and momentum_pct < 0:
            return None  # Binance fällt, aber UP ist teuer → Widerspruch
        if snipe_direction == "DOWN" and momentum_pct > 0:
            return None  # Binance steigt, aber DOWN ist teuer → Widerspruch

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 4: Orderbook-Imbalance >= +15%                │
        # └─────────────────────────────────────────────────────────┘
        if ask_size > 0 and bid_size > 0:
            raw_imbalance = (bid_size / ask_size - 1) * 100
        else:
            raw_imbalance = 0.0

        if snipe_direction == "UP":
            directional_imbalance = raw_imbalance
        else:
            directional_imbalance = -raw_imbalance

        if directional_imbalance < self.MIN_OB_IMBALANCE_PCT:
            return None

        # === Duplicate Prevention ===
        trade_key = f"{window.slug}_{snipe_direction}_snipe"
        if trade_key in self._traded_windows:
            return None

        # === Position Sizing: Max 1.2% des Kapitals ===
        max_size = capital_usd * (self.MAX_RISK_PCT / 100.0)
        position_usd = min(max_size, self.settings.max_live_position_usd)

        if position_usd < 1.0:
            return None

        shares = position_usd / ask_price if ask_price > 0 else 0
        if shares < 5.0:  # Polymarket CLOB Minimum
            return None

        # Spread berechnen
        spread_pct = 0.0
        if bid_price > 0 and ask_price > 0:
            spread_pct = (ask_price - bid_price) / ((ask_price + bid_price) / 2) * 100

        # === Liquidity Check (aus HMSF Config) ===
        min_liq = hmsf_config.get("min_liquidity_usd", 8000.0)
        if window.liquidity_usd < min_liq:
            return None

        # Duplicate markieren
        self._traded_windows.add(trade_key)

        # Net EV Berechnung für Sniping:
        # Bei Ask 0.80: Gewinn wenn richtig = (1.00 - 0.80) / 0.80 = 25%
        # Verlust wenn falsch = -100%
        # Bei 90% WR: EV = 0.90 * 25% - 0.10 * 100% = +12.5%
        gross_return_if_win = (1.0 - ask_price) / ask_price * 100
        # Konservative WR-Schätzung basierend auf Ask-Preis (0.72 → 72%, 0.85 → 85%)
        estimated_wr = ask_price
        net_ev_pct = estimated_wr * gross_return_if_win - (1 - estimated_wr) * 100

        return TradeSignal(
            module=self.MODULE_NAME,
            asset=asset,
            direction=snipe_direction,
            momentum_pct=momentum_pct,
            ask_price=ask_price,
            bid_price=bid_price,
            binance_price=binance_price,
            seconds_to_expiry=seconds_to_expiry,
            window_slug=window.slug,
            market_question=window.question,
            timeframe=window.timeframe,
            net_ev_pct=round(net_ev_pct, 2),
            fee_pct=0.0,  # Bei extremen Preisen ist Fee minimal
            p_true=ask_price,  # Implied probability = Ask-Preis
            kelly_fraction=0.0,
            position_usd=round(position_usd, 2),
            spread_pct=round(spread_pct, 2),
            liquidity_usd=window.liquidity_usd,
            transit_latency_ms=transit_latency_ms,
            price_gap_pct=round(price_gap_pct, 4),
            orderbook_imbalance_pct=round(directional_imbalance, 2),
            confidence_tag="snipe_last_seconds",
            signal_time=time.time(),
            shares=round(shares, 2),
        )

    def discard_window(self, slug: str, direction: str) -> None:
        self._traded_windows.discard(f"{slug}_{direction}_snipe")


# ═══════════════════════════════════════════════════════════════════════
# MODUL 3: Both-Sides-Arbitrage (Delta Neutral)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ArbSignal:
    """Spezielles Signal für Both-Sides-Arbitrage (2 Trades gleichzeitig)."""
    module: str = "both_sides_arb"
    asset: str = ""
    window_slug: str = ""
    market_question: str = ""
    timeframe: str = ""
    seconds_to_expiry: float = 0.0

    # UP-Seite
    up_ask_price: float = 0.0
    up_shares: float = 0.0
    up_size_usd: float = 0.0
    up_token_id: str = ""

    # DOWN-Seite
    down_ask_price: float = 0.0
    down_shares: float = 0.0
    down_size_usd: float = 0.0
    down_token_id: str = ""

    # Arb-Metriken
    combined_cost: float = 0.0       # UP Ask + DOWN Ask (muss < 1.00)
    guaranteed_profit_pct: float = 0.0  # (1.00 - combined_cost) / combined_cost * 100
    guaranteed_profit_usd: float = 0.0
    total_size_usd: float = 0.0
    liquidity_usd: float = 0.0

    confidence_tag: str = "both_sides_arb"
    signal_time: float = 0.0


class BothSidesArbModule:
    """Delta-Neutral Arbitrage: Kaufe BEIDE Seiten gleichzeitig.

    Trigger:
    1. UP-Ask + DOWN-Ask < $0.98 (garantierter Profit bei $1.00 Payout)
    2. Liquidität auf BEIDEN Seiten >= $8,000
    3. Mindestens 15 Sekunden bis Expiry

    Aktion: Kaufe BEIDE Seiten mit EXAKT dem gleichen USD-Betrag.

    Beispiel:
    - UP Ask: $0.47, DOWN Ask: $0.48
    - Combined: $0.95 → Payout: $1.00 → Profit: $0.05 (5.26%)
    - Kaufe $2.50 UP + $2.50 DOWN = $5.00 total
    - Egal wer gewinnt: $1.00 × Shares einer Seite > $5.00
    """

    MODULE_NAME = "both_sides_arb"
    CONFIG_KEY = "both_sides_arbitrage_enabled"

    # Schwellenwerte
    FEE_RATE = 0.072             # Polymarket Crypto feeRate (seit 30.03.2026)
    MIN_LIQUIDITY_USD = 8000.0   # Mindest-Liquidität JEDE Seite
    MIN_EXPIRY_S = 15.0

    def __init__(self, settings) -> None:
        self.settings = settings
        self._traded_windows: set = set()

    def is_enabled(self) -> bool:
        return hmsf_config.is_strategy_enabled(self.CONFIG_KEY)

    def evaluate(
        self,
        asset: str,
        window: WindowData,
        capital_usd: float,
    ) -> Optional[ArbSignal]:
        """Prüft ob eine Both-Sides-Arbitrage möglich ist.

        WICHTIG: Dieses Modul braucht KEIN Momentum-Signal.
        Es triggert rein auf Basis der Polymarket-Preise.
        """
        if not self.is_enabled():
            return None

        seconds_to_expiry = window.seconds_to_expiry

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 3: Mindestens 15 Sekunden bis Expiry          │
        # └─────────────────────────────────────────────────────────┘
        if seconds_to_expiry < self.MIN_EXPIRY_S:
            return None

        if not window.orderbook_fresh:
            return None

        up_ask = window.up_best_ask
        down_ask = window.down_best_ask

        if up_ask <= 0 or up_ask >= 1 or down_ask <= 0 or down_ask >= 1:
            return None

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 1: Dynamischer Fee-Aware Threshold             │
        # │  STRESSTEST FIX (04.04.2026): Fees beider Seiten        │
        # │  einrechnen statt hardcoded 0.98.                       │
        # │  Effektive Kosten = ask × (1 + fee%) pro Seite.         │
        # │  Nur profitabel wenn effective_cost < $1.00 (Payout).   │
        # └─────────────────────────────────────────────────────────┘
        combined_cost = up_ask + down_ask

        # Fee pro Seite: fee_fraction = feeRate × (1-p) → effective_ask = p / (1 - fee_fraction)
        fee_up_fraction = self.FEE_RATE * (1 - up_ask)
        fee_down_fraction = self.FEE_RATE * (1 - down_ask)
        effective_up = up_ask / (1 - fee_up_fraction) if fee_up_fraction < 1 else 99
        effective_down = down_ask / (1 - fee_down_fraction) if fee_down_fraction < 1 else 99
        effective_cost = effective_up + effective_down

        if effective_cost >= 1.00:
            return None  # Kein Arb nach Fees

        # ┌─────────────────────────────────────────────────────────┐
        # │  TRIGGER 2: Liquidität auf BEIDEN Seiten >= $8,000     │
        # └─────────────────────────────────────────────────────────┘
        # Wir checken die Gesamt-Liquidität. Für eine präzisere Prüfung
        # bräuchten wir separate Liquiditäts-Zahlen pro Seite,
        # die aktuell nicht verfügbar sind.
        if window.liquidity_usd < self.MIN_LIQUIDITY_USD:
            return None

        # === Duplicate Prevention ===
        trade_key = f"{window.slug}_arb"
        if trade_key in self._traded_windows:
            return None

        # === Position Sizing ===
        # Quarter-Kelly Cap aus HMSF Config
        max_risk_pct = hmsf_config.get("max_risk_per_trade_percent", 2.5)
        max_total_size = capital_usd * (max_risk_pct / 100.0)
        # Aufgeteilt auf beide Seiten (gleicher Betrag)
        per_side_usd = min(max_total_size / 2.0, self.settings.max_live_position_usd)

        if per_side_usd < 1.0:
            return None

        # Shares pro Seite
        up_shares = per_side_usd / up_ask if up_ask > 0 else 0
        down_shares = per_side_usd / down_ask if down_ask > 0 else 0

        # Minimum Shares Check (5 pro Seite)
        if up_shares < 5.0 or down_shares < 5.0:
            return None

        # === Fee-Aware Profit-Berechnung (STRESSTEST FIX 04.04.2026) ===
        # Fees werden auf BEIDE Legs abgezogen.
        # fee_dollar = shares × feeRate × p × (1-p)
        fee_up_usd = up_shares * self.FEE_RATE * up_ask * (1 - up_ask)
        fee_down_usd = down_shares * self.FEE_RATE * down_ask * (1 - down_ask)
        total_fees = fee_up_usd + fee_down_usd

        total_invested = per_side_usd * 2
        # Worst case: die teurere Seite gewinnt → weniger Shares
        min_payout = min(up_shares, down_shares) * 1.0
        guaranteed_profit = min_payout - total_invested - total_fees
        guaranteed_profit_pct = (guaranteed_profit / total_invested * 100
                                 if total_invested > 0 else 0)

        if guaranteed_profit <= 0:
            return None  # Kein garantierter Profit nach Fees + Sizing

        # Duplicate markieren
        self._traded_windows.add(trade_key)

        logger.info(
            f"ARB SIGNAL: {asset} | UP@{up_ask:.3f} + DOWN@{down_ask:.3f} = {combined_cost:.3f} | "
            f"Profit: ${guaranteed_profit:.4f} ({guaranteed_profit_pct:.2f}%)"
        )

        return ArbSignal(
            module=self.MODULE_NAME,
            asset=asset,
            window_slug=window.slug,
            market_question=window.question,
            timeframe=window.timeframe,
            seconds_to_expiry=seconds_to_expiry,
            up_ask_price=up_ask,
            up_shares=round(up_shares, 2),
            up_size_usd=round(per_side_usd, 2),
            up_token_id=window.up_token_id,
            down_ask_price=down_ask,
            down_shares=round(down_shares, 2),
            down_size_usd=round(per_side_usd, 2),
            down_token_id=window.down_token_id,
            combined_cost=round(combined_cost, 4),
            guaranteed_profit_pct=round(guaranteed_profit_pct, 4),
            guaranteed_profit_usd=round(guaranteed_profit, 4),
            total_size_usd=round(total_invested, 2),
            liquidity_usd=window.liquidity_usd,
            confidence_tag="both_sides_arb",
            signal_time=time.time(),
        )

    def discard_window(self, slug: str) -> None:
        self._traded_windows.discard(f"{slug}_arb")
