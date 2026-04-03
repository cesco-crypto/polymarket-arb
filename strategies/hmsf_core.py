"""Hybrid Multi-Strategy Framework (HMSF) — Core Foundation.

Enthält:
1. STRATEGY_CONFIG — Live-aktualisierbare Konfiguration für alle Strategien
2. GlobalRiskFilter — Position Sizing, Fee-Check, Liquidity, Spread Guard
3. RegimeDetector — Dynamische Marktphasen-Erkennung (Trend-Malus)
4. AntiWhipsawExit — Intelligenter Early-Exit mit Hysteresis

Alle Komponenten sind UNABHÄNGIG von der Signal-Logik.
Sie werden VOR und WÄHREND eines Trades angewendet.
"""

from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


# ═══════════════════════════════════════════════════════════════════════
# 1. STRATEGY_CONFIG — Live-aktualisierbar via Web-UI
# ═══════════════════════════════════════════════════════════════════════

class StrategyConfig:
    """Thread-safe, live-aktualisierbare Konfiguration.

    Die Web-UI kann jeden Wert per POST ändern ohne Bot-Neustart.
    Alle Strategien lesen diese Config bevor sie einen Trade evaluieren.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config: dict = {
            # --- Strategie-Toggles (welche Module aktiv sind) ---
            "legacy_momentum_enabled": True,
            "enhanced_latency_momentum_enabled": True,
            "last_seconds_sniping_enabled": True,
            "both_sides_arbitrage_enabled": True,

            # --- Risiko-Modus ---
            "conservative_mode": True,      # Quarter-Kelly, strenge Filter
            "aggressive_mode": False,       # Half-Kelly, lockerere Filter

            # --- Position Sizing ---
            "max_risk_per_trade_percent": 2.5,  # Quarter-Kelly Cap

            # --- Globale Filter-Schwellen ---
            "min_net_ev_pct": 1.8,          # Mindest Net-EV nach Fees
            "min_liquidity_usd": 8000.0,    # Mindest-Marktliquidität
            "max_transit_latency_ms": 80.0,  # Max Binance→Bot Latenz
            "max_spread_pct": 5.5,          # Max Bid-Ask Spread
            "min_shares": 5.0,              # Polymarket CLOB Minimum

            # --- Regime-Filter ---
            "regime_trend_window_minutes": 30,  # Trend-Berechnung über N Minuten
            "regime_eth_up_malus_pct": 30.0,    # Malus für ETH UP in Downtrend

            # --- Anti-Whipsaw Early-Exit ---
            "exit_min_hold_seconds": 8.0,       # Hysteresis: min Haltezeit
            "exit_mid_price_loss_pct": 18.0,    # Exit wenn Mid-Price X% im Minus
            "exit_oracle_adverse_pct": 0.25,    # UND Oracle X% in falsche Richtung
        }

    def get(self, key: str, default=None):
        """Thread-safe Read."""
        with self._lock:
            return self._config.get(key, default)

    def set(self, key: str, value) -> None:
        """Thread-safe Write (von Web-UI aufgerufen)."""
        with self._lock:
            old = self._config.get(key)
            self._config[key] = value
            logger.info(f"HMSF Config Update: {key} = {old} → {value}")

    def get_all(self) -> dict:
        """Snapshot der gesamten Konfiguration."""
        with self._lock:
            return dict(self._config)

    def update_many(self, updates: dict) -> dict:
        """Mehrere Werte gleichzeitig setzen. Gibt die geänderten Keys zurück."""
        changed = {}
        with self._lock:
            for key, value in updates.items():
                if key in self._config:
                    old = self._config[key]
                    if old != value:
                        self._config[key] = value
                        changed[key] = {"old": old, "new": value}
        if changed:
            logger.info(f"HMSF Config Bulk Update: {len(changed)} Werte geändert")
        return changed

    def is_strategy_enabled(self, strategy_key: str) -> bool:
        """Prüft ob eine Strategie aktiviert ist."""
        return bool(self.get(strategy_key, False))


# Singleton — wird von allen Strategien importiert
hmsf_config = StrategyConfig()


# ═══════════════════════════════════════════════════════════════════════
# 2. GLOBAL RISK FILTER — Gelten für ALLE Trades, JEDE Strategie
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class FilterResult:
    """Ergebnis eines Filter-Checks."""
    passed: bool
    reason: str = ""
    adjusted_size_usd: float = 0.0
    adjusted_net_ev_pct: float = 0.0


class GlobalRiskFilter:
    """Zentrale Risiko-Prüfung die jeder Trade durchlaufen MUSS.

    Wird VOR der Order-Platzierung aufgerufen.
    Kann die Positionsgrösse anpassen oder den Trade komplett blockieren.
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self._trades_today: int = 0
        self._pnl_today: float = 0.0
        self._day_start: float = time.time()

    def check(
        self,
        *,
        capital_usd: float,
        size_usd: float,
        net_ev_pct: float,
        fee_pct: float,
        spread_pct: float,
        liquidity_usd: float,
        transit_latency_ms: float,
        shares: float,
        asset: str = "",
        direction: str = "",
    ) -> FilterResult:
        """Führt alle globalen Filter-Checks durch.

        Returns:
            FilterResult mit passed=True/False und ggf. angepasster Size.
        """
        # --- 1. Position Sizing: Quarter-Kelly Cap ---
        max_risk_pct = self.config.get("max_risk_per_trade_percent", 2.5)
        max_size = capital_usd * (max_risk_pct / 100.0)
        adjusted_size = min(size_usd, max_size)

        if adjusted_size < 1.0:
            return FilterResult(False, f"Position zu klein: ${adjusted_size:.2f} (Quarter-Kelly Cap ${max_size:.2f})")

        # --- 2. Fee-Check: Net EV nach Fees muss positiv genug sein ---
        min_net_ev = self.config.get("min_net_ev_pct", 1.8)
        if net_ev_pct < min_net_ev:
            return FilterResult(False, f"Net EV zu niedrig: {net_ev_pct:.2f}% < {min_net_ev}% Minimum")

        # --- 3. Liquidity Check ---
        min_liq = self.config.get("min_liquidity_usd", 8000.0)
        if liquidity_usd < min_liq:
            return FilterResult(False, f"Liquidität zu niedrig: ${liquidity_usd:.0f} < ${min_liq:.0f}")

        # --- 4. Transit Latency Check ---
        max_transit = self.config.get("max_transit_latency_ms", 80.0)
        if transit_latency_ms > max_transit and transit_latency_ms > 0:
            return FilterResult(False, f"Transit Latenz zu hoch: {transit_latency_ms:.0f}ms > {max_transit:.0f}ms")

        # --- 5. Spread Guard ---
        max_spread = self.config.get("max_spread_pct", 5.5)
        if spread_pct > max_spread:
            return FilterResult(False, f"Spread zu breit: {spread_pct:.1f}% > {max_spread}%")

        # --- 6. Minimum Shares (Polymarket CLOB) ---
        min_shares = self.config.get("min_shares", 5.0)
        if shares < min_shares:
            return FilterResult(False, f"Shares zu wenig: {shares:.1f} < {min_shares}")

        # --- Alle Filter bestanden ---
        return FilterResult(
            passed=True,
            reason="OK",
            adjusted_size_usd=adjusted_size,
            adjusted_net_ev_pct=net_ev_pct,
        )

    def record_trade(self, pnl_usd: float) -> None:
        """Trackt tägliche PnL für Kill-Switch."""
        self._trades_today += 1
        self._pnl_today += pnl_usd

    def daily_stats(self) -> dict:
        return {
            "trades_today": self._trades_today,
            "pnl_today": round(self._pnl_today, 2),
            "uptime_hours": round((time.time() - self._day_start) / 3600, 1),
        }


# ═══════════════════════════════════════════════════════════════════════
# 3. REGIME DETECTOR — Dynamische Marktphasen-Erkennung
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class RegimeState:
    """Aktueller Markt-Regime-Zustand."""
    btc_trend_30m_pct: float = 0.0   # BTC Trend über 30 Min (%)
    eth_trend_30m_pct: float = 0.0   # ETH Trend über 30 Min (%)
    btc_trending_down: bool = False
    eth_trending_down: bool = False
    regime_tag: str = "unknown"       # "trending_down" / "trending_up" / "ranging"
    last_update: float = 0.0


class RegimeDetector:
    """Erkennt Marktphasen und wendet Malus/Bonus auf Trades an.

    Regel: Wenn der Markt in den letzten 30 Minuten fallend ist,
    bekommen ETH UP Signale einen -30% Malus auf Net-EV.
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.state = RegimeState()
        # QA FIX (04.04.2026): deque statt list — O(1) statt O(n) bei pop
        # maxlen=150_000 ≈ 69 ticks/s × 2100s (35 Min) mit Headroom
        self._price_history: dict[str, deque] = {
            "BTC": deque(maxlen=150_000),
            "ETH": deque(maxlen=150_000),
        }

    def record_price(self, asset: str, price: float) -> None:
        """Zeichnet einen Preis auf (aufgerufen bei jedem Binance Tick)."""
        now = time.time()
        asset_upper = asset.upper().replace("/USDT", "")
        if asset_upper not in self._price_history:
            return

        # deque mit maxlen evictet automatisch — kein manuelles pop nötig
        self._price_history[asset_upper].append((now, price))

    def update(self) -> RegimeState:
        """Berechnet den aktuellen Regime-Zustand."""
        now = time.time()
        window_minutes = self.config.get("regime_trend_window_minutes", 30)
        cutoff = now - window_minutes * 60

        for asset in ["BTC", "ETH"]:
            history = self._price_history.get(asset, [])
            recent = [(ts, p) for ts, p in history if ts >= cutoff]

            if len(recent) >= 2:
                first_price = recent[0][1]
                last_price = recent[-1][1]
                trend_pct = ((last_price - first_price) / first_price) * 100 if first_price > 0 else 0

                # STRESSTEST FIX (04.04.2026): Dead Zone bei -0.15%
                # Vorher: trend_pct < 0 (triggerte bei -0.001% = Rauschen)
                # Nachher: trend_pct < -0.15 (nur bei echtem Downtrend)
                TREND_DEAD_ZONE = -0.15  # Prozent

                if asset == "BTC":
                    self.state.btc_trend_30m_pct = round(trend_pct, 4)
                    self.state.btc_trending_down = trend_pct < TREND_DEAD_ZONE
                else:
                    self.state.eth_trend_30m_pct = round(trend_pct, 4)
                    self.state.eth_trending_down = trend_pct < TREND_DEAD_ZONE

        # Regime-Tag bestimmen
        both_down = self.state.btc_trending_down and self.state.eth_trending_down
        both_up = not self.state.btc_trending_down and not self.state.eth_trending_down
        if both_down:
            self.state.regime_tag = "trending_down"
        elif both_up:
            self.state.regime_tag = "trending_up"
        else:
            self.state.regime_tag = "mixed"

        self.state.last_update = now
        return self.state

    def apply_regime_adjustment(
        self, asset: str, direction: str, net_ev_pct: float
    ) -> tuple[float, str]:
        """Wendet Regime-basierte Anpassungen auf Net-EV an.

        Returns:
            (adjusted_net_ev_pct, reason)
        """
        # ETH UP in fallendem Markt → PROPORTIONALER Malus
        # STRESSTEST FIX (04.04.2026): Statt flat 30%, proportional zum Trend.
        # -0.15% Trend → 3% Malus, -0.50% → 10%, -1.0% → 20%, -1.5% → 30% (Cap)
        if asset.upper() == "ETH" and direction.upper() == "UP" and self.state.eth_trending_down:
            max_malus = self.config.get("regime_eth_up_malus_pct", 30.0)
            trend_magnitude = abs(self.state.eth_trend_30m_pct)
            malus_pct = min(max_malus, trend_magnitude * 20.0)  # 20x Skalierung
            adjusted = net_ev_pct * (1 - malus_pct / 100.0)
            reason = f"ETH UP Malus ({malus_pct:.1f}%, proportional): ETH 30m={self.state.eth_trend_30m_pct:+.3f}%"
            logger.debug(f"Regime Adjustment: {net_ev_pct:.2f}% → {adjusted:.2f}% ({reason})")
            return adjusted, reason

        return net_ev_pct, ""

    def get_status(self) -> dict:
        return {
            "btc_trend_30m_pct": self.state.btc_trend_30m_pct,
            "eth_trend_30m_pct": self.state.eth_trend_30m_pct,
            "regime_tag": self.state.regime_tag,
            "btc_trending_down": self.state.btc_trending_down,
            "eth_trending_down": self.state.eth_trending_down,
        }


# ═══════════════════════════════════════════════════════════════════════
# 4. ANTI-WHIPSAW EARLY-EXIT — Intelligenter Stop-Loss
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class OpenPosition:
    """Eine offene Position die auf Early-Exit geprüft wird."""
    trade_id: str
    asset: str
    direction: str          # "UP" / "DOWN"
    entry_price: float      # Polymarket Ask bei Entry
    entry_oracle_price: float  # Binance-Preis bei Entry
    entry_time: float       # Unix timestamp
    size_usd: float


@dataclass
class ExitSignal:
    """Ergebnis der Early-Exit-Prüfung."""
    should_exit: bool = False
    reason: str = ""
    mid_price: float = 0.0
    mid_price_pnl_pct: float = 0.0
    oracle_adverse_pct: float = 0.0
    hold_time_s: float = 0.0


class AntiWhipsawExit:
    """Prüft offene Positionen auf frühzeitigen Exit.

    REGELN:
    1. Nutze IMMER mid_price = (bid + ask) / 2, nie nur den Bid
    2. Exit nur wenn Mid-Price ≥ 18% im Minus UND Oracle ≥ 0.25% in falsche Richtung
    3. Mindestens 8 Sekunden Haltezeit vor dem ersten Check (Hysteresis)
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def check_exit(
        self,
        position: OpenPosition,
        current_bid: float,
        current_ask: float,
        current_oracle_price: float,
    ) -> ExitSignal:
        """Prüft ob eine Position frühzeitig geschlossen werden sollte.

        Args:
            position: Die offene Position
            current_bid: Aktueller Polymarket Bid
            current_ask: Aktueller Polymarket Ask
            current_oracle_price: Aktueller Binance-Preis
        """
        now = time.time()
        hold_time = now - position.entry_time

        # --- Hysteresis: Mindesthaltezeit ---
        min_hold = self.config.get("exit_min_hold_seconds", 8.0)
        if hold_time < min_hold:
            return ExitSignal(
                should_exit=False,
                reason=f"Hysteresis: {hold_time:.1f}s < {min_hold}s Minimum",
                hold_time_s=hold_time,
            )

        # --- Mid-Price berechnen (NICHT nur Bid) ---
        if current_bid <= 0 or current_ask <= 0:
            return ExitSignal(should_exit=False, reason="Kein gültiger Bid/Ask")

        mid_price = (current_bid + current_ask) / 2.0

        # PnL basierend auf Mid-Price (nicht auf Bid!)
        # Wenn wir bei 0.42 gekauft haben und mid_price jetzt 0.35:
        # PnL% = (0.35 - 0.42) / 0.42 * 100 = -16.7%
        mid_price_pnl_pct = ((mid_price - position.entry_price) / position.entry_price * 100
                             if position.entry_price > 0 else 0)

        # --- Oracle Adverse Check ---
        # Prüft ob der Binance-Preis sich in die FALSCHE Richtung bewegt hat
        oracle_move_pct = ((current_oracle_price - position.entry_oracle_price)
                           / position.entry_oracle_price * 100
                           if position.entry_oracle_price > 0 else 0)

        # "Falsche Richtung" hängt von der Position ab:
        # UP-Position: adverse wenn Oracle FÄLLT (oracle_move < 0)
        # DOWN-Position: adverse wenn Oracle STEIGT (oracle_move > 0)
        if position.direction.upper() == "UP":
            oracle_adverse_pct = -oracle_move_pct  # Negativ = adverse für UP
        else:
            oracle_adverse_pct = oracle_move_pct   # Positiv = adverse für DOWN

        # --- Exit-Bedingung: BEIDE müssen erfüllt sein ---
        exit_loss_threshold = self.config.get("exit_mid_price_loss_pct", 18.0)
        exit_oracle_threshold = self.config.get("exit_oracle_adverse_pct", 0.25)

        should_exit = (
            mid_price_pnl_pct <= -exit_loss_threshold
            and oracle_adverse_pct >= exit_oracle_threshold
        )

        reason = ""
        if should_exit:
            reason = (
                f"EARLY EXIT: Mid-Price {mid_price_pnl_pct:+.1f}% (Threshold: -{exit_loss_threshold}%) "
                f"+ Oracle adverse {oracle_adverse_pct:+.3f}% (Threshold: {exit_oracle_threshold}%)"
            )
            logger.warning(f"AntiWhipsaw {position.trade_id}: {reason}")

        return ExitSignal(
            should_exit=should_exit,
            reason=reason,
            mid_price=mid_price,
            mid_price_pnl_pct=round(mid_price_pnl_pct, 2),
            oracle_adverse_pct=round(oracle_adverse_pct, 4),
            hold_time_s=round(hold_time, 1),
        )


# ═══════════════════════════════════════════════════════════════════════
# CONVENIENCE: Erstelle globale Instanzen
# ═══════════════════════════════════════════════════════════════════════

# Diese werden von allen HMSF-Strategien importiert
global_risk_filter = GlobalRiskFilter(hmsf_config)
regime_detector = RegimeDetector(hmsf_config)
anti_whipsaw = AntiWhipsawExit(hmsf_config)
