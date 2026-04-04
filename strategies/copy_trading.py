"""Copy Trading Strategy — Kopiert die besten Polymarket Wallets.

Überwacht Top-Wallets auf neue Trades und kopiert sie sofort.
Nutzt die öffentliche Polymarket Data API (kein Auth nötig).

Unabhängig von den Momentum-Strategien — kann parallel laufen.
Fokus auf langfristige Märkte (Politik, Sport, Events) wo
5-10s Kopier-Delay irrelevant ist.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from loguru import logger

from config import Settings
from core.executor import PolymarketExecutor
from core.trade_journal import TradeJournal, TradeRecord
from strategies.base import StrategyBase
from strategies.registry import register as register_strategy
from utils import telegram


# ═══════════════════════════════════════════════════════════════════
# TRACKED WALLETS — Top Polymarket Traders (aktiv, verifiziert)
# ═══════════════════════════════════════════════════════════════════

DEFAULT_TRACKED_WALLETS = [
    {
        "address": "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
        "name": "kch123",
        "pnl": 11_114_948,
        "category": "sports",
        "notes": "Sports specialist, large positions ($70K+), NHL/Hockey focus",
    },
    {
        "address": "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
        "name": "RN1",
        "pnl": 6_796_892,
        "category": "sports",
        "notes": "Diversified sports, small positions ($1-$57)",
    },
    {
        "address": "0x204f72f35326db932158cba6adff0b9a1da95e14",
        "name": "swisstony",
        "pnl": 5_696_383,
        "category": "sports",
        "notes": "Soccer/football focus, small positions",
    },
    {
        "address": "0x94f199fb7789f1aef7fff6b758d6b375100f4c7a",
        "name": "KeyTransporter",
        "pnl": 5_711_460,
        "category": "mixed",
        "notes": "Top 10 all-time, diverse categories",
    },
    {
        "address": "0x23786fdad0073692157c6d7dc81f281843a35fcb",
        "name": "mikatrade77",
        "pnl": 5_147_999,
        "category": "mixed",
        "notes": "Top 15 all-time",
    },
]


# ═══════════════════════════════════════════════════════════════════
# DATA TYPES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TrackedTrade:
    """Ein erkannter Trade von einer tracked Wallet."""
    wallet_name: str
    wallet_address: str
    side: str             # "BUY" / "SELL"
    outcome: str          # "Yes" / "No" / "Up" / "Down" etc.
    price: float
    size_usd: float
    title: str            # Market title
    slug: str             # Market slug
    condition_id: str
    timestamp: int
    asset_id: str = ""    # Token asset ID für Order


@dataclass
class CopiedPosition:
    """Eine kopierte Position."""
    trade_id: str
    source_wallet: str
    source_name: str
    market_title: str
    market_slug: str
    condition_id: str
    side: str
    outcome: str
    entry_price: float
    size_usd: float
    copied_at: float
    live_order_id: str = ""
    live_success: bool = False
    resolved: bool = False
    pnl_usd: float = 0.0


# ═══════════════════════════════════════════════════════════════════
# COPY TRADING STRATEGY
# ═══════════════════════════════════════════════════════════════════

class CopyTradingStrategy(StrategyBase):
    """Kopiert die Trades der besten Polymarket Wallets.

    Überwacht N Wallets, erkennt neue BUY-Trades,
    und platziert sofort eine $5 LIVE Order auf demselben Market.
    """

    STRATEGY_NAME = "copy_trading"
    DESCRIPTION = (
        "Copy Trading: Kopiert Top-Wallets (kch123, RN1, swisstony, etc.) "
        "in Echtzeit. $5 pro Trade, alle Märkte (Sport, Politik, Events)."
    )

    @property
    def name(self) -> str:
        return self.STRATEGY_NAME

    @property
    def description(self) -> str:
        return self.DESCRIPTION

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.executor = PolymarketExecutor(settings)
        self.journal = TradeJournal()

        # Config
        self.poll_interval_s = 5.0         # Poll alle 5 Sekunden
        self.max_copy_size_usd = 5.0       # Fix $5 pro Copy-Trade
        self.max_concurrent = 5            # Max 5 gleichzeitig (25% von $94 = vertretbar)
        self.min_seconds_to_copy = 5       # Trade muss < 5 Min alt sein
        self.only_buys = False             # BUY + SELL kopieren (SELL = Exit-Signal)
        self.min_copy_price = 0.25         # Nicht unter 25¢ (>75% Verlustchance)
        self.max_copy_price = 0.85         # Nicht über 85¢ (schlechtes Risk/Reward)

        # Tracked Wallets
        self.tracked_wallets = list(DEFAULT_TRACKED_WALLETS)

        # State
        self._running = False
        self._last_seen: dict[str, int] = {}  # wallet → last seen timestamp
        self._seen_trade_keys: set = set()     # unique Trade-Keys → verhindert Duplikate per Wallet
        self._copied_positions: list[CopiedPosition] = []
        self._copy_count = 0
        self._recent_copies: deque = deque(maxlen=50)
        self._session: Optional[aiohttp.ClientSession] = None

    # ═══════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═══════════════════════════════════════════════════════════════

    async def run(self) -> None:
        self._running = True
        telegram.configure(self.settings)
        logger.info(f"Copy Trading Strategy startet — {len(self.tracked_wallets)} Wallets tracked")

        # Executor initialisieren (für LIVE Trading)
        if self.settings.live_trading:
            live_ok = await self.executor.initialize()
            if live_ok:
                logger.info("Copy Trading: LIVE MODUS — Echte Orders!")
            else:
                logger.warning("Copy Trading: Live Init fehlgeschlagen")
        else:
            logger.info("Copy Trading: PAPER MODUS")

        # HTTP Session
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "polymarket-arb/2.0"},
        )

        # Initial: Letzte Trades jeder Wallet laden (um keine alten Trades zu kopieren)
        await self._initialize_last_seen()

        try:
            await asyncio.gather(
                self._poll_loop(),
                self._status_loop(),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info(f"Copy Trading beendet. {self._copy_count} Trades kopiert.")

    # ═══════════════════════════════════════════════════════════════
    # INITIALIZATION
    # ═══════════════════════════════════════════════════════════════

    async def _initialize_last_seen(self) -> None:
        """Lade den letzten Trade jeder Wallet um altes nicht zu kopieren."""
        for wallet in self.tracked_wallets:
            try:
                trades = await self._fetch_activity(wallet["address"], limit=1)
                if trades:
                    self._last_seen[wallet["address"]] = trades[0].get("timestamp", 0)
                    logger.debug(f"Copy Init: {wallet['name']} last seen at {self._last_seen[wallet['address']]}")
                else:
                    self._last_seen[wallet["address"]] = int(time.time())
            except Exception as e:
                logger.error(f"Copy Init Error for {wallet['name']}: {e}")
                self._last_seen[wallet["address"]] = int(time.time())

        logger.info(f"Copy Trading initialisiert: {len(self._last_seen)} Wallets tracked")

    # ═══════════════════════════════════════════════════════════════
    # POLL LOOP
    # ═══════════════════════════════════════════════════════════════

    async def _poll_loop(self) -> None:
        """Hauptloop: Pollt alle Wallets auf neue Trades."""
        logger.info("Copy Trading Poll-Loop gestartet")
        while self._running:
            for wallet in self.tracked_wallets:
                if not self._running:
                    break
                try:
                    await self._check_wallet(wallet)
                except Exception as e:
                    logger.error(f"Copy Poll Error ({wallet['name']}): {e}")

            await asyncio.sleep(self.poll_interval_s)

    async def _check_wallet(self, wallet: dict) -> None:
        """Prüft eine Wallet auf neue Trades — mit allen Safety Guards."""
        addr = wallet["address"]
        name = wallet["name"]

        trades = await self._fetch_activity(addr, limit=5)
        if not trades:
            return

        last_seen = self._last_seen.get(addr, 0)

        for trade in trades:
            ts = trade.get("timestamp", 0)
            if ts <= last_seen:
                continue  # Schon gesehen

            # Neuer Trade!
            self._last_seen[addr] = max(self._last_seen.get(addr, 0), ts)

            side = trade.get("side", "")

            # ── GUARD 1: Unique Trade Key (verhindert Duplikate per Wallet) ──
            trade_key = f"{addr}_{ts}_{trade.get('conditionId','')}"
            if trade_key in self._seen_trade_keys:
                continue
            self._seen_trade_keys.add(trade_key)

            # ── SELL-Trades: Alert senden (Smart Wallet Exit = wichtiges Signal) ──
            if side == "SELL":
                logger.info(f"COPY EXIT SIGNAL: {name} SELL {trade.get('outcome','')} @ {trade.get('price',0)} | {trade.get('title','')[:40]}")
                asyncio.create_task(telegram.send_alert(
                    f"🔔 <b>EXIT SIGNAL</b>\n"
                    f"{'─'*26}\n"
                    f"👤 <b>{name}</b> hat VERKAUFT!\n"
                    f"📊 SELL {trade.get('outcome','')} @ {float(trade.get('price',0)):.3f}\n"
                    f"📍 {trade.get('title','')[:50]}\n"
                    f"⚠️ Prüfe ob du diese Position auch schliessen willst"
                ))
                continue  # SELL-Trades nicht kopieren (nur alertieren)

            # ── GUARD 2: Price Filter (keine Lotterietickets, kein schlechtes R/R) ──
            price = float(trade.get("price", 0))
            if price < self.min_copy_price or price > self.max_copy_price:
                logger.info(f"Copy Skip (Preis {price:.3f} ausserhalb {self.min_copy_price}-{self.max_copy_price}): {name} {trade.get('title','')[:40]}")
                continue

            # ── GUARD 3: Trade zu alt? ──
            age = time.time() - ts
            if age > self.min_seconds_to_copy * 60:  # 5 Minuten
                logger.debug(f"Copy Skip (zu alt: {age:.0f}s): {name}")
                continue

            # ── GUARD 4: Max concurrent? ──
            active = sum(1 for p in self._copied_positions if not p.resolved)
            if active >= self.max_concurrent:
                logger.warning(f"Copy Skip (max {self.max_concurrent} erreicht): {name}")
                continue

            # ── Alle Guards bestanden → Kopieren! ──
            # Cross-Wallet Confluence ERLAUBT: Wenn 2 Top-Wallets denselben
            # Markt kaufen ist das ein STÄRKERES Signal, nicht ein schwächeres.
            condition_id = trade.get("conditionId", "")
            tracked = TrackedTrade(
                wallet_name=name,
                wallet_address=addr,
                side=side,
                outcome=trade.get("outcome", ""),
                price=price,
                size_usd=float(trade.get("usdcSize", trade.get("size", 0))),
                title=trade.get("title", ""),
                slug=trade.get("slug", ""),
                condition_id=condition_id,
                timestamp=ts,
                asset_id=trade.get("asset", ""),
            )

            await self._copy_trade(tracked)

    # ═══════════════════════════════════════════════════════════════
    # COPY EXECUTION
    # ═══════════════════════════════════════════════════════════════

    async def _copy_trade(self, trade: TrackedTrade) -> None:
        """Kopiert einen erkannten Trade."""
        self._copy_count += 1
        trade_id = f"CT-{self._copy_count:04d}"

        logger.info(
            f"COPY #{self._copy_count}: {trade.wallet_name} → "
            f"{trade.side} {trade.outcome} @ {trade.price:.3f} | "
            f"${trade.size_usd:,.0f} | {trade.title[:50]}"
        )

        # Position tracken
        pos = CopiedPosition(
            trade_id=trade_id,
            source_wallet=trade.wallet_address,
            source_name=trade.wallet_name,
            market_title=trade.title,
            market_slug=trade.slug,
            condition_id=trade.condition_id,
            side=trade.side,
            outcome=trade.outcome,
            entry_price=trade.price,
            size_usd=self.max_copy_size_usd,
            copied_at=time.time(),
        )
        self._copied_positions.append(pos)

        # Recent Copies für Dashboard
        self._recent_copies.appendleft({
            "ts": time.strftime("%H:%M:%S"),
            "trade_id": trade_id,
            "source": trade.wallet_name,
            "side": trade.side,
            "outcome": trade.outcome,
            "price": round(trade.price, 3),
            "market": trade.title[:40],
            "original_size": round(trade.size_usd, 0),
        })

        # Telegram Alert — zeige Wallet-PnL aus Config
        wallet_pnl = 0
        for w in self.tracked_wallets:
            if w["address"] == trade.wallet_address:
                wallet_pnl = w.get("pnl", 0)
                break
        size_str = f"${trade.size_usd:,.0f}" if trade.size_usd > 0 else "N/A"
        asyncio.create_task(telegram.send_alert(
            f"📋 <b>COPY TRADE #{self._copy_count}</b>\n"
            f"{'─'*26}\n"
            f"👤 Source: <b>{trade.wallet_name}</b> (${wallet_pnl:,.0f} Lifetime PnL)\n"
            f"📊 {trade.side} {trade.outcome} @ {trade.price:.3f} (Orig: {size_str})\n"
            f"💰 Copy Size: ${self.max_copy_size_usd:.2f}\n"
            f"📍 {trade.title[:50]}\n"
            f"🔗 <code>{trade.slug}</code>"
        ))

        # LIVE Order platzieren
        if self.executor.is_live and trade.asset_id:
            try:
                res = await self.executor.place_order(
                    token_id=trade.asset_id,
                    side="BUY",
                    price=trade.price,
                    size_usd=self.max_copy_size_usd,
                    asset=trade.wallet_name,
                    direction=trade.outcome,
                )
                if res.success:
                    pos.live_order_id = res.order_id
                    pos.live_success = True
                    logger.info(f"COPY LIVE OK: {trade_id} — {res.order_id}")
                else:
                    logger.error(f"COPY LIVE FAILED: {trade_id} — {res.error}")
            except Exception as e:
                logger.error(f"COPY LIVE EXCEPTION: {trade_id} — {e}")

        # Journal
        self.journal.record_open(TradeRecord(
            trade_id=trade_id,
            asset=trade.wallet_name,
            direction=trade.outcome,
            signal_ts=float(trade.timestamp),
            entry_ts=time.time(),
            window_slug=trade.slug,
            market_question=trade.title[:60],
            executed_price=trade.price,
            size_usd=self.max_copy_size_usd,
            order_type="copy_trade",
            live_order_id=pos.live_order_id,
            live_order_success=pos.live_success,
        ))

    # ═══════════════════════════════════════════════════════════════
    # DATA API
    # ═══════════════════════════════════════════════════════════════

    async def _fetch_activity(self, wallet: str, limit: int = 5) -> list:
        """Holt die letzten Trades einer Wallet via Polymarket Data API."""
        if not self._session or self._session.closed:
            return []
        try:
            url = f"https://data-api.polymarket.com/activity?user={wallet}&limit={limit}&type=TRADE"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                return []
        except Exception as e:
            logger.debug(f"Fetch Activity Error: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════
    # STATUS
    # ═══════════════════════════════════════════════════════════════

    async def _status_loop(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            active = sum(1 for p in self._copied_positions if not p.resolved)
            logger.info(
                f"COPY STATUS | Tracked: {len(self.tracked_wallets)} Wallets | "
                f"Copies: {self._copy_count} | Active: {active}"
            )

    def get_status(self) -> dict:
        active = sum(1 for p in self._copied_positions if not p.resolved)
        return {
            "strategy": self.STRATEGY_NAME,
            "running": self._running,
            "tracked_wallets": [
                {"name": w["name"], "address": w["address"][:10] + "...", "pnl": w["pnl"]}
                for w in self.tracked_wallets
            ],
            "copies_total": self._copy_count,
            "copies_active": active,
            "recent_copies": list(self._recent_copies),
            "config": {
                "poll_interval_s": self.poll_interval_s,
                "max_copy_size_usd": self.max_copy_size_usd,
                "max_concurrent": self.max_concurrent,
                "only_buys": self.only_buys,
            },
        }


# Auto-Register
register_strategy(CopyTradingStrategy.STRATEGY_NAME, CopyTradingStrategy)
