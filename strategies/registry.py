"""Strategy Registry — Factory Pattern für Strategy-Switching.

Registriert alle verfügbaren Strategien. Dashboard nutzt dies um:
1. Alle Strategien aufzulisten (Name + Beschreibung)
2. Per Klick eine neue Strategie zu instanziieren
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from config import Settings
    from strategies.base import StrategyBase

# Registry: name → (class, description)
_REGISTRY: dict[str, type] = {}


def register(name: str, cls: type) -> None:
    """Registriert eine Strategie-Klasse unter einem eindeutigen Namen."""
    _REGISTRY[name] = cls
    logger.debug(f"Strategy registriert: {name}")


def create_strategy(name: str, settings: "Settings", **kwargs) -> "StrategyBase":
    """Erstellt eine neue Strategie-Instanz per Name.

    kwargs werden an den Constructor durchgereicht (z.B. learn_machine=...).
    Strategien die den Parameter nicht kennen ignorieren ihn.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        available = ", ".join(_REGISTRY.keys()) or "(keine)"
        raise ValueError(f"Unbekannte Strategie: '{name}'. Verfügbar: {available}")
    return cls(settings, **kwargs)


def list_strategies() -> dict[str, str]:
    """Gibt alle registrierten Strategien zurück: {name: description}."""
    result = {}
    for name, cls in _REGISTRY.items():
        desc = getattr(cls, "DESCRIPTION", cls.__doc__ or "")
        result[name] = desc
    return result


def get_registry() -> dict[str, type]:
    """Gibt die rohe Registry zurück (für Tests/Debugging)."""
    return dict(_REGISTRY)
