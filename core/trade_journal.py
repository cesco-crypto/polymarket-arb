"""Trade Journal — Unverlierbare forensische Handelsdaten.

Speichert JEDEN Trade in drei unabhängigen Systemen:
1. JSON-Append-Log (lokal, überlebt Code-Änderungen, NICHT deploys)
2. Telegram-Nachricht (permanent, überlebt alles)
3. REST-API Endpoint (Dashboard kann Daten abfragen)

Felder pro Trade (HFT-Grade):
- Timing: entry_ts, exit_ts, signal_ts, order_post_ts
- Preise: oracle_entry, oracle_exit, polymarket_bid, polymarket_ask, executed_price
- Latenz: signal_to_order_ms, transit_latency_ms, tick_age_ms
- Sizing: size_usd, shares, fee_usd, kelly_fraction
- Signal: momentum_pct, p_true, p_market, net_ev_pct, fee_pct
- Result: outcome_correct, pnl_usd, pnl_pct
- Mark-out: price_t1s, price_t5s, price_t10s, price_t30s, price_t60s
- Meta: asset, direction, order_type (maker/taker), window_slug, market_question
- Execution: live_order_id, live_order_success, live_error
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from loguru import logger
from core import db
from utils import telegram


JOURNAL_PATH = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"


@dataclass
class TradeRecord:
    """Forensischer Trade-Record mit allen HFT-relevanten Daten."""

    # --- ID ---
    trade_id: str = ""
    event: str = ""  # "open" / "close"

    # --- Timing ---
    signal_ts: float = 0.0        # Wann das Momentum-Signal erkannt wurde
    entry_ts: float = 0.0         # Wann die Position eröffnet wurde
    exit_ts: float = 0.0          # Wann aufgelöst
    order_post_ts: float = 0.0    # Wann die Live-Order an CLOB gesendet wurde

    # --- Asset & Direction ---
    asset: str = ""               # BTC / ETH
    direction: str = ""           # UP / DOWN
    window_slug: str = ""         # btc-updown-5m-{ts}
    market_question: str = ""
    timeframe: str = ""           # 5m / 15m

    # --- Preise ---
    oracle_price_entry: float = 0.0   # Binance-Preis bei Signal
    oracle_price_exit: float = 0.0    # Binance-Preis bei Resolution
    polymarket_bid: float = 0.0       # PM Best Bid bei Entry
    polymarket_ask: float = 0.0       # PM Best Ask bei Entry
    executed_price: float = 0.0       # Tatsächlicher Fill-Preis (= ask bei Taker)

    # --- Signal-Qualität ---
    momentum_pct: float = 0.0
    p_true: float = 0.0
    p_market: float = 0.0
    raw_edge_pct: float = 0.0
    fee_pct: float = 0.0
    net_ev_pct: float = 0.0

    # --- Sizing ---
    size_usd: float = 0.0
    shares: float = 0.0
    fee_usd: float = 0.0
    kelly_fraction: float = 0.0

    # --- Latenz ---
    signal_to_order_ms: float = 0.0   # Signal erkannt → Order gepostet
    transit_latency_ms: float = 0.0   # Binance Server → unsere Maschine
    tick_age_ms: float = 0.0          # Alter des Ticks bei Signal

    # --- Result ---
    outcome_correct: bool = False
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0

    # --- Mark-out ---
    markout_1s: float = 0.0
    markout_5s: float = 0.0
    markout_10s: float = 0.0
    markout_30s: float = 0.0
    markout_60s: float = 0.0

    # --- Execution ---
    order_type: str = ""          # taker / maker
    live_order_id: str = ""
    live_order_success: bool = False
    live_error: str = ""

    # --- Meta ---
    seconds_to_expiry: float = 0.0
    market_liquidity_usd: float = 0.0
    spread_pct: float = 0.0


class TradeJournal:
    """Unverlierbare Trade-Datenbank."""

    def __init__(self) -> None:
        self._records: list[TradeRecord] = []
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Lade bestehende Records beim Start
        self._load_existing()

    def _load_existing(self) -> None:
        """Lädt bestehende Journal-Einträge beim Start.

        Merges live_update events into their corresponding open/close records.
        """
        if JOURNAL_PATH.exists():
            try:
                live_updates: dict[str, dict] = {}  # trade_id -> live data
                raw_records: list[dict] = []
                with open(JOURNAL_PATH) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            data = json.loads(line)
                            if data.get("event") == "live_update":
                                live_updates[data.get("trade_id", "")] = data
                            else:
                                raw_records.append(data)

                # Merge live_update data into close records
                for data in raw_records:
                    if data.get("event") == "close":
                        tid = data.get("trade_id", "")
                        if tid in live_updates:
                            upd = live_updates[tid]
                            data["live_order_success"] = upd.get("live_order_success", False)
                            data["live_order_id"] = upd.get("live_order_id", "")
                            data["live_error"] = upd.get("live_error", "")
                            data["order_post_ts"] = upd.get("order_post_ts", 0)

                    self._records.append(TradeRecord(**{
                        k: v for k, v in data.items()
                        if k in TradeRecord.__dataclass_fields__
                    }))
                logger.info(f"TradeJournal: {len(self._records)} Einträge geladen, {len(live_updates)} live_updates merged")
            except Exception as e:
                logger.warning(f"TradeJournal Load Fehler: {e}")

    def record_open(self, rec: TradeRecord) -> None:
        """Speichert Trade-Open in alle 4 Systeme: RAM + JSONL + Telegram + Supabase."""
        rec.event = "open"
        self._records.append(rec)
        self._append_to_file(rec)
        asyncio.create_task(self._safe_async(self._send_telegram_open(rec), "telegram_open"))
        asyncio.create_task(self._safe_async(db.insert_trade(asdict(rec)), "supabase_open"))
        logger.info(f"JOURNAL OPEN: {rec.trade_id} | RAM + JSONL + Telegram + Supabase")

    def update_live_result(self, trade_id: str, success: bool, order_id: str, error: str) -> None:
        """Schreibt das Live-Order-Ergebnis zurück in den bestehenden TradeRecord.

        Wird aufgerufen nachdem die Live-Order auf Polymarket platziert wurde.
        Aktualisiert RAM + schreibt Update-Zeile ins JSONL.
        """
        # RAM-Record aktualisieren
        for rec in reversed(self._records):
            if rec.trade_id == trade_id:
                rec.live_order_success = success
                rec.live_order_id = order_id
                rec.live_error = error
                rec.order_post_ts = time.time()
                break

        # JSONL: Update-Zeile anhängen (event="live_update")
        update = {
            "event": "live_update",
            "trade_id": trade_id,
            "live_order_success": success,
            "live_order_id": order_id,
            "live_error": error,
            "order_post_ts": time.time(),
        }
        try:
            with open(JOURNAL_PATH, "a") as f:
                f.write(json.dumps(update) + "\n")
        except Exception as e:
            logger.error(f"Journal live_update write Fehler: {e}")

        # Supabase
        asyncio.create_task(self._safe_async(db.insert_trade(update), "supabase_live_update"))

        status = "✅ SUCCESS" if success else f"❌ FAILED: {error[:60]}"
        logger.info(f"JOURNAL LIVE_UPDATE: {trade_id} | {status}")

    def record_close(self, rec: TradeRecord) -> None:
        """Speichert Trade-Close in alle 4 Systeme: RAM + JSONL + Telegram + Supabase."""
        rec.event = "close"
        self._records.append(rec)
        self._append_to_file(rec)
        asyncio.create_task(self._safe_async(self._send_telegram_close(rec), "telegram_close"))
        asyncio.create_task(self._safe_async(db.insert_trade(asdict(rec)), "supabase_close"))
        logger.info(f"JOURNAL CLOSE: {rec.trade_id} | RAM + JSONL + Telegram + Supabase")

    async def _safe_async(self, coro, label: str) -> None:
        """Fire-and-forget wrapper that never crashes the caller."""
        try:
            await coro
        except Exception as e:
            logger.error(f"Journal async '{label}' Fehler: {e}")

    def _append_to_file(self, rec: TradeRecord) -> None:
        """Append-only JSONL — eine Zeile pro Event, überlebt keine Deploys aber Render Starter Restarts."""
        try:
            with open(JOURNAL_PATH, "a") as f:
                f.write(json.dumps(asdict(rec)) + "\n")
        except Exception as e:
            logger.error(f"Journal Write Fehler: {e}")

    async def _send_telegram_open(self, rec: TradeRecord) -> None:
        """Forensisches Telegram-Log: OPEN."""
        await telegram.send_alert(
            f"📊 <b>JOURNAL: OPEN {rec.trade_id}</b>\n"
            f"<code>"
            f"{rec.asset} {rec.direction} | {rec.timeframe}\n"
            f"Oracle: {rec.oracle_price_entry:.2f}\n"
            f"PM: bid={rec.polymarket_bid:.3f} ask={rec.polymarket_ask:.3f}\n"
            f"Mom: {rec.momentum_pct:+.4f}% | p={rec.p_true:.4f}\n"
            f"EV: {rec.net_ev_pct:.2f}% | Fee: {rec.fee_pct:.2f}%\n"
            f"Size: ${rec.size_usd:.2f} | Kelly: {rec.kelly_fraction:.4f}\n"
            f"Transit: {rec.transit_latency_ms:.0f}ms\n"
            f"Liq: ${rec.market_liquidity_usd:.0f} | Spread: {rec.spread_pct:.2f}%\n"
            f"Expiry: {rec.seconds_to_expiry:.0f}s | Type: {rec.order_type}\n"
            f"Slug: {rec.window_slug}"
            f"</code>",
            silent=True,
        )

    async def _send_telegram_close(self, rec: TradeRecord) -> None:
        """Forensisches Telegram-Log: CLOSE."""
        icon = "✅" if rec.outcome_correct else "❌"
        await telegram.send_alert(
            f"📊 <b>JOURNAL: {icon} {rec.trade_id}</b>\n"
            f"<code>"
            f"{rec.asset} {rec.direction} → {'WIN' if rec.outcome_correct else 'LOSS'}\n"
            f"PnL: ${rec.pnl_usd:+.4f} ({rec.pnl_pct:+.2f}%)\n"
            f"Oracle: {rec.oracle_price_entry:.2f} → {rec.oracle_price_exit:.2f}\n"
            f"Markout: T+1s={rec.markout_1s:+.4f}% T+5s={rec.markout_5s:+.4f}%\n"
            f"         T+10s={rec.markout_10s:+.4f}% T+60s={rec.markout_60s:+.4f}%\n"
            f"Exec: {rec.live_order_id[:16] if rec.live_order_id else 'PAPER'}\n"
            f"Signal→Order: {rec.signal_to_order_ms:.0f}ms"
            f"</code>",
            silent=True,
        )

    def get_all_records(self) -> list[dict]:
        """Alle Records als dict-Liste für API/Dashboard."""
        return [asdict(r) for r in self._records]

    def get_closed_records(self) -> list[dict]:
        return [asdict(r) for r in self._records if r.event == "close"]

    def stats(self) -> dict:
        closed = [r for r in self._records if r.event == "close"]
        if not closed:
            return {"total": 0, "wins": 0, "losses": 0, "pnl": 0}
        wins = sum(1 for r in closed if r.outcome_correct)
        return {
            "total": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / len(closed) * 100, 1),
            "pnl": round(sum(r.pnl_usd for r in closed), 4),
            "avg_markout_5s": round(
                sum(r.markout_5s for r in closed) / len(closed), 4
            ) if closed else 0,
        }
