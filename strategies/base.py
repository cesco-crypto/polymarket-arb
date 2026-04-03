"""Strategy Base Class — Interface für alle Trading-Strategien.

Jede Strategie muss diese ABC implementieren um im Dashboard
per Klick gewechselt werden zu können.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class StrategyBase(ABC):
    """Gemeinsames Interface für alle Trading-Strategien."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Eindeutiger interner Name (z.B. 'momentum_latency_v2')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Kurzbeschreibung für das Dashboard."""
        ...

    @abstractmethod
    async def run(self) -> None:
        """Startet die Strategie (blockierend, läuft bis shutdown)."""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful Shutdown — stoppt alle Komponenten."""
        ...

    @abstractmethod
    def get_status(self) -> dict:
        """Vollständiger Status für Web-Dashboard."""
        ...
