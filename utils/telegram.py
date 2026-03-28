"""Telegram Alert Module — Sendet Trading-Alerts via Telegram Bot API.

Kein extra Package nötig — nutzt aiohttp (bereits installiert) für HTTPS POST.

Setup:
1. @BotFather → /newbot → Token kopieren
2. @userinfobot → Chat-ID kopieren
3. In .env: TELEGRAM_BOT_TOKEN=... und TELEGRAM_CHAT_ID=...
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiohttp
from loguru import logger

if TYPE_CHECKING:
    from config import Settings

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Singleton-Session (wird beim ersten Alert erstellt)
_session: aiohttp.ClientSession | None = None
_token: str = ""
_chat_id: str = ""
_enabled: bool = False


def configure(settings: Settings) -> None:
    """Konfiguriert Telegram mit Settings."""
    global _token, _chat_id, _enabled
    _token = settings.telegram_bot_token
    _chat_id = settings.telegram_chat_id
    _enabled = bool(_token and _chat_id)
    if _enabled:
        logger.info(f"Telegram Alerts aktiviert (Chat-ID: {_chat_id})")
    else:
        logger.info("Telegram Alerts deaktiviert (kein Token/Chat-ID in .env)")


async def send_alert(message: str, silent: bool = False) -> bool:
    """Sendet eine Nachricht via Telegram Bot API.

    Args:
        message: Text der Nachricht (Markdown V2 erlaubt)
        silent: True = keine Benachrichtigung auf dem Handy (stille Zustellung)

    Returns:
        True wenn erfolgreich gesendet
    """
    global _session

    if not _enabled:
        return False

    try:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

        url = _API_BASE.format(token=_token)
        payload = {
            "chat_id": _chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }

        async with _session.post(url, json=payload) as resp:
            if resp.status == 200:
                return True
            else:
                body = await resp.text()
                logger.warning(f"Telegram API Fehler {resp.status}: {body[:100]}")
                return False

    except Exception as e:
        logger.warning(f"Telegram Alert fehlgeschlagen: {e}")
        return False


async def close() -> None:
    """Schließt die HTTP-Session."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


# --- Vorgefertigte Alert-Templates ---

async def alert_startup(capital: float, mode: str) -> None:
    await send_alert(
        f"🟢 <b>Bot gestartet</b>\n"
        f"Modus: {mode}\n"
        f"Kapital: ${capital:.2f}\n"
        f"Strategie: Polymarket Latency Arb"
    )


async def alert_shutdown(capital: float, trades: int, pnl: float) -> None:
    await send_alert(
        f"🔴 <b>Bot gestoppt</b>\n"
        f"Trades: {trades}\n"
        f"PnL: ${pnl:+.2f}\n"
        f"Endkapital: ${capital:.2f}"
    )


async def alert_signal(
    asset: str, direction: str, momentum: float,
    ask: float, ev: float, size: float, question: str
) -> None:
    arrow = "⬆️" if direction == "UP" else "⬇️"
    await send_alert(
        f"{arrow} <b>SIGNAL: {asset} {direction}</b>\n"
        f"Momentum: {momentum:+.3f}%\n"
        f"PM Ask: {ask:.3f}\n"
        f"Net EV: {ev:.2f}%\n"
        f"Size: ${size:.2f}\n"
        f"<i>{question[:60]}</i>"
    )


async def alert_resolved(
    trade_id: str, asset: str, direction: str,
    won: bool, pnl: float, capital: float
) -> None:
    icon = "✅" if won else "❌"
    await send_alert(
        f"{icon} <b>{trade_id}: {'WIN' if won else 'LOSS'}</b>\n"
        f"{asset} {direction}\n"
        f"PnL: ${pnl:+.2f}\n"
        f"Kapital: ${capital:.2f}"
    )


async def alert_drawdown(pct: float, capital: float) -> None:
    await send_alert(
        f"⚠️ <b>Drawdown-Warnung: {pct:.1f}%</b>\n"
        f"Kapital: ${capital:.2f}\n"
        f"Kill-Switch bei -20%"
    )


async def alert_kill_switch(pct: float, capital: float) -> None:
    await send_alert(
        f"🚨 <b>KILL SWITCH AKTIVIERT</b>\n"
        f"Drawdown: {pct:.1f}%\n"
        f"Kapital: ${capital:.2f}\n"
        f"Keine weiteren Trades!"
    )
