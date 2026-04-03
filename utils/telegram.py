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
    # --- Advanced Metrics ---
    slippage_pct: float = 0, ob_imbalance_pct: float = 0,
    cex_lag_ms: float = 0, regime: str = "",
    confluence: int = 0, confidence_score: float = 0,
    fill_type: str = "taker", gas_fee_usd: float = 0,
    implied_prob: float = 0,
) -> None:
    """Signal erkannt: Vollstaendige Analyse mit Advanced HFT Metrics."""
    _stats["signals"] += 1
    arrow = "🟩" if direction == "UP" else "🟥"
    conf = "🔥" if abs(momentum) > 0.30 else "⚡" if abs(momentum) > 0.15 else "📊"

    implied_str = f"{implied_prob * 100:.1f}%" if implied_prob > 0 else f"{ask * 100:.1f}%"
    regime_str = regime if regime else "N/A"
    # OB Imbalance: positive = bullish pressure
    ob_sign = "+" if ob_imbalance_pct >= 0 else ""

    await send_alert(
        f"{arrow} <b>SIGNAL #{_stats['signals']}: {asset} {direction}</b> {conf}\n\n"
        f"📈 Momentum: <b>{momentum:+.3f}%</b> → Model p={p_true:.3f}\n"
        f"💱 Ask: {ask:.3f} | Implied: {implied_str}\n"
        f"💰 Size: ${size:.2f} | Kelly: {kelly:.3f}\n\n"
        f"🔍 <b>ADVANCED METRICS</b>\n"
        f"<pre>"
        f"Net EV: {ev:+.2f}%     Fee: {fee_pct:.2f}%\n"
        f"Spread: {spread_pct:.1f}%      Slip: {slippage_pct:.1f}%\n"
        f"Liq:    ${liquidity:,.0f}\n"
        f"OB Imb: {ob_sign}{ob_imbalance_pct:.1f}%\n"
        f"CEX Lag:{cex_lag_ms:5.1f}s   Transit: {transit_ms:.0f}ms\n"
        f"Expiry: {seconds_to_expiry:.0f}s     Regime: {regime_str}\n"
        f"Confl:  {confluence}/5    Score: {confidence_score:.0f}/100\n"
        f"Fill:   {fill_type:7s}  Gas: ${gas_fee_usd:.3f}"
        f"</pre>\n\n"
        f"📍 <i>{question[:60]}</i>"
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


def _format_trade_table(trades: list[dict], max_rows: int = 8) -> str:
    """Formatiert eine monospace Trade-Tabelle fuer den Hourly Report.

    Jeder Trade-Dict braucht: trade_id, asset, direction, size_usd, pnl_usd,
    net_ev_pct, fee_pct, spread_pct, market_liquidity_usd, transit_latency_ms,
    seconds_to_expiry, confidence_score.
    """
    if not trades:
        return "  (keine Trades)\n"

    # Header
    header = "ID      A Dir  $Sz   PnL    EV%  Fee% Spr% Liq$   Lat Exp  Sc"
    lines = [header]

    recent = sorted(trades, key=lambda t: t.get("exit_ts", 0), reverse=True)[:max_rows]
    for t in recent:
        tid = t.get("trade_id", "?")[-3:]      # Last 3 chars: "028"
        asset_code = t.get("asset", "?")[0]      # B or E
        direction = t.get("direction", "?")[:2]  # UP or DN
        size = t.get("size_usd", 0)
        pnl = t.get("pnl_usd", 0)
        ev = t.get("net_ev_pct", 0)
        fee = t.get("fee_pct", 0)
        spread = t.get("spread_pct", 0)
        liq = t.get("market_liquidity_usd", 0)
        transit = t.get("transit_latency_ms", 0)
        expiry = t.get("seconds_to_expiry", 0)
        score = t.get("confidence_score", 0)

        # Liquidity in K
        liq_k = f"{liq/1000:.1f}k" if liq >= 1000 else f"{liq:.0f}"

        lines.append(
            f"{tid:>3}    {asset_code} {direction:<2} {size:5.2f} {pnl:+6.2f} "
            f"{ev:+5.1f} {fee:4.2f} {spread:4.1f} {liq_k:>5} "
            f"{transit:4.0f} {expiry:3.0f} {score:3.0f}"
        )

    return "\n".join(lines) + "\n"


async def alert_hourly_report(
    # --- Wallet ---
    balance: float,
    initial_deposit: float = 79.99,
    tangem_withdrawal: float = 10.00,
    # --- Trade Stats ---
    real_trades: list[dict] | None = None,
    rejected_count: int = 0,
    wins_count: int = 0,
    losses_count: int = 0,
    avg_win: float = 0,
    avg_loss: float = 0,
    wl_ratio: float = 0,
    live_pnl: float = 0,
    # --- Market Info ---
    windows_tradeable: int = 0,
    windows_total: int = 0,
    signals_session: int = 0,
    orders_placed: int = 0,
    ticks: int = 0,
    # --- Advanced Aggregate Stats ---
    avg_net_ev: float = 0,
    avg_liquidity: float = 0,
    avg_transit_ms: float = 0,
    avg_cex_lag_s: float = 0,
    regime_breakdown: str = "",
    avg_confluence: float = 0,
) -> None:
    """Stuendlicher Forensik-Report mit monospace Trade-Tabelle und Advanced Stats."""
    if real_trades is None:
        real_trades = []

    total_real = len(real_trades)
    wr = wins_count / total_real * 100 if total_real > 0 else 0

    # Portfolio
    portfolio = balance + tangem_withdrawal
    total_profit = portfolio - initial_deposit
    profit_pct = total_profit / initial_deposit * 100 if initial_deposit > 0 else 0

    # Trade Table
    trade_table = _format_trade_table(real_trades)

    # Regime fallback
    if not regime_breakdown:
        regime_breakdown = "N/A"

    msg = (
        f"{'═' * 29}\n"
        f"📊 <b>STUENDLICHER REPORT</b> | {_uptime()}\n"
        f"{'═' * 29}\n\n"
        f"🏦 <b>WALLET</b>\n"
        f"├ Balance: <b>${balance:.2f}</b> USDC.e\n"
        f"├ Portfolio: <b>${portfolio:.2f}</b> (+Tangem ${tangem_withdrawal:.0f})\n"
        f"└ Profit: <b>${total_profit:+.2f} ({profit_pct:+.1f}%)</b>\n\n"
        f"📈 <b>LIVE TRADES</b> ({total_real} echte | {rejected_count} rejected)\n"
        f"├ {wins_count}W / {losses_count}L = <b>{wr:.0f}% WR</b>\n"
        f"├ Avg Win: ${avg_win:.2f} | Avg Loss: ${avg_loss:.2f}\n"
        f"├ W/L Ratio: <b>{wl_ratio:.2f}x</b>\n"
        f"└ Live PnL: <b>${live_pnl:+.2f}</b>\n\n"
        f"<pre>{trade_table}</pre>\n"
        f"📊 <b>ADVANCED STATS</b>\n"
        f"├ Avg Net EV: <b>{avg_net_ev:+.1f}%</b>\n"
        f"├ Avg Liquidity: ${avg_liquidity:,.0f}\n"
        f"├ Avg Transit: {avg_transit_ms:.0f}ms | Avg CEX Lag: {avg_cex_lag_s:.1f}s\n"
        f"├ Regime: {regime_breakdown}\n"
        f"└ Avg Confluence: {avg_confluence:.1f}/5\n\n"
        f"🎯 <b>MARKT</b>\n"
        f"├ Windows: {windows_tradeable}/{windows_total}\n"
        f"├ Signale: {signals_session} | Orders: {orders_placed}\n"
        f"└ Ticks: {ticks}\n\n"
        f"🎯 Naechstes Ziel: $1,000 → $100 Charity"
    )

    await send_alert(msg, silent=True)
