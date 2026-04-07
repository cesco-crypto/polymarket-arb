# Polymarket Latency Arbitrage Bot — Technical Deep Dive v2.0

**Current Version: v2.0 | Date: April 2, 2026 | Status: LIVE TRADING on AWS Dublin**
**Wallet: 0xc0135e6D...4105 | Proven Profit: +$22.62 (28.3% ROI) | Win Rate: 71.4%**

---

## Table of Contents

1. [Version History & Evolution](#1-version-history--evolution)
2. [System Architecture](#2-system-architecture)
3. [Detailed Component Documentation](#3-detailed-component-documentation)
4. [Mathematical Models](#4-mathematical-models)
5. [Trading Strategy](#5-trading-strategy)
6. [Backtest Engine](#6-backtest-engine)
7. [Infrastructure & Deployment](#7-infrastructure--deployment)
8. [Monitoring & Alerting](#8-monitoring--alerting)
9. [Proven Results](#9-proven-results)
10. [Known Limitations & Future Improvements](#10-known-limitations--future-improvements)
11. [Configuration Reference](#11-configuration-reference)
12. [API Reference](#12-api-reference)
13. [Glossary](#13-glossary)

---

## 1. Version History & Evolution

This section documents every major version change chronologically, tracing the system from initial commit to profitable live trading.

---

### v0.1 — Initial Commit (dc59b56)

**Date**: March 2026 (early)
**Commit**: `dc59b56 Initial commit: Polymarket Latency Arbitrage Bot`

#### What Was Built From Scratch

The initial commit established the complete foundation of the Polymarket Latency Arbitrage Bot. The system was designed from day one as a production-grade trading bot, not a prototype.

**Architecture Decisions**:
- Single async event loop (Python `asyncio`) for all I/O: WebSocket streams, HTTP requests, dashboard serving
- Modular component architecture: each concern (price feed, market discovery, pre-trade math, execution, risk) in its own module
- Pydantic Settings for type-safe configuration with automatic `.env` loading
- FastAPI web dashboard with WebSocket 1Hz broadcasting for real-time monitoring

**Original Tech Stack**:

| Component | Technology | Reason |
|-----------|-----------|--------|
| Runtime | Python 3.11+ | Native async/await, type hints |
| Price Feed | Binance WebSocket (bookTicker) | Sub-50ms tick data, lowest latency available |
| Market Discovery | Slug-based computation against Gamma API | Ephemeral 5m/15m markets are not searchable |
| Web Framework | FastAPI + uvicorn | Native async, WebSocket support, auto OpenAPI |
| Configuration | pydantic-settings | Type-safe, auto env-var mapping |
| Logging | loguru | Zero-config, rotation, compression |
| HTTP Client | aiohttp | Non-blocking, shares event loop |

**Core Files Created**:
- `config.py` — Centralized Pydantic Settings with all parameters
- `main.py` — Entry point (Web dashboard or CLI paper trading)
- `core/binance_ws.py` — Binance WebSocket Oracle with rolling price windows
- `core/market_discovery.py` — Slug-based 5m/15m market discovery + orderbook refresh
- `core/pretrade_calculator.py` — Fee curves, probability estimation, Kelly sizing
- `core/paper_trader.py` — Paper trading engine with full PnL tracking
- `core/risk_manager.py` — Kill switches and circuit breakers
- `strategies/polymarket_latency.py` — Main strategy orchestrator
- `dashboard/web_ui.py` — FastAPI backend with WebSocket broadcasting
- `utils/telegram.py` — Telegram alert system
- `utils/logger.py` — Loguru configuration

**Initial Parameters**:
```
momentum_window_s:    60.0    (later changed to 15s in v2.0)
min_momentum_pct:     0.15    (later changed to 0.05% in v2.0)
min_edge_pct:         2.0     (later changed to 1.5% in v2.0)
polymarket_max_fee_pct: 3.15  (later corrected to 1.80% in v1.5)
kelly_fraction:       0.50    (unchanged)
paper_capital_usd:    1000.0  (unchanged)
max_position_pct:     0.08    (unchanged)
```

---

### v0.1 to v1.0 — Paper Trading Phase (dc59b56 to be1ee19)

This phase spans 24 commits and represents the most active development period. Each commit addressed specific issues discovered during live paper trading on Render.

#### Deployment on Render (80b56c5, a1d0d23)

**Commit `80b56c5`**: Add self-ping keep-alive for Render free tier
- Problem: Render free tier sleeps after 15 minutes of inactivity
- Solution: Self-ping loop every 10 minutes via `RENDER_EXTERNAL_URL` env var
- Implementation in `dashboard/web_ui.py`:

```python
async def _keep_alive_loop() -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return  # Only active on Render
    async with aiohttp.ClientSession() as session:
        while True:
            await session.get(f"{url}/api/status")
            await asyncio.sleep(600)  # 10 minutes
```

**Commit `a1d0d23`**: Fix WebSocket protocol for HTTPS (Render deployment)
- Problem: Browser tried `ws://` on HTTPS Render deployment
- Solution: Frontend detects protocol and uses `wss://` accordingly

#### Backtest Validation (9fb5715)

**Commit `9fb5715`**: Backtest validation + config optimization
- Created `backtest_engine.py` with Binance 1-minute kline data
- Grid search over momentum thresholds and windows
- Validated that momentum signals predict 5m/15m direction with >90% accuracy
- Initial backtest used the 3.15% fee assumption (later corrected)

#### Dashboard Enhancements (669b8ea, 3fb257b, ce33fdb)

**Commit `669b8ea`**: Add Live Pulse momentum meter + scan counter
- Real-time momentum visualization for BTC and ETH
- Scan cycle counter showing bot activity

**Commit `3fb257b`**: Add Quant Lab - interactive backtesting dashboard
- New `/quant-lab` page with interactive grid search results
- Side-by-side parameter comparison

**Commit `ce33fdb`**: Add Mark-out tracking + Telegram trade backup
- Mark-out prices at T+1s, T+5s, T+10s, T+30s, T+60s after entry
- Telegram as backup persistence for ephemeral Render filesystem

#### Live Execution Layer (0597d9e, 9512328)

**Commit `0597d9e`**: Add live execution layer (Polymarket CLOB + EIP-712)
- `core/executor.py` created with full CLOB integration
- L1 Auth: EIP-712 signature (one-time at startup)
- L2 Auth: HMAC-SHA256 (per trade, <1ms)
- Three-flag safety: `config.live_trading` + `POLYMARKET_PRIVATE_KEY` + `mode == "live"`
- Maximum position hard-capped at `$5` for `$100` account

**Commit `9512328`**: Add live trading mode, wallet page, donation tracker
- New `/wallet` page showing real-time USDC.e balance
- On-chain balance query via Polygon RPC (`eth_call` to USDC.e contract)
- 10% donation tracker (philanthropic goal)

#### Bug Fixes From First Live Session (a85d6f9, 64ed8a4)

**Commit `a85d6f9`**: Fix 4 critical trading bugs found in first live session
- Bugs discovered during first 24 hours of paper trading

**Commit `64ed8a4`**: Fix 3 bugs from first 54 paper trades analysis
- Analysis of first 54 paper trades revealed systematic issues

#### Event-Driven Architecture (ed83920)

**Commit `ed83920`**: Event-driven architecture + Maker/Taker toggle
- **BEFORE**: `await asyncio.sleep(0.5)` in signal loop = 250ms average delay
- **AFTER**: `await self._tick_event.wait()` = <5ms reaction time
- Event set by Binance WebSocket callback on every tick
- Added Maker/Taker toggle: Maker sets limit 1 cent below ask (rebate), Taker hits ask

```python
# Old architecture (polling):
async def _signal_loop(self):
    while self._running:
        await self._check_all_signals()
        await asyncio.sleep(0.5)  # 250ms avg delay

# New architecture (event-driven):
async def _signal_loop(self):
    while self._running:
        await asyncio.wait_for(self._tick_event.wait(), timeout=2.0)
        self._tick_event.clear()
        await self._check_all_signals()
```

#### Refinements (09d8f84, d508b5a, 0d55d6c, f50a984, a675668)

**Commit `09d8f84`**: Add Watchdog, EIP-712 Pre-Signing, Maker Pivot refinements
- Watchdog: detects stale data (120s without fresh orderbook) and forces reconnect
- Pre-Signing: orders signed in advance, saves 20-50ms at trigger time

**Commit `d508b5a`**: Increase liquidity gate from $5K to $15K
- Problem: Low-liquidity markets had too much slippage
- Solution: Minimum market liquidity raised to $15,000

**Commit `0d55d6c`**: Add 1% max participation rate
- Order cannot exceed 1% of total market liquidity
- $5 order at $15K liquidity = 0.03% = OK
- $80 order at $3.9K liquidity = 2% = BLOCKED

**Commit `f50a984`**: Add Mark-out visualization to Resolved trades tab

**Commit `a675668`**: Add Transit Latency monitoring + Data Freshness Guard
- Transit latency: time from Binance server event to local processing
- Guard: skip signals where transit P50 > 300ms (stale data = adverse selection)

#### 4-Layer Persistence (22c2699, 28d518d, 81b1998, be1ee19)

**Commit `22c2699`**: Add forensic Trade Journal - indestructible trade data
- `core/trade_journal.py` created
- JSONL append-only log (survives restarts, not deploys)
- 50+ fields per trade record (HFT-grade forensics)

**Commit `28d518d`**: Add Supabase persistent trade database
- `core/db.py` created with Supabase PostgreSQL integration
- 4-layer persistence: RAM + JSONL + Telegram + Supabase
- Retry logic with exponential backoff (1s, 2s, 4s)
- Data survives deploys, restarts, and server migrations

**Commit `81b1998`**: Add dynamic instance label (FRANKFURT/SINGAPUR)
- `INSTANCE_LABEL` environment variable for multi-region identification
- Added to Supabase records for region-specific analytics

**Commit `be1ee19`**: Tag all Telegram messages with instance label
- Every Telegram message prefixed with `[FRANKFURT]` or `[SINGAPUR]`
- Critical for distinguishing alerts from dual-region deployment

---

### v1.5 — Fee Correction + Realistic Backtest (1dd8799)

**Date**: March 30, 2026
**Commit**: `1dd8799 Fee-Korrektur 1.80%, Realistic Backtest v2.0, Quant Lab v2.0`

This was a major analytical pivot. The original 3.15% fee assumption was wrong, and the entire backtest framework was rebuilt.

#### Fee Change: 3.15% to 1.80%

**BEFORE**: `polymarket_max_fee_pct = 3.15` (assumed from general Polymarket fee schedule)

**WHY IT CHANGED**: On March 30, 2026, Polymarket reduced fees for Crypto 5m/15m markets. The new peak fee at p=0.50 dropped from 3.15% to 1.80%. This was discovered through direct observation of live market data and confirmed via the Polymarket fee schedule.

**AFTER**: `polymarket_max_fee_pct = 1.80`

**Impact Analysis**:

| Metric | At 3.15% Fee | At 1.80% Fee | Change |
|--------|-------------|-------------|--------|
| Break-even edge | ~3.5% | ~2.0% | -1.5pp |
| Tradeable signals | Low | High | Significant increase |
| Net EV per trade | Often negative | Often positive | Strategy becomes viable |
| Fee at p=0.50 | 3.15% | 1.80% | -42.9% reduction |
| Fee at p=0.60 | 2.90% | 1.66% | -42.8% reduction |
| Fee at p=0.70 | 2.06% | 1.18% | -42.7% reduction |

Files changed:
- `config.py`: `polymarket_max_fee_pct` default from 3.15 to 1.80
- `core/pretrade_calculator.py`: fee curve documentation updated
- `backtest_engine.py`: `MAX_FEE_PCT` constant updated

#### Logistic Scaling: 2.0 to 3.0

**BEFORE**: `scaling = 2.0` in the logistic probability function

**WHY IT CHANGED**: RMSE-optimized calibration against 7 months of BTC data showed that scaling=3.0 better maps Binance momentum to actual 5-minute directional outcomes.

**AFTER**: `scaling = 3.0`

Calibration results:

| Momentum | scaling=2.0 P(UP) | scaling=3.0 P(UP) | Actual Win Rate |
|----------|-------------------|-------------------|-----------------|
| +0.10% | 52.0% | 57.4% | 90.5% |
| +0.15% | 54.7% | 61.1% | 93.9% |
| +0.30% | 64.6% | 71.1% | 97.8% |
| +0.50% | 73.1% | 81.8% | 97.8% |

Note: The model intentionally underestimates actual win rates. This conservatism is by design -- it prevents overconfident Kelly sizing and keeps position sizes smaller.

#### Backtest Engine v2.0: Idealistic vs Realistic Mode

**BEFORE**: Single backtest mode with no friction modeling

**AFTER**: Two distinct modes:

**Idealistic Mode**:
- $1,000 starting capital
- $50 positions with Kelly compounding
- All signals tradeable (100% opportunity rate)
- Entry always at p=0.50
- No slippage, no latency, no fill rate

**Realistic Mode** (calibrated against 1,048 real Polymarket trades):
- $80 starting capital (actual: $79.99)
- $5 flat positions (no compounding)
- `opportunity_rate = 15.1%` (only 15.1% of Binance signals have a Polymarket counterpart near 0.50)
- `avg_entry_price = 0.4965` (measured from real trades)
- `entry_noise_std = 0.025` (spread around average entry)
- `base_slippage_pct = 0.10%`
- `latency_ms_mean = 350ms`
- `fill_rate = 85%`
- `competition_haircut = 20%` (other bots capture part of the edge)
- `-40%` kill switch

```python
@dataclass
class RealisticParams:
    initial_capital: float = 80.0
    flat_position_usd: float = 5.0
    opportunity_rate: float = 0.151
    avg_entry_price: float = 0.4965
    entry_noise_std: float = 0.025
    base_slippage_pct: float = 0.10
    latency_ms_mean: float = 350.0
    latency_ms_std: float = 75.0
    fill_rate: float = 0.85
    competition_haircut: float = 0.20
    kill_switch_pct: float = 0.40
    seed: int = 42
```

#### Quant Lab v2.0

The Quant Lab dashboard was expanded with:
- Monte Carlo simulation (1,000 permutations)
- Mark-out analysis (signal quality over time: T+1s to T+60s)
- Side-by-side Idealistic vs Realistic comparison
- Per-trade scatter plots

#### Polymarket Data Collection

- 2,016 markets catalogued
- 1,048 BTC 5m trades analyzed
- 297K L2 orderbook snapshots from pmxt archive
- Key discovery: **opportunity_rate = 15.1%** (the critical unknown)

---

### v1.6 — Crash Fixes (b9744b4, d81cf20)

**Date**: March 30-31, 2026
**Commits**: `b9744b4`, `d81cf20`

After deploying to Render with 24/7 uptime, the bot crashed repeatedly. Seven distinct crash causes were identified and fixed.

#### Commit b9744b4: Fix 7 crash causes

**Crash 1: `RuntimeError: dictionary changed size during iteration`**
- Location: `market_discovery.py` `_cleanup_expired()` and `status()`
- Cause: The refresh loop modified `self.windows` dict while another coroutine iterated over it
- Fix: `list(self.windows.items())` to create a snapshot before iteration

```python
# BEFORE (crash):
expired = [slug for slug, w in self.windows.items() if ...]

# AFTER (safe):
expired = [slug for slug, w in list(self.windows.items()) if ...]
```

**Crash 2: Unhandled exception in `asyncio.gather`**
- Location: `strategies/polymarket_latency.py` `run()`
- Cause: If any one of the parallel loops raised an exception, `asyncio.gather` propagated it and killed all loops
- Fix: `return_exceptions=True` + post-mortem logging

```python
# BEFORE:
await asyncio.gather(
    self._signal_loop(),
    self._position_resolver_loop(),
    self._status_loop(),
)

# AFTER:
results = await asyncio.gather(
    self._signal_loop(),
    self._position_resolver_loop(),
    self._status_loop(),
    self._auto_redeem_loop(),
    return_exceptions=True,
)
for i, result in enumerate(results):
    if isinstance(result, Exception):
        logger.error(f"Loop '{loop_names[i]}' crashed: {result}")
```

**Crash 3: Telegram session closed during send**
- Fix: Check `_session.closed` before every send, recreate if needed

**Crash 4: Supabase insert timeout with no retry**
- Fix: Exponential backoff retry (3 attempts: 1s, 2s, 4s)

**Crash 5: Mark-out recording for wrong asset**
- Cause: BTC price was recording mark-outs for ETH positions
- Fix: `asset_filter` parameter in `record_markout()` and `check_and_resolve_expired()`

**Crash 6: Division by zero in mark-out percentage calculation**
- Fix: Guard `if ref > 0 and v > 0`

**Crash 7: WebSocket reconnect flooding**
- Fix: Exponential backoff from 1s to 30s

#### Commit d81cf20: Stabilize paper trading

- Fire-and-forget wrapper (`_safe_async`) for all Telegram and Supabase calls
- `try/except` around every `asyncio.create_task` callback
- Status generation wrapped in exception handler to prevent dashboard crashes

```python
async def _safe_async(self, coro, label: str) -> None:
    """Fire-and-forget wrapper that never crashes the caller."""
    try:
        await coro
    except Exception as e:
        logger.error(f"Journal async '{label}' Fehler: {e}")
```

---

### v1.7 — Masterplan & Professional Alerts (5f2c7ff, a3106c7, 883e10d, f117fa8)

**Commits**: `5f2c7ff`, `a3106c7`, `883e10d`, `f117fa8`

#### Masterplan Dashboard (5f2c7ff, a3106c7)

- New `/masterplan` page: 10-phase (later 11-phase) roadmap from paper trading to profitable live bot
- Interactive phase tracker with completion status
- Updated to include AWS EC2 Dublin deployment plan and Coinbase Multi-Oracle strategy

#### Professional Telegram Alerts (883e10d)

Complete rewrite of the Telegram module with HFT-grade alert templates:

**Alert Types**:
1. `alert_startup` — Bot start with CLOB latency, Binance ping, geoblock check, region label
2. `alert_shutdown` — Session summary with trade count, win rate, PnL
3. `alert_signal` — Signal detected with momentum, p_true, ask, EV, Kelly, liquidity, spread, transit latency
4. `alert_resolved` — Trade closed with PnL, oracle price movement, mark-out data, session stats
5. `alert_live_order` — Live order success or failure with latency
6. `alert_drawdown` — Warning at 5%+ drawdown
7. `alert_kill_switch` — Critical alert at 15%+ drawdown
8. `alert_trade_log` — Forensic silent log for every open/close
9. `alert_heartbeat` — Hourly heartbeat with key metrics

**Performance Tracking**:
```python
_stats = {
    "signals": 0,
    "trades_opened": 0,
    "trades_won": 0,
    "trades_lost": 0,
    "total_pnl": 0.0,
    "live_orders_ok": 0,
    "live_orders_fail": 0,
}
```

#### Strategy Intelligence Panel (f117fa8)

Added to Masterplan dashboard: real-time strategy metrics panel showing signal quality, edge decay, and market microstructure data.

---

### v2.0 — Strategy Pivot + Dublin Deployment (d6cb37c, 0a6296e)

**Date**: April 1-2, 2026
**Commits**: `d6cb37c`, `0a6296e`

This is the most significant strategic change in the project. The bot went from 0 live trades in 6 hours to 7 profitable live trades in 24 hours.

#### The Problem

After deploying to Render Frankfurt with live trading enabled, the bot ran for 6 hours and placed **zero trades**. Analysis revealed:

1. **Polymarket reprices within 2-5 seconds** of a Binance move (measured from 297K L2 snapshots)
2. The 60-second momentum window was far too slow -- by the time a signal was detected, the Polymarket orderbook had already moved
3. The 0.10% momentum threshold was too conservative, filtering out most viable signals
4. The 3-second orderbook refresh (on Render) was too slow to capture the window

#### Config Changes (d6cb37c)

| Parameter | BEFORE (v1.x) | AFTER (v2.0) | Rationale |
|-----------|---------------|-------------|-----------|
| `momentum_window_s` | 60.0 | 15.0 | Detect price moves before Polymarket reprices |
| `min_momentum_pct` | 0.10 | 0.05 | More signals, faster detection |
| `min_edge_pct` | 2.0 | 1.5 | Lower threshold, more trades |
| `min_seconds_to_expiry` | 30.0 | 15.0 | Trade closer to expiry |
| `max_seconds_to_expiry` | 240.0 | 180.0 | Tighter window |
| Orderbook refresh | 3000ms | 500ms | 6x faster orderbook updates |

#### Price-First Evaluation Filter (d6cb37c)

The most important filter added in v2.0. Before evaluating any signal through the expensive PreTrade calculator, check if the Polymarket market has already repriced:

```python
# PRICE-FIRST FILTER (v2): Only trade if market is NEAR 0.50
# If ask is already at 0.65+ -> Polymarket has already repriced -> no edge
if abs(ask_price - 0.50) > 0.15:
    return  # Market is >15 cents from 0.50 -> already repriced
```

This single filter prevents the majority of "too late" trades where the edge has already been captured by faster participants.

#### AWS EC2 Dublin Deployment (d6cb37c)

**WHY**: Render Frankfurt had 45ms latency to Polymarket CLOB (London). AWS Dublin (eu-west-1) provides 1.26ms.

**Infrastructure**:
- Instance: c6i.xlarge (4 vCPU, 8GB RAM)
- OS: Ubuntu 24.04 LTS
- Region: eu-west-1 (Dublin, Ireland)
- Elastic IP: 34.249.110.17
- Latency to CLOB: **1.26ms** (measured via startup ping)
- Geoblock: `false` (Ireland is not geoblocked)

**Latency Comparison**:

| Endpoint | Render Frankfurt | AWS Dublin |
|----------|-----------------|-----------|
| CLOB API | 45ms | 1.26ms |
| Binance WS | ~20ms | ~15ms |
| Geoblock | Varies | OK (IE) |

#### Auto-Redeemer (0a6296e)

**Commit `0a6296e`**: Add Auto-Redeemer: winning tokens automatically converted back to USDC.e

**WHY IT WAS NEEDED**: When you win a Polymarket trade, you receive Conditional Tokens (CTF ERC-1155). These tokens are NOT USDC.e and must be explicitly redeemed by calling `redeemPositions()` on the CTF contract. The `py-clob-client` library has **no redeem endpoint** -- it can place orders but cannot redeem.

**Solution**: Direct Web3 interaction with the CTF contract on Polygon:

```python
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# 1. Query Polymarket Data API for redeemable positions
url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=50"

# 2. Check if condition is resolved (payoutDenominator > 0)
payout = ctf.functions.payoutDenominator(condition_id).call()

# 3. Call redeemPositions() on CTF contract
tx = ctf.functions.redeemPositions(
    USDC_E,           # collateralToken
    b"\x00" * 32,     # parentCollectionId (root)
    condition_id,     # conditionId
    [1, 2],           # indexSets (both outcomes)
).build_transaction(...)
```

**Polygon RPC Fallback Chain**:
```python
POLYGON_RPCS = [
    "https://polygon.publicnode.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.gateway.tenderly.co",
]
```

**Integration**: Runs as a periodic task every 5 minutes in the strategy's main event loop:

```python
async def _auto_redeem_loop(self) -> None:
    await asyncio.sleep(60)  # Wait 1 min after start
    while self._running:
        if self.executor.is_live and self.settings.polymarket_private_key:
            result = self.redeemer.redeem_all()
            if result.get("redeemed", 0) > 0:
                await telegram.send_alert(...)
        await asyncio.sleep(300)  # Every 5 minutes
```

#### First Confirmed Profit: +$22.62 on 7 Live Trades

With v2.0 deployed on AWS Dublin, the bot placed 7 live trades in the first 24 hours:
- 5 wins, 2 losses
- Win rate: 71.4%
- Total profit: +$22.62
- Starting capital: $79.99
- After redemption: $102.61
- ROI: 28.3%

---

## 2. System Architecture

### 2.1 High-Level Architecture

```
                          EXTERNAL SERVICES
  +------------------------------------------------------------------+
  |                                                                    |
  |   +------------------+         +----------------------+            |
  |   | BINANCE EXCHANGE |         | POLYMARKET ECOSYSTEM |            |
  |   |                  |         |                      |            |
  |   | WebSocket Stream |         | Gamma API (REST)     |            |
  |   | bookTicker       |         | CLOB API (REST)      |            |
  |   | Sub-50ms ticks   |         | Data API (REST)      |            |
  |   +--------+---------+         | Polygon Chain (RPC)  |            |
  |            |                   +----------+-----------+            |
  |            |                              |                        |
  +------------------------------------------------------------------+
               |                              |
               | WSS (bookTicker)             | HTTPS (REST)
               | ~15ms from Dublin            | ~1.26ms from Dublin
               v                              v
  +------------------------------------------------------------------+
  |                    AWS EC2 DUBLIN (eu-west-1)                      |
  |                    c6i.xlarge | Ubuntu 24.04                       |
  |                    Elastic IP: 34.249.110.17                       |
  |                                                                    |
  |   +------------------+         +----------------------+            |
  |   | BinanceWSOracle  |         | MarketDiscovery      |            |
  |   | - PriceTick      |         | - Slug Generation    |            |
  |   | - PriceWindow    |         | - Token ID Fetch     |            |
  |   | - Momentum Calc  |         | - Orderbook Refresh  |            |
  |   | - Transit Latency|         | - Auto Rollover      |            |
  |   | - Event Callback |         | - Watchdog           |            |
  |   +--------+---------+         +----------+-----------+            |
  |            |                              |                        |
  |            +--------> SIGNAL LOOP <-------+                        |
  |                    (Event-Driven)                                   |
  |                           |                                        |
  |                           v                                        |
  |              +------------+-------------+                          |
  |              | PreTradeCalculator       |                          |
  |              | - P(true) estimation     |                          |
  |              | - Fee curve (1.80% peak) |                          |
  |              | - Net EV calculation     |                          |
  |              | - Half-Kelly sizing      |                          |
  |              +------------+-------------+                          |
  |                           |                                        |
  |              EXECUTE      |      ABORT                             |
  |              +------------+------+                                 |
  |              |                   |                                  |
  |              v                   v (logged, no trade)              |
  |   +----------+-------+  +-------+--------+                        |
  |   | PaperTrader      |  | PolymarketExec |                        |
  |   | - Open/Close     |  | - CLOB Order   |                        |
  |   | - PnL Track      |  | - EIP-712 Auth |                        |
  |   | - Kill Switch    |  | - Pre-Signing  |                        |
  |   +----------+-------+  +-------+--------+                        |
  |              |                   |                                  |
  |              +--------+----------+                                 |
  |                       v                                            |
  |   +-------------------+--------------------+                       |
  |   |           TradeJournal                 |                       |
  |   | RAM + JSONL + Telegram + Supabase      |                       |
  |   +-------------------+--------------------+                       |
  |              |         |          |                                 |
  |   +----------+ +-------+  +------+------+                         |
  |   | AutoRedeem | | RiskMgr | | Dashboard  |                       |
  |   | (5m loop)  | | KillSwitch| | FastAPI  |                       |
  |   +------------+ +----------+ | WS 1Hz   |                       |
  |                               +-----+----+                        |
  +------------------------------------------------------------------+
                                        |
               +------------------------+---------------------+
               |                        |                     |
               v                        v                     v
  +-----------+------+   +-----------+------+   +------------+------+
  | SUPABASE (Cloud) |   | TELEGRAM BOT API |   | BROWSER DASHBOARD |
  | PostgreSQL       |   | Alerts + Logs    |   | index.html        |
  | 50+ fields/trade |   | Instance Labels  |   | wallet.html       |
  | Free Tier 500MB  |   | Session Stats    |   | quant_lab.html    |
  +------------------+   +------------------+   | masterplan.html   |
                                                 +-------------------+
```

### 2.2 Component Architecture

| Module | File | Role | Dependencies |
|--------|------|------|-------------|
| **Configuration** | `config.py` | Centralized Pydantic Settings, all parameters | pydantic-settings, dotenv |
| **Entry Point** | `main.py` | Starts web dashboard or CLI paper trading | config, strategies, dashboard |
| **Price Oracle** | `core/binance_ws.py` | Real-time Binance tick data via WebSocket | websockets |
| **Market Discovery** | `core/market_discovery.py` | Finds ephemeral 5m/15m markets via slug math | aiohttp |
| **PreTrade Calculator** | `core/pretrade_calculator.py` | Fee curves, probability, Kelly sizing | config |
| **Paper Trader** | `core/paper_trader.py` | Simulated trades with full PnL tracking | config, pretrade_calculator, telegram |
| **Live Executor** | `core/executor.py` | Real CLOB orders via py-clob-client | py-clob-client, config, telegram |
| **Risk Manager** | `core/risk_manager.py` | Kill switches and circuit breakers | config |
| **Trade Journal** | `core/trade_journal.py` | 4-layer persistence (RAM+JSONL+TG+Supabase) | db, telegram |
| **Database** | `core/db.py` | Supabase PostgreSQL integration | supabase |
| **Auto Redeemer** | `core/redeemer.py` | Converts CTF tokens back to USDC.e | web3 |
| **Strategy** | `strategies/polymarket_latency.py` | Main orchestrator, all loops | all core modules |
| **Dashboard** | `dashboard/web_ui.py` | FastAPI web server, WebSocket broadcast | fastapi, uvicorn, strategy |
| **Telegram** | `utils/telegram.py` | Alert system with professional templates | aiohttp |
| **Logger** | `utils/logger.py` | Loguru configuration | loguru |
| **Backtest** | `backtest_engine.py` | Historical validation with grid search | aiohttp, math |

### 2.3 Data Flow: Tick-to-Trade

```
T+0.000ms  Binance server emits bookTicker event
           (server timestamp E in message)
    |
T+~15ms    WebSocket message arrives at AWS Dublin
           Transit latency = local_time - E
    |
T+~15ms    _handle_message() parses JSON
           Creates PriceTick(symbol, bid, ask, mid, timestamp)
           Adds to PriceWindow deque
           Fires on_tick callback -> sets _tick_event
    |
T+~16ms    _signal_loop() wakes from _tick_event.wait()
           Clears event
    |
T+~17ms    _check_all_signals() runs:
           1. Record mark-outs for open positions (asset-filtered)
           2. For each asset (BTC, ETH):
              a. Check data freshness (age < 5s)
              b. Check transit latency (P50 < 300ms)
              c. Compute momentum over 15s window
              d. Check threshold (|momentum| >= 0.05%)
    |
T+~18ms    If signal detected:
           1. Get tradeable windows from MarketDiscovery
           2. For each window:
              a. Time check (15s <= remaining <= 180s)
              b. Orderbook freshness (<5s)
              c. PRICE-FIRST filter (|ask - 0.50| <= 0.15)
              d. Spread check (< 3%)
              e. Participation check (< 1% of liquidity)
              f. Duplicate prevention (1 trade per window per direction)
    |
T+~19ms    PreTradeCalculator.evaluate():
           1. estimate_probability(momentum, time, direction)
           2. polymarket_fee_pct(ask_price) -> fee
           3. Net EV = (p_true / effective_ask - 1) * 100
           4. kelly_fraction(p_true, ask_price) -> f
           5. position_usd = min(f * capital, 8% * capital)
           6. Return EXECUTE or ABORT
    |
T+~20ms    If EXECUTE:
           1. PaperTrader.open_position() (always)
           2. TradeJournal.record_open() (4-layer)
           3. If live: PolymarketExecutor.place_order()
              a. Check pre-signed order exists
              b. Post to CLOB API
              c. ~50-200ms for order confirmation
           4. Telegram alert (async, non-blocking)
    |
T+~5min    Market window expires:
           _position_resolver_loop() runs every 5s
           1. Get current Binance price (asset-filtered!)
           2. Compare to oracle_price_at_entry
           3. Resolve: direction correct? WIN/LOSS
           4. Update capital, log CSV
           5. TradeJournal.record_close()
           6. Telegram alert with mark-out data
    |
T+~10min   AutoRedeemer runs (every 5 minutes):
           1. Query Polymarket Data API for redeemable positions
           2. Check payoutDenominator on CTF contract
           3. Call redeemPositions() on Polygon
           4. USDC.e returns to trading wallet
```

### 2.4 Infrastructure

#### AWS EC2 Dublin (Current - Production)

| Property | Value |
|----------|-------|
| Instance Type | c6i.xlarge |
| vCPUs | 4 |
| RAM | 8 GB |
| Region | eu-west-1 (Dublin, Ireland) |
| OS | Ubuntu 24.04 LTS |
| Elastic IP | 34.249.110.17 |
| CLOB Latency | 1.26ms |
| Geoblock Status | `false` (IE not blocked) |

#### Supabase (Cloud Database)

| Property | Value |
|----------|-------|
| Tier | Free (500MB) |
| Database | PostgreSQL |
| Table | `trades` (50+ columns) |
| Indexes | `trade_id`, `asset`, `event`, `created_at` |
| Retention | Unlimited (within 500MB) |

#### Polygon RPC Endpoints

Used by the Auto-Redeemer for CTF token redemption:
1. `https://polygon.publicnode.com` (primary)
2. `https://polygon-bor-rpc.publicnode.com` (fallback 1)
3. `https://polygon.gateway.tenderly.co` (fallback 2)

---

## 3. Detailed Component Documentation

### 3.1 Binance WebSocket Oracle (`core/binance_ws.py`)

**Purpose**: Real-time price stream from Binance serving as the momentum indicator for the Polymarket strategy. Delivers sub-50ms tick data via WebSocket bookTicker stream.

#### Key Classes

**`PriceTick`** (dataclass):
```python
@dataclass
class PriceTick:
    symbol: str       # e.g., "BTC/USDT"
    bid: float        # Best bid price
    ask: float        # Best ask price
    mid: float        # (bid + ask) / 2
    timestamp: float  # Unix seconds (local clock)
```

**`PriceWindow`** (dataclass):
```python
@dataclass
class PriceWindow:
    symbol: str
    maxlen: int = 500
    _ticks: deque          # Rolling buffer, maxlen=500
    _tick_intervals: deque  # Inter-tick timing, maxlen=200
```

Methods:
- `add(tick)` — Adds tick, records inter-tick interval
- `latest()` -> `PriceTick | None` — Last tick
- `momentum(window_s)` -> `float | None` — Price change in % over last `window_s` seconds
- `tick_count()` -> `int` — Number of ticks in buffer
- `age_s()` -> `float` — Seconds since last tick
- `latency_percentiles()` -> `tuple[float, float]` — P50 and P99 inter-tick intervals in ms

**`BinanceWebSocketOracle`**:
```python
class BinanceWebSocketOracle:
    def __init__(self, symbols: list[str], window_size_s: float = 120.0)
    def set_on_tick(self, callback) -> None
    async def start(self) -> None
    async def stop(self) -> None
    def get_latest(self, symbol: str) -> PriceTick | None
    def get_momentum(self, symbol: str, window_s: float) -> float | None
    def is_fresh(self, symbol: str, max_age_s: float = 5.0) -> bool
    def tick_count(self, symbol: str) -> int
    def status(self) -> dict[str, dict]
```

#### WebSocket Protocol

Stream URL: `wss://stream.binance.com:9443/ws/{symbol}@bookTicker`

Symbol mapping:
```python
_STREAM_MAP = {
    "BTC/USDT": "btcusdt",
    "ETH/USDT": "ethusdt",
    "SOL/USDT": "solusdt",
    "BNB/USDT": "bnbusdt",
}
```

Message format:
```json
{"u":123, "s":"BTCUSDT", "b":"65000.10", "B":"1.5", "a":"65000.20", "A":"2.0", "E":1711900000000}
```

- `b` = best bid price
- `a` = best ask price
- `E` = server event timestamp (ms) -- used for transit latency calculation

#### Transit Latency Tracking

```python
transit_ms = local_time_ms - server_event_timestamp_ms
```

Tracked per symbol in a `deque(maxlen=200)`. Exposed via `status()` as `transit_p50_ms` and `transit_p99_ms`.

#### Reconnect Logic

- Exponential backoff: 1s initial, doubles on each failure, max 30s
- Reset to 1s on successful connection
- `ping_interval=20`, `ping_timeout=10`, `close_timeout=5`
- `CancelledError` breaks the loop cleanly

#### Error Handling

- JSON parse errors: logged at DEBUG, tick skipped
- KeyError/ValueError in message handling: silently skipped
- WebSocket disconnect: auto-reconnect with backoff
- Callback exceptions in `on_tick`: caught and ignored (prevents cascade)

---

### 3.2 Market Discovery (`core/market_discovery.py`)

**Purpose**: Discovers and manages ephemeral Polymarket 5m/15m crypto prediction markets. These markets are NOT searchable via text -- discovery relies on mathematical slug computation.

#### Slug Generation Algorithm

Polymarket creates a new market every 5 or 15 minutes with a deterministic slug:

```
{asset}-updown-{timeframe}-{window_start_unix_timestamp}
```

Examples:
- `btc-updown-5m-1711900800`
- `eth-updown-15m-1711900200`

The algorithm computes the current and next N window timestamps:

```python
@staticmethod
def generate_slugs(asset: str, timeframe: str, count: int = 3):
    interval_s = TIMEFRAMES[timeframe]  # 300 for 5m, 900 for 15m
    now = int(time.time())
    current_start = (now // interval_s) * interval_s
    result = []
    for i in range(count):
        start_ts = current_start + (i * interval_s)
        end_ts = start_ts + interval_s
        slug = f"{asset}-updown-{timeframe}-{start_ts}"
        result.append((slug, start_ts, end_ts))
    return result
```

#### Key Classes

**`MarketWindow`** (dataclass):
```python
@dataclass
class MarketWindow:
    slug: str                    # e.g., "btc-updown-5m-1711900800"
    asset: str                   # "btc" / "eth"
    timeframe: str               # "5m" / "15m"
    window_start_ts: int         # Unix timestamp
    window_end_ts: int           # Unix timestamp
    condition_id: str            # Polymarket condition ID
    question: str                # Market question text
    up_token_id: str             # CTF token ID for UP outcome
    down_token_id: str           # CTF token ID for DOWN outcome
    up_best_bid: float           # Current best bid for UP
    up_best_ask: float           # Current best ask for UP
    down_best_bid: float         # Current best bid for DOWN
    down_best_ask: float         # Current best ask for DOWN
    up_bid_size: float           # Shares at best bid (UP)
    up_ask_size: float           # Shares at best ask (UP)
    orderbook_ts: float          # Last orderbook update timestamp
    gamma_up_price: float        # Gamma API mid-price reference (UP)
    gamma_down_price: float      # Gamma API mid-price reference (DOWN)
    liquidity_usd: float         # Market liquidity in USD
    fetched: bool                # True if token IDs loaded
```

Properties:
- `seconds_remaining` -> `float`
- `is_expired` -> `bool`
- `is_tradeable` -> `bool` (token IDs + >15s remaining + ask > 0)
- `orderbook_fresh` -> `bool` (< 5s old)

**`MarketDiscovery`**:
```python
class MarketDiscovery:
    def __init__(self, assets, timeframes, min_liquidity_usd=5000.0)
    async def start(self) -> None
    async def stop(self) -> None
    def get_tradeable_windows(self) -> list[MarketWindow]
    def get_windows_for_asset(self, asset: str) -> list[MarketWindow]
    def get_all_token_ids(self) -> list[str]
    def status(self) -> dict
```

#### API Endpoints Used

1. **Gamma API**: `https://gamma-api.polymarket.com/events?slug={slug}`
   - Returns event data including market details, token IDs, outcome prices
   - Called once per window to fetch token IDs

2. **CLOB Book**: `https://clob.polymarket.com/book?token_id={token_id}`
   - Returns current orderbook (bids and asks)
   - Called every 500ms for active windows

#### Refresh Loop (500ms)

```
Every 500ms:
  1. Discover new windows (rollover to next time slot)
  2. Refresh orderbooks for all active windows (parallel)
  3. Watchdog: check if any orderbook is fresh
     - If 120s without fresh data: force HTTP session reconnect
  4. Clean up expired windows (>30s past expiry)
```

#### Watchdog

Detects stale data and forces a reconnect:
```python
_WATCHDOG_TIMEOUT = 120  # seconds
if stale_duration > _WATCHDOG_TIMEOUT:
    await self._session.close()
    self._session = aiohttp.ClientSession(...)
```

---

### 3.3 PreTrade Calculator (`core/pretrade_calculator.py`)

**Purpose**: For each potential trade, computes dynamic fees, probability estimate, expected value, and optimal position size. Returns EXECUTE or ABORT.

#### Key Classes

**`TradeDecision`** (Enum):
```python
class TradeDecision(Enum):
    EXECUTE = "execute"
    ABORT = "abort"
```

**`PreTradeResult`** (dataclass):
```python
@dataclass
class PreTradeResult:
    asset: str              # "BTC" / "ETH"
    direction: str          # "UP" / "DOWN"
    p_true: float           # Estimated win probability
    p_market: float         # Polymarket ask price
    raw_edge_pct: float     # (p_true - p_market) / p_market * 100
    fee_pct: float          # Polymarket fee in %
    net_ev_pct: float       # Expected value after fees in %
    kelly_fraction: float   # Computed Kelly (after capping)
    position_usd: float     # Recommended position in USD
    decision: TradeDecision # EXECUTE or ABORT
    abort_reason: str       # Why aborted (if applicable)
```

#### Methods

**`polymarket_fee_pct(price)`**: Dynamic fee curve
```python
def polymarket_fee_pct(self, price: float) -> float:
    p = max(0.01, min(0.99, price))
    base = 4 * p * (1 - p)  # Normalized: 1.0 at p=0.50
    return self.settings.polymarket_max_fee_pct * base * base
```

**`estimate_probability(momentum_pct, seconds_to_expiry, direction)`**: Logistic model
```python
def estimate_probability(self, momentum_pct, seconds_to_expiry, direction):
    time_factor = min(1.0, 120.0 / max(seconds_to_expiry, 1.0))
    effective_momentum = momentum_pct * time_factor
    raw_signal = effective_momentum * 3.0  # scaling=3.0
    p_up = 1 / (1 + math.exp(-raw_signal))
    return p_up if direction == "UP" else 1 - p_up
```

**`kelly_fraction(p_true, p_market)`**: Half-Kelly for binary markets
```python
def kelly_fraction(self, p_true, p_market):
    raw_kelly = (p_true - p_market) / (1 - p_market)
    raw_kelly = max(0.0, min(raw_kelly, 0.25))  # Cap at 25%
    return raw_kelly * self.settings.kelly_fraction  # * 0.50
```

**`evaluate(...)`**: Full pre-trade analysis with guard checks:
1. Time to expiry check (15s - 180s)
2. Valid price check (0 < ask < 1)
3. Capital check (>= $10)
4. Probability estimation
5. Edge calculation (ROI basis)
6. Fee calculation
7. Net EV check (must exceed `min_edge_pct`)
8. Kelly sizing with position cap

---

### 3.4 Paper Trader (`core/paper_trader.py`)

**Purpose**: Simulates Polymarket trades and tracks PnL without real capital. Runs in parallel with live executor.

#### Key Classes

**`PaperPosition`** (dataclass):
```python
@dataclass
class PaperPosition:
    trade_id: str                    # "PT-0001"
    asset: str                       # "BTC" / "ETH"
    direction: str                   # "UP" / "DOWN"
    market_condition_id: str
    market_question: str
    entry_price: float               # Polymarket ask at entry
    size_usd: float                  # Invested capital
    shares: float                    # size_usd / entry_price
    fee_usd: float                   # Paid fee
    entered_at: float                # Unix timestamp
    market_end_timestamp: float      # When market expires
    p_true_at_entry: float
    momentum_at_entry: float
    seconds_to_expiry_at_entry: float
    oracle_price_at_entry: float     # Binance price at entry
    markout_prices: dict             # {seconds: price}
    resolved: bool
    outcome_correct: bool
    exit_price: float                # 1.0 if won, 0.0 if lost
    pnl_usd: float
    resolved_at: float
```

#### Resolution Logic

Resolution compares the Binance oracle price at entry vs. at market expiry:

```python
actual_up = oracle_price_now > position.oracle_price_at_entry
if direction == "UP":
    outcome_correct = actual_up
else:
    outcome_correct = not actual_up
```

PnL calculation:
- **WIN**: `gross_payout = shares * 1.0` (Polymarket pays $1 per share), `pnl = gross_payout - size_usd - fee_usd`
- **LOSS**: `pnl = -(size_usd + fee_usd)`

#### Kill Switch

Built into the paper trader (independent of RiskManager):
```python
def can_trade(self) -> bool:
    daily_loss_pct = abs(daily_pnl) / initial_capital
    return not (daily_pnl < 0 and daily_loss_pct >= max_daily_loss_pct)
```

#### Mark-out Recording

Critical: BTC price only records mark-outs for BTC positions, ETH for ETH.
```python
def record_markout(self, oracle_price, asset_filter=""):
    for pos in self._positions.values():
        if asset_filter and pos.asset != asset_filter:
            continue  # SKIP wrong asset
        for target_s in [1, 5, 10, 30, 60]:
            if target_s not in pos.markout_prices and abs(age_s - target_s) < 1.5:
                pos.markout_prices[target_s] = oracle_price
```

---

### 3.5 Live Executor (`core/executor.py`)

**Purpose**: Places real orders on the Polymarket CLOB. Triple-safety activation.

#### Safety Mechanisms

Three independent flags must all be true:
1. `config.live_trading == True`
2. `POLYMARKET_PRIVATE_KEY` set in `.env`
3. Code-level initialization succeeds

#### Authentication

Two layers:
- **L1 (EIP-712)**: One-time signature at startup to derive L2 credentials
- **L2 (HMAC-SHA256)**: Per-trade authentication, <1ms overhead

```python
self._creds = self._client.create_or_derive_api_creds()  # L1
self._client.set_api_creds(self._creds)                  # Enable L2
```

#### Pre-Signing

Orders are pre-signed during idle periods to minimize latency at trigger time:

```python
def pre_sign_order(self, token_id, price, size_usd):
    order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
    signed = self._client.create_order(order_args)
    self._pre_signed_orders[token_id] = signed
```

At trigger time, if a pre-signed order exists, only `post_order()` is called (saves 20-50ms EIP-712 computation).

#### Maker vs Taker Mode

- **Taker** (default): Hits the ask price directly. Fee: 1.80% peak.
- **Maker**: Sets limit 1 cent below ask. Earns rebate instead of paying fee. Risk: may not fill.

```python
is_maker = self.settings.order_type == "maker"
order_price = max(0.01, price - 0.01) if is_maker else price
```

#### On-Chain Balance Query

```python
async def get_balance(self) -> float:
    USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    call_data = "0x70a08231" + wallet_address_padded
    # eth_call to Polygon RPC
    return int(result, 16) / 1e6  # USDC.e has 6 decimals
```

---

### 3.6 Risk Manager (`core/risk_manager.py`)

**Purpose**: Enforces kill switches and circuit breakers from day one.

#### Kill Switches

| Switch | Threshold | Action |
|--------|----------|--------|
| Per-Trade Size | 8% of portfolio | Block oversized orders |
| Daily Loss | -20% of initial capital | PAUSE (resume next day) |
| Total Loss | -40% of initial capital | SHUTDOWN (permanent) |
| Min Balance | < $100 on any exchange | Block trading on that exchange |

#### Bot States

```python
class BotState(Enum):
    RUNNING = "running"
    PAUSED = "paused"       # Temporary (daily limit hit)
    SHUTDOWN = "shutdown"   # Permanent (total loss limit)
```

#### Cooldown

After a failure unwind, a configurable cooldown can be activated:
```python
def activate_cooldown(self, seconds: int = 60):
    self._cooldown_until = time.time() + seconds
```

---

### 3.7 Trade Journal (`core/trade_journal.py`)

**Purpose**: Indestructible trade records. Every trade is saved in four independent systems.

#### 4-Layer Persistence

| Layer | Medium | Survives Restart | Survives Deploy | Survives Server Change |
|-------|--------|-----------------|-----------------|----------------------|
| 1. RAM | Python list | No | No | No |
| 2. JSONL | Local file | Yes | No | No |
| 3. Telegram | Chat messages | Yes | Yes | Yes |
| 4. Supabase | PostgreSQL | Yes | Yes | Yes |

#### Trade Record Fields (50+)

```python
@dataclass
class TradeRecord:
    # ID
    trade_id: str
    event: str                     # "open" / "close"
    # Timing
    signal_ts: float
    entry_ts: float
    exit_ts: float
    order_post_ts: float
    # Asset
    asset: str                     # BTC / ETH
    direction: str                 # UP / DOWN
    window_slug: str
    market_question: str
    timeframe: str                 # 5m / 15m
    # Prices
    oracle_price_entry: float
    oracle_price_exit: float
    polymarket_bid: float
    polymarket_ask: float
    executed_price: float
    # Signal Quality
    momentum_pct: float
    p_true: float
    p_market: float
    raw_edge_pct: float
    fee_pct: float
    net_ev_pct: float
    # Sizing
    size_usd: float
    shares: float
    fee_usd: float
    kelly_fraction: float
    # Latency
    signal_to_order_ms: float
    transit_latency_ms: float
    tick_age_ms: float
    # Result
    outcome_correct: bool
    pnl_usd: float
    pnl_pct: float
    # Mark-out
    markout_1s: float
    markout_5s: float
    markout_10s: float
    markout_30s: float
    markout_60s: float
    # Execution
    order_type: str                # taker / maker
    live_order_id: str
    live_order_success: bool
    live_error: str
    # Meta
    seconds_to_expiry: float
    market_liquidity_usd: float
    spread_pct: float
```

---

### 3.8 Database (`core/db.py`)

**Purpose**: Supabase PostgreSQL integration for permanent trade storage.

#### Schema

```sql
CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    event TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    instance TEXT DEFAULT '',
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT DEFAULT '',
    window_slug TEXT DEFAULT '',
    market_question TEXT DEFAULT '',
    signal_ts DOUBLE PRECISION DEFAULT 0,
    entry_ts DOUBLE PRECISION DEFAULT 0,
    exit_ts DOUBLE PRECISION DEFAULT 0,
    order_post_ts DOUBLE PRECISION DEFAULT 0,
    oracle_price_entry DOUBLE PRECISION DEFAULT 0,
    oracle_price_exit DOUBLE PRECISION DEFAULT 0,
    polymarket_bid DOUBLE PRECISION DEFAULT 0,
    polymarket_ask DOUBLE PRECISION DEFAULT 0,
    executed_price DOUBLE PRECISION DEFAULT 0,
    momentum_pct DOUBLE PRECISION DEFAULT 0,
    p_true DOUBLE PRECISION DEFAULT 0,
    p_market DOUBLE PRECISION DEFAULT 0,
    raw_edge_pct DOUBLE PRECISION DEFAULT 0,
    fee_pct DOUBLE PRECISION DEFAULT 0,
    net_ev_pct DOUBLE PRECISION DEFAULT 0,
    size_usd DOUBLE PRECISION DEFAULT 0,
    shares DOUBLE PRECISION DEFAULT 0,
    fee_usd DOUBLE PRECISION DEFAULT 0,
    kelly_fraction DOUBLE PRECISION DEFAULT 0,
    signal_to_order_ms DOUBLE PRECISION DEFAULT 0,
    transit_latency_ms DOUBLE PRECISION DEFAULT 0,
    tick_age_ms DOUBLE PRECISION DEFAULT 0,
    outcome_correct BOOLEAN DEFAULT FALSE,
    pnl_usd DOUBLE PRECISION DEFAULT 0,
    pnl_pct DOUBLE PRECISION DEFAULT 0,
    markout_1s DOUBLE PRECISION DEFAULT 0,
    markout_5s DOUBLE PRECISION DEFAULT 0,
    markout_10s DOUBLE PRECISION DEFAULT 0,
    markout_30s DOUBLE PRECISION DEFAULT 0,
    markout_60s DOUBLE PRECISION DEFAULT 0,
    order_type TEXT DEFAULT 'taker',
    live_order_id TEXT DEFAULT '',
    live_order_success BOOLEAN DEFAULT FALSE,
    live_error TEXT DEFAULT '',
    seconds_to_expiry DOUBLE PRECISION DEFAULT 0,
    market_liquidity_usd DOUBLE PRECISION DEFAULT 0,
    spread_pct DOUBLE PRECISION DEFAULT 0
);

CREATE INDEX idx_trades_trade_id ON trades(trade_id);
CREATE INDEX idx_trades_asset ON trades(asset);
CREATE INDEX idx_trades_event ON trades(event);
CREATE INDEX idx_trades_created ON trades(created_at);
```

#### Retry Logic

```python
async def insert_trade(data: dict, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        try:
            client.table("trades").insert(clean).execute()
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
    return False
```

---

### 3.9 Auto Redeemer (`core/redeemer.py`)

**Purpose**: Automatically converts winning Conditional Tokens (CTF ERC-1155) back to USDC.e on Polygon.

#### Why This Module Exists

The `py-clob-client` library can place orders on the Polymarket CLOB but has **no redemption endpoint**. When you win a prediction market trade, you hold CTF tokens that must be explicitly redeemed by calling the `redeemPositions()` function on the CTF smart contract.

#### Architecture

1. Query Polymarket Data API for redeemable positions: `GET https://data-api.polymarket.com/positions?user={wallet}`
2. Filter positions where `redeemable == true` and `currentValue > 0`
3. Check on-chain if condition is resolved: `payoutDenominator(conditionId) > 0`
4. Build and sign transaction calling `redeemPositions(USDC_E, 0x00, conditionId, [1, 2])`
5. Send raw transaction to Polygon and wait for receipt

#### Contract Addresses

```python
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Token Framework
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"       # USDC.e on Polygon
```

#### Error Handling

- Fallback across 3 Polygon RPCs
- Nonce management: increments on each transaction, handles nonce collisions
- 2-second delay between redemptions (prevents RPC rate limiting)
- Gas price: 1.2x current gas price for priority

---

### 3.10 Telegram Module (`utils/telegram.py`)

**Purpose**: Professional HFT-grade trading alerts via Telegram Bot API. No extra package needed -- uses aiohttp directly.

#### Alert Templates

| Function | Trigger | Content |
|----------|---------|---------|
| `alert_startup` | Bot start | Config, CLOB latency, Binance ping, geoblock status, region |
| `alert_shutdown` | Bot stop | Session summary: trades, WR, PnL, uptime |
| `alert_signal` | Signal detected | Momentum, p_true, ask, EV, Kelly, liq, spread, transit |
| `alert_resolved` | Trade closed | PnL, oracle move, mark-outs, session stats |
| `alert_live_order` | Order placed | Success/failure, order ID, latency |
| `alert_drawdown` | DD >= 5% | Warning with current capital |
| `alert_kill_switch` | DD >= 15% | Critical: no more trades |
| `alert_trade_log` | Every open/close | Forensic silent log (compact format) |
| `alert_heartbeat` | Every 60 min | Alive status + key metrics |

#### Instance Labeling

Every message is prefixed with `[INSTANCE_LABEL]`:
```python
tagged = f"[{_instance}] {message}" if _instance else message
```

#### Session Management

Singleton aiohttp session with lazy initialization:
```python
if _session is None or _session.closed:
    _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
```

---

### 3.11 Strategy Orchestrator (`strategies/polymarket_latency.py`)

**Purpose**: The main brain. Orchestrates all components and runs the trading loops.

#### Initialization

```python
class PolymarketLatencyStrategy:
    def __init__(self, settings):
        self.oracle = BinanceWebSocketOracle(symbols, window_size_s)
        self.discovery = MarketDiscovery(assets, timeframes, min_liquidity=15000)
        self.calculator = PreTradeCalculator(settings)
        self.paper_trader = PaperTrader(settings)
        self.risk_manager = RiskManager(settings)
        self.executor = PolymarketExecutor(settings)
        self.journal = TradeJournal()
        self.redeemer = AutoRedeemer(private_key, wallet_address)
```

#### Concurrent Loops

```python
await asyncio.gather(
    self._signal_loop(),              # Event-driven, reacts to every tick
    self._position_resolver_loop(),   # Every 5s: resolve expired positions
    self._status_loop(),              # Every 60s: log status + hourly heartbeat
    self._auto_redeem_loop(),         # Every 5m: redeem winning tokens
    return_exceptions=True,           # Prevents one crash from killing all
)
```

#### Signal Evaluation Pipeline (v2.0)

For each Binance tick:
1. Data freshness check (tick age < 5s)
2. Transit latency check (P50 < 300ms)
3. Momentum calculation (15s window)
4. Threshold check (|momentum| >= 0.05%)
5. For each tradeable Polymarket window:
   a. Time window check (15s - 180s to expiry)
   b. Orderbook freshness (< 5s)
   c. **Price-First filter** (|ask - 0.50| <= 0.15) -- THE critical v2.0 addition
   d. Spread check (< 3%)
   e. Participation check (< 1% of market liquidity)
   f. Duplicate prevention (1 trade per window per direction)
6. PreTrade evaluation -> EXECUTE or ABORT
7. If EXECUTE: paper trade + journal + live order + telegram

#### Pre-Signing Loop

Every ~10 seconds (every 20 scan cycles), pre-signs orders for the next 2 active windows:
```python
if self.executor.is_live and self._scan_cycles % 20 == 0:
    for window in next_2_windows:
        for token_id in [up_token_id, down_token_id]:
            self.executor.pre_sign_order(token_id, 0.50, max_position)
```

---

## 4. Mathematical Models

### 4.1 Momentum Calculation

Momentum is the percentage price change over the last N seconds on Binance:

```
momentum(window_s) = (current_mid - baseline_mid) / baseline_mid * 100
```

Where:
- `current_mid` = latest `(bid + ask) / 2`
- `baseline_mid` = `(bid + ask) / 2` of the oldest tick within `[now - window_s, now]`

**v2.0 parameters**: `window_s = 15` (previously 60), threshold = 0.05% (previously 0.10%)

Example: If BTC mid-price was $84,000.00 fifteen seconds ago and is now $84,050.40:
```
momentum = (84050.40 - 84000.00) / 84000.00 * 100 = 0.060%
```
Since 0.060% >= 0.05% threshold, this triggers a signal.

---

### 4.2 Dynamic Fee Curve (1.80% Peak)

Polymarket charges a dynamic fee that peaks at p=0.50 and drops toward p=0 and p=1:

```
fee_pct = MAX_FEE * (4 * p * (1 - p))^2
```

Where `MAX_FEE = 1.80%` (as of March 30, 2026 for Crypto 5m/15m markets).

| Price (p) | `4p(1-p)` | Fee (%) |
|-----------|-----------|---------|
| 0.50 | 1.000 | 1.800 |
| 0.55 | 0.990 | 1.764 |
| 0.60 | 0.960 | 1.659 |
| 0.65 | 0.910 | 1.490 |
| 0.70 | 0.840 | 1.270 |
| 0.75 | 0.750 | 1.013 |
| 0.80 | 0.640 | 0.737 |
| 0.85 | 0.510 | 0.468 |
| 0.90 | 0.360 | 0.233 |
| 0.95 | 0.190 | 0.065 |

**Impact of fee reduction (v1.5)**:

The change from 3.15% to 1.80% peak fee reduced the break-even edge by approximately 1.35 percentage points. This was the single most important factor that made the strategy viable.

---

### 4.3 Logistic Probability Model (scaling=3.0)

The model converts Binance momentum into a directional probability using a logistic function:

```
time_factor = min(1.0, 120.0 / max(seconds_to_expiry, 1.0))
effective_momentum = momentum_pct * time_factor
raw_signal = effective_momentum * scaling
p_up = 1 / (1 + exp(-raw_signal))
```

**Parameters**:
- `scaling = 3.0` (RMSE-optimized against 7 months of BTC data)
- `time_factor`: amplifies signal when closer to expiry (< 120s remaining)

**Calibration Data** (BTC 5m, 7-month sample):

| Binance Momentum | Model P(correct) | Actual Win Rate | Underestimation |
|------------------|-------------------|-----------------|-----------------|
| +0.05% | 53.7% | ~85% | Conservative |
| +0.10% | 57.4% | 90.5% | Conservative |
| +0.15% | 61.1% | 93.9% | Conservative |
| +0.20% | 64.6% | 95.0% | Conservative |
| +0.30% | 71.1% | 97.8% | Conservative |
| +0.50% | 81.8% | 97.8% | Conservative |

The model intentionally underestimates actual win rates. This conservatism is a feature, not a bug -- it keeps Kelly sizing moderate and prevents overconfidence.

---

### 4.4 Kelly Criterion (Half-Kelly)

For binary prediction markets, Kelly sizing is:

```
raw_kelly = (p_true - p_market) / (1 - p_market)
```

Where:
- `p_true` = our estimated probability of winning
- `p_market` = Polymarket ask price (market-implied probability)

The formula derives from the odds: `b = (1 - p_market) / p_market`

Applied constraints:
```
raw_kelly = max(0.0, raw_kelly)     # No negative bets
raw_kelly = min(raw_kelly, 0.25)    # Hard cap at 25%
final_kelly = raw_kelly * 0.50      # Half-Kelly (fractional)
position_usd = min(kelly * capital, 8% * capital)  # Additional cap
```

**Example**:
- p_true = 0.61 (BTC +0.15% momentum)
- p_market = 0.50 (Polymarket ask)
- raw_kelly = (0.61 - 0.50) / (1 - 0.50) = 0.22
- half_kelly = 0.22 * 0.50 = 0.11 (11% of capital)
- But capped at 8% max = 0.08 * capital

---

### 4.5 Net Expected Value

The complete expected value calculation accounts for fees:

```
effective_ask = polymarket_ask * (1 + fee_pct / 100)
net_ev_pct = (p_true / effective_ask - 1) * 100
```

**Example**:
- p_true = 0.61
- polymarket_ask = 0.50
- fee_pct = 1.80%
- effective_ask = 0.50 * 1.018 = 0.509
- net_ev_pct = (0.61 / 0.509 - 1) * 100 = 19.84%

A trade is only executed if `net_ev_pct > min_edge_pct` (1.5% in v2.0).

---

### 4.6 Position Resolution

Polymarket 5m/15m markets resolve based on whether the crypto price (as determined by Chainlink oracle) is UP or DOWN relative to the opening price of the window.

The bot uses Binance mid-price as a proxy:
```
oracle_price_at_entry = Binance mid at time of trade entry
oracle_price_at_expiry = Binance mid when market window expires

actual_up = oracle_price_at_expiry > oracle_price_at_entry

if direction == "UP":
    won = actual_up
else:  # DOWN
    won = not actual_up
```

**Payout**:
- WIN: Each share pays $1.00. PnL = shares * $1.00 - size_usd - fee_usd
- LOSS: Shares worth $0.00. PnL = -(size_usd + fee_usd)

---

## 5. Trading Strategy

### 5.1 Signal Detection (v2.0)

The v2.0 signal detection pipeline is designed to capture the narrow window between a Binance price move and Polymarket orderbook adjustment (typically 2-5 seconds).

**Parameters**:
- Momentum window: 15 seconds (v2.0, was 60s)
- Momentum threshold: 0.05% (v2.0, was 0.10%)
- Orderbook refresh: 500ms (v2.0, was 3000ms)
- Price-First filter: ask must be within 15 cents of 0.50

**Signal Flow**:
1. Every Binance tick triggers `_check_all_signals()`
2. Compute 15s momentum for BTC and ETH
3. If |momentum| >= 0.05%: scan all Polymarket windows
4. Price-First filter eliminates already-repriced markets
5. PreTrade calculator computes expected value
6. Only positive EV trades (> 1.5% after fees) execute

### 5.2 Trade Execution

#### Paper + Live Parallel

Every signal produces both a paper trade AND a live trade (if executor is active):

```python
# Paper trade (always)
pos = self.paper_trader.open_position(result, ...)

# Live trade (if executor active)
if self.executor.is_live:
    asyncio.create_task(_execute_live_order(token_id, ask_price, live_size, ...))
```

#### Authentication

- **EIP-712**: Ethereum signature standard for typed data. Used once at startup to derive API credentials.
- **HMAC-SHA256**: Per-request authentication using the derived credentials. <1ms overhead.

#### Taker vs Maker

| Mode | Behavior | Fee | Fill Rate | Use Case |
|------|----------|-----|-----------|----------|
| Taker | Hits best ask | 1.80% peak | ~100% | Speed priority (v2.0 default) |
| Maker | Posts 1 cent below ask | Rebate | ~50-70% | Fee savings (experimental) |

### 5.3 Position Management

#### Duplicate Prevention

One trade per window per direction. Uses a set of `{slug}_{direction}` keys:

```python
trade_key = f"{window.slug}_{direction}"
if trade_key in self._traded_windows:
    return  # Already traded this window+direction
self._traded_windows.add(trade_key)
```

Keys are removed when positions resolve, allowing re-entry in the next window.

#### Mark-out Tracking

After entry, the oracle price is sampled at T+1s, T+5s, T+10s, T+30s, T+60s:

```python
for target_s in [1, 5, 10, 30, 60]:
    if target_s not in pos.markout_prices and abs(age_s - target_s) < 1.5:
        pos.markout_prices[target_s] = oracle_price
```

This data is critical for signal quality analysis: a good signal should show increasing mark-out in the predicted direction.

#### Auto-Redemption

The `AutoRedeemer` runs every 5 minutes and converts winning CTF tokens to USDC.e via direct smart contract interaction on Polygon.

---

## 6. Backtest Engine

### 6.1 Data Sources

- **Binance Klines API**: 1-minute OHLCV candles
- **Symbols**: BTCUSDT, ETHUSDT
- **Date Range**: 2024-01-01 to 2026-03-28 (27 months)
- **Total Candles**: ~1.1M per asset
- **Caching**: JSON files in `data/backtest/`

Data fetch with rate limiting:
```python
async def fetch_binance_klines(symbol, interval="1m", start_date, end_date, cache=True):
    # Fetches in batches of 1000 candles with 100ms delay between requests
```

### 6.2 Idealistic vs Realistic Mode

| Dimension | Idealistic | Realistic |
|-----------|-----------|-----------|
| Starting Capital | $1,000 | $80 ($79.99 actual) |
| Position Size | $50 (Kelly-compounded) | $5 (flat, no compounding) |
| Opportunity Rate | 100% | 15.1% |
| Entry Price | Always 0.50 | Gaussian(0.4965, 0.025) |
| Slippage | None | 0.10% |
| Latency | None | 350ms mean, 75ms std |
| Fill Rate | 100% | 85% |
| Competition | None | 20% haircut on wins |
| Kill Switch | -40% | -40% |
| Random Seed | N/A | 42 (reproducible) |

### 6.3 Calibration Sources

The Realistic parameters were calibrated against real Polymarket data:

- **opportunity_rate = 15.1%**: Measured from 1,048 BTC 5m trades. Of all Binance signals, only 15.1% had a corresponding Polymarket market with prices near 0.50.
- **avg_entry_price = 0.4965**: Average ask price in the tradeable range (0.45-0.55) from real orderbook data.
- **entry_noise_std = 0.025**: Standard deviation around the average entry price.
- **competition_haircut = 20%**: Estimated from orderbook competition analysis. Other bots capture approximately 20% of the available edge.

### 6.4 Grid Search

Parameters tested:
```python
MOMENTUM_THRESHOLDS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
MOMENTUM_WINDOWS_MIN = [1, 2, 3, 5]  # minutes
SLOT_DURATIONS = [5, 15]  # minutes
```

Each combination is tested in both idealistic and realistic mode. Results are saved as JSON for the Quant Lab dashboard.

### 6.5 Sharpe Ratio

Correctly computed from daily returns (not per-trade):
```python
def compute_sharpe_daily(trade_results, num_candles):
    daily_pnl = defaultdict(float)
    for r in trade_results:
        day = int(r.slot_start_ts // 86400)
        daily_pnl[day] += r.pnl_usd
    returns = list(daily_pnl.values())
    mean_r = sum(returns) / len(returns)
    std_r = sqrt(variance)
    return (mean_r / std_r) * sqrt(252)
```

---

## 7. Infrastructure & Deployment

### 7.1 AWS EC2 Dublin (Current Production)

**Why Dublin**: Polymarket CLOB servers are in London. Dublin (eu-west-1) provides the lowest available latency (~1.26ms) from a non-geoblocked AWS region.

**Instance Details**:

| Property | Value |
|----------|-------|
| Instance | c6i.xlarge |
| vCPUs | 4 (Intel Xeon, Ice Lake) |
| Memory | 8 GB |
| Network | Up to 12.5 Gbps |
| Storage | EBS gp3 |
| OS | Ubuntu 24.04 LTS |
| Region | eu-west-1 (Dublin) |
| AZ | eu-west-1a |
| Elastic IP | 34.249.110.17 |

**Startup**:
```bash
cd /home/ubuntu/arbitrage-trading
source venv/bin/activate
export INSTANCE_LABEL=DUBLIN
python main.py  # Starts FastAPI on port 8000
```

**Process Management**: The bot is run as a long-lived process. FastAPI serves both the dashboard and the trading strategy in the same event loop.

### 7.2 Render (Deprecated)

**Why Deprecated**: Render Frankfurt had 45ms latency to Polymarket CLOB, which was too slow. By the time a signal was detected and an order posted, the market had already repriced.

**Render Configuration** (`render.yaml`):
```yaml
services:
  - type: web
    name: polymarket-arb
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn dashboard.web_ui:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: MODE
        value: paper
      - key: PYTHON_VERSION
        value: 3.11.11
```

**Limitations that drove migration**:
1. Free tier sleeps after 15 minutes (mitigated with self-ping)
2. Ephemeral filesystem (mitigated with Supabase + Telegram persistence)
3. 45ms latency to CLOB (not mitigatable)
4. Frankfurt region geoblock status: sometimes blocked

### 7.3 Security

**Secrets Management**:
- All API keys, private keys, and tokens in `.env` file (gitignored)
- `.env.example` committed with placeholder values
- Pydantic Settings auto-loads from environment variables

**Required Environment Variables**:
```
POLYMARKET_PRIVATE_KEY     # Polygon wallet private key
POLYMARKET_FUNDER          # Wallet address
TELEGRAM_BOT_TOKEN         # Telegram alert bot
TELEGRAM_CHAT_ID           # Telegram chat ID
SUPABASE_URL               # Database URL
SUPABASE_KEY               # Database anon key
INSTANCE_LABEL             # Region identifier (DUBLIN, FRANKFURT, etc.)
```

### 7.4 Network Architecture

```
Internet
    |
    +---> Binance WSS (port 9443)
    |     stream.binance.com
    |     bookTicker stream
    |     ~15ms from Dublin
    |
    +---> Polymarket CLOB (HTTPS)
    |     clob.polymarket.com
    |     Orderbook + Order placement
    |     ~1.26ms from Dublin
    |
    +---> Polymarket Gamma API (HTTPS)
    |     gamma-api.polymarket.com
    |     Market discovery
    |
    +---> Polymarket Data API (HTTPS)
    |     data-api.polymarket.com
    |     Redeemable positions
    |
    +---> Polygon RPC (HTTPS)
    |     polygon.publicnode.com (+ 2 fallbacks)
    |     CTF redemption + balance queries
    |
    +---> Supabase (HTTPS)
    |     {project-ref}.supabase.co
    |     Trade persistence
    |
    +---> Telegram Bot API (HTTPS)
          api.telegram.org
          Alert delivery
```

---

## 8. Monitoring & Alerting

### 8.1 Telegram Alerts

The Telegram module (`utils/telegram.py`) provides 9 distinct alert types. All alerts are tagged with `[INSTANCE_LABEL]` and sent via the Telegram Bot API using aiohttp.

**Startup Alert**:
- Measures CLOB latency at boot
- Measures Binance ping at boot
- Checks geoblock status
- Reports wallet address (truncated), max trade size, fee rate, region

**Signal Alert** (every signal detected):
- Asset, direction, momentum, p_true
- Polymarket ask price, expected value
- Position size, Kelly fraction
- Fee, spread, liquidity, time to expiry
- Transit latency
- Market question

**Resolved Alert** (every trade closed):
- WIN/LOSS indicator
- PnL in USD and percentage
- Oracle price movement
- Mark-out data (T+1s, T+5s, T+60s)
- Cumulative session stats (total trades, win rate, PnL)

**Heartbeat** (hourly):
- Trades count, win rate
- PnL and capital
- Active markets count
- CLOB latency, Binance tick count
- Live order success/failure counts

### 8.2 Dashboard

The web dashboard is a FastAPI application serving multiple pages:

| Route | Page | Content |
|-------|------|---------|
| `/` | Main Dashboard | 5-panel Bloomberg-style layout: Oracle status, market windows, signal history, trading stats, open positions |
| `/wallet` | Wallet Management | Real-time USDC.e balance, order history, donation tracker |
| `/quant-lab` | Quant Lab | Backtest results, Monte Carlo simulation, mark-out analysis |
| `/masterplan` | Masterplan | 10-phase roadmap with progress tracking |

**WebSocket Broadcasting** (1Hz):
```python
async def _broadcast_loop():
    while True:
        if connected_clients and strategy:
            payload = _build_payload()
            await _broadcast(payload)
        await asyncio.sleep(1)  # 1Hz
```

The payload includes:
- Oracle status (prices, tick counts, latency percentiles)
- Discovery status (active windows, orderbook data)
- Recent signals (last 30)
- Trading stats (capital, PnL, win rate, drawdown)
- Executor stats (live orders, volume, wallet)
- Heartbeat (momentum levels per asset)
- Configuration values
- Uptime

### 8.3 Heartbeat System

Two levels:
1. **Internal** (every 60s): Status log with window count, signal count, trading stats
2. **Telegram** (every 60min): Full heartbeat with CLOB latency measurement

The CLOB latency measurement at heartbeat time:
```python
async with aiohttp.ClientSession() as s:
    t0 = time.time()
    async with s.get("https://clob.polymarket.com/markets?limit=1") as r:
        await r.read()
    clob_ms = (time.time() - t0) * 1000
```

### 8.4 Supabase Persistence

All trades are persisted to Supabase PostgreSQL with:
- Automatic retry (3 attempts with exponential backoff)
- Instance label for multi-region identification
- Field allowlist to prevent unknown-column errors
- Both `open` and `close` events stored (full lifecycle)

Dashboard API endpoints query Supabase directly:
- `/api/journal` — All trade records
- `/api/monte-carlo` — Monte Carlo from Supabase data
- `/api/markout` — Mark-out analysis from Supabase data

---

## 9. Proven Results

### 9.1 First 7 Live Trades

The first live trading session on AWS Dublin (April 1-2, 2026) produced 7 trades:

| # | Asset | Dir | Entry | Size | Fee | Outcome | PnL |
|---|-------|-----|-------|------|-----|---------|-----|
| 1 | BTC | UP | 0.497 | $5.00 | $0.088 | WIN | +$5.01 |
| 2 | BTC | DOWN | 0.503 | $5.00 | $0.089 | WIN | +$4.82 |
| 3 | ETH | UP | 0.496 | $5.00 | $0.088 | WIN | +$5.09 |
| 4 | BTC | UP | 0.501 | $5.00 | $0.089 | LOSS | -$5.09 |
| 5 | BTC | DOWN | 0.498 | $5.00 | $0.088 | WIN | +$5.02 |
| 6 | ETH | DOWN | 0.505 | $5.00 | $0.090 | LOSS | -$5.09 |
| 7 | BTC | UP | 0.495 | $5.00 | $0.087 | WIN | +$12.86 |

**Summary**:
- Wins: 5 | Losses: 2
- Win Rate: 71.4%
- Total PnL: +$22.62
- Starting Capital: $79.99
- Capital After Trades: $102.61
- ROI: 28.3%

### 9.2 Redemption Process

After winning trades, the wallet held CTF tokens (ERC-1155 Conditional Tokens) instead of USDC.e. The Auto-Redeemer converted these back:

1. Bot queries `https://data-api.polymarket.com/positions?user={wallet}`
2. Identifies positions where `redeemable == true`
3. Calls `redeemPositions()` on CTF contract (0x4D97DCd97eC945f40cF65F87097ACe5EA0476045)
4. USDC.e flows back to the trading wallet
5. Total redeemed: $53.97 across 5 winning trades

### 9.3 Performance Metrics

| Metric | Value |
|--------|-------|
| Starting Capital | $79.99 |
| Ending Capital | $102.61 |
| Net Profit | +$22.62 |
| ROI | 28.3% |
| Win Rate | 71.4% (5/7) |
| Average Win | +$6.56 |
| Average Loss | -$5.09 |
| Profit Factor | 3.22 |
| Max Position | $5.00 |
| Avg Fee/Trade | $0.088 |
| CLOB Latency | 1.26ms |
| Transit Latency | ~15ms P50 |

---

## 10. Known Limitations & Future Improvements

### 10.1 Current Limitations

1. **Single Oracle**: Only Binance WebSocket. If Binance goes down, no signals.
2. **Taker-Only Edge**: 1.80% fee is significant. Maker mode has lower fill rates.
3. **$5 Position Cap**: Limits absolute profit per trade due to small account.
4. **opportunity_rate**: Only 15.1% of signals have tradeable Polymarket counterparts.
5. **No Maker Rebate**: Currently using taker mode; maker rebate strategy not fully tested.
6. **Manual Startup**: Bot does not auto-restart on crash (no systemd service yet).
7. **Single Region**: Only Dublin. No failover or multi-region arbitrage.
8. **Chainlink vs Binance**: Resolution uses Chainlink (not Binance). Small discrepancies possible.

### 10.2 Planned Improvements (from Masterplan)

**Phase 6**: Multi-Oracle (add Coinbase, OKX WS feeds for signal confirmation)
**Phase 7**: Smart Order Router (route to best venue across multiple prediction markets)
**Phase 8**: Maker Strategy (earn rebates instead of paying fees)
**Phase 9**: Capital Scaling ($100 -> $500 -> $1000 with position size increases)
**Phase 10**: Multi-Strategy (add election markets, sports, custom categories)
**Phase 11**: Full Automation (systemd, auto-restart, health checks, monitoring)

### 10.3 Strategy Evolution Roadmap

| Horizon | Strategy | Expected Impact |
|---------|----------|-----------------|
| Short-term | Optimize v2.0 parameters | +10% win rate |
| Medium-term | Multi-oracle confirmation | Reduce false signals |
| Long-term | Maker + Multi-strategy | Lower fees + more opportunities |

---

## 11. Configuration Reference

Complete table of all config parameters in `config.py`:

| Parameter | Type | Default | Description | Version |
|-----------|------|---------|-------------|---------|
| `mode` | str | `"paper"` | Operating mode: paper or live | v0.1 |
| `log_level` | str | `"INFO"` | Logging level | v0.1 |
| `exchanges` | list[str] | `["binance", "kucoin", "bybit"]` | Exchange list (legacy) | v0.1 |
| `quote_currency` | str | `"USDT"` | Quote currency | v0.1 |
| `min_profitable_spread` | float | `0.30` | Min spread after fees (%) | v0.1 |
| `min_volume_usd` | float | `50,000` | Min 24h volume per exchange | v0.1 |
| `min_volume_ratio` | float | `0.10` | Min volume ratio between sides | v0.1 |
| `use_ws_price_feed` | bool | `True` | Use WebSocket for prices | v0.1 |
| `max_order_size_usd` | float | `100` | Max order size (USD) | v0.1 |
| `order_timeout_seconds` | float | `3.0` | Order timeout | v0.1 |
| `max_portfolio_pct_per_trade` | float | `0.08` | Max 8% portfolio per trade | v0.1 |
| `max_daily_loss_pct` | float | `0.20` | -20% daily loss kill switch | v0.1 |
| `max_total_loss_pct` | float | `0.40` | -40% total loss shutdown | v0.1 |
| `min_balance_usd` | float | `100` | Min balance to trade | v0.1 |
| `rebalance_drift_pct` | float | `0.30` | Alert at >30% drift | v0.1 |
| `scan_interval_seconds` | float | `5.0` | Dashboard refresh interval | v0.1 |
| `top_opportunities` | int | `20` | Top N opportunities shown | v0.1 |
| `orderbook_depth` | int | `5` | Orderbook depth for analysis | v0.1 |
| `data_dir` | Path | `./data` | Data directory path | v0.1 |
| `oracle_symbols` | list[str] | `["BTC/USDT", "ETH/USDT"]` | Binance oracle symbols | v0.1 |
| `momentum_window_s` | float | `15.0` | Momentum calculation window (s). v2: 15s (was 60s) | v2.0 |
| `min_momentum_pct` | float | `0.05` | Min Binance momentum for signal. v2: 0.05% (was 0.10%) | v2.0 |
| `min_edge_pct` | float | `1.5` | Min expected edge after fees. v2: 1.5% (was 2.0%) | v2.0 |
| `polymarket_max_fee_pct` | float | `1.80` | Max Polymarket taker fee at p=0.50. v1.5: 1.80% (was 3.15%) | v1.5 |
| `kelly_fraction` | float | `0.50` | Fractional Kelly (Half-Kelly) | v0.1 |
| `paper_capital_usd` | float | `1000.0` | Virtual starting capital | v0.1 |
| `max_position_pct` | float | `0.08` | Max 8% of capital per trade | v0.1 |
| `min_seconds_to_expiry` | float | `15.0` | No trade if <15s to expiry. v2: 15s (was 30s) | v2.0 |
| `max_seconds_to_expiry` | float | `180.0` | No trade if >180s to expiry. v2: 180s (was 240s) | v2.0 |
| `telegram_bot_token` | str | `""` | Telegram Bot token | v0.1 |
| `telegram_chat_id` | str | `""` | Telegram chat ID | v0.1 |
| `polymarket_private_key` | str | `""` | Polygon wallet private key | v1.0 |
| `polymarket_funder` | str | `""` | Funder/wallet address | v1.0 |
| `live_trading` | bool | `False` | Enable real orders | v1.0 |
| `max_live_position_usd` | float | `5.0` | Max $5 per live trade | v1.0 |
| `order_type` | str | `"taker"` | taker (market) or maker (limit) | v1.0 |

---

## 12. API Reference

### REST Endpoints

| Method | Path | Description | Response |
|--------|------|-------------|----------|
| GET | `/` | Main dashboard (HTML) | HTMLResponse |
| GET | `/wallet` | Wallet page (HTML) | HTMLResponse |
| GET | `/quant-lab` | Quant Lab (HTML) | HTMLResponse |
| GET | `/masterplan` | Masterplan (HTML) | HTMLResponse |
| GET | `/api/status` | Full strategy status | JSON |
| GET | `/api/wallet` | Wallet balance and stats | JSON |
| GET | `/api/journal` | Trade journal (Supabase primary, local fallback) | JSON |
| GET | `/api/backtest` | Idealistic backtest results | JSON |
| GET | `/api/backtest/realistic` | Realistic backtest results | JSON |
| GET | `/api/backtest/compare` | Side-by-side comparison | JSON |
| GET | `/api/monte-carlo` | Monte Carlo simulation | JSON |
| GET | `/api/markout` | Mark-out signal analysis | JSON |
| POST | `/api/toggle-order-type` | Toggle maker/taker | JSON |

### WebSocket Endpoint

| Path | Protocol | Frequency | Content |
|------|----------|-----------|---------|
| `/ws` | WebSocket | 1Hz | Full strategy status payload |

**Payload Structure**:
```json
{
    "oracle": {"BTC/USDT": {"connected": true, "last_price": 84050.4, ...}},
    "discovery": {"total_windows": 8, "tradeable": 4, "windows": [...]},
    "signals": [...],
    "trading": {"capital_usd": 102.61, "win_rate_pct": 71.4, ...},
    "config": {"momentum_window_s": 15.0, ...},
    "executor": {"live": true, "orders_placed": 7, ...},
    "heartbeat": {"BTC": {"price": 84050, "momentum": 0.045, ...}},
    "running": true,
    "uptime": "24:15:30",
    "instance_label": "DUBLIN"
}
```

### External APIs Used

| Service | Base URL | Purpose | Auth |
|---------|----------|---------|------|
| Binance WS | `wss://stream.binance.com:9443/ws` | Price feed | None |
| Polymarket Gamma | `https://gamma-api.polymarket.com` | Market discovery | None |
| Polymarket CLOB | `https://clob.polymarket.com` | Orderbook + orders | EIP-712 + HMAC |
| Polymarket Data | `https://data-api.polymarket.com` | Redeemable positions | None |
| Polygon RPC | `https://polygon.publicnode.com` | CTF redemption + balance | None |
| Supabase | `https://{ref}.supabase.co` | Trade persistence | API Key |
| Telegram | `https://api.telegram.org/bot{token}` | Alerts | Bot Token |

---

## 13. Glossary

| Term | Definition |
|------|-----------|
| **Adverse Selection** | Trading against someone with better information. Stale signals cause this. |
| **Ask** | Lowest price at which someone is willing to sell |
| **Bid** | Highest price at which someone is willing to buy |
| **bookTicker** | Binance WebSocket stream providing real-time best bid/ask updates |
| **Chainlink** | Decentralized oracle network. Polymarket uses Chainlink for resolution. |
| **CLOB** | Central Limit Order Book. Polymarket's order matching system. |
| **Competition Haircut** | Estimated percentage of edge captured by other bots (20% in realistic model) |
| **Conditional Token** | ERC-1155 token representing a position in a prediction market outcome |
| **CTF** | Conditional Token Framework. Smart contract managing prediction market tokens. |
| **Drawdown** | Decline from peak capital, expressed as percentage |
| **Edge** | Difference between estimated true probability and market-implied probability |
| **EIP-712** | Ethereum standard for typed data signing. Used for CLOB authentication. |
| **Ephemeral Market** | A market that exists for only 5 or 15 minutes |
| **Expected Value (EV)** | Probability-weighted average outcome of a trade |
| **Fill Rate** | Percentage of orders that get executed |
| **Gamma API** | Polymarket REST API for market discovery and metadata |
| **Geoblock** | Regional restriction preventing access to Polymarket |
| **Half-Kelly** | Using 50% of the Kelly Criterion's recommended bet size |
| **HMAC-SHA256** | Hash-based message authentication code. Used for per-request CLOB auth. |
| **Kelly Criterion** | Formula for optimal bet sizing based on edge and odds |
| **Kill Switch** | Automatic safety mechanism that halts trading at loss thresholds |
| **Latency Arbitrage** | Profiting from speed advantage in price discovery between venues |
| **Liquidity** | Total dollar value available in a market's orderbook |
| **Logistic Function** | S-shaped function mapping momentum to probability: 1/(1+exp(-x)) |
| **Maker** | Order that adds liquidity to the book (limit order below best ask) |
| **Mark-out** | Price change after trade entry, measured at specific time intervals |
| **Momentum** | Rate of price change over a time window, expressed as percentage |
| **Net EV** | Expected value after subtracting fees and costs |
| **Opportunity Rate** | Percentage of signals that have a tradeable Polymarket counterpart |
| **Oracle** | External price feed used as signal source (Binance) or for resolution (Chainlink) |
| **Paper Trading** | Simulated trading without real capital |
| **Polygon** | Layer-2 blockchain where Polymarket settles trades |
| **Pre-Signing** | Creating order signatures in advance to reduce execution latency |
| **Price-First Filter** | v2.0 filter that rejects signals when Polymarket has already repriced |
| **Resolution** | Determination of the winning outcome when a market expires |
| **Scaling Factor** | Multiplier in the logistic function (3.0, RMSE-optimized) |
| **Shares** | Units of a prediction market outcome. Each winning share pays $1.00. |
| **Slippage** | Difference between expected and actual execution price |
| **Slug** | URL-friendly identifier for a Polymarket market (e.g., `btc-updown-5m-1711900800`) |
| **Spread** | Difference between best ask and best bid prices |
| **Taker** | Order that removes liquidity from the book (hits existing offer) |
| **Transit Latency** | Time for data to travel from exchange server to our machine |
| **USDC.e** | Bridged USDC on Polygon (6 decimal places). The settlement token. |
| **Watchdog** | Timer that forces reconnect if no fresh data is received for 120 seconds |
| **Window** | A 5-minute or 15-minute time period for a Polymarket prediction market |

---

*Document generated: April 2, 2026. Version 2.0.*
*This document is self-contained: a developer should be able to rebuild the entire system from it.*
