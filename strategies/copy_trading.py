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
# SMART GUARDS — AI-Driven Risk Management für Copy Trades
# ═══════════════════════════════════════════════════════════════════

import math
from collections import Counter


RISK_STATE_FILE = Path("data/copy_risk_state.json")


class CopyRiskManager:
    """Kill-Switch + Drawdown-Limits für Copy Trading.

    Unabhängig vom Momentum-RiskManager — eigene Limits für Copy Trades.
    PERSISTENT: State wird auf Disk gespeichert und überlebt Restarts.
    """

    def __init__(self) -> None:
        self.daily_pnl_usd: float = 0.0
        self.total_pnl_usd: float = 0.0
        self.daily_trades: int = 0
        self.daily_losses: int = 0
        self.last_reset_day: str = ""
        self.halted: bool = False
        self.halt_reason: str = ""

        # Limits
        self.max_daily_loss_usd: float = -15.0    # -$15/Tag → Stop
        self.max_total_loss_usd: float = -30.0     # -$30 gesamt → Kill
        self.max_daily_trades: int = 30            # Max 30 Trades/Tag
        self.max_consecutive_losses: int = 5       # 5 Verluste in Folge → Pause

        self._consecutive_losses: int = 0

        # Load persistent state
        self._load_state()

    def _load_state(self) -> None:
        """Lädt persistenten Risk-State von Disk."""
        try:
            if RISK_STATE_FILE.exists():
                with open(RISK_STATE_FILE) as f:
                    s = json.load(f)
                self.total_pnl_usd = s.get("total_pnl_usd", 0.0)
                self.daily_pnl_usd = s.get("daily_pnl_usd", 0.0)
                self.daily_trades = s.get("daily_trades", 0)
                self.daily_losses = s.get("daily_losses", 0)
                self.last_reset_day = s.get("last_reset_day", "")
                self.halted = s.get("halted", False)
                self.halt_reason = s.get("halt_reason", "")
                self._consecutive_losses = s.get("consecutive_losses", 0)
                logger.info(f"RISK STATE LOADED: total_pnl=${self.total_pnl_usd:+.2f}, daily=${self.daily_pnl_usd:+.2f}, halted={self.halted}")
        except Exception as e:
            logger.warning(f"Risk state load error: {e}")

    def _save_state(self) -> None:
        """Speichert Risk-State persistent auf Disk (atomic write)."""
        try:
            RISK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = RISK_STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump({
                    "total_pnl_usd": round(self.total_pnl_usd, 4),
                    "daily_pnl_usd": round(self.daily_pnl_usd, 4),
                    "daily_trades": self.daily_trades,
                    "daily_losses": self.daily_losses,
                    "last_reset_day": self.last_reset_day,
                    "halted": self.halted,
                    "halt_reason": self.halt_reason,
                    "consecutive_losses": self._consecutive_losses,
                    "saved_at": time.time(),
                }, f)
            import os
            os.replace(str(tmp), str(RISK_STATE_FILE))  # Atomic on POSIX
        except Exception as e:
            logger.error(f"Risk state save error: {e}")

    def _check_reset(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.last_reset_day:
            self.daily_pnl_usd = 0.0
            self.daily_trades = 0
            self.daily_losses = 0
            self._consecutive_losses = 0
            self.last_reset_day = today
            # Täglicher Halt wird zurückgesetzt, aber totaler Kill bleibt
            if self.halt_reason == "daily_loss":
                self.halted = False
                self.halt_reason = ""
                logger.info("COPY RISK: Daily halt reset — trading resumed")

    def can_trade(self) -> tuple[bool, str]:
        """Prüft ob ein neuer Copy Trade erlaubt ist."""
        self._check_reset()

        if self.halted:
            return False, f"HALTED: {self.halt_reason}"

        if self.daily_pnl_usd <= self.max_daily_loss_usd:
            self.halted = True
            self.halt_reason = "daily_loss"
            logger.warning(f"COPY KILL-SWITCH: Daily loss ${self.daily_pnl_usd:.2f} <= ${self.max_daily_loss_usd}")
            return False, f"Daily loss limit reached (${self.daily_pnl_usd:.2f})"

        if self.total_pnl_usd <= self.max_total_loss_usd:
            self.halted = True
            self.halt_reason = "total_loss"
            logger.error(f"COPY KILL-SWITCH: Total loss ${self.total_pnl_usd:.2f} <= ${self.max_total_loss_usd}")
            return False, f"Total loss limit reached (${self.total_pnl_usd:.2f})"

        if self.daily_trades >= self.max_daily_trades:
            return False, f"Max daily trades reached ({self.daily_trades})"

        if self._consecutive_losses >= self.max_consecutive_losses:
            return False, f"Consecutive losses ({self._consecutive_losses}) — cooling off"

        return True, "OK"

    def record_trade(self, pnl_usd: float = 0.0) -> None:
        """Registriert einen abgeschlossenen Trade + persistiert State."""
        self._check_reset()
        self.daily_trades += 1
        self.daily_pnl_usd += pnl_usd
        self.total_pnl_usd += pnl_usd
        if pnl_usd < 0:
            self.daily_losses += 1
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        self._save_state()  # Persistent über Restarts

    def get_status(self) -> dict:
        self._check_reset()
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "daily_pnl": round(self.daily_pnl_usd, 2),
            "total_pnl": round(self.total_pnl_usd, 2),
            "daily_trades": self.daily_trades,
            "consecutive_losses": self._consecutive_losses,
        }


class SmartGuards:
    """AI-gesteuerte Schutzschicht für Copy Trades.

    Guards:
    - Slippage Protection: Preis seit Original-Trade gecheckt
    - Liquidity Profiling: Market muss genug Volumen haben
    - Bait/Dump Detection: Tracker verkauft kurz nach unserem Copy → Blacklist
    - Market Correlation: Max 2x gleicher Market von verschiedenen Wallets
    - Model Drift Detection: Erkennt Verhaltensänderungen der Tracker
    """

    def __init__(self) -> None:
        # Slippage
        self.max_slippage_pct: float = 12.0  # Max 12% Preisverschiebung (3s-Polling braucht Spielraum)

        # Bait Detection
        self._recent_copies_ts: dict[str, float] = {}  # condition_id → timestamp of our copy
        self._dump_alerts: dict[str, int] = {}         # wallet_addr → dump count
        self._blacklisted: set[str] = set()            # Auto-blacklisted wallets

        # Market Correlation
        self._active_markets: Counter = Counter()  # condition_id → number of copy positions
        self.max_market_exposure: int = 2          # Max 2 Positionen pro Market

        # Model Drift Detection
        self._wallet_categories: dict[str, Counter] = {}  # wallet → category histogram

    def check_slippage(self, original_price: float, current_ask: float) -> tuple[bool, str]:
        """Prüft ob sich der Preis seit dem Original-Trade zu stark verschlechtert hat."""
        if original_price <= 0 or current_ask <= 0:
            return True, "no price data"
        # Hard-Cap: Markt entschieden (>0.95) → immer blocken, kein Profit möglich
        if current_ask > 0.95:
            return False, f"Market resolved (ask {current_ask:.3f} > 0.95)"
        # Nur GEGEN uns (Preis gestiegen = schlechter für BUY) blocken
        # Preis gefallen = besser für uns → NICHT blocken
        drift_pct = (current_ask - original_price) / original_price * 100
        if drift_pct > self.max_slippage_pct:
            return False, f"Slippage +{drift_pct:.1f}% > {self.max_slippage_pct}% (orig: {original_price:.3f}, now: {current_ask:.3f})"
        return True, f"Slippage OK ({drift_pct:+.1f}%)"

    def check_liquidity(self, trade: dict) -> tuple[bool, str]:
        """Prüft ob der Market genug Liquidität hat für unseren Copy-Trade."""
        usdc_size = trade.get("usdcSize", 0)
        # Wenn der Original-Trader > $50 in diesem Market investiert hat, hat er genug Liquidität gefunden
        if usdc_size >= 50:
            return True, f"Liquidity OK (orig: ${usdc_size:.0f})"
        # Kleine Trades in potentiell illiquiden Markets → vorsichtig
        if usdc_size < 1:
            return False, f"Tiny trade ${usdc_size:.2f} — below $1 minimum"
        return True, f"Liquidity acceptable (${usdc_size:.0f})"

    def check_market_correlation(self, condition_id: str) -> tuple[bool, str]:
        """Prüft ob wir bereits zu viele Positionen auf diesem Market haben."""
        current = self._active_markets.get(condition_id, 0)
        if current >= self.max_market_exposure:
            return False, f"Market already has {current} copies (max {self.max_market_exposure})"
        return True, f"Correlation OK ({current}/{self.max_market_exposure})"

    def register_copy(self, condition_id: str) -> None:
        """Registriert einen neuen Copy Trade für Korrelations-Tracking."""
        self._active_markets[condition_id] += 1
        self._recent_copies_ts[condition_id] = time.time()

    def check_bait(self, wallet_addr: str) -> tuple[bool, str]:
        """Prüft ob dieser Trader als Baiter/Dumper bekannt ist."""
        addr = wallet_addr.lower()
        if addr in self._blacklisted:
            return False, f"BLACKLISTED: {self._dump_alerts.get(addr, 0)} dump alerts"
        dumps = self._dump_alerts.get(addr, 0)
        if dumps >= 3:
            self._blacklisted.add(addr)
            return False, f"AUTO-BLACKLISTED: {dumps} dumps detected"
        return True, f"Bait check OK (dumps: {dumps})"

    def report_dump(self, wallet_addr: str, condition_id: str) -> None:
        """Meldet einen Dump: Trader hat kurz nach unserem Copy verkauft."""
        addr = wallet_addr.lower()
        copy_ts = self._recent_copies_ts.get(condition_id, 0)
        if copy_ts and time.time() - copy_ts < 600:  # Innerhalb von 10 Min nach unserem Copy
            self._dump_alerts[addr] = self._dump_alerts.get(addr, 0) + 1
            count = self._dump_alerts[addr]
            logger.warning(f"DUMP ALERT #{count}: {addr[:10]}... sold within 10min of our copy on {condition_id[:12]}...")
            if count >= 3:
                self._blacklisted.add(addr)
                logger.error(f"AUTO-BLACKLIST: {addr[:10]}... — {count} dumps!")

    def detect_drift(self, wallet_name: str, trade_title: str) -> str | None:
        """Erkennt Verhaltensänderungen eines Traders (Category Drift)."""
        tl = trade_title.lower()
        if any(w in tl for w in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana"]):
            cat = "crypto"
        elif any(w in tl for w in ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "hockey"]):
            cat = "sports"
        elif any(w in tl for w in ["trump", "biden", "election", "president", "congress"]):
            cat = "politics"
        else:
            cat = "other"

        if wallet_name not in self._wallet_categories:
            self._wallet_categories[wallet_name] = Counter()
        hist = self._wallet_categories[wallet_name]
        hist[cat] += 1

        total = sum(hist.values())
        if total < 5:
            return None  # Nicht genug Daten

        # Dominante Kategorie
        dominant_cat, dominant_count = hist.most_common(1)[0]
        dominant_pct = dominant_count / total * 100

        # Drift: aktuelle Kategorie weicht stark von Historie ab
        if cat != dominant_cat and dominant_pct > 70:
            return f"DRIFT: {wallet_name} usually trades {dominant_cat} ({dominant_pct:.0f}%), now trading {cat}"
        return None

    def release_market(self, condition_id: str) -> None:
        """Decrements market correlation counter when a position resolves."""
        if condition_id in self._active_markets:
            self._active_markets[condition_id] = max(0, self._active_markets[condition_id] - 1)
            if self._active_markets[condition_id] == 0:
                del self._active_markets[condition_id]

    def get_status(self) -> dict:
        return {
            "blacklisted": list(self._blacklisted),
            "dump_alerts": dict(self._dump_alerts),
            "active_markets": dict(self._active_markets),
            "slippage_max": self.max_slippage_pct,
        }


def kelly_copy_size(win_rate: float, avg_odds: float, base_size: float, max_cap_pct: float = 0.08) -> float:
    """Berechnet optimale Copy-Size nach Half-Kelly.

    Args:
        win_rate: Historische Win-Rate des Traders (0.0-1.0)
        avg_odds: Durchschnittliche Auszahlungsquote (z.B. 1/avg_price)
        base_size: Basis-Größe ($5 default)
        max_cap_pct: Max % des Portfolios (8% default)
    Returns:
        Optimale Copy-Größe in USD
    """
    if win_rate <= 0 or avg_odds <= 0:
        return base_size

    # Kelly: f* = (p * b - q) / b
    # p = win_rate, q = 1-p, b = avg_odds
    p = min(0.95, max(0.05, win_rate))
    q = 1.0 - p
    b = avg_odds

    kelly_full = (p * b - q) / b
    kelly_half = kelly_full / 2.0  # Half-Kelly for safety

    if kelly_half <= 0:
        return max(1.0, base_size * 0.5)  # Minimum bei negativem Kelly

    # Skaliere basierend auf base_size
    optimal = base_size * min(2.0, max(0.2, 1.0 + kelly_half))
    return round(min(optimal, base_size * 2), 2)  # Max 2x base_size


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
        self.poll_interval_s = 3.0         # Poll alle 3 Sekunden (schneller = weniger Slippage)
        self.max_copy_size_usd = 5.0       # Fix $5 pro Copy-Trade
        self.max_concurrent = 10           # Max 10 Markets (bei $5/Trade = $50-$100 max exposure)
        self.min_seconds_to_copy = 5       # Trade muss < 5 Min alt sein
        self.only_buys = False             # BUY + SELL kopieren (SELL = Exit-Signal)
        self.min_copy_price = 0.15         # Value Bets ab 15¢ (3:1+ R/R)
        self.max_copy_price = 0.90         # High-Confidence bis 90¢ (11% Return)
        self._api_limit = 20              # Activity API trades per request

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
        self._paused_wallets: set[str] = set()  # Paused wallet addresses

        # AI Risk Management
        self.risk = CopyRiskManager()
        self.guards = SmartGuards()

    # ═══════════════════════════════════════════════════════════════
    # WALLET MANAGEMENT (Dashboard API)
    # ═══════════════════════════════════════════════════════════════

    MAX_TRACKED_WALLETS = 20  # Hard cap — prevents poll loop DoS


    TRACKED_WALLETS_FILE = Path("data/tracked_wallets.json")

    def _save_tracked_wallets(self) -> None:
        """Persists tracked wallets + paused state to disk (atomic write)."""
        try:
            self.TRACKED_WALLETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "wallets": self.tracked_wallets,
                "paused": list(self._paused_wallets),
                "_saved_at": time.time(),
            }
            tmp = self.TRACKED_WALLETS_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            import os
            os.replace(str(tmp), str(self.TRACKED_WALLETS_FILE))
            logger.debug(f"WALLETS SAVED: {len(self.tracked_wallets)} wallets to disk")
        except Exception as e:
            logger.error(f"Wallet save error: {e}")

    def _load_tracked_wallets(self) -> None:
        """Loads persisted wallets from disk. Falls back to DEFAULT_TRACKED_WALLETS."""
        try:
            if self.TRACKED_WALLETS_FILE.exists():
                with open(self.TRACKED_WALLETS_FILE) as f:
                    data = json.load(f)
                wallets = data.get("wallets", [])
                paused = set(data.get("paused", []))
                if wallets:
                    self.tracked_wallets = wallets
                    self._paused_wallets = paused
                    logger.info(
                        f"WALLETS LOADED: {len(wallets)} from disk "
                        f"({len(paused)} paused)"
                    )
                    return
        except Exception as e:
            logger.warning(f"Wallet load error (using defaults): {e}")
        # Fallback: defaults already set in __init__
        logger.info(f"WALLETS: Using {len(self.tracked_wallets)} defaults (no saved state)")

    def add_wallet(self, wallet: dict) -> bool:
        """Fügt eine Wallet zur Tracking-Liste hinzu (persistent auf Disk)."""
        addr = wallet.get("address", "").lower()
        if not addr or len(addr) != 42 or not addr.startswith("0x"):
            return False
        if len(self.tracked_wallets) >= self.MAX_TRACKED_WALLETS:
            logger.warning(f"WALLET ADD REJECTED: max {self.MAX_TRACKED_WALLETS} reached")
            return False
        # Duplikat-Check
        if any(w["address"].lower() == addr for w in self.tracked_wallets):
            return False
        wallet["added_ts"] = time.time()
        self.tracked_wallets.append(wallet)
        self._last_seen[addr] = int(time.time())  # Keine alten Trades kopieren
        self._save_tracked_wallets()
        logger.info(f"WALLET ADDED: {wallet.get('name', addr[:10])} ({addr[:10]}...) — LIVE copy active")
        return True

    def remove_wallet(self, address: str) -> bool:
        """Entfernt eine Wallet aus der Tracking-Liste."""
        addr = address.lower()
        before = len(self.tracked_wallets)
        self.tracked_wallets = [w for w in self.tracked_wallets if w["address"].lower() != addr]
        self._last_seen.pop(addr, None)
        self._paused_wallets.discard(addr)
        removed = len(self.tracked_wallets) < before
        if removed:
            self._save_tracked_wallets()
            logger.info(f"WALLET REMOVED: {addr[:10]}...")
        return removed

    def pause_wallet(self, address: str) -> bool:
        """Pausiert eine Wallet (keine neuen Copies, bestehende bleiben)."""
        addr = address.lower()
        if any(w["address"].lower() == addr for w in self.tracked_wallets):
            self._paused_wallets.add(addr)
            self._save_tracked_wallets()
            logger.info(f"WALLET PAUSED: {addr[:10]}...")
            return True
        return False

    def resume_wallet(self, address: str) -> bool:
        """Reaktiviert eine pausierte Wallet."""
        addr = address.lower()
        if addr in self._paused_wallets:
            self._paused_wallets.discard(addr)
            self._save_tracked_wallets()
            logger.info(f"WALLET RESUMED: {addr[:10]}...")
            return True
        return False

    # ═══════════════════════════════════════════════════════════════
    # MEMORY REBUILD — Offene Positionen nach Restart wiederherstellen
    # ═══════════════════════════════════════════════════════════════

    async def _rebuild_copied_positions(self) -> None:
        """Stellt _copied_positions aus Journal + Wallet-Positionen wieder her.

        Nach jedem Restart ist _copied_positions leer. Ohne Rebuild:
        - SELL-Copy kann nicht feuern (findet keine matching Position)
        - Hedge-Unwind funktioniert nicht
        - Market-Exposure-Cap zählt falsch (0 statt real)

        Rebuild-Logik:
        1. Lese alle CT- Opens aus dem Journal
        2. Prüfe welche KEIN close/redeemed/resolved_loss Event haben
        3. Cross-check mit Polymarket Wallet-Positionen (Sanity)
        4. Baue CopiedPosition Objekte für alle noch offenen Trades
        """
        journal_path = Path("data/trade_journal.jsonl")
        # Try multiple paths
        for p in [
            journal_path,
            Path(__file__).parent.parent / "data" / "trade_journal.jsonl",
            Path("/home/ubuntu/polymarket-arb/data/trade_journal.jsonl"),
        ]:
            if p.exists():
                journal_path = p
                break

        if not journal_path.exists():
            logger.warning("REBUILD: Journal nicht gefunden — keine Positionen wiederhergestellt")
            return

        # 1. Journal lesen — alle CT- Events
        opens: dict[str, dict] = {}  # trade_id → open event
        closed_ids: set[str] = set()  # trade_ids die geschlossen sind

        try:
            with open(journal_path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    tid = e.get("trade_id", "")
                    if not tid.startswith("CT-"):
                        continue

                    event = e.get("event", "")
                    if event == "open":
                        opens[tid] = e
                    elif event in ("redeemed", "resolved_loss", "close", "copy_sell"):
                        closed_ids.add(tid)
                        # Auch den _SELL Suffix checken
                        base_tid = tid.replace("_SELL", "")
                        closed_ids.add(base_tid)
        except Exception as e:
            logger.error(f"REBUILD: Journal read error: {e}")
            return

        # 2. Offene Trades = Opens OHNE Close
        open_trades = {tid: data for tid, data in opens.items() if tid not in closed_ids}

        if not open_trades:
            logger.info("REBUILD: Keine offenen Copy-Trades im Journal gefunden")
            return

        # 3. Wallet-Positionen als Sanity-Check
        wallet_cids: set[str] = set()
        if self._session and not self._session.closed:
            try:
                wallet = self.settings.polymarket_funder
                url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=200"
                async with self._session.get(url) as resp:
                    if resp.status == 200:
                        positions = await resp.json()
                        for p in positions:
                            if p.get("currentValue", 0) > 0.01:
                                wallet_cids.add(p.get("conditionId", ""))
            except Exception:
                pass  # Sanity-Check optional

        # 4. Rebuild CopiedPosition Objekte
        rebuilt = 0
        for tid, data in open_trades.items():
            cid = data.get("condition_id", "")

            # Sanity: Skip positions without condition_id (legacy, can't verify)
            if not cid:
                logger.debug(f"REBUILD SKIP: {tid} — no condition_id (legacy trade)")
                continue

            # Sanity: Wenn wir Wallet-Daten haben, prüfe ob Position noch existiert
            if wallet_cids and cid not in wallet_cids:
                logger.debug(f"REBUILD SKIP: {tid} — nicht mehr in Wallet (redeemed/resolved)")
                continue

            pos = CopiedPosition(
                trade_id=tid,
                source_wallet=data.get("source_wallet", ""),
                source_name=data.get("source_wallet_name", "") or data.get("asset", ""),
                market_title=data.get("market_question", ""),
                market_slug=data.get("window_slug", ""),
                condition_id=cid,
                side="BUY",
                outcome=data.get("direction", ""),
                entry_price=data.get("executed_price", 0),
                size_usd=data.get("size_usd", 0),
                copied_at=data.get("entry_ts", 0),
                token_id="",  # Nicht im Journal — wird bei nächstem SELL via API geholt
                outcome_index=-1,  # Nicht im Journal — wird bei Match via API bestimmt
                is_hedge=data.get("order_type", "") == "copy_hedge",
                source_tx_hash=data.get("source_tx_hash", ""),
                source_orig_usd=data.get("size_usd", 0),
                live_order_id=data.get("live_order_id", ""),
                live_success=data.get("live_order_success", False),
            )
            self._copied_positions.append(pos)
            rebuilt += 1

        logger.info(
            f"REBUILD SUCCESS | "
            f"restored={rebuilt} positions | "
            f"journal_opens={len(open_trades)} | "
            f"closed={len(closed_ids)} | "
            f"copy_count={self._copy_count} | "
            f"live_trading={self.settings.live_trading}"
        )

        # Recent Copies aus Journal füllen (letzte 20 CT-opens, für Dashboard)
        all_ct_opens = sorted(opens.values(), key=lambda e: e.get("entry_ts", 0), reverse=True)
        for data in all_ct_opens[:20]:
            self._recent_copies.append({
                "ts": time.strftime("%H:%M", time.localtime(data.get("entry_ts", 0))),
                "trade_id": data.get("trade_id", ""),
                "source": data.get("source_wallet_name", "") or data.get("asset", ""),
                "side": "BUY",
                "outcome": data.get("direction", ""),
                "price": round(data.get("executed_price", 0), 3),
                "market": (data.get("market_question", "") or "")[:40],
                "source_tx_hash": data.get("source_tx_hash", ""),
                "condition_id": data.get("condition_id", ""),
            })
        if self._recent_copies:
            logger.info(f"REBUILD: {len(self._recent_copies)} recent copies restored from journal")

        # Copy-Count auf letzten Stand setzen
        if open_trades:
            max_num = max(
                int(tid.replace("CT-", "").replace("_SELL", ""))
                for tid in opens.keys()
                if tid.startswith("CT-") and tid.replace("CT-", "").replace("_SELL", "").isdigit()
            )
            if max_num > self._copy_count:
                self._copy_count = max_num
                logger.info(f"REBUILD: Copy-Count auf {self._copy_count} gesetzt")

    # ═══════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═══════════════════════════════════════════════════════════════

    async def run(self) -> None:
        self._running = True
        telegram.configure(self.settings)

        # Load persisted wallets (overrides defaults if file exists)
        self._load_tracked_wallets()

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

        # MEMORY REBUILD: Offene Positionen aus Journal + Wallet wiederherstellen
        await self._rebuild_copied_positions()

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
                trades = await self._fetch_activity(addr, limit=self._api_limit)
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
            for wallet in list(self.tracked_wallets):  # list() copy: safe against mutation
                if not self._running:
                    break
                if wallet["address"].lower() in self._paused_wallets:
                    continue  # Paused wallet — skip
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

        trades = await self._fetch_activity(addr, limit=self._api_limit)
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
            is_scaling = False
            if outcome_index in (0, 1):
                outcome_key = f"{addr}_{condition_id}_{outcome_index}"
                if outcome_key in self._seen_trade_keys:
                    # SCALING CHECK: Ist das ein NEUER Trade (andere tx_hash)?
                    tx_hash = trade.get("transactionHash", "")
                    tx_key = f"tx_{tx_hash}"
                    if tx_key in self._seen_trade_keys or not tx_hash:
                        logger.debug(f"Copy Skip (Tranche, gleicher Outcome): {name} {trade.get('title','')[:30]}")
                        continue
                    # Neue TX auf gleichem Outcome = NACHKAUF (Scaling!)
                    self._seen_trade_keys.add(tx_key)
                    is_scaling = True
                    logger.info(f"SCALING DETECTED: {name} kauft NACH auf {trade.get('title','')[:35]}")
                else:
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
                        self._resolve_position(p, 0.0)
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

            # ── GUARD 7: Kill-Switch (Daily/Total Loss Limits) ──
            can_trade, reason = self.risk.can_trade()
            if not can_trade:
                logger.warning(f"Copy BLOCKED by Kill-Switch: {reason}")
                continue

            # ── GUARD 8: Bait/Dump Detection (blacklisted wallets) ──
            bait_ok, bait_msg = self.guards.check_bait(addr)
            if not bait_ok:
                logger.warning(f"Copy Skip (Bait): {name} — {bait_msg}")
                continue

            # ── GUARD 9: Slippage Protection (Echter CLOB Orderbook-Check) ──
            asset_id = trade.get("asset", "")
            if asset_id:
                current_ask = await self._fetch_current_ask(asset_id)
                if current_ask > 0:
                    slip_ok, slip_msg = self.guards.check_slippage(price, current_ask)
                    if not slip_ok:
                        logger.info(f"Copy Skip (Slippage): {name} — {slip_msg}")
                        continue

            # ── GUARD 10: Liquidity Profiling ──
            liq_ok, liq_msg = self.guards.check_liquidity(trade)
            if not liq_ok:
                logger.info(f"Copy Skip (Liquidity): {name} — {liq_msg}")
                continue

            # ── GUARD 11: Market Correlation (max 2x gleicher Market) ──
            corr_ok, corr_msg = self.guards.check_market_correlation(condition_id)
            if not corr_ok:
                logger.info(f"Copy Skip (Correlation): {name} — {corr_msg}")
                continue

            # ── GUARD 12: Model Drift Detection ──
            drift = self.guards.detect_drift(name, trade.get("title", ""))
            if drift:
                logger.info(f"DRIFT WARNING: {drift}")
                # Drift ist nur eine Warnung, blockiert nicht

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

            # ── Proportionale Sizing für Scaling (Nachkäufe) ──
            if is_scaling:
                first_trade = next(
                    (p for p in self._copied_positions
                     if p.condition_id == condition_id
                     and p.outcome_index == outcome_index
                     and p.source_wallet == addr
                     and not p.resolved),
                    None,
                )
                trader_scale_usd = float(trade.get("usdcSize", 0))
                if first_trade and first_trade.source_orig_usd > 0 and trader_scale_usd > 0:
                    ratio = min(1.0, trader_scale_usd / first_trade.source_orig_usd)
                    copy_size = max(1.0, round(self.max_copy_size_usd * ratio, 2))
                    logger.info(
                        f"SCALING SIZING: ${copy_size:.2f} (trader scales ${trader_scale_usd:,.0f} "
                        f"/ first ${first_trade.source_orig_usd:,.0f} = {ratio:.1%})"
                    )
                else:
                    copy_size = self.max_copy_size_usd

            # ── GUARD 13: Market-Level Risiko-Cap (8% des Wallets) ──
            MAX_MARKET_PCT = 0.08
            if self.executor.is_live and (is_scaling or is_hedge):
                try:
                    balance = await self.executor.get_balance()
                    max_per_market = balance * MAX_MARKET_PCT
                    market_exposure = sum(
                        p.size_usd for p in self._copied_positions
                        if p.condition_id == condition_id and not p.resolved
                    )
                    if market_exposure + copy_size > max_per_market:
                        capped = max(0, max_per_market - market_exposure)
                        if capped < 1.0:
                            logger.warning(f"RISK CAP: ${market_exposure:.2f} + ${copy_size:.2f} > 8% (${max_per_market:.2f}) — SKIP")
                            continue
                        copy_size = round(capped, 2)
                        logger.info(f"RISK CAP: copy reduced to ${copy_size:.2f} (8% = ${max_per_market:.2f})")
                except Exception:
                    pass

            # ── Kelly Position Sizing ──
            # Verwende Trader's historische Win-Rate für dynamische Sizing
            wallet_stats = {n: s for n, s in
                           ((p.source_name, None) for p in self._copied_positions[:0])}  # Placeholder
            # Kelly Position Sizing mit ECHTEN Trader-Daten
            if not is_hedge and price > 0:
                real_wr = self._compute_wallet_win_rate(name)
                implied_odds = 1.0 / price  # z.B. Price 0.60 → Odds 1.67
                copy_size = kelly_copy_size(
                    win_rate=real_wr,
                    avg_odds=implied_odds,
                    base_size=copy_size,
                )
                logger.debug(f"KELLY SIZING: ${copy_size:.2f} (wr={real_wr:.1%}, price={price:.3f}, odds={implied_odds:.2f})")

            # Register copy in SmartGuards
            self.guards.register_copy(condition_id)

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

    def _resolve_position(self, pos: CopiedPosition, pnl_usd: float = 0.0) -> None:
        """Zentrale Resolution: Kill-Switch + Market Counter + Position markieren."""
        if pos.resolved:
            return
        pos.resolved = True
        pos.pnl_usd = pnl_usd
        self.risk.record_trade(pnl_usd)
        self.guards.release_market(pos.condition_id)
        logger.info(f"RESOLVED: {pos.trade_id} | PnL: ${pnl_usd:+.2f} | Market released: {pos.condition_id[:12]}...")

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

        # FIX: Source wallet filter — nur Positionen von DIESEM Trader + DIESEM Outcome
        matching = [
            p for p in self._copied_positions
            if p.condition_id == condition_id
            and p.outcome_index == outcome_index
            and p.source_wallet == addr
            and not p.resolved
        ]

        # FIX: Phantom cleanup — Positionen ohne token_id (failed BUYs) auflösen
        phantoms = [p for p in matching if not p.token_id]
        for ph in phantoms:
            self._resolve_position(ph, 0.0)
            logger.warning(f"PHANTOM CLEANUP: {ph.trade_id} — no token_id, marked resolved")
        matching = [p for p in matching if p.token_id]

        if not matching:
            logger.debug(f"SELL SIGNAL (no match): {name} SELL {outcome} @ {sell_price:.3f} | {trade.get('title','')[:40]}")
            return

        # BAIT DETECTION: Nur feuern wenn wir wirklich eine Position von DIESEM Wallet haben
        # (nicht bei unmatched sells oder hedge-management)
        self.guards.report_dump(addr, condition_id)

        # Proportionaler Teilverkauf: Wie viel % hat der Tracker verkauft?
        sell_usdc = float(trade.get("usdcSize", 0))
        tracker_pos = await self._fetch_tracker_position(addr, condition_id, outcome_index)
        if tracker_pos and sell_usdc > 0:
            tracker_remaining = tracker_pos.get("initialValue", 0)
            tracker_total_before = tracker_remaining + sell_usdc
            sell_ratio = min(1.0, sell_usdc / max(1, tracker_total_before))
        else:
            sell_ratio = 1.0  # Fallback: alles verkaufen

        for pos in matching:
            # Proportionaler Teilverkauf
            our_sell_size = max(1.0, round(pos.size_usd * sell_ratio, 2))
            if our_sell_size >= pos.size_usd * 0.9:
                our_sell_size = pos.size_usd  # Fast alles → lieber ganz (kein Dust)

            logger.info(
                f"SELL-COPY: {name} verkauft {sell_ratio:.0%} → wir ${our_sell_size:.2f} von ${pos.size_usd:.2f} | {pos.trade_id}"
            )

            # Aggressives Pricing: Best Bid vom CLOB (nicht stale Tracker-Preis)
            actual_sell_price = sell_price
            if pos.token_id:
                current_bid = await self._fetch_current_bid(pos.token_id)
                if current_bid > 0:
                    actual_sell_price = current_bid
                    logger.debug(f"SELL using current bid {current_bid:.3f} (tracker sold at {sell_price:.3f})")

            # LIVE SELL Order
            sell_success = False
            sell_order_id = ""
            if self.executor.is_live and pos.token_id:
                try:
                    res = await self.executor.place_order(
                        token_id=pos.token_id,
                        side="SELL",
                        price=actual_sell_price,
                        size_usd=our_sell_size,
                        asset=name,
                        direction=f"SELL_{outcome}",
                    )
                    if res.success:
                        sell_success = True
                        sell_order_id = res.order_id

                        # Fill-Check (wie ODA)
                        await asyncio.sleep(2)
                        try:
                            import asyncio as _aio
                            loop = _aio.get_event_loop()
                            order = await loop.run_in_executor(None, self.executor._client.get_order, res.order_id)
                            if order and float(order.get("size_matched", 0)) < 0.01:
                                logger.warning(f"SELL-COPY NOT FILLED → cancelling {pos.trade_id}")
                                await loop.run_in_executor(None, self.executor._client.cancel, res.order_id)
                                sell_success = False
                        except Exception:
                            pass

                        if sell_success:
                            # Echtes PnL: (Shares × Verkaufspreis) - Kaufkosten
                            shares_sold = our_sell_size / pos.entry_price if pos.entry_price > 0 else 0
                            sell_proceeds = shares_sold * actual_sell_price
                            real_pnl = sell_proceeds - our_sell_size
                            # Partial Sell: Position nur teilweise resolven
                            if our_sell_size >= pos.size_usd * 0.9:
                                self._resolve_position(pos, real_pnl)
                            else:
                                pos.size_usd -= our_sell_size  # Restposition weiter tracken
                                pos.pnl_usd += real_pnl
                                self.risk.record_trade(real_pnl)
                                logger.info(f"PARTIAL SELL: {pos.trade_id} — sold ${our_sell_size:.2f}, remaining ${pos.size_usd:.2f}, PnL ${real_pnl:+.2f}")
                            logger.info(f"SELL-COPY LIVE OK: {pos.trade_id} — {res.order_id} PnL ${real_pnl:+.2f}")
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
                                # Echtes PnL für Hedge Unwind
                                h_shares = hedge.size_usd / hedge.entry_price if hedge.entry_price > 0 else 0
                                h_sell_price = max(0.01, 1.0 - actual_sell_price)
                                h_pnl = (h_shares * h_sell_price) - hedge.size_usd
                                self._resolve_position(hedge, h_pnl)
                                logger.info(f"HEDGE UNWIND OK: {hedge.trade_id} — {hr.order_id} PnL ${h_pnl:+.2f}")
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

    async def _fetch_current_ask(self, token_id: str) -> float:
        """Holt den aktuellen Best-Ask-Preis vom CLOB Orderbook."""
        if not self._session or self._session.closed or not token_id:
            return 0.0
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    book = await resp.json()
                    asks = book.get("asks", [])
                    if asks:
                        return float(asks[0].get("price", 0))
                return 0.0
        except Exception:
            return 0.0

    async def _fetch_current_bid(self, token_id: str) -> float:
        """Holt den aktuellen Best-Bid-Preis vom CLOB Orderbook (für SELL)."""
        if not self._session or self._session.closed or not token_id:
            return 0.0
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    book = await resp.json()
                    bids = book.get("bids", [])
                    if bids:
                        return float(bids[0].get("price", 0))
                return 0.0
        except Exception:
            return 0.0

    async def _fetch_tracker_position(self, wallet: str, condition_id: str, outcome_index: int) -> dict | None:
        """Holt die aktuelle Position eines Trackers auf einem bestimmten Market."""
        if not self._session or self._session.closed:
            return None
        try:
            url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=50"
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    positions = await resp.json()
                    for p in positions:
                        if p.get("conditionId") == condition_id and p.get("outcomeIndex") == outcome_index:
                            return p
                return None
        except Exception:
            return None

    def _compute_wallet_win_rate(self, wallet_name: str) -> float:
        """Berechnet echte Win-Rate mit Bayesian Prior.

        Verwendet Beta(3,3) Prior (= 50% Erwartung) + beobachtete Daten.
        Auch pnl_usd=0 (unresolved/unknown) wird als neutral behandelt, nicht als Verlust.
        """
        wins = 0
        losses = 0
        for p in self._copied_positions:
            if p.source_name == wallet_name and p.resolved:
                if p.pnl_usd > 0:
                    wins += 1
                elif p.pnl_usd < 0:
                    losses += 1
                # pnl_usd == 0 → unbekannt, ignorieren
        # Bayesian: Beta(prior_a + wins, prior_b + losses)
        prior_a, prior_b = 3.0, 3.0  # Prior: 50% mit Konfidenz von ~6 Pseudobeobachtungen
        return round((prior_a + wins) / (prior_a + prior_b + wins + losses), 3)

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

        # Per-wallet stats — Journal = copies total, RAM = active positions
        per_wallet: dict[str, dict] = {}

        # 1. Journal: total copies per wallet (Single Source of Truth for history)
        try:
            journal_path = Path("data/trade_journal.jsonl")
            for p in [journal_path, Path(__file__).parent.parent / "data" / "trade_journal.jsonl"]:
                if p.exists():
                    journal_path = p
                    break
            if journal_path.exists():
                with open(journal_path) as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        tid = e.get("trade_id", "")
                        if not tid.startswith("CT-") or e.get("event") != "open":
                            continue
                        wname = e.get("source_wallet_name", "") or e.get("asset", "") or "unknown"
                        if wname not in per_wallet:
                            per_wallet[wname] = {"copies": 0, "active": 0, "resolved": 0, "pnl": 0.0}
                        per_wallet[wname]["copies"] += 1
        except Exception:
            pass

        # 2. RAM: active positions (rebuilt from journal + wallet sanity check)
        for p in self._copied_positions:
            name = p.source_name or "unknown"
            if name not in per_wallet:
                per_wallet[name] = {"copies": 0, "active": 0, "resolved": 0, "pnl": 0.0}
            if not p.resolved:
                per_wallet[name]["active"] += 1
            per_wallet[name]["pnl"] += p.pnl_usd

        return {
            "strategy": self.STRATEGY_NAME,
            "running": self._running,
            "tracked_wallets": [
                {
                    "name": w["name"],
                    "address": w["address"],
                    "pnl": w.get("pnl", 0),
                    "category": w.get("category", "mixed"),
                    "notes": w.get("notes", ""),
                    "added_ts": w.get("added_ts", 0),
                    "status": "paused" if w["address"].lower() in self._paused_wallets else "active",
                }
                for w in self.tracked_wallets
            ],
            "copies_total": self._copy_count,
            "copies_active": active,
            "recent_copies": list(self._recent_copies),
            "per_wallet_stats": per_wallet,
            "config": {
                "poll_interval_s": self.poll_interval_s,
                "max_copy_size_usd": self.max_copy_size_usd,
                "max_concurrent": self.max_concurrent,
                "min_copy_price": self.min_copy_price,
                "max_copy_price": self.max_copy_price,
                "only_buys": self.only_buys,
            },
            "risk": self.risk.get_status(),
            "guards": self.guards.get_status(),
        }


# Auto-Register
register_strategy(CopyTradingStrategy.STRATEGY_NAME, CopyTradingStrategy)
