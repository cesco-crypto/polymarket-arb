# DEFINITIVE STRATEGY REFERENCE
## Polymarket Trading Bot -- All Strategies Documented

Generated: 2026-04-05
Source: Complete codebase analysis of `/Users/cesco/CODING/ARBITRAGE TRADING/`

---

# TABLE OF CONTENTS

1. [Architecture Overview](#1-architecture-overview)
2. [Strategy 1: Momentum Latency v2 (polymarket_latency.py)](#2-strategy-1-momentum-latency-v2)
3. [Strategy 2: Copy Trading (copy_trading.py)](#3-strategy-2-copy-trading)
4. [Strategy 3: Oracle Delay Arbitrage (oracle_delay_arb.py)](#4-strategy-3-oracle-delay-arbitrage)
5. [Strategy 4: HMSF Decision Engine (hmsf_strategy.py)](#5-strategy-4-hmsf-decision-engine)
6. [Shared Infrastructure](#6-shared-infrastructure)
7. [Dashboard / Web UI Reference](#7-dashboard--web-ui-reference)
8. [Strategy Interaction Matrix](#8-strategy-interaction-matrix)

---

# 1. ARCHITECTURE OVERVIEW

## 1.1 Strategy Base Class

**File:** `strategies/base.py`

Every strategy implements `StrategyBase` (ABC) with five methods:

| Method | Type | Purpose |
|--------|------|---------|
| `name` | `@property` | Unique internal name (e.g. `"momentum_latency_v2"`) |
| `description` | `@property` | Human-readable description for dashboard |
| `run()` | `async` | Starts the strategy (blocking, runs until shutdown) |
| `shutdown()` | `async` | Graceful shutdown of all components |
| `get_status()` | `sync` | Full status dict for the web dashboard |

## 1.2 Strategy Registry

**File:** `strategies/registry.py`

Factory pattern. Global dict `_REGISTRY: dict[str, type]` maps strategy names to classes.

- `register(name, cls)` -- Adds a strategy class. Called at module import via auto-register at bottom of each strategy file.
- `create_strategy(name, settings)` -- Instantiates a strategy by name. Used by dashboard startup and /api/strategy/enable.
- `list_strategies()` -- Returns `{name: description}` for all registered strategies.

**Registration happens automatically** via a call at the bottom of each strategy module:
```python
register_strategy(PolymarketLatencyStrategy.STRATEGY_NAME, PolymarketLatencyStrategy)
```

The four registered strategies at the time of import in `web_ui.py` (lines 26-29):
```python
import strategies.polymarket_latency    # "momentum_latency_v2"
import strategies.hmsf_strategy         # "hmsf_decision_engine"
import strategies.copy_trading          # "copy_trading"
import strategies.oracle_delay_arb      # "oracle_delay_arb"
```

## 1.3 Shared Execution Pipeline

All strategies that place live orders share:

- **PolymarketExecutor** (`core/executor.py`) -- CLOB client with EIP-712 L1 auth, HMAC L2 auth, pre-signing, BUY/SELL support
- **PreTradeCalculator** (`core/pretrade_calculator.py`) -- Fee formula, probability model, Kelly sizing (used by momentum strategies)
- **TradeJournal** (`core/trade_journal.py`) -- JSONL forensic logging for every trade open/close/live_update
- **Telegram** (`utils/telegram.py`) -- Real-time alerts

---

# 2. STRATEGY 1: MOMENTUM LATENCY v2

## 2A. Name + Registration

| Field | Value |
|-------|-------|
| **Registry Name** | `momentum_latency_v2` |
| **Class** | `PolymarketLatencyStrategy` |
| **File** | `strategies/polymarket_latency.py` |
| **Registration** | Line 971: `register_strategy(PolymarketLatencyStrategy.STRATEGY_NAME, PolymarketLatencyStrategy)` |

## 2B. What It Does

**Trading Thesis:** Binance price moves faster than Polymarket reprices its 5-minute/15-minute crypto binary options. When BTC/ETH moves sharply on Binance, the Polymarket orderbook still shows stale prices near 0.50 -- buy the correct side before the market catches up.

**Resolution Oracle:** Chainlink BTC/USD (NOT Binance). Binance is used only as a momentum indicator.

**Step-by-Step Execution Flow:**

1. **BinanceWebSocketOracle** streams real-time BTC/USDT and ETH/USDT ticks (sub-50ms)
2. **MarketDiscovery** finds active 5m/15m Polymarket crypto windows via slug calculation, refreshes orderbooks every 3s
3. **Signal Loop** (event-driven, reacts to each tick via `asyncio.Event` -- <5ms reaction time):
   - For each asset (BTC, ETH): check data freshness (max 5s), compute momentum over 15s window
   - Data Freshness Guard: skip if transit latency >300ms (prevents ghost momentum)
   - If `|momentum| >= 0.05%`, determine direction (UP/DOWN)
   - For each matching Polymarket window, run `_evaluate_window()`
4. **Window Evaluation** applies 9 sequential filters (see Guards below)
5. **PreTradeCalculator.evaluate()** runs: probability estimate, fee calc, edge check, Kelly sizing
6. If EXECUTE: open paper position, record in journal with 14 HFT metrics, fire Telegram alert
7. If live: place real CLOB limit order (BUY) with pre-signed digest for latency advantage
8. **Position Resolver Loop** (every 5s): resolve expired positions using correct asset oracle
9. **Auto-Redeem Loop** (every 5 min): redeem won conditional tokens back to USDC.e via dedicated thread pool
10. **Status Loop** (every 60s): log status, hourly forensic Telegram report

**Pre-Signing** (latency optimization, lines 296-304):
Every ~10 seconds, pre-signs orders for the next 2 windows on both UP and DOWN token IDs. Saves 20-50ms EIP-712 signature time when a signal actually fires.

**Markets:** Polymarket 5-minute and 15-minute BTC/ETH Up-or-Down binary options.

**Data Sources:** Binance WebSocket (momentum), Polymarket CLOB (orderbooks), Polymarket Gamma API (market discovery).

## 2C. Key Parameters

From `config.py` (with line numbers in config.py):

| Parameter | Default | Config Line | Description |
|-----------|---------|-------------|-------------|
| `oracle_symbols` | `["BTC/USDT", "ETH/USDT"]` | 94 | Binance symbols for price feed |
| `momentum_window_s` | `15.0` | 98 | Seconds for momentum calculation |
| `min_momentum_pct` | `0.05` | 103 | Minimum price move on Binance for signal (%) |
| `min_edge_pct` | `1.5` | 108 | Minimum net edge over fees (%) |
| `polymarket_max_fee_pct` | `1.80` | 112 | Max Polymarket taker fee at p=0.50 |
| `kelly_fraction` | `0.50` | 117 | Fractional Kelly (Half-Kelly) |
| `paper_capital_usd` | `1000.0` | 121 | Virtual starting capital |
| `max_position_pct` | `0.08` | 124 | Max 8% of capital per trade |
| `min_seconds_to_expiry` | `15.0` | 129 | No trade if <15s to expiry |
| `max_seconds_to_expiry` | `180.0` | 132 | No trade if >180s to expiry |
| `max_live_position_usd` | `5.0` | 143 | Max $5 per live trade |
| `order_type` | `"taker"` | 144 | taker (1.80% fee) or maker (rebate) |

From `polymarket_latency.py` (hardcoded in constructor and method bodies):

| Parameter | Value | Line | Description |
|-----------|-------|------|-------------|
| `min_liquidity_usd` | `15000.0` | 83 | Minimum $15K market liquidity |
| Price-First filter | `abs(ask - 0.50) > 0.15` | 343 | Block if ask >15 cents from 0.50 |
| Spread guard | `spread_pct > 3.0` | 357 | Block if bid-ask spread >3% |
| Max participation | `1.0%` | 362 | Order must be <1% of market liquidity |

## 2D. Guards / Risk Management

The **9 sequential filters** applied in `_evaluate_window()` and `_check_all_signals()`:

| # | Guard | Check | Location |
|---|-------|-------|----------|
| 1 | Tick Freshness | `oracle.is_fresh(symbol, max_age_s=5.0)` | Line 253 |
| 2 | Transit Latency | `transit_p50_ms > 300` --> skip | Line 276 |
| 3 | Momentum Threshold | `abs(momentum) >= min_momentum_pct` (0.05%) | Line 279 |
| 4 | Time Window | `15s <= seconds_to_expiry <= 180s` | Lines 319-322 |
| 5 | Orderbook Freshness | `window.orderbook_fresh` | Line 327 |
| 6 | Price-First | `abs(ask_price - 0.50) > 0.15` --> block | Line 343 |
| 7 | Spread Guard | `spread > 3.0%` --> block | Lines 348-357 |
| 8 | Participation Limit | Order > 1% market liquidity --> block | Lines 362-368 |
| 9 | Duplicate Prevention | 1 trade per window per direction | Lines 371-373 |

After passing all 9 filters, **PreTradeCalculator** adds:
- Edge check: `net_ev_pct > min_edge_pct` (1.5%)
- Kelly sizing: position_usd = min(kelly_bet, 8% of capital)
- Capital check: available_capital >= $10

Plus system-wide:
- **Kill-Switch:** -20% daily drawdown stops trading
- **RiskManager:** enforces portfolio limits

## 2E. How It Interacts With Other Strategies

- **Can run in parallel with:** `copy_trading`, `oracle_delay_arb`
- **CONFLICTS with:** `hmsf_decision_engine` (same signal source: Binance momentum on same 5m/15m windows). The dashboard enforces mutual exclusion -- enabling HMSF auto-disables momentum_latency_v2 and vice versa (lines 863-882 of web_ui.py).
- **Shared state:** ODA strategy reads from this strategy's `discovery.windows` to get token IDs (lines 296-303 of oracle_delay_arb.py)
- **Independent instances:** Each strategy creates its own BinanceWebSocketOracle, MarketDiscovery, PaperTrader, PolymarketExecutor, and TradeJournal

## 2F. Dashboard / Web UI

- **Page:** Main dashboard at `/` (index.html) -- shows real-time status via WebSocket at 1Hz
- **Strategy management:** `/strategy` page -- enable/disable toggle
- **API Endpoints:**
  - `GET /api/status` -- full strategy status (oracle, discovery, signals, paper trading)
  - `POST /api/strategy/enable {"strategy": "momentum_latency_v2"}` -- start
  - `POST /api/strategy/disable {"strategy": "momentum_latency_v2"}` -- stop
  - `POST /api/redeem` -- manual redeem of won tokens

## 2G. Key Code -- Core Signal Detection (polymarket_latency.py, lines 279-290)

```python
# Signal stark genug?
if abs(momentum) < self.settings.min_momentum_pct:
    continue

# Richtung bestimmen
direction = "UP" if momentum > 0 else "DOWN"

# Passende Polymarket-Fenster pruefen
windows = self.discovery.get_windows_for_asset(asset)
for window in windows:
    self._evaluate_window(asset, direction, momentum, tick.mid, window)
```

**Core Pre-Trade Decision (pretrade_calculator.py, lines 195-228):**

```python
p_true = self.estimate_probability(momentum_pct, seconds_to_expiry, direction)
raw_edge_pct = (p_true - polymarket_ask) / polymarket_ask * 100
fee_pct = self.polymarket_fee_pct(polymarket_ask)
effective_ask = polymarket_ask * (1 + fee_pct / 100)
net_ev_pct = (p_true / effective_ask - 1) * 100
kelly = self.kelly_fraction(p_true, polymarket_ask)
position_usd = min(available_capital_usd * kelly, available_capital_usd * max_position_pct)
```

---

# 3. STRATEGY 2: COPY TRADING

## 3A. Name + Registration

| Field | Value |
|-------|-------|
| **Registry Name** | `copy_trading` |
| **Class** | `CopyTradingStrategy` |
| **File** | `strategies/copy_trading.py` |
| **Registration** | Line 1290: `register_strategy(CopyTradingStrategy.STRATEGY_NAME, CopyTradingStrategy)` |

## 3B. What It Does

**Trading Thesis:** The top Polymarket traders (by lifetime PnL) have a proven edge on long-term markets (sports, politics, events). Copy their trades in real-time at small size ($5) to piggyback on their research and information advantage.

**Step-by-Step Execution Flow:**

1. **Startup:** Load persistent dedup state from `data/copy_seen_trades.json`, fetch last 20 trades per wallet from Polymarket Data API and mark them as "seen"
2. **Poll Loop** (every 5 seconds): iterate through all tracked wallets
3. For each wallet: fetch latest 5 trades via `https://data-api.polymarket.com/activity?user={wallet}&limit=5&type=TRADE`
4. For each new trade (timestamp > last_seen):
   - Run through **12 Guards** (see below)
   - If SELL trade: trigger SELL-Copy (exit our position)
   - If BUY and all guards pass: execute copy trade
5. **Copy Execution:** Place BUY limit order at same price as source trader
6. **SELL-Copy** (auto-exit): When tracked wallet sells, we sell our matching position
7. **Hedge Detection:** If a wallet buys BOTH sides of the same market, we copy both (proportional sizing)
8. **Kelly Sizing:** Dynamic position size based on wallet's historical win rate (Bayesian Beta(3,3) prior)

**Tracked Wallets** (hardcoded defaults, lines 341-377):

| Name | Address | Lifetime PnL | Category |
|------|---------|-------------|----------|
| kch123 | `0x6a72...` | $11.1M | Sports (NHL/Hockey) |
| RN1 | `0x2005...` | $6.8M | Sports (diversified) |
| swisstony | `0x204f...` | $5.7M | Sports (soccer) |
| KeyTransporter | `0x94f1...` | $5.7M | Mixed |
| mikatrade77 | `0x2378...` | $5.1M | Mixed |

**Markets:** ALL Polymarket markets (sports, politics, events, crypto). NOT limited to 5m/15m windows.

**Data Sources:** Polymarket Data API (wallet activity), Polymarket CLOB (orderbooks for slippage check).

## 3C. Key Parameters

From `copy_trading.py` constructor (lines 458-464):

| Parameter | Default | Line | Description |
|-----------|---------|------|-------------|
| `poll_interval_s` | `5.0` | 458 | Seconds between wallet checks |
| `max_copy_size_usd` | `5.0` | 459 | Fixed $5 per copy trade |
| `max_concurrent` | `10` | 460 | Max 10 concurrent market positions |
| `min_seconds_to_copy` | `5` | 461 | Trade must be <5 min old to copy |
| `only_buys` | `False` | 462 | Copy both BUY + SELL signals |
| `min_copy_price` | `0.25` | 463 | Don't copy below 25 cents |
| `max_copy_price` | `0.85` | 464 | Don't copy above 85 cents |
| `MAX_TRACKED_WALLETS` | `20` | 488 | Hard cap to prevent poll DoS |
| `PAPER_COPY_HOURS` | `24` | 490 | New wallets: 24h observe before live copy |

**CopyRiskManager limits** (lines 60-63):

| Limit | Value | Description |
|-------|-------|-------------|
| `max_daily_loss_usd` | `-$15` | Daily loss kill-switch |
| `max_total_loss_usd` | `-$30` | Total loss kill-switch |
| `max_daily_trades` | `30` | Max trades per day |
| `max_consecutive_losses` | `5` | Consecutive losses pause |

**SmartGuards thresholds** (lines 189, 198):

| Guard | Threshold | Description |
|-------|-----------|-------------|
| `max_slippage_pct` | `5.0%` | Max price drift since original trade |
| `max_market_exposure` | `2` | Max 2 positions per market |
| Bait blacklist | `3 dumps` | Auto-blacklist after 3 dump-after-copy events |
| Dump detection window | `600s` (10 min) | Time window for dump detection |

## 3D. Guards / Risk Management -- THE 12 GUARDS

| # | Guard | Check | Line |
|---|-------|-------|------|
| 1 | **Unique Trade Key** | Dedup via `{addr}_{ts}_{condition_id}` | 701-704 |
| 2 | **Outcome-Aware Dedup** | Same outcome on same market blocked; hedge (other outcome) ALLOWED | 712-733 |
| 3 | **Price Filter** | `0.25 <= price <= 0.85` | 736-739 |
| 4 | **Age Filter** | Trade must be <5 min old (15 min for hedges) | 742-745 |
| 5 | **Max Concurrent Markets** | Max 10 distinct markets (hedges don't count extra); 7-day auto-resolve | 751-764 |
| 6 | **Balance Check** | Wallet balance >= 1.5x copy size | 767-774 |
| 6b | **Paper-Copy Period** | New wallets: 24h observe, no live execution | 777-786 |
| 7 | **Kill-Switch** | CopyRiskManager: daily/total loss limits | 789-791 |
| 8 | **Bait/Dump Detection** | Auto-blacklist wallets that dump after we copy | 794-798 |
| 9 | **Slippage Protection** | Current CLOB ask vs original price, max 5% drift | 801-808 |
| 10 | **Liquidity Profiling** | Original trade >= $5, reject tiny trades | 811-813 |
| 11 | **Market Correlation** | Max 2 copies on same condition_id | 816-819 |
| 12 | **Model Drift Detection** | Warning if trader changes category (sports->crypto) | 822-826 |

## 3E. SELL-Copy and Hedge-Copy (Advanced Features)

**SELL-Copy** (lines 912-1057): When a tracked wallet SELLS a position:
1. Find our matching position (same condition_id, same outcome_index, same source wallet)
2. Clean up phantom positions (failed BUYs without token_id)
3. Execute SELL order at the source trader's sell price
4. Remove outcome_key from dedup set (enables re-entry if trader re-buys)
5. Unwind hedge leg if primary is sold

**Hedge-Copy** (lines 712-725, 828-854): When a wallet buys BOTH outcomes of the same market:
1. Detected via outcome_key: `{addr}_{condition_id}_{1 - outcome_index}` already exists
2. Proportional sizing: ratio = trader_hedge_usd / trader_first_leg_usd
3. Our hedge size = base_size * ratio (minimum $1)

**Kelly Sizing** (lines 306-334, 856-869): Uses `kelly_copy_size()` function:
- Win rate from Bayesian Beta(3,3) prior + observed wins/losses per wallet
- Kelly: `f* = (p * b - q) / b` where b = implied odds from price
- Half-Kelly for safety
- Max 2x base_size cap

## 3F. Dashboard / Web UI

- **Page:** `/copytrading` (copytrading.html)
- **API Endpoints:**
  - `GET /api/copy/status` -- full status (wallets, copies, risk, guards)
  - `GET /api/copy/leaderboard` -- Polymarket top traders leaderboard
  - `GET /api/copy/analyze?wallet={address}` -- deep analysis of a wallet
  - `POST /api/copy/add-wallet {"address": "0x...", "name": "..."}` -- add wallet
  - `POST /api/copy/remove-wallet {"address": "0x..."}` -- remove wallet
  - `POST /api/copy/pause-wallet {"address": "0x..."}` -- pause wallet
  - `POST /api/copy/resume-wallet {"address": "0x..."}` -- resume wallet
  - `POST /api/copy/config {"max_copy_size_usd": 10, ...}` -- update runtime config
  - `POST /api/strategy/enable {"strategy": "copy_trading"}` -- start
  - `POST /api/strategy/disable {"strategy": "copy_trading"}` -- stop

**Runtime-configurable via API** (lines 1329-1358):
- `max_copy_size_usd` (1-100)
- `max_concurrent` (1-50)
- `poll_interval_s` (1-60)
- `min_copy_price` (0.01-0.99)
- `max_copy_price` (0.01-0.99)

## 3G. Key Code -- Core Copy Decision (copy_trading.py, lines 700-798)

```python
# GUARD 1: Unique Trade Key
trade_key = f"{addr}_{ts}_{condition_id}"
if trade_key in self._seen_trade_keys:
    continue

# GUARD 3: Price Filter
if price < self.min_copy_price or price > self.max_copy_price:
    continue

# GUARD 7: Kill-Switch
can_trade, reason = self.risk.can_trade()
if not can_trade:
    continue

# GUARD 8: Bait/Dump Detection
bait_ok, bait_msg = self.guards.check_bait(addr)
if not bait_ok:
    continue
```

---

# 4. STRATEGY 3: ORACLE DELAY ARBITRAGE

## 4A. Name + Registration

| Field | Value |
|-------|-------|
| **Registry Name** | `oracle_delay_arb` |
| **Class** | `OracleDelayArbStrategy` |
| **File** | `strategies/oracle_delay_arb.py` |
| **Registration** | Line 643: `register_strategy(OracleDelayArbStrategy.STRATEGY_NAME, OracleDelayArbStrategy)` |

## 4B. What It Does

**Trading Thesis:** After a 5-minute crypto window closes, the result is already known (Binance shows the outcome) but the Polymarket CLOB is still open because the oracle hasn't resolved yet. Buy the winning token at 0.97-0.99, wait for oracle resolution, redeem at 1.00. Profit ~1% per trade with near-certainty.

**This is the "Sharky6999" strategy** -- a known Polymarket whale who makes $4K+ per window with 50-67 split orders. This implementation does the same at smaller scale ($5-50 per window).

**Key Insight:** NO prediction needed. The outcome is already determined. The only risk is oracle disagreeing with Binance (rare).

**Fee Advantage:** At entry price 0.99, the Polymarket fee is only 0.07% (vs 1.80% at 0.50).

**Step-by-Step Execution Flow:**

1. **Main Loop** (every 2 seconds): compute next window close times aligned to 5-min epoch intervals
2. For each window that closed 2-45 seconds ago:
   - Dedup check (skip already sniped windows)
   - Max concurrent check (max 5 simultaneous snipes)
   - Get token IDs: first try from running momentum strategy's discovery, fallback to CLOB API
3. **Winner Determination:** Fetch asks for UP and DOWN tokens from CLOB. If one side's ask >= 0.90, that side won.
4. **Entry Price Check:** Winner ask must be between 0.90 and 0.99
5. **Snipe Execution:** Place BUY limit order at min(0.99, ask_price)
6. **Fill Verification** (2s after order): Check if order was matched
   - FILLED: success
   - UNFILLED: cancel order
   - PARTIAL: keep tracking
7. Wait for oracle resolution --> token redeems at $1.00

**Markets:** Polymarket 5-minute BTC/ETH Up-or-Down windows (same markets as momentum strategy, but post-close instead of pre-close).

**Data Sources:** Polymarket CLOB (orderbook, order status), Momentum strategy's MarketDiscovery (token IDs), Polymarket Gamma API (fallback).

## 4C. Key Parameters

From `oracle_delay_arb.py` constructor (lines 112-117):

| Parameter | Default | Line | Description |
|-----------|---------|------|-------------|
| `trade_size_usd` | `5.0` | 112 | $5 per trade (data collection phase) |
| `min_entry_price` | `0.90` | 113 | Buy only if price >= 0.90 |
| `max_entry_price` | `0.99` | 114 | Max 0.99 (CLOB limit) |
| `delay_after_close_s` | `2.0` | 115 | Wait 2s after window close |
| `max_delay_s` | `45.0` | 116 | Max 45s after close (faster than Sharky's 71s) |
| `max_concurrent` | `5` | 117 | Max 5 simultaneous snipes |

## 4D. Guards / Risk Management

| Guard | Check | Location |
|-------|-------|----------|
| Window timing | Only 2-45s after close | Lines 219-222 |
| Dedup | slug-based set `_sniped_windows` | Lines 225-226 |
| Max concurrent | Active unresolved trades < 5 | Lines 229-231 |
| Token availability | Token IDs must be fetchable | Lines 237-238 |
| Winner clarity | One side's ask must be >= 0.90 | Lines 250-263 |
| Price ceiling | ask <= 0.99 (CLOB max) | Lines 266-269 |
| Fill verification | Cancel unfilled orders after 2s | Lines 528-538 |

**Risks documented in module docstring:**
- Oracle resolves differently than Binance shows (rare, but -100% loss)
- CLOB liquidity at 0.99 is thin (split orders needed)
- Competition from other bots

## 4E. How It Interacts With Other Strategies

- **Can run in parallel with:** ALL other strategies (no shared state, no conflicts)
- **Depends on:** Momentum strategy's `MarketDiscovery` for token IDs (soft dependency -- has CLOB API fallback). Lines 296-303 directly import from `dashboard.web_ui.active_strategies`.
- **No shared state:** Own executor, own journal, own session
- **Complementary timing:** This strategy trades AFTER window close; momentum trades BEFORE. They cover different time windows on the same markets.

## 4F. Dashboard / Web UI

- **Page:** Status visible on main dashboard via `get_status()`
- **API Endpoints:**
  - `POST /api/strategy/enable {"strategy": "oracle_delay_arb"}` -- start
  - `POST /api/strategy/disable {"strategy": "oracle_delay_arb"}` -- stop
- **Status dict** (lines 623-639): snipes_total, snipes_active, recent_snipes, config

## 4G. Key Code -- Window Close Detection (oracle_delay_arb.py, lines 172-192)

```python
def _compute_next_window_closes(self) -> list[dict]:
    now = time.time()
    interval = 300  # 5 minutes
    base_ts = int(now // interval) * interval
    for offset in range(-3, 2):
        start_ts = base_ts + offset * interval
        end_ts = start_ts + interval
        seconds_since_close = now - end_ts
        for asset in ["btc", "eth"]:
            slug = f"{asset}-updown-5m-{start_ts}"
            results.append({...})
```

**Snipe Execution with Fill Verification (lines 513-538):**

```python
res = await self.executor.place_order(
    token_id=token_id, side="BUY",
    price=min(0.99, ask_price),  # Max 0.99 (CLOB Limit!)
    size_usd=self.trade_size_usd, ...
)
if res.success:
    await asyncio.sleep(2)  # Give CLOB time to match
    fill_status = await self._check_fill(res.order_id)
    if fill_status == "UNFILLED":
        await self._cancel_order(res.order_id)  # Cancel unfilled
```

---

# 5. STRATEGY 4: HMSF DECISION ENGINE

## 5A. Name + Registration

| Field | Value |
|-------|-------|
| **Registry Name** | `hmsf_decision_engine` |
| **Class** | `HMSFStrategy` |
| **File** | `strategies/hmsf_strategy.py` |
| **Registration** | Line 620: `register_strategy(HMSFStrategy.STRATEGY_NAME, HMSFStrategy)` |

The HMSF (Hybrid Multi-Strategy Framework) is composed of 4 files:

| File | Purpose |
|------|---------|
| `strategies/hmsf_strategy.py` | Main StrategyBase implementation (lifecycle, loops, execution) |
| `strategies/hmsf_engine.py` | DecisionEngine -- orchestrates all 4 modules per tick |
| `strategies/hmsf_modules.py` | 4 independent trading modules (the actual signal logic) |
| `strategies/hmsf_core.py` | StrategyConfig singleton, GlobalRiskFilter, RegimeDetector, AntiWhipsawExit |

## 5B. What It Does

**Trading Thesis:** A single momentum signal is not enough. The HMSF framework runs 4 different modules in parallel, each with different entry criteria, and uses a central Decision Engine to resolve conflicts and select the best signal. This creates a more robust, multi-factor trading system.

**The 4 Modules:**

### Module 0: Legacy Momentum (UNMODIFIED v2.0 clone)

Exact copy of the momentum_latency_v2 logic, isolated in its own class. Same 9 filters, same PreTradeCalculator. Exists as a baseline comparison.

**Config key:** `legacy_momentum_enabled` (default: True)

### Module 1: Enhanced Latency-Momentum

Smarter version requiring ALL 4 triggers simultaneously:

1. **Binance Momentum >= 0.05%** in 15s
2. **Price-Gap >= 0.08%**: Polymarket ask must lag behind Binance signal. Calculated as `(0.50 - ask_price) / 0.50 * 100` when ask <= 0.50 (higher = more edge)
3. **Orderbook Imbalance >= 12%**: Directional bid/ask volume confirms the move. `(bid_vol / ask_vol - 1) * 100` must be positive for UP (inverted for DOWN)
4. **Ask price 0.35-0.65**: Sweet spot for fee/edge ratio

Plus: HMSF GlobalRiskFilter + Regime adjustment (ETH UP malus in downtrend).

**Config key:** `enhanced_latency_momentum_enabled` (default: True)

**Module-specific thresholds (hmsf_modules.py, lines 270-273):**
| Threshold | Value |
|-----------|-------|
| `MIN_PRICE_GAP_PCT` | 0.08% |
| `MIN_OB_IMBALANCE_PCT` | 12.0% |
| `MIN_ASK` | 0.35 |
| `MAX_ASK` | 0.65 |

### Module 2: Last-Seconds-Sniping

High-probability trades near expiry where one side is already winning:

1. **15-60 seconds to expiry**
2. **One side Ask >= 0.72** (market is 72%+ certain)
3. **Price-Gap >= 0.10%** (stricter than Module 1)
4. **Orderbook Imbalance >= 15%** (stricter than Module 1)
5. **Direction consistency**: momentum must confirm the winning side

**Special rules:**
- Max position: 1.2% of capital (not 2.5%)
- Early exit: COMPLETELY DISABLED (hold to resolution)
- Win rate estimate: implied from ask price (ask 0.80 = 80% WR)

**Config key:** `last_seconds_sniping_enabled` (default: True)

**Module-specific thresholds (hmsf_modules.py, lines 494-500):**
| Threshold | Value |
|-----------|-------|
| `MIN_EXPIRY_S` | 15.0s |
| `MAX_EXPIRY_S` | 60.0s |
| `MIN_ASK_PRICE` | 0.72 |
| `MIN_PRICE_GAP_PCT` | 0.10% |
| `MIN_OB_IMBALANCE_PCT` | 15.0% |
| `MAX_RISK_PCT` | 1.2% |

### Module 3: Both-Sides-Arbitrage (Delta Neutral)

Risk-free arbitrage when UP + DOWN asks sum to less than $1.00 after fees:

1. **Effective cost < $1.00**: `effective_up + effective_down < 1.00` where effective_price = ask / (1 - feeRate * (1-ask))
2. **Liquidity >= $8,000** on the market
3. **>= 15 seconds to expiry**
4. Buy BOTH sides with equal USD amounts
5. One side will always resolve to $1.00 --> guaranteed profit

**Fee calculation** (lines 783-787): `FEE_RATE = 0.072` (Polymarket crypto feeRate since 30.03.2026)

**Config key:** `both_sides_arbitrage_enabled` (default: True)

**Module-specific thresholds (hmsf_modules.py, lines 731-733):**
| Threshold | Value |
|-----------|-------|
| `FEE_RATE` | 0.072 |
| `MIN_LIQUIDITY_USD` | $8,000 |
| `MIN_EXPIRY_S` | 15.0s |

## 5C. Key Parameters -- HMSF Core Config

From `hmsf_core.py` StrategyConfig (lines 37-66), all live-updatable via web UI:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `legacy_momentum_enabled` | `True` | Module 0 toggle |
| `enhanced_latency_momentum_enabled` | `True` | Module 1 toggle |
| `last_seconds_sniping_enabled` | `True` | Module 2 toggle |
| `both_sides_arbitrage_enabled` | `True` | Module 3 toggle |
| `conservative_mode` | `True` | Quarter-Kelly, strict filters |
| `aggressive_mode` | `False` | Half-Kelly, relaxed filters |
| `max_risk_per_trade_percent` | `2.5` | Quarter-Kelly cap |
| `min_net_ev_pct` | `1.8` | Minimum Net-EV after fees |
| `min_liquidity_usd` | `8000.0` | Minimum market liquidity |
| `max_transit_latency_ms` | `80.0` | Max Binance->Bot latency |
| `max_spread_pct` | `5.5` | Max bid-ask spread |
| `min_shares` | `5.0` | Polymarket CLOB minimum |
| `regime_trend_window_minutes` | `30` | Trend calculation window |
| `regime_eth_up_malus_pct` | `30.0` | Max ETH UP malus in downtrend |
| `exit_min_hold_seconds` | `8.0` | Hysteresis minimum hold time |
| `exit_mid_price_loss_pct` | `18.0` | Exit threshold (DEACTIVATED) |
| `exit_oracle_adverse_pct` | `0.25` | Oracle adverse threshold (DEACTIVATED) |

## 5D. Guards / Risk Management

### GlobalRiskFilter (hmsf_core.py, lines 134-192)

Applied to EVERY trade signal from Modules 0, 1, and 1 (Module 2 has own sizing; Module 3 is self-contained):

| # | Filter | Threshold | Action |
|---|--------|-----------|--------|
| 1 | Quarter-Kelly Cap | `capital * 2.5%` | Cap position size |
| 2 | Fee Check | `net_ev_pct >= 1.8%` | Block if too low |
| 3 | Liquidity | `>= $8,000` | Block if thin |
| 4 | Transit Latency | `<= 80ms` | Block if stale |
| 5 | Spread Guard | `<= 5.5%` | Block if wide |
| 6 | Min Shares | `>= 5 shares` | Block if too small for CLOB |

### RegimeDetector (hmsf_core.py, lines 222-318)

Tracks 30-minute BTC and ETH price trends:
- **Dead zone:** trend must be < -0.15% to count as "trending down" (prevents noise)
- **ETH UP malus:** When ETH is in a 30-min downtrend, ETH UP signals get a proportional Net-EV reduction: `malus = min(30%, |trend| * 20)`. Example: -0.50% trend = 10% malus.
- **Regime tags:** "trending_down", "trending_up", "mixed"

### AntiWhipsawExit (hmsf_core.py, lines 348-399)

Currently **DEACTIVATED** (stresstest fix 04.04.2026):
- Stop-loss on 5-min binary options is mathematically useless (EV of selling = EV of holding)
- Hold-to-resolution is strictly better
- Code preserved for future SELL implementation

### DecisionEngine Conflict Resolution (hmsf_engine.py, lines 132-281)

1. **Module 3 (Arb)** is always checked first, independently of momentum
2. **Module 2 + 3 are mutually exclusive** for the same token (Arb takes priority)
3. **Conservative mode** (default):
   - At least 1 primary module (0, 1, or 2) must fire
   - Best signal by Net-EV is selected
   - GlobalRiskFilter applied (except for Module 2 which has own sizing)
   - M0 + M1 count as ONE "momentum family" for confluence calculation
4. **Aggressive mode:**
   - Any single module firing is sufficient
   - Best signal by Net-EV, no additional confirmation needed

## 5E. How It Interacts With Other Strategies

- **CONFLICTS with:** `momentum_latency_v2` (same signal source, same markets). Dashboard enforces mutual exclusion.
- **Can run in parallel with:** `copy_trading` (different markets), `oracle_delay_arb` (different timing)
- **Completely independent instances:** Own BinanceWebSocketOracle, MarketDiscovery, PaperTrader, PolymarketExecutor, TradeJournal, DecisionEngine
- **No shared state** with any other strategy (each creates fresh instances of all components)

## 5F. Dashboard / Web UI

- **Page:** Status visible on main dashboard (same layout as momentum_latency_v2)
- **Strategy management:** `/strategy` page
- **HMSF-specific API:**
  - `GET /api/hmsf/config` -- all module toggles and thresholds
  - `POST /api/hmsf/config {"legacy_momentum_enabled": false, ...}` -- update any config value live (no restart needed)
  - `POST /api/strategy/enable {"strategy": "hmsf_decision_engine"}` -- start (auto-disables momentum_latency_v2)

## 5G. Key Code -- Decision Engine Core (hmsf_engine.py, lines 88-148)

```python
def evaluate_tick(self, *, asset, direction, momentum_pct, binance_price,
                  window, calculator, capital_usd, transit_latency_ms, ...):

    # Phase 1: Anti-Whipsaw -- DEACTIVATED

    # Phase 2: Module 3 (Arb) -- always check first
    arb_signal = self.mod3_arb.evaluate(asset=asset, window=window, capital_usd=capital_usd)
    if arb_signal:
        result.action = "ARB"
        return result

    # Phase 3: Primary modules (0, 1, 2)
    sig0 = self.mod0_legacy.evaluate(...)
    sig1 = self.mod1_enhanced.evaluate(...)
    sig2 = self.mod2_snipe.evaluate(...)

    # Phase 4: Conservative = best by Net-EV + GlobalRiskFilter
    #          Aggressive = best by Net-EV, no extra confirmation
```

---

# 6. SHARED INFRASTRUCTURE

## 6.1 PolymarketExecutor (core/executor.py)

**Three-flag safety gate:** Live orders only if `config.live_trading == True` AND `POLYMARKET_PRIVATE_KEY` set AND mode is correct.

| Feature | Details |
|---------|---------|
| Auth | L1: EIP-712 (once at init), L2: HMAC (per order) |
| Pre-signing | Signs orders in advance, saves 20-50ms at execution. Drift check: max 5% price change. |
| BUY/SELL | Full support. SELL never capped by max position. |
| Maker/Taker | Maker: 1 cent better than market (rebate). Taker: at market price. |
| Min order | $1.00, 1 share |
| Max order | `max_live_position_usd` ($5 default) |
| Balance check | On-chain USDC.e via Polygon RPC with 4 fallback endpoints |

## 6.2 PreTradeCalculator (core/pretrade_calculator.py)

**Fee formula (line 82-84):**
```python
fee_pct = max_fee * 4 * p * (1 - p)  # Peak 1.80% at p=0.50
```

**Probability model (lines 107-126):**
- Time-dampened momentum: `effective_momentum = momentum * min(1.0, 120 / seconds_to_expiry)`
- Logistic function: `p_up = 1 / (1 + exp(-momentum * 3.0))`
- Calibrated against 7-month BTC data (scaling=3.0)

**Kelly criterion (lines 130-147):**
```python
raw_kelly = (p_true - p_market) / (1 - p_market)  # Binary Kelly
capped = min(raw_kelly, 0.25)                       # 25% hard cap
final = capped * kelly_fraction                      # 0.50 = Half-Kelly
```

**Net EV calculation (lines 207-211):**
```python
effective_ask = polymarket_ask * (1 + fee_pct / 100)
net_ev_pct = (p_true / effective_ask - 1) * 100
```

## 6.3 Telegram Integration

Every strategy sends alerts for: startup, signals, live orders, resolved positions, drawdown warnings, hourly forensic reports, auto-redeems.

---

# 7. DASHBOARD / WEB UI REFERENCE

## 7.1 Pages

| Route | File | Description |
|-------|------|-------------|
| `/` | `index.html` | Main dashboard (WebSocket 1Hz status) |
| `/strategy` | `strategy.html` | Strategy enable/disable + HMSF module toggles |
| `/copytrading` | `copytrading.html` | Copy trading dashboard (wallets, trades, risk) |
| `/wallet` | `wallet.html` | Wallet management |
| `/quant-lab` | `quant_lab.html` | Backtesting and research |
| `/masterplan` | `masterplan.html` | 10-phase roadmap |

## 7.2 Strategy Management Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Full primary strategy status |
| GET | `/api/strategies` | List all registered strategies + active states |
| POST | `/api/strategy/enable` | Start a strategy (body: `{"strategy": "name"}`) |
| POST | `/api/strategy/disable` | Stop a strategy (body: `{"strategy": "name"}`) |

## 7.3 HMSF Configuration Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/hmsf/config` | Read all HMSF config values |
| POST | `/api/hmsf/config` | Update any HMSF config values (partial update) |

## 7.4 Copy Trading Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/copy/status` | Full copy trading status |
| GET | `/api/copy/leaderboard` | Top Polymarket traders |
| GET | `/api/copy/analyze?wallet=0x...` | Deep wallet analysis |
| POST | `/api/copy/add-wallet` | Add tracked wallet |
| POST | `/api/copy/remove-wallet` | Remove tracked wallet |
| POST | `/api/copy/pause-wallet` | Pause wallet tracking |
| POST | `/api/copy/resume-wallet` | Resume wallet tracking |
| POST | `/api/copy/config` | Update runtime config |

## 7.5 Other Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/wallet` | Wallet balance and positions |
| GET | `/api/redeemable` | Redeemable token positions |
| POST | `/api/redeem` | Manual redeem trigger |
| GET | `/api/collect/status` | Auto-collect (redeem + merge) status |
| POST | `/api/collect/now` | Manual collect trigger |

## 7.6 Conflict Resolution (web_ui.py, lines 860-883)

```
MOMENTUM_GROUP = {"momentum_latency_v2", "hmsf_decision_engine"}
```

- Enabling a strategy in MOMENTUM_GROUP auto-disables the other one in that group
- `copy_trading` and `oracle_delay_arb` can ALWAYS run in parallel with anything
- Primary strategy for dashboard broadcast: momentum strategies preferred over copy trading

---

# 8. STRATEGY INTERACTION MATRIX

## 8.1 Parallel Execution Compatibility

| | momentum_v2 | hmsf | copy_trading | oracle_delay |
|---|---|---|---|---|
| **momentum_v2** | -- | CONFLICT | PARALLEL OK | PARALLEL OK |
| **hmsf** | CONFLICT | -- | PARALLEL OK | PARALLEL OK |
| **copy_trading** | PARALLEL OK | PARALLEL OK | -- | PARALLEL OK |
| **oracle_delay** | PARALLEL OK | PARALLEL OK | PARALLEL OK | -- |

## 8.2 State Sharing

| Component | Shared? | Details |
|-----------|---------|---------|
| BinanceWebSocketOracle | NO | Each momentum strategy creates its own instance |
| MarketDiscovery | NO | Each creates own instance. ODA reads from momentum's discovery as optimization. |
| PaperTrader | NO | Per-strategy capital tracking |
| PolymarketExecutor | NO | Each creates own CLOB client |
| TradeJournal | SEMI | All write to same `data/trade_journal.jsonl` but with different trade ID prefixes (LT-, CT-, ODA-) |
| CopyRiskManager | NO | Copy trading only, persistent to `data/copy_risk_state.json` |
| SmartGuards | NO | Copy trading only, in-memory |
| HMSF StrategyConfig | SINGLETON | `hmsf_config` is a module-level singleton shared across all HMSF modules (thread-safe) |

## 8.3 Summary of All Strategies

| Strategy | Markets | Signal Source | Timing | Risk Profile | Live Trade Size |
|----------|---------|--------------|--------|-------------|----------------|
| **Momentum v2** | 5m/15m crypto binary | Binance momentum | 15-180s before expiry | Medium (Half-Kelly, 8% cap) | $5 |
| **HMSF** | 5m/15m crypto binary | Binance momentum + orderbook + regime | 15-180s (M0/M1), 15-60s (M2), any (M3) | Conservative (Quarter-Kelly, 2.5% cap) | $5 |
| **Copy Trading** | ALL markets | Top wallet activity | Real-time (5s poll) | Low ($5 fixed, 12 guards) | $5 |
| **Oracle Delay** | 5m crypto binary (post-close) | CLOB price after close | 2-45s after window close | Very low (~1% gain, near-certain) | $5 |

---

*End of Strategy Reference Document*
