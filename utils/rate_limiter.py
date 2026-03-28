"""Adaptive Throttling mit Exponential Backoff."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class RateLimiter:
    """Exponential Backoff bei Rate-Limit-Fehlern.

    Nutzung:
        limiter = RateLimiter()
        async with limiter.throttle("binance"):
            await exchange.fetch_order_book(...)
    """

    max_retries: int = 5
    base_delay: float = 1.0
    max_delay: float = 16.0

    _failures: dict[str, int] = field(default_factory=dict)

    def _get_delay(self, exchange_id: str) -> float:
        failures = self._failures.get(exchange_id, 0)
        if failures == 0:
            return 0
        delay = min(self.base_delay * (2 ** (failures - 1)), self.max_delay)
        return delay

    async def wait(self, exchange_id: str) -> None:
        """Wartet basierend auf bisherigen Fehlern."""
        delay = self._get_delay(exchange_id)
        if delay > 0:
            logger.warning(f"Rate limit backoff {exchange_id}: {delay:.1f}s")
            await asyncio.sleep(delay)

    def record_success(self, exchange_id: str) -> None:
        """Setzt Failure-Counter zurück."""
        if exchange_id in self._failures:
            self._failures[exchange_id] = 0

    def record_failure(self, exchange_id: str) -> bool:
        """Zählt Fehler hoch. Gibt False zurück wenn max_retries erreicht."""
        self._failures[exchange_id] = self._failures.get(exchange_id, 0) + 1
        count = self._failures[exchange_id]
        if count >= self.max_retries:
            logger.error(f"Max retries ({self.max_retries}) erreicht für {exchange_id}")
            self._failures[exchange_id] = 0
            return False
        return True
