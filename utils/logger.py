"""Loguru-Setup für den Arbitrage Bot."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_CONFIGURED = False


def setup_logger(level: str = "INFO", data_dir: Path | None = None) -> None:
    """Konfiguriert Loguru mit Console + File Output."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    logger.remove()  # Default-Handler entfernen

    # Console: kurzes Format
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # File: detailliertes Format
    if data_dir:
        data_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(data_dir / "bot.log"),
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
            rotation="10 MB",
            retention="7 days",
            compression="gz",
        )

    _CONFIGURED = True
