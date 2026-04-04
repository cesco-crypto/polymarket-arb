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
from pathlib import Path
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
    outcome_index: int = -1   # 0 oder 1 (welches Outcome)
    source_tx_hash: str = ""  # Original Blockchain Transaction Hash


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
    token_id: str = ""           # Polymarket Token ID (für SELL-Orders)
    outcome_index: int = -1      # 0 oder 1 (welches Outcome)
    is_hedge: bool = False       # True wenn dies der Hedge-Leg ist
    source_tx_hash: str = ""     # Original trader's tx hash on Polygon
    source_orig_usd: float = 0.0 # Trader's original trade size in USD (for hedge ratio)
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
        self._seen_trade_keys: set = set()     # unique Trade-Keys → verhindert Duplikate
        self._seen_file = Path("data/copy_seen_trades.json")  # Persistent auf Disk
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
        """Lade gesehene Trades von Disk + API um Restart-Duplikate zu verhindern.

        PERSISTENTER DEDUP: _seen_trade_keys wird auf Disk gespeichert und beim
        Restart wiedergeladen. Kein Re-Copy mehr nach Deploys/Crashes.
        """
        # 1. Lade persistenten State von Disk
        self._seen_file.parent.mkdir(parents=True, exist_ok=True)
        if self._seen_file.exists():
            try:
                with open(self._seen_file) as f:
                    saved = json.load(f)
                self._seen_trade_keys = set(saved.get("seen_keys", []))
                self._last_seen = saved.get("last_seen", {})
                self._copy_count = saved.get("copy_count", 0)
                logger.info(f"Copy Trading: {len(self._seen_trade_keys)} gesehene Trades von Disk geladen")
            except Exception as e:
                logger.warning(f"Copy Seen Load Error: {e}")

        # 2. Zusätzlich: aktuelle API-Daten laden und markieren
        total_marked = 0
        for wallet in self.tracked_wallets:
            addr = wallet["address"]
            try:
                trades = await self._fetch_activity(addr, limit=20)
                if trades:
                    for t in trades:
                        ts = t.get("timestamp", 0)
                        cid = t.get("conditionId", "")
                        oi = t.get("outcomeIndex", -1)
                        # Alle Key-Typen markieren (v3.0: Outcome-aware)
                        self._seen_trade_keys.add(f"{addr}_{ts}_{cid}")
                        self._seen_trade_keys.add(f"{addr}_{cid}_{oi}")  # Outcome-Key (v3.0)
                        self._seen_trade_keys.add(f"{addr}_{cid}")  # Legacy Market-Key
                        total_marked += 1
                    self._last_seen[addr] = max(
                        self._last_seen.get(addr, 0),
                        trades[0].get("timestamp", 0)
                    )
                else:
                    if addr not in self._last_seen:
                        self._last_seen[addr] = int(time.time())
            except Exception as e:
                logger.error(f"Copy Init Error for {wallet['name']}: {e}")
                if addr not in self._last_seen:
                    self._last_seen[addr] = int(time.time())

        # 3. State sofort sichern
        self._save_seen_state()
        logger.info(f"Copy Trading initialisiert: {len(self._last_seen)} Wallets, {len(self._seen_trade_keys)} Keys persistent")

    def _save_seen_state(self) -> None:
        """Speichert _seen_trade_keys persistent auf Disk."""
        try:
            self._seen_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._seen_file, "w") as f:
                json.dump({
                    "seen_keys": list(self._seen_trade_keys),
                    "last_seen": self._last_seen,
                    "copy_count": self._copy_count,
                    "saved_at": time.time(),
                }, f)
        except Exception as e:
            logger.error(f"Copy Seen Save Error: {e}")

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
        """Prüft eine Wallet auf neue Trades — mit allen Safety Guards.

        v3.0: Outcome-aware Dedup (Hedge-Copy erlaubt), SELL-Copy,
        proportionales Sizing, Market-basierter Concurrent-Counter.
        """
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
            condition_id = trade.get("conditionId", "")
            outcome_index = trade.get("outcomeIndex", -1)

            # ── GUARD 1: Unique Trade Key (verhindert Duplikate per Wallet) ──
            trade_key = f"{addr}_{ts}_{condition_id}"
            if trade_key in self._seen_trade_keys:
                continue
            self._seen_trade_keys.add(trade_key)

            # ── SELL-Trades: SELL-COPY wenn wir die Position haben ──
            if side == "SELL":
                await self._handle_sell_signal(trade, name, addr)
                continue

            # ── GUARD 2: Outcome-aware Dedup (Hedge = anderes Outcome ERLAUBT) ──
            # SAFETY: outcomeIndex muss 0 oder 1 sein, sonst Fallback auf Market-Key
            is_hedge = False
            if outcome_index in (0, 1):
                outcome_key = f"{addr}_{condition_id}_{outcome_index}"
                if outcome_key in self._seen_trade_keys:
                    logger.debug(f"Copy Skip (Tranche, gleicher Outcome): {name} {trade.get('title','')[:30]}")
                    continue
                self._seen_trade_keys.add(outcome_key)

                # Prüfe ob dies ein Hedge ist (anderes Outcome auf gleichem Market)
                other_key = f"{addr}_{condition_id}_{1 - outcome_index}"
                if other_key in self._seen_trade_keys:
                    is_hedge = True
                    logger.info(f"HEDGE DETECTED: {name} kauft BEIDE Seiten auf {trade.get('title','')[:35]}")
            else:
                # Fallback: kein outcomeIndex → Market-Level Dedup (konservativ)
                market_key = f"{addr}_{condition_id}"
                if market_key in self._seen_trade_keys:
                    logger.debug(f"Copy Skip (Market-level, no outcomeIndex): {name} {trade.get('title','')[:30]}")
                    continue
                self._seen_trade_keys.add(market_key)
                logger.warning(f"outcomeIndex missing ({outcome_index}) for {name} on {condition_id[:12]}... — fallback to market dedup")

            # ── GUARD 3: Price Filter (keine Lotterietickets, kein schlechtes R/R) ──
            price = float(trade.get("price", 0))
            if price < self.min_copy_price or price > self.max_copy_price:
                logger.info(f"Copy Skip (Preis {price:.3f} ausserhalb {self.min_copy_price}-{self.max_copy_price}): {name} {trade.get('title','')[:40]}")
                continue

            # ── GUARD 4: Age Filter — lockerer für Hedges (15 Min statt 5 Min) ──
            age = time.time() - ts
            max_age = self.min_seconds_to_copy * 60 * 3 if is_hedge else self.min_seconds_to_copy * 60
            if age > max_age:
                logger.debug(f"Copy Skip (zu alt: {age:.0f}s > {max_age:.0f}s{'[hedge]' if is_hedge else ''}): {name}")
                continue

            # ── GUARD 5: Max concurrent — zähle MÄRKTE, nicht einzelne Seiten ──
            # DEADLOCK FIX: Positionen älter als 7 Tage werden auto-resolved
            # (Markets lösen sich natürlich auf, aber unser Code erkennt das nicht)
            MAX_POSITION_AGE_S = 7 * 24 * 3600  # 7 Tage
            now = time.time()
            active_markets = set()
            for p in self._copied_positions:
                if not p.resolved:
                    if now - p.copied_at > MAX_POSITION_AGE_S:
                        p.resolved = True  # Auto-expire stale position
                        logger.info(f"AUTO-RESOLVE: {p.trade_id} ({p.market_title[:30]}) — älter als 7d")
                    else:
                        active_markets.add(p.condition_id)
            # Neuer Market oder Hedge auf bestehendem? Hedge zählt nicht extra.
            if condition_id not in active_markets and len(active_markets) >= self.max_concurrent:
                logger.warning(f"Copy Skip (max {self.max_concurrent} Markets erreicht): {name}")
                continue

            # ── GUARD 6: Balance-Check (nicht ordern wenn Wallet zu leer) ──
            if self.executor.is_live:
                try:
                    balance = await self.executor.get_balance()
                    if balance < self.max_copy_size_usd * 1.5:
                        logger.warning(f"Copy Skip (Balance ${balance:.2f} < ${self.max_copy_size_usd*1.5:.2f}): {name}")
                        continue
                except Exception:
                    pass  # Balance-Check optional, CLOB blockt auch

            # ── Proportionale Sizing für Hedges ──
            # Hedge-Leg bekommt proportional less (based on trader's OWN ratio).
            # CORRECT formula: ratio = trader_hedge_usd / trader_first_leg_usd
            # We stored the trader's original USD size in source_orig_usd.
            copy_size = self.max_copy_size_usd
            trader_hedge_usd = float(trade.get("usdcSize", 0) or 0)
            if is_hedge:
                # Find our first-leg copy FROM THE SAME SOURCE WALLET
                first_leg = next(
                    (p for p in self._copied_positions
                     if p.condition_id == condition_id
                     and p.source_wallet == addr
                     and not p.resolved),
                    None,
                )
                if first_leg and trader_hedge_usd > 0 and first_leg.source_orig_usd > 0:
                    # Both values are the TRADER's USD sizes → correct apples-to-apples ratio
                    ratio = min(1.0, trader_hedge_usd / first_leg.source_orig_usd)
                    copy_size = max(1.0, round(self.max_copy_size_usd * ratio, 2))
                    logger.info(
                        f"HEDGE SIZING: ${copy_size:.2f} (trader hedge ${trader_hedge_usd:,.0f} "
                        f"/ trader main ${first_leg.source_orig_usd:,.0f} = {ratio:.1%} "
                        f"of our ${self.max_copy_size_usd})"
                    )
                else:
                    # Fallback: no first-leg data → copy at full size
                    logger.info(f"HEDGE SIZING: ${copy_size:.2f} (fallback, no first-leg trader data)")

            # ── Alle Guards bestanden → Kopieren! ──
            # Cross-Wallet Confluence ERLAUBT: Wenn 2 Top-Wallets denselben
            # Markt kaufen ist das ein STÄRKERES Signal, nicht ein schwächeres.
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
                outcome_index=outcome_index,
                source_tx_hash=trade.get("transactionHash", ""),
            )

            try:
                await self._copy_trade(tracked, copy_size=copy_size, is_hedge=is_hedge)
            finally:
                self._save_seen_state()  # Persist IMMER — auch bei Exception

    # ═══════════════════════════════════════════════════════════════
    # SELL-COPY (Auto-Exit wenn Source Wallet verkauft)
    # ═══════════════════════════════════════════════════════════════

    async def _handle_sell_signal(self, trade: dict, name: str, addr: str) -> None:
        """Verarbeitet SELL-Trade eines Trackers: verkauft unsere Position.

        FIXES applied (10-Agent Stress Test):
        - Source wallet filter (nur Positionen von DIESEM Tracker verkaufen)
        - Simplified sell sizing (sell all — $5 Positionen sind Minimum, kein Partial)
        - Re-entry key removal (outcome_key wird entfernt → Trader kann re-buyen)
        - Hedge unwind (wenn Primary sold → Hedge auch auflösen)
        - Phantom position cleanup (failed BUYs → resolved markieren)
        - Journal recording (SELL-Copy ins JSONL schreiben)
        """
        condition_id = trade.get("conditionId", "")
        outcome = trade.get("outcome", "")
        outcome_index = trade.get("outcomeIndex", -1)
        sell_price = float(trade.get("price", 0))
        source_tx = trade.get("transactionHash", "")

        # FIX: Source wallet filter — nur Positionen von DIESEM Trader
        matching = [
            p for p in self._copied_positions
            if p.condition_id == condition_id
            and p.outcome_index == outcome_index
            and p.source_wallet == addr  # FIX: nur von diesem Wallet
            and not p.resolved
        ]

        # FIX: Phantom cleanup — Positionen ohne token_id (failed BUYs) auflösen
        phantoms = [p for p in matching if not p.token_id]
        for ph in phantoms:
            ph.resolved = True
            logger.warning(f"PHANTOM CLEANUP: {ph.trade_id} — no token_id, marked resolved")
        matching = [p for p in matching if p.token_id]

        if not matching:
            logger.info(f"SELL SIGNAL (no match): {name} SELL {outcome} @ {sell_price:.3f} | {trade.get('title','')[:40]}")
            asyncio.create_task(telegram.send_alert(
                f"🔔 <b>EXIT SIGNAL (unmatched)</b>\n"
                f"👤 <b>{name}</b> SELL {outcome} @ {sell_price:.3f}\n"
                f"📍 {trade.get('title','')[:50]}\n"
                f"ℹ️ Wir haben keine Position auf diesem Outcome von {name}"
            ))
            return

        for pos in matching:
            # FIX: Simplified — bei $5 Positionen immer komplett verkaufen
            # Partial sells auf $5 Positionen erzeugen Dust + extra Fees
            our_sell_size = pos.size_usd

            logger.info(
                f"SELL-COPY: {name} verkauft {outcome} → wir auch! "
                f"${our_sell_size:.2f} | {pos.trade_id}"
            )

            # LIVE SELL Order
            sell_success = False
            sell_order_id = ""
            if self.executor.is_live and pos.token_id:
                try:
                    res = await self.executor.place_order(
                        token_id=pos.token_id,
                        side="SELL",
                        price=sell_price,
                        size_usd=our_sell_size,
                        asset=name,
                        direction=f"SELL_{outcome}",
                    )
                    if res.success:
                        sell_success = True
                        sell_order_id = res.order_id
                        pos.resolved = True
                        logger.info(f"SELL-COPY LIVE OK: {pos.trade_id} — {res.order_id}")
                    else:
                        logger.error(f"SELL-COPY LIVE FAILED: {pos.trade_id} — {res.error}")
                except Exception as e:
                    logger.error(f"SELL-COPY EXCEPTION: {pos.trade_id} — {e}")

            # FIX: Re-entry key removal — outcome_key entfernen damit Trader re-buyen kann
            if pos.resolved and outcome_index in (0, 1):
                reentry_key = f"{addr}_{condition_id}_{outcome_index}"
                self._seen_trade_keys.discard(reentry_key)
                logger.info(f"RE-ENTRY ENABLED: {reentry_key} removed from dedup")

            # FIX: Hedge unwind — wenn Primary verkauft, Hedge auch auflösen
            if pos.resolved and not pos.is_hedge:
                hedges = [
                    h for h in self._copied_positions
                    if h.condition_id == condition_id
                    and h.is_hedge
                    and not h.resolved
                    and h.token_id
                    and h.source_wallet == addr
                ]
                for hedge in hedges:
                    logger.info(f"HEDGE UNWIND: Closing hedge {hedge.trade_id} (primary sold)")
                    if self.executor.is_live:
                        try:
                            hr = await self.executor.place_order(
                                token_id=hedge.token_id,
                                side="SELL",
                                price=max(0.01, 1.0 - sell_price),  # Approximate other side
                                size_usd=hedge.size_usd,
                                asset=name,
                                direction=f"SELL_{hedge.outcome}",
                            )
                            if hr.success:
                                hedge.resolved = True
                                logger.info(f"HEDGE UNWIND OK: {hedge.trade_id} — {hr.order_id}")
                        except Exception as e:
                            logger.error(f"HEDGE UNWIND FAILED: {hedge.trade_id} — {e}")

            # FIX: Journal recording — SELL-Copy ins JSONL schreiben
            self.journal.record_open(TradeRecord(
                trade_id=f"{pos.trade_id}_SELL",
                asset=name,
                direction=f"SELL_{outcome}",
                signal_ts=float(trade.get("timestamp", 0)),
                entry_ts=time.time(),
                window_slug=pos.market_slug,
                market_question=pos.market_title[:60],
                executed_price=sell_price,
                size_usd=our_sell_size,
                order_type="copy_sell",
                live_order_id=sell_order_id,
                live_order_success=sell_success,
                condition_id=condition_id,
                source_wallet=addr,
                source_wallet_name=name,
                source_tx_hash=source_tx,
            ))

            # Telegram Alert
            tx_short = source_tx[:12] + "…" if source_tx else "N/A"
            asyncio.create_task(telegram.send_alert(
                f"📤 <b>SELL-COPY</b> (Auto-Exit)\n"
                f"{'─'*26}\n"
                f"👤 {name} SELL → wir folgen!\n"
                f"📊 SELL {outcome} @ {sell_price:.3f}\n"
                f"💰 Sell Size: ${our_sell_size:.2f}\n"
                f"📍 {pos.market_title[:40]}\n"
                f"🔗 {pos.trade_id}\n"
                f"⛓️ Source TX: {tx_short}"
            ))

    # ═══════════════════════════════════════════════════════════════
    # COPY EXECUTION
    # ═══════════════════════════════════════════════════════════════

    async def _copy_trade(
        self, trade: TrackedTrade, copy_size: float = 0, is_hedge: bool = False
    ) -> None:
        """Kopiert einen erkannten Trade (BUY oder Hedge-Leg)."""
        self._copy_count += 1
        trade_id = f"CT-{self._copy_count:04d}"
        if copy_size <= 0:
            copy_size = self.max_copy_size_usd

        hedge_tag = " [HEDGE]" if is_hedge else ""
        logger.info(
            f"COPY #{self._copy_count}{hedge_tag}: {trade.wallet_name} → "
            f"{trade.side} {trade.outcome} @ {trade.price:.3f} | "
            f"Orig: ${trade.size_usd:,.0f} | Copy: ${copy_size:.2f} | {trade.title[:45]}"
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
            size_usd=copy_size,
            copied_at=time.time(),
            token_id=trade.asset_id,
            outcome_index=trade.outcome_index,
            is_hedge=is_hedge,
            source_tx_hash=trade.source_tx_hash,
            source_orig_usd=trade.size_usd,  # Trader's original USD size (for hedge ratio)
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
            "source_tx_hash": trade.source_tx_hash,
            "condition_id": trade.condition_id,
        })

        # Telegram Alert — zeige Wallet-PnL aus Config
        wallet_pnl = 0
        for w in self.tracked_wallets:
            if w["address"] == trade.wallet_address:
                wallet_pnl = w.get("pnl", 0)
                break
        size_str = f"${trade.size_usd:,.0f}" if trade.size_usd > 0 else "N/A"
        tx_short = trade.source_tx_hash[:12] + "…" if trade.source_tx_hash else "N/A"
        tx_link = f"https://polygonscan.com/tx/{trade.source_tx_hash}" if trade.source_tx_hash else ""
        hedge_emoji = "🛡️ HEDGE " if is_hedge else ""
        asyncio.create_task(telegram.send_alert(
            f"📋 <b>{hedge_emoji}COPY TRADE #{self._copy_count}</b>\n"
            f"{'─'*26}\n"
            f"👤 Source: <b>{trade.wallet_name}</b> (${wallet_pnl:,.0f} Lifetime PnL)\n"
            f"📊 {trade.side} {trade.outcome} @ {trade.price:.3f} (Orig: {size_str})\n"
            f"💰 Copy Size: ${copy_size:.2f}{' (proportional)' if is_hedge else ''}\n"
            f"📍 {trade.title[:50]}\n"
            f"🔗 <code>{trade.slug}</code>\n"
            f"⛓️ Source TX: <a href=\"{tx_link}\">{tx_short}</a>"
        ))

        # LIVE Order platzieren
        if self.executor.is_live and trade.asset_id:
            try:
                res = await self.executor.place_order(
                    token_id=trade.asset_id,
                    side="BUY",
                    price=trade.price,
                    size_usd=copy_size,
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

        # Journal — Full Forensic Record mit Source-Zuordnung
        self.journal.record_open(TradeRecord(
            trade_id=trade_id,
            asset=trade.wallet_name,
            direction=trade.outcome,
            signal_ts=float(trade.timestamp),
            entry_ts=time.time(),
            window_slug=trade.slug,
            market_question=trade.title[:60],
            executed_price=trade.price,
            size_usd=copy_size,
            order_type="copy_hedge" if is_hedge else "copy_trade",
            live_order_id=pos.live_order_id,
            live_order_success=pos.live_success,
            # Copy Trading Forensics — 100% Traceability
            condition_id=trade.condition_id,
            source_wallet=trade.wallet_address,
            source_wallet_name=trade.wallet_name,
            source_tx_hash=trade.source_tx_hash,
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
