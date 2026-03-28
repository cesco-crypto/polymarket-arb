"""Zentrale Konfiguration mit Pydantic Settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

# .env laden
load_dotenv(Path(__file__).parent / ".env")


class ExchangeKeys(BaseSettings):
    """API-Keys für eine Börse."""

    api_key: str = ""
    secret: str = ""
    passphrase: str = ""  # nur KuCoin


class Settings(BaseSettings):
    """Alle Bot-Einstellungen an einem Ort."""

    # --- Modus ---
    mode: str = Field(default="paper", description="paper | live")
    log_level: str = Field(default="INFO")

    # --- Börsen ---
    exchanges: list[str] = Field(default=["binance", "kucoin", "bybit"])
    quote_currency: str = "USDT"

    # --- Spread-Filter ---
    min_profitable_spread: float = Field(
        default=0.30, description="Mindest-Spread nach Gebühren (%)"
    )
    min_volume_usd: float = Field(
        default=50_000, description="Mindest-24h-Volumen pro Börse (USD)"
    )
    min_volume_ratio: float = Field(
        default=0.10,
        description="Mindest-Volumen-Verhältnis beider Seiten (10% = dünnere Seite muss mind. 10% der dickeren haben)",
    )

    # --- WebSocket Price Feed ---
    use_ws_price_feed: bool = Field(
        default=True, description="WebSocket für Echtzeit-Preise nutzen (statt REST-Batch)"
    )

    # --- Order ---
    max_order_size_usd: float = Field(
        default=100, description="Maximale Ordergröße (USD)"
    )
    order_timeout_seconds: float = 3.0

    # --- Risikomanagement / Kill-Switches ---
    max_portfolio_pct_per_trade: float = Field(
        default=0.08, description="Max 8% des Portfolios pro Trade"
    )
    max_daily_loss_pct: float = Field(
        default=0.20, description="-20% Tagesverlust → Stop"
    )
    max_total_loss_pct: float = Field(
        default=0.40, description="-40% Gesamtverlust → Shutdown"
    )
    min_balance_usd: float = Field(
        default=100, description="Kein Trading wenn Balance < 100 USD"
    )

    # --- Rebalancing ---
    rebalance_drift_pct: float = Field(
        default=0.30, description="Alert wenn >30% Drift"
    )

    # --- Scanner ---
    scan_interval_seconds: float = Field(
        default=5.0, description="Dashboard-Refresh alle N Sekunden"
    )
    top_opportunities: int = Field(
        default=20, description="Anzahl Top-Opportunitäten im Dashboard"
    )
    orderbook_depth: int = Field(
        default=5, description="Orderbuch-Tiefe für Spread-Berechnung"
    )

    # --- Paths ---
    data_dir: Path = Field(default=Path(__file__).parent / "data")

    # --- Polymarket Latency Strategy ---
    # Symbole die als Binance-Orakel dienen
    oracle_symbols: list[str] = Field(default=["BTC/USDT", "ETH/USDT"])

    # Momentum-Fenster: Preisänderung über N Sekunden als Signal
    momentum_window_s: float = Field(
        default=60.0,
        description="Sekunden für Momentum-Berechnung (Backtest: 1m optimal für 5m Slots)",
    )
    # Mindest-Momentum für Handelssignal (%)
    min_momentum_pct: float = Field(
        default=0.10,
        description="Mindest-Preisbewegung auf Binance für Signal (Backtest: 0.10% = 90.4% WR bei 14K Trades)",
    )
    # Polymarket: minimale erwartete Edge nach Gebühren
    min_edge_pct: float = Field(
        default=2.0, description="Mindest-Edge über Polymarket-Gebühren (%)"
    )
    # Polymarket: maximale Taker-Gebühr (bei p=0.50)
    polymarket_max_fee_pct: float = Field(
        default=3.15,
        description="Max Polymarket-Taker-Gebühr bei p=0.50 (seit Gebühren-Update für Kurzzeit-Kryptomärkte)",
    )
    # Fractional Kelly: Anteil des Kelly-Kriteriums (konservativ)
    kelly_fraction: float = Field(
        default=0.50, description="Fractional Kelly (0.50 = Half Kelly)"
    )
    # Paper-Trading: Startkapital
    paper_capital_usd: float = Field(
        default=1000.0, description="Virtuelles Startkapital für Paper Trading"
    )
    # Maximale Positionsgröße pro Trade (% des Kapitals)
    max_position_pct: float = Field(
        default=0.08, description="Max 8% des Kapitals pro Polymarket-Trade (Hard Limit)"
    )
    # Nur innerhalb dieses Zeitfensters vor Markt-Ablauf handeln
    min_seconds_to_expiry: float = Field(
        default=30.0, description="Kein Trade wenn < 30s bis Ablauf"
    )
    max_seconds_to_expiry: float = Field(
        default=240.0, description="Kein Trade wenn > 240s bis Ablauf"
    )

    # --- Telegram Alerts ---
    telegram_bot_token: str = Field(default="", description="Telegram Bot Token von @BotFather")
    telegram_chat_id: str = Field(default="", description="Telegram Chat-ID für Alerts")

    model_config = {"env_prefix": "", "env_file": ".env", "extra": "ignore"}


def get_exchange_keys(exchange_id: str) -> ExchangeKeys:
    """Lädt API-Keys für eine Börse aus Umgebungsvariablen."""
    prefix = exchange_id.upper()
    return ExchangeKeys(
        api_key=os.getenv(f"{prefix}_API_KEY", ""),
        secret=os.getenv(f"{prefix}_SECRET", ""),
        passphrase=os.getenv(f"{prefix}_PASSPHRASE", ""),
    )


# Singleton
settings = Settings()
