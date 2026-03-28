"""Modul 2: Opportunity Scanner — Findet profitable Arbitrage-Spreads."""

from __future__ import annotations

import asyncio
import csv
import time
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from loguru import logger

from config import Settings
from core.cost_calculator import CostCalculator, TradeCost
from core.market_data import MarketDataCollector, TickerData


@dataclass
class Opportunity:
    """Eine erkannte Arbitrage-Opportunität."""

    symbol: str
    buy_exchange: str
    sell_exchange: str
    ask_price: float
    bid_price: float
    gross_spread_pct: float
    net_spread_pct: float
    estimated_profit_usd: float
    buy_volume_24h: float
    sell_volume_24h: float
    timestamp: float


# Symbole die auf verschiedenen Börsen verschiedene Tokens referenzieren
SYMBOL_BLACKLIST = {"U/USDT", "TUSD/USDT"}

# Maximaler plausibler Spread — alles darüber ist ein Datenfehler
MAX_PLAUSIBLE_SPREAD_PCT = 50.0


class Scanner:
    """Scannt kontinuierlich alle Börsen-Kombinationen nach Arbitrage-Spreads."""

    def __init__(
        self,
        settings: Settings,
        market_data: MarketDataCollector,
        cost_calculator: CostCalculator,
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.cost_calculator = cost_calculator
        self._opportunities: list[Opportunity] = []
        self._scan_count: int = 0
        self._last_scan_duration: float = 0
        self._total_spotted_ever: int = 0  # alle profitablen Opps kumuliert (für Capture Rate)
        self._csv_path = settings.data_dir / "spreads.csv"
        self._csv_initialized = False

    @property
    def opportunities(self) -> list[Opportunity]:
        return self._opportunities

    @property
    def scan_count(self) -> int:
        return self._scan_count

    @property
    def last_scan_duration(self) -> float:
        return self._last_scan_duration

    @property
    def total_spotted_ever(self) -> int:
        return self._total_spotted_ever

    async def scan_once(self) -> list[Opportunity]:
        """Führt einen vollständigen Scan über alle Börsen und Paare durch."""
        start = time.time()
        self._scan_count += 1

        # Ticker-Daten von allen Börsen holen
        ticker_data = await self.market_data.fetch_orderbooks()

        if not ticker_data:
            logger.warning("Keine Ticker-Daten erhalten")
            return []

        opportunities: list[Opportunity] = []
        exchange_ids = list(ticker_data.keys())

        # Alle Börsen-Paare durchgehen
        for ex_a, ex_b in combinations(exchange_ids, 2):
            tickers_a = ticker_data.get(ex_a, {})
            tickers_b = ticker_data.get(ex_b, {})

            common = set(tickers_a.keys()) & set(tickers_b.keys())

            for symbol in common:
                if symbol in SYMBOL_BLACKLIST:
                    continue

                ta = tickers_a[symbol]
                tb = tickers_b[symbol]

                # Volumen-Filter: beide Seiten müssen Mindestvolumen haben
                if (
                    ta.volume_24h_usd < self.settings.min_volume_usd
                    or tb.volume_24h_usd < self.settings.min_volume_usd
                ):
                    continue

                # Asymmetrie-Filter: verhindert Trades gegen dünne Märkte
                # (z.B. Binance 10M vs KuCoin 500K → Ratio 0.05 → verworfen bei Limit 0.10)
                if ta.volume_24h_usd > 0 and tb.volume_24h_usd > 0:
                    vol_ratio = min(ta.volume_24h_usd, tb.volume_24h_usd) / max(
                        ta.volume_24h_usd, tb.volume_24h_usd
                    )
                    if vol_ratio < self.settings.min_volume_ratio:
                        continue

                # Richtung 1: Kaufe auf A, verkaufe auf B
                opp = self._check_spread(ta, tb, symbol)
                if opp:
                    opportunities.append(opp)

                # Richtung 2: Kaufe auf B, verkaufe auf A
                opp = self._check_spread(tb, ta, symbol)
                if opp:
                    opportunities.append(opp)

        # Nach Netto-Spread sortieren (höchster zuerst)
        opportunities.sort(key=lambda o: o.net_spread_pct, reverse=True)

        # Kumulativ alle profitablen Opps zählen (für Capture Rate)
        self._total_spotted_ever += len(opportunities)

        # Top N behalten
        self._opportunities = opportunities[: self.settings.top_opportunities]
        self._last_scan_duration = time.time() - start

        # Logging
        if self._opportunities:
            best = self._opportunities[0]
            logger.info(
                f"Scan #{self._scan_count}: {len(opportunities)} Opps gefunden, "
                f"Top: {best.symbol} {best.net_spread_pct:.3f}% "
                f"({best.buy_exchange}→{best.sell_exchange}) "
                f"[{self._last_scan_duration:.1f}s]"
            )
            self._log_to_csv(self._opportunities[:5])
        else:
            logger.debug(f"Scan #{self._scan_count}: Keine profitablen Spreads [{self._last_scan_duration:.1f}s]")

        return self._opportunities

    def _check_spread(
        self, buy_ticker: TickerData, sell_ticker: TickerData, symbol: str
    ) -> Opportunity | None:
        """Prüft ob ein Spread profitabel ist (Kauf auf buy_ticker, Verkauf auf sell_ticker)."""
        ask = buy_ticker.best_ask
        bid = sell_ticker.best_bid

        if bid <= ask:
            return None  # Kein positiver Spread

        # L1-Tiefen-Check: reicht die Liquidität für unsere Ordergröße?
        # ask_volume/bid_volume kommen vom WS-Feed (0 bei REST-Fallback → kein Filter)
        order_size = self.settings.max_order_size_usd
        if buy_ticker.ask_volume > 0 and buy_ticker.ask_volume < order_size:
            return None  # Nicht genug Tiefe am Best Ask für Kauf
        if sell_ticker.bid_volume > 0 and sell_ticker.bid_volume < order_size:
            return None  # Nicht genug Tiefe am Best Bid für Verkauf

        cost = self.cost_calculator.calculate(
            symbol=symbol,
            buy_exchange=buy_ticker.exchange_id,
            sell_exchange=sell_ticker.exchange_id,
            ask_price=ask,
            bid_price=bid,
        )

        if not cost.is_profitable:
            return None

        # Plausibilitäts-Check: unrealistisch hohe Spreads = Datenfehler
        if cost.gross_spread_pct > MAX_PLAUSIBLE_SPREAD_PCT:
            logger.debug(f"Unplausibler Spread {symbol}: {cost.gross_spread_pct:.1f}% — übersprungen")
            return None

        return Opportunity(
            symbol=symbol,
            buy_exchange=buy_ticker.exchange_id,
            sell_exchange=sell_ticker.exchange_id,
            ask_price=ask,
            bid_price=bid,
            gross_spread_pct=cost.gross_spread_pct,
            net_spread_pct=cost.net_spread_pct,
            estimated_profit_usd=cost.estimated_profit_usd,
            buy_volume_24h=buy_ticker.volume_24h_usd,
            sell_volume_24h=sell_ticker.volume_24h_usd,
            timestamp=time.time(),
        )

    def _log_to_csv(self, opportunities: list[Opportunity]) -> None:
        """Loggt die besten Opportunities in eine CSV-Datei."""
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)

        write_header = not self._csv_initialized or not self._csv_path.exists()

        try:
            with open(self._csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow([
                        "timestamp", "symbol", "buy_exchange", "sell_exchange",
                        "ask_price", "bid_price", "gross_spread_pct",
                        "net_spread_pct", "estimated_profit_usd",
                    ])
                    self._csv_initialized = True

                for opp in opportunities:
                    writer.writerow([
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        opp.symbol,
                        opp.buy_exchange,
                        opp.sell_exchange,
                        f"{opp.ask_price:.8f}",
                        f"{opp.bid_price:.8f}",
                        f"{opp.gross_spread_pct:.4f}",
                        f"{opp.net_spread_pct:.4f}",
                        f"{opp.estimated_profit_usd:.4f}",
                    ])
        except OSError as e:
            logger.warning(f"CSV-Logging fehlgeschlagen: {e}")
