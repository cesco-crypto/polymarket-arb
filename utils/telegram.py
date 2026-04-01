"""Telegram Alert Module — Professional HFT Trading Alerts.

Kein extra Package nötig — nutzt aiohttp (bereits installiert) für HTTPS POST.

Setup:
1. @BotFather → /newbot → Token kopieren
2. @userinfobot → Chat-ID kopieren
3. In .env: TELEGRAM_BOT_TOKEN=... und TELEGRAM_CHAT_ID=...
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp
from loguru import logger

if TYPE_CHECKING:
    from config import Settings

import os

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Singleton-Session (wird beim ersten Alert erstellt)
_session: aiohttp.ClientSession | None = None
_token: str = ""
_chat_id: str = ""
_enabled: bool = False
_instance: str = ""
_start_time: float = 0

# Performance Tracking
_stats = {
    "signals": 0,
    "trades_opened": 0,
    "trades_won": 0,
    "trades_lost": 0,
    "total_pnl": 0.0,
    "live_orders_ok": 0,
    "live_orders_fail": 0,
}


def configure(settings: Settings) -> None:
    """Konfiguriert Telegram mit Settings."""
    global _token, _chat_id, _enabled, _instance, _start_time
    _token = settings.telegram_bot_token
    _chat_id = settings.telegram_chat_id
    _enabled = bool(_token and _chat_id)
    _instance = os.environ.get("INSTANCE_LABEL", "LOCAL")
    _start_time = time.time()
    if _enabled:
        logger.info(f"Telegram Alerts aktiviert (Chat-ID: {_chat_id}, Instance: {_instance})")


def _uptime() -> str:
    """Formatierte Uptime."""
    s = int(time.time() - _start_time)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m"


def _win_rate() -> str:
    total = _stats["trades_won"] + _stats["trades_lost"]
    if total == 0:
        return "—"
    return f"{_stats['trades_won']/total*100:.0f}%"


async def send_alert(message: str, silent: bool = False) -> bool:
    """Sendet eine Nachricht via Telegram Bot API."""
    global _session

    if not _enabled:
        return False

    try:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

        tagged = f"[{_instance}] {message}" if _instance else message

        url = _API_BASE.format(token=_token)
        payload = {
            "chat_id": _chat_id,
            "text": tagged,
            "parse_mode": "HTML",
            "disable_notification": silent,
            "disable_web_page_preview": True,
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


# === PROFESSIONAL ALERT TEMPLATES ===

async def alert_startup(capital: float, mode: str, live: bool = False, wallet: str = "") -> None:
    """Bot-Start: Zeigt Konfiguration und Infrastruktur-Details."""
    # Measure CLOB latency
    clob_ms = "?"
    binance_ms = "?"
    try:
        t0 = time.time()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get("https://clob.polymarket.com/markets?limit=1") as r:
                await r.read()
        clob_ms = f"{(time.time()-t0)*1000:.0f}ms"
    except Exception:
        pass
    try:
        t0 = time.time()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get("https://api.binance.com/api/v3/ping") as r:
                await r.read()
        binance_ms = f"{(time.time()-t0)*1000:.0f}ms"
    except Exception:
        pass

    # Geoblock check
    geo = "?"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get("https://polymarket.com/api/geoblock") as r:
                data = await r.json()
                blocked = data.get("blocked", True)
                country = data.get("country", "?")
                geo = f"{'BLOCKED' if blocked else 'OK'} ({country})"
    except Exception:
        pass

    if live:
        await send_alert(
            f"{'='*28}\n"
            f"🟢 <b>BOT GESTARTET — LIVE</b>\n"
            f"{'='*28}\n\n"
            f"💰 <b>Wallet:</b> <code>{wallet[:10]}...{wallet[-6:]}</code>\n"
            f"📊 <b>Max Trade:</b> $5.00 | Kill: -20%\n"
            f"💵 <b>Fee:</b> 1.80% Peak (Taker)\n\n"
            f"🌐 <b>Infrastruktur:</b>\n"
            f"├ CLOB Latenz: <b>{clob_ms}</b>\n"
            f"├ Binance Ping: <b>{binance_ms}</b>\n"
            f"├ Geoblock: <b>{geo}</b>\n"
            f"└ Region: {_instance}\n\n"
            f"⚡ Oracle: Binance WS (BTC+ETH)\n"
            f"🎯 Strategy: Latency Arb 5m/15m"
        )
    else:
        await send_alert(
            f"{'='*28}\n"
            f"🟢 <b>BOT GESTARTET — PAPER</b>\n"
            f"{'='*28}\n\n"
            f"💰 Kapital: ${capital:.2f}\n"
            f"🌐 Region: {_instance}\n"
            f"📊 CLOB: {clob_ms} | Binance: {binance_ms}"
        )


async def alert_shutdown(capital: float, trades: int, pnl: float) -> None:
    """Bot-Stop: Zusammenfassung der Session."""
    await send_alert(
        f"{'='*28}\n"
        f"🔴 <b>BOT GESTOPPT</b>\n"
        f"{'='*28}\n\n"
        f"📊 Trades: {trades} | WR: {_win_rate()}\n"
        f"💰 PnL: ${pnl:+.2f}\n"
        f"🏦 Kapital: ${capital:.2f}\n"
        f"⏱ Uptime: {_uptime()}\n"
        f"📈 Live Orders: {_stats['live_orders_ok']} OK / {_stats['live_orders_fail']} Fail"
    )


async def alert_signal(
    asset: str, direction: str, momentum: float,
    ask: float, ev: float, size: float, question: str,
    p_true: float = 0, fee_pct: float = 0, kelly: float = 0,
    liquidity: float = 0, spread_pct: float = 0,
    seconds_to_expiry: float = 0, transit_ms: float = 0,
) -> None:
    """Signal erkannt: Detaillierte Analyse."""
    _stats["signals"] += 1
    arrow = "🟩" if direction == "UP" else "🟥"
    conf = "🔥" if abs(momentum) > 0.30 else "⚡" if abs(momentum) > 0.15 else "📊"

    await send_alert(
        f"{arrow} <b>SIGNAL #{_stats['signals']}: {asset} {direction}</b> {conf}\n\n"
        f"📈 Mom: <b>{momentum:+.3f}%</b> → p={p_true:.3f}\n"
        f"💱 Ask: {ask:.3f} | EV: <b>{ev:+.2f}%</b>\n"
        f"💰 Size: ${size:.2f} | Kelly: {kelly:.3f}\n"
        f"📉 Fee: {fee_pct:.2f}% | Spread: {spread_pct:.1f}%\n"
        f"💧 Liq: ${liquidity:,.0f} | Exp: {seconds_to_expiry:.0f}s\n"
        f"⏱ Transit: {transit_ms:.0f}ms\n\n"
        f"<i>{question[:55]}</i>"
    )


async def alert_resolved(
    trade_id: str, asset: str, direction: str,
    won: bool, pnl: float, capital: float,
    entry_price: float = 0, oracle_entry: float = 0, oracle_exit: float = 0,
    markout_1s: float = 0, markout_5s: float = 0, markout_60s: float = 0,
    fee_usd: float = 0, size_usd: float = 0, exec_type: str = "PAPER",
) -> None:
    """Trade aufgelöst: PnL + Mark-out + Session Stats."""
    if won:
        _stats["trades_won"] += 1
    else:
        _stats["trades_lost"] += 1
    _stats["total_pnl"] += pnl

    icon = "✅" if won else "❌"
    pnl_pct = (pnl / size_usd * 100) if size_usd > 0 else 0
    oracle_move = ((oracle_exit - oracle_entry) / oracle_entry * 100) if oracle_entry > 0 else 0
    total_trades = _stats["trades_won"] + _stats["trades_lost"]

    await send_alert(
        f"{icon} <b>{trade_id}: {'WIN' if won else 'LOSS'}</b>\n"
        f"{'─'*26}\n"
        f"🎯 {asset} {direction} @ {entry_price:.3f}\n"
        f"💰 <b>PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)</b>\n"
        f"💵 Fee: ${fee_usd:.3f} | Size: ${size_usd:.2f}\n\n"
        f"📊 <b>Oracle:</b> ${oracle_entry:,.2f} → ${oracle_exit:,.2f} ({oracle_move:+.3f}%)\n"
        f"📉 <b>Mark-out:</b> T+1s={markout_1s:+.4f}% T+5s={markout_5s:+.4f}% T+60s={markout_60s:+.4f}%\n"
        f"⚙️ Exec: {exec_type}\n\n"
        f"{'─'*26}\n"
        f"📈 <b>Session:</b> {total_trades} Trades | WR: {_win_rate()}\n"
        f"💰 Total PnL: ${_stats['total_pnl']:+.2f}\n"
        f"🏦 Kapital: ${capital:.2f} | ⏱ {_uptime()}"
    )


async def alert_live_order(
    trade_id: str, asset: str, direction: str,
    success: bool, price: float = 0, size: float = 0,
    order_id: str = "", error: str = "", latency_ms: float = 0,
) -> None:
    """Live Order Ergebnis: Erfolg oder Fehler mit Details."""
    if success:
        _stats["live_orders_ok"] += 1
        await send_alert(
            f"🟢 <b>LIVE ORDER OK</b>\n"
            f"{'─'*26}\n"
            f"🎯 {asset} {direction} @ {price:.3f}\n"
            f"💰 Size: ${size:.2f}\n"
            f"🔑 ID: <code>{order_id[:20]}</code>\n"
            f"⏱ Latenz: <b>{latency_ms:.0f}ms</b>\n"
            f"📊 Live: {_stats['live_orders_ok']}/{_stats['live_orders_ok']+_stats['live_orders_fail']}"
        )
    else:
        _stats["live_orders_fail"] += 1
        await send_alert(
            f"❌ <b>ORDER FEHLER</b>\n"
            f"{'─'*26}\n"
            f"🎯 {asset} {direction} @ {price:.3f}\n"
            f"⚠️ <code>{error[:120]}</code>\n"
            f"📊 Fails: {_stats['live_orders_fail']}"
        )


async def alert_drawdown(pct: float, capital: float) -> None:
    await send_alert(
        f"⚠️ <b>DRAWDOWN: {pct:.1f}%</b>\n"
        f"{'─'*26}\n"
        f"🏦 Kapital: ${capital:.2f}\n"
        f"🚨 Kill-Switch bei -20%\n"
        f"📈 Session: {_stats['trades_won']+_stats['trades_lost']} Trades | WR: {_win_rate()}"
    )


async def alert_kill_switch(pct: float, capital: float) -> None:
    await send_alert(
        f"🚨🚨🚨 <b>KILL SWITCH</b> 🚨🚨🚨\n"
        f"{'─'*26}\n"
        f"📉 Drawdown: <b>{pct:.1f}%</b>\n"
        f"🏦 Kapital: ${capital:.2f}\n"
        f"⛔ KEINE WEITEREN TRADES\n\n"
        f"📊 Session: {_stats['trades_won']+_stats['trades_lost']} Trades | WR: {_win_rate()}\n"
        f"💰 Total PnL: ${_stats['total_pnl']:+.2f}"
    )


async def alert_trade_log(
    event: str, trade_id: str, asset: str, direction: str,
    entry: float, size: float, fee: float, momentum: float,
    p_true: float, pnl: float = 0, correct: bool = False,
    capital: float = 0, oracle_entry: float = 0, oracle_now: float = 0,
) -> None:
    """Forensischer Trade-Log (silent delivery)."""
    _stats["trades_opened"] += 1 if event == "open" else 0

    if event == "open":
        await send_alert(
            f"📋 <b>OPEN {trade_id}</b>\n"
            f"<code>{asset}|{direction}|@{entry:.4f}|${size:.2f}|"
            f"fee=${fee:.4f}|mom={momentum:.4f}%|p={p_true:.4f}|"
            f"oracle=${oracle_entry:.2f}</code>",
            silent=True,
        )
    elif event == "close":
        icon = "W" if correct else "L"
        await send_alert(
            f"📋 <b>CLOSE {trade_id}: {icon}</b>\n"
            f"<code>{asset}|{direction}|pnl=${pnl:+.4f}|@{entry:.4f}|"
            f"oracle=${oracle_entry:.2f}→${oracle_now:.2f}|"
            f"cap=${capital:.2f}</code>",
            silent=True,
        )


async def alert_heartbeat(
    trades: int, pnl: float, capital: float,
    windows_total: int = 0, windows_tradeable: int = 0,
    clob_latency_ms: float = 0, binance_ticks: int = 0,
) -> None:
    """Stündlicher Heartbeat: Bot ist am Leben + Key Metrics."""
    await send_alert(
        f"💓 <b>HEARTBEAT</b> | {_uptime()}\n"
        f"{'─'*26}\n"
        f"📊 Trades: {trades} | WR: {_win_rate()}\n"
        f"💰 PnL: ${pnl:+.2f} | Cap: ${capital:.2f}\n"
        f"🎯 Markets: {windows_tradeable}/{windows_total}\n"
        f"⏱ CLOB: {clob_latency_ms:.0f}ms | Ticks: {binance_ticks}\n"
        f"📈 Live: {_stats['live_orders_ok']} OK / {_stats['live_orders_fail']} Fail",
        silent=True,
    )
