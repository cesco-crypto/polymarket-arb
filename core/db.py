"""Persistente Trade-Datenbank via Supabase (Postgres).

Unverlierbar: Daten überleben Deploys, Restarts, Server-Wechsel.
Forensisch: 50+ Felder pro Trade für professionelle Analyse.
Self-Optimizing: Bot liest historische Performance und passt Parameter an.

Setup:
1. Erstelle Projekt auf supabase.com (Free Tier: 500MB)
2. Erstelle Tabelle 'trades' (Schema unten)
3. Setze SUPABASE_URL und SUPABASE_KEY als Environment Variables

SQL für Tabelle (einmalig in Supabase SQL Editor ausführen):

CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Event
    event TEXT NOT NULL,  -- 'open' / 'close'
    trade_id TEXT NOT NULL,
    instance TEXT DEFAULT '',  -- 'FRANKFURT' / 'SINGAPUR'

    -- Asset & Direction
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT DEFAULT '',
    window_slug TEXT DEFAULT '',
    market_question TEXT DEFAULT '',

    -- Timing
    signal_ts DOUBLE PRECISION DEFAULT 0,
    entry_ts DOUBLE PRECISION DEFAULT 0,
    exit_ts DOUBLE PRECISION DEFAULT 0,
    order_post_ts DOUBLE PRECISION DEFAULT 0,

    -- Preise
    oracle_price_entry DOUBLE PRECISION DEFAULT 0,
    oracle_price_exit DOUBLE PRECISION DEFAULT 0,
    polymarket_bid DOUBLE PRECISION DEFAULT 0,
    polymarket_ask DOUBLE PRECISION DEFAULT 0,
    executed_price DOUBLE PRECISION DEFAULT 0,

    -- Signal
    momentum_pct DOUBLE PRECISION DEFAULT 0,
    p_true DOUBLE PRECISION DEFAULT 0,
    p_market DOUBLE PRECISION DEFAULT 0,
    raw_edge_pct DOUBLE PRECISION DEFAULT 0,
    fee_pct DOUBLE PRECISION DEFAULT 0,
    net_ev_pct DOUBLE PRECISION DEFAULT 0,

    -- Sizing
    size_usd DOUBLE PRECISION DEFAULT 0,
    shares DOUBLE PRECISION DEFAULT 0,
    fee_usd DOUBLE PRECISION DEFAULT 0,
    kelly_fraction DOUBLE PRECISION DEFAULT 0,

    -- Latenz
    signal_to_order_ms DOUBLE PRECISION DEFAULT 0,
    transit_latency_ms DOUBLE PRECISION DEFAULT 0,
    tick_age_ms DOUBLE PRECISION DEFAULT 0,

    -- Result
    outcome_correct BOOLEAN DEFAULT FALSE,
    pnl_usd DOUBLE PRECISION DEFAULT 0,
    pnl_pct DOUBLE PRECISION DEFAULT 0,

    -- Mark-out
    markout_1s DOUBLE PRECISION DEFAULT 0,
    markout_5s DOUBLE PRECISION DEFAULT 0,
    markout_10s DOUBLE PRECISION DEFAULT 0,
    markout_30s DOUBLE PRECISION DEFAULT 0,
    markout_60s DOUBLE PRECISION DEFAULT 0,

    -- Execution
    order_type TEXT DEFAULT 'taker',
    live_order_id TEXT DEFAULT '',
    live_order_success BOOLEAN DEFAULT FALSE,
    live_error TEXT DEFAULT '',

    -- Meta
    seconds_to_expiry DOUBLE PRECISION DEFAULT 0,
    market_liquidity_usd DOUBLE PRECISION DEFAULT 0,
    spread_pct DOUBLE PRECISION DEFAULT 0
);

CREATE INDEX idx_trades_trade_id ON trades(trade_id);
CREATE INDEX idx_trades_asset ON trades(asset);
CREATE INDEX idx_trades_event ON trades(event);
CREATE INDEX idx_trades_created ON trades(created_at);
"""

from __future__ import annotations

import os
from loguru import logger


# Lazy-init: nur wenn SUPABASE_URL gesetzt ist
_client = None
_initialized = False


def _get_client():
    """Lazy Supabase Client Initialization."""
    global _client, _initialized
    if _initialized:
        return _client

    _initialized = True
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")

    if not url or not key:
        logger.info("DB: Kein SUPABASE_URL/KEY — Datenbank deaktiviert (nur JSONL + Telegram)")
        return None

    try:
        from supabase import create_client
        _client = create_client(url, key)
        logger.info(f"DB: Supabase verbunden ({url[:30]}...)")
        return _client
    except Exception as e:
        logger.error(f"DB: Supabase Init Fehler: {e}")
        return None


async def insert_trade(data: dict, max_retries: int = 3) -> bool:
    """Speichert einen Trade in Supabase mit Retry — Daten dürfen nie verloren gehen."""
    client = _get_client()
    if not client:
        return False

    # Instance Label hinzufügen
    data["instance"] = os.environ.get("INSTANCE_LABEL", "LOCAL")

    # Nur bekannte Felder senden (Supabase wirft Fehler bei unbekannten)
    allowed_fields = {
        "event", "trade_id", "instance", "asset", "direction", "timeframe",
        "window_slug", "market_question", "signal_ts", "entry_ts", "exit_ts",
        "order_post_ts", "oracle_price_entry", "oracle_price_exit",
        "polymarket_bid", "polymarket_ask", "executed_price",
        "momentum_pct", "p_true", "p_market", "raw_edge_pct",
        "fee_pct", "net_ev_pct", "size_usd", "shares", "fee_usd",
        "kelly_fraction", "signal_to_order_ms", "transit_latency_ms",
        "tick_age_ms", "outcome_correct", "pnl_usd", "pnl_pct",
        "markout_1s", "markout_5s", "markout_10s", "markout_30s",
        "markout_60s", "order_type", "live_order_id", "live_order_success",
        "live_error", "seconds_to_expiry", "market_liquidity_usd", "spread_pct",
    }
    clean = {k: v for k, v in data.items() if k in allowed_fields}

    for attempt in range(max_retries):
        try:
            client.table("trades").insert(clean).execute()
            event = clean.get("event", "?")
            tid = clean.get("trade_id", "?")
            logger.info(f"DB: {event} {tid} → Supabase OK")
            return True
        except Exception as e:
            logger.warning(f"DB Insert Versuch {attempt+1}/{max_retries} fehlgeschlagen: {e}")
            if attempt < max_retries - 1:
                import asyncio
                await asyncio.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s

    logger.error(f"DB Insert ENDGÜLTIG FEHLGESCHLAGEN nach {max_retries} Versuchen: {clean.get('trade_id', '?')}")
    return False


async def get_all_trades(limit: int = 500) -> list[dict]:
    """Holt alle Trades aus Supabase."""
    client = _get_client()
    if not client:
        return []

    try:
        result = client.table("trades").select("*").order("created_at", desc=True).limit(limit).execute()
        return result.data or []
    except Exception as e:
        logger.error(f"DB Query Fehler: {e}")
        return []


async def get_closed_trades(limit: int = 200) -> list[dict]:
    """Holt nur geschlossene Trades."""
    client = _get_client()
    if not client:
        return []

    try:
        result = (
            client.table("trades")
            .select("*")
            .eq("event", "close")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"DB Query Fehler: {e}")
        return []


async def get_trade_stats() -> dict:
    """Aggregierte Statistiken aus der DB."""
    closed = await get_closed_trades(500)
    if not closed:
        return {"total": 0, "wins": 0, "losses": 0, "pnl": 0, "win_rate": 0}

    wins = sum(1 for t in closed if t.get("outcome_correct"))
    total = len(closed)
    pnl = sum(t.get("pnl_usd", 0) for t in closed)
    avg_markout_5s = sum(t.get("markout_5s", 0) for t in closed) / total if total else 0

    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "pnl": round(pnl, 4),
        "avg_pnl": round(pnl / total, 4) if total else 0,
        "avg_markout_5s": round(avg_markout_5s, 4),
    }
