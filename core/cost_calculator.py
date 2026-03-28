"""Modul 3: Cost Calculator — Gebühren, Slippage, Netto-Profit."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from config import Settings


@dataclass
class TradeCost:
    """Kostenaufschlüsselung eines Arbitrage-Trades."""

    symbol: str
    buy_exchange: str
    sell_exchange: str

    ask_price: float  # Kaufpreis
    bid_price: float  # Verkaufspreis

    gross_spread_pct: float  # Brutto-Spread
    buy_fee_pct: float  # Kaufgebühr
    sell_fee_pct: float  # Verkaufsgebühr
    total_fee_pct: float  # Gesamtgebühren
    estimated_slippage_pct: float  # Geschätzte Slippage
    net_spread_pct: float  # Netto-Spread nach allen Kosten

    is_profitable: bool
    estimated_profit_usd: float  # Bei gegebener Ordergröße


# Standard-Gebühren pro Börse (Taker, da wir Limit-Orders am Spread platzieren)
DEFAULT_FEES: dict[str, float] = {
    "binance": 0.10,  # 0.10% (mit BNB-Discount: 0.075%)
    "kucoin": 0.10,   # 0.10% (mit KCS-Discount: 0.08%)
    "bybit": 0.10,    # 0.10%
}


class CostCalculator:
    """Berechnet die tatsächlichen Kosten eines Arbitrage-Trades."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._fee_cache: dict[str, float] = {}

    def set_exchange_fee(self, exchange_id: str, fee_pct: float) -> None:
        """Überschreibt die Standard-Gebühr für eine Börse."""
        self._fee_cache[exchange_id] = fee_pct

    def get_fee(self, exchange_id: str) -> float:
        """Gibt die Gebühr für eine Börse zurück (in %)."""
        return self._fee_cache.get(exchange_id, DEFAULT_FEES.get(exchange_id, 0.10))

    def calculate(
        self,
        symbol: str,
        buy_exchange: str,
        sell_exchange: str,
        ask_price: float,
        bid_price: float,
        order_size_usd: float | None = None,
        slippage_pct: float = 0.02,
    ) -> TradeCost:
        """Berechnet alle Kosten und den Netto-Profit.

        Args:
            symbol: Handelspaar (z.B. "BTC/USDT")
            buy_exchange: Börse auf der gekauft wird
            sell_exchange: Börse auf der verkauft wird
            ask_price: Bester Ask-Preis (Kaufseite)
            bid_price: Bester Bid-Preis (Verkaufsseite)
            order_size_usd: Ordergröße in USD
            slippage_pct: Geschätzte Slippage in %

        Returns:
            TradeCost mit allen Berechnungen
        """
        if order_size_usd is None:
            order_size_usd = self.settings.max_order_size_usd

        gross_spread_pct = (bid_price - ask_price) / ask_price * 100

        buy_fee = self.get_fee(buy_exchange)
        sell_fee = self.get_fee(sell_exchange)
        total_fee = buy_fee + sell_fee

        net_spread_pct = gross_spread_pct - total_fee - slippage_pct
        is_profitable = net_spread_pct >= self.settings.min_profitable_spread

        estimated_profit_usd = order_size_usd * (net_spread_pct / 100)

        return TradeCost(
            symbol=symbol,
            buy_exchange=buy_exchange,
            sell_exchange=sell_exchange,
            ask_price=ask_price,
            bid_price=bid_price,
            gross_spread_pct=round(gross_spread_pct, 4),
            buy_fee_pct=buy_fee,
            sell_fee_pct=sell_fee,
            total_fee_pct=total_fee,
            estimated_slippage_pct=slippage_pct,
            net_spread_pct=round(net_spread_pct, 4),
            is_profitable=is_profitable,
            estimated_profit_usd=round(estimated_profit_usd, 4),
        )
