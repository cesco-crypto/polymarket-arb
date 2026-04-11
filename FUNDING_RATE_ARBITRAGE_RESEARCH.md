# Funding Rate Arbitrage + Polymarket: Deep Research Report
**Date: 2026-04-02**

---

## 1. FUNDING RATE ARBITRAGE -- HOW IT WORKS

### The Mechanism
Perpetual futures have no expiration date. To anchor their price to spot, exchanges use a **funding rate**: a periodic payment between longs and shorts.

- **Positive funding** (perp > spot): Longs pay shorts
- **Negative funding** (perp < spot): Shorts pay longs

Funding is typically settled every **8 hours** on CEXs (Binance, Bybit, OKX) and every **1 hour** on DEXs (Hyperliquid, dYdX).

### The Classic Cash-and-Carry
```
Buy 1 BTC spot @ $83,000          --> delta +1
Short 1 BTC perpetual @ $83,000   --> delta -1
Net delta = 0 (market neutral)

If funding rate = +0.01% per 8h:
  $83,000 * 0.01% = $8.30 per 8h
  $8.30 * 3 = $24.90 per day
  $24.90 * 365 = $9,088.50 per year
  APY on $83K deployed = ~10.95%
```

### Return Spectrum (2025-2026 Data)
| Condition | Avg Funding/8h | APY (no leverage) | APY (2x leverage) |
|-----------|---------------|--------------------|--------------------|
| Bear/Flat market | 0.005% | ~5-7% | ~10-14% |
| Normal market | 0.01% | ~10-11% | ~20-22% |
| Bull market | 0.03-0.05% | ~30-50% | ~60-100% |
| Extreme sentiment | 0.1%+ | ~100%+ | Dangerous |

Academic research (ScienceDirect, Jan 2026): Up to **115.9% returns over 6 months**, max drawdown 1.92%. Average annual return **19.26% in 2025** (up from 14.39% in 2024).

---

## 2. EXCHANGE FUNDING RATE COMPARISON

### CEX Comparison (Perpetual Futures)
| Exchange | Settlement | Maker Fee | Taker Fee | Max Leverage | Notes |
|----------|-----------|-----------|-----------|-------------|-------|
| **Binance** | 4-8h | 0.02% | 0.05% | 125x | Built-in arb bot, highest liquidity |
| **Bybit** | 8h | 0.02% | 0.055% | 100x | 2nd largest, 1600+ pairs |
| **OKX** | 8h | 0.02% | 0.05% | 100x | Smart arbitrage bot |
| **Bitget** | 8h | 0.02% | 0.06% | 125x | Updated arb bot (Jan 2026) |

### DEX Comparison (Perpetual Futures)
| Exchange | Settlement | Maker Fee | Taker Fee | Funding Cap | Notes |
|----------|-----------|-----------|-----------|-------------|-------|
| **Hyperliquid** | **1 hour** | 0.01% | 0.035% | **4%/hour** | Dominant: 50% DEX market, $208B/30d |
| **dYdX** | 1 hour | 0.02% | 0.05% | Variable | Cosmos-based, regulatory focus |
| **GMX** | Variable | 0.04-0.06% | 0.04-0.06% | N/A | AMM model, simpler but higher fees |

### Why Hyperliquid Stands Out
1. **Lowest fees**: 0.01% maker / 0.035% taker (vs 0.02/0.05 on Binance)
2. **Hourly settlement**: 8x more granular than CEXs, faster capture
3. **Higher funding caps**: 4% per hour vs variable lower caps on Binance
4. **Bullish user base**: On-chain traders tend to be long, meaning shorts consistently receive funding
5. **Massive volume**: $208B/30 days, 229K+ active traders

### Cross-Exchange Funding Rate Arbitrage Returns
From BitMEX research (H1 2025):
- Short Hyperliquid / Long Binance on **SOL**: ~15.6% annualized
- Short Hyperliquid / Long Binance on **AVAX**: ~15.7% annualized
- With 2-3x leverage: **25-30%+** headline yield, still delta-neutral

From Pendle Boros data:
- Cross-perp BTC arb (Hyperliquid vs Binance): **5.98-11.4% Fixed APR**
- Peak opportunities: **23%+ APR**
- With Boros fixed-rate lock: Up to **30% combined yield**

---

## 3. POLYMARKET + FUNDING RATE -- THE COMBINED STRATEGY

### Polymarket Fee Structure (as of March 2026)

**Most markets remain fee-free.** Fees only apply to specific market types:

| Market Type | Taker Fee (@ 50% odds) | Maker Rebate |
|-------------|----------------------|--------------|
| 5-minute crypto | **0.44%** max | Up to 50% of taker fee |
| 15-minute crypto | **3.15%** max | Up to 50% of taker fee |
| Sports (NCAAB/Serie A) | **1.56%** max | Up to 50% of taker fee |
| All other markets | **0% (free)** | N/A |
| Winner fee (resolution) | **2%** on winnings | N/A |

Key: Fees are highest at 50% probability, decrease toward extremes. Maker orders (limit orders) earn rebates.

### Strategy A: Pure Polymarket Market-Making + Funding Rate Layer

```
LAYER 1 -- Polymarket Market Making (non-crypto event markets, 0% fee)
  Buy YES @ 0.49 + Buy NO @ 0.49 = $0.98 cost
  Guaranteed $1.00 payout minus 2% winner fee
  Net: $1.00 - $0.02 winner fee - $0.98 cost = $0.00 to ~$0.02 per round-trip
  (Profit only when spread allows buying both sides below $0.49 each)

LAYER 2 -- Funding Rate Collection (separate capital allocation)
  Hold $5,000 in BTC spot
  Short $5,000 in BTC perpetual on Hyperliquid
  Funding @ 0.01%/8h = $0.50/8h = $1.50/day
  Monthly: ~$45 on $10K deployed (5.4% APY)
  During bull markets: 3-5x higher
```

### Strategy B: Polymarket Crypto Hedge + Funding Rate

This is the more interesting combination:

```
SCENARIO: BTC at $83,000

LEG 1: Buy "BTC Up" on Polymarket 5-min market @ 0.50 ($500 position)
LEG 2: Short BTC perpetual on Hyperliquid (equivalent notional ~$500)

CASE A -- BTC goes UP in 5 min:
  Polymarket: Win ~$500 (minus 0.44% taker fee + 2% winner fee)
  Perpetual: Lose ~same delta amount
  Funding: Collect hourly funding on the short
  Net: ~breakeven on directional + funding income

CASE B -- BTC goes DOWN in 5 min:
  Polymarket: Lose $500
  Perpetual: Gain ~same delta amount
  Funding: Still collect funding on the short
  Net: ~breakeven on directional + funding income
```

**PROBLEM**: The delta match between a Polymarket binary option (pays $1 or $0) and a perpetual future (linear P&L) is NOT exact. A Polymarket "Up" token is a BINARY outcome -- it is NOT a linear derivative. The hedge ratio changes with probability.

### Strategy C: Delta-Neutral Polymarket + Funding Rate (Institutional Approach)

```
CAPITAL: $10,000

Allocation 1: $5,000 -- Polymarket market-making on event markets
  Target: 50 trades/day at 1-2% edge = $25-50/day
  Risk: Event mispricing, smart money, low liquidity

Allocation 2: $5,000 -- Funding rate arbitrage
  $2,500 in BTC/ETH spot
  $2,500 short BTC/ETH perpetual on Hyperliquid
  Target: $1.50-3.00/day from funding
  Risk: Funding reversal, liquidation

COMBINED DAILY TARGET: $26.50-53.00/day
COMBINED MONTHLY TARGET: $795-1,590
COMBINED APY: ~95-190% (optimistic, assumes consistent edge)
```

---

## 4. PENDLE BOROS -- GAME CHANGER FOR FUNDING RATE ARB

Pendle Boros tokenizes funding rates into tradeable instruments:

- **Yield Units (YUs)**: Each represents funding from 1 unit of an asset
- **Long YU**: Pay fixed rate, receive floating funding
- **Short YU**: Receive fixed rate, pay floating funding

### The Boros-Enhanced Strategy
```
LEG 1: Short BTC perpetual on Hyperliquid (receive floating funding)
LEG 2: Long BTC spot on any exchange (delta hedge)
LEG 3: Short YU on Boros (lock in FIXED APR, pay floating)

Net effect:
  - Floating funding received (Leg 1) offsets floating funding paid (Leg 3)
  - You receive the FIXED APR from Boros
  - Delta-neutral from Leg 1 + Leg 2
  - Funding rate risk ELIMINATED -- you lock in your yield at entry

Documented fixed yields: 5.98% to 11.4% APR
Peak: 23%+ APR
Combined with leverage: Up to 30%
```

Boros has $6.9B in open interest and $2.8B+ cumulative volume. This is the institutional-grade tool for this strategy.

---

## 5. EXISTING IMPLEMENTATIONS

### Exchange-Native Bots
1. **Binance Funding Rate Arbitrage Bot**: Built-in, user inputs investment size. Backtested APR: ~10.95% (at 0.09% 3-day cumulative rate). Breakeven: ~10 days for VIP0.
2. **Bitget Bot (Jan 2026 update)**: Supports reverse arbitrage, multi-currency, basis timing.
3. **OKX Smart Arbitrage**: Backtested APY: 4.39-9.46%.

### Open Source
- **ARBOT** (GitHub: IrakliXYZ/ARBOT): Spot-futures arb bot, claims 15-30% annually.
- **funding-rate-arbitrage** (GitHub: ksmit323): Hackathon winner, farms rates across DEXs.
- **50shadesofgwei/funding-rate-arbitrage**: Delta-neutral funding rate arbitrage searcher.

### Academic Validation
- ScienceDirect (2025): 60 arbitrage scenarios evaluated, returns up to 115.9% over 6 months.
- BSIC Bocconi: 17% of observations show economically significant spreads (>20bp), but only 40% profitable after costs.
- Wharton (arXiv): Funding-spot gap shrinks ~11% per year as markets mature.

---

## 6. RISK ANALYSIS

### Risk Matrix

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Funding rate reversal | Medium | Medium | Monitor via CoinGlass, exit when 3-day avg turns negative |
| Liquidation on perp leg | Low (with 1-2x) | High | Max 2x leverage, 150% maintenance margin alarm |
| Exchange downtime | Low | High | Multi-exchange deployment (max 25-30% per exchange) |
| Basis blowout | Low | Very High | Stop-loss at 2-3% basis divergence |
| Fee increase (Polymarket) | Medium | Low | Use maker orders only, monitor fee announcements |
| Smart contract risk (DEX) | Low | Very High | Limit DEX exposure to 30% of capital |
| Execution slippage | Medium | Low | Limit orders, avoid market orders |
| Regulatory action | Low | Very High | Geographic diversification, KYC compliance |

### What CANNOT Go Wrong (True Invariants)
- If you hold 1 BTC spot and are short 1 BTC perp: you ARE delta-neutral. Price moves cancel out.
- Funding is paid/received at each settlement period regardless of price.
- Polymarket binary outcomes resolve to $0 or $1 -- no ambiguity.

### What CAN Go Wrong
1. **Funding flips negative**: In bear markets, shorts PAY funding. Your "income" becomes an expense. Historical data shows funding is positive ~70% of the time for BTC.
2. **Liquidation cascade**: Even delta-neutral, if your short perp leg faces sudden 30%+ move with insufficient margin, you get liquidated on ONE leg while the other stays open -- suddenly you are NOT delta-neutral anymore.
3. **Polymarket-perp basis risk**: A Polymarket 5-min binary option has VERY different risk characteristics than a perpetual future. The binary pays $1 or $0. The perp has linear P&L. Perfect hedging is mathematically impossible between these instruments.
4. **Timing mismatch**: Polymarket resolves every 5 minutes. Funding settles every 1-8 hours. These are fundamentally different time horizons.

---

## 7. CONCRETE STRATEGY BLUEPRINT

### Phase 1: Pure Funding Rate Arbitrage (Week 1-4)
**Capital required: $500 minimum**

```python
# Strategy: Spot-Perp Cash and Carry
# Exchange: Binance (built-in bot) or manual on Hyperliquid

ALLOCATION:
  50% -> BTC spot
  50% -> BTC short perpetual

ENTRY CRITERIA:
  - 3-day cumulative funding rate > 0.03% (annualized > 3.65%)
  - Spread between spot and perp < 0.05%
  - Use limit orders only

POSITION SIZING:
  - Max leverage: 2x on perp leg
  - Keep 20% capital as reserve margin

EXIT CRITERIA:
  - 3-day avg funding turns negative
  - Basis exceeds 0.3%
  - Any exchange issues (downtime, withdrawal freeze)

EXPECTED RETURN: 5-15% APY (conservative)
```

### Phase 2: Cross-Exchange Funding Arb (Week 5-8)
**Capital required: $2,000 minimum (split across exchanges)**

```python
# Strategy: Long low-funding exchange, Short high-funding exchange

MONITORING:
  - CoinGlass real-time funding comparison
  - Track: Binance, Bybit, Hyperliquid, dYdX

ENTRY CRITERIA:
  - Funding rate spread between exchanges > 0.02% per 8h
  - After fees, net positive by > 0.01% per settlement
  - Sufficient liquidity on both sides

EXECUTION:
  1. Identify highest-paying short (usually Hyperliquid)
  2. Open long on lowest-paying exchange (usually Binance)
  3. Match position sizes exactly
  4. Monitor hourly

EXPECTED RETURN: 10-20% APY
```

### Phase 3: Polymarket + Funding Rate Combo (Week 9+)
**Capital required: $5,000 minimum**

```python
# Strategy: Separate but concurrent operations

POOL A -- Polymarket Market Making ($2,500):
  - Focus on event markets (0% fee)
  - Bid-ask spread capture
  - Max 2% per trade, Kelly criterion sizing
  - Target: 20-50 trades/day

POOL B -- Funding Rate Arb ($2,500):
  - Spot-perp on BTC/ETH
  - Hyperliquid preferred (hourly, low fees)
  - Cross-exchange when spread justifies

POOL C -- Polymarket Crypto Hedge (experimental, $500):
  - Only when 5-min market odds are significantly mispriced
  - Hedge directional risk via perp
  - Accept that hedge is imperfect (binary vs linear payoff)

REBALANCING:
  - Weekly: Reallocate between pools based on returns
  - Monthly: Full strategy review
```

---

## 8. TOOLS AND DATA SOURCES

| Tool | Purpose | URL |
|------|---------|-----|
| CoinGlass Funding Heatmap | Real-time funding rates all exchanges | coinglass.com/FundingRateHeatMap |
| CoinGlass Arb Scanner | Cross-exchange funding arbitrage | coinglass.com/FrArbitrage |
| ArbitrageScanner | Funding rate comparison + alerts | arbitragescanner.io/funding-rates |
| Hyperliquid Funding Comparison | Hyperliquid vs CEX funding | app.hyperliquid.xyz/fundingComparison |
| Binance Arb Data | Binance-specific arb opportunities | binance.com/en/futures/funding-history/perpetual/arbitrage-data |
| Pendle Boros | Fixed-yield funding rate trading | pendle.finance/boros |
| Polymarket 5-min markets | Short-term crypto prediction | polymarket.com/crypto/5M |

---

## 9. KEY FINDINGS SUMMARY

1. **Funding rate arbitrage is proven**: 5-15% APY conservative, 15-30% with cross-exchange, up to 100%+ in extreme conditions. Academic validation exists.

2. **Hyperliquid is the best venue for the short leg**: Lowest fees, hourly settlement, highest funding caps, bullish user base means shorts consistently receive.

3. **Pendle Boros is the game changer**: Locks in FIXED yields on funding rates. Eliminates funding reversal risk. 5.98-11.4% fixed APR documented.

4. **Polymarket + funding rate is a PARALLEL strategy, not a combined one**: The binary nature of Polymarket outcomes makes perfect hedging with perpetual futures mathematically imprecise. Best to run them as separate profit centers sharing the same capital base.

5. **The real edge**: Polymarket market-making on event markets (0% maker fee) + Hyperliquid funding rate collection on BTC/ETH. Two uncorrelated income streams.

6. **Biggest risk**: Funding rate reversal (solvable with Boros fixed-rate) and liquidation on the perp leg (solvable with low leverage + margin buffers).

7. **Competition is increasing**: Arb opportunity windows shrunk from 12.3 seconds (2024) to 2.7 seconds (2026). Execution speed matters. But funding rate arb is less speed-sensitive than spread arb.

---

## 10. EXCHANGE RANKING BY FUNDING RATE ATTRACTIVENESS

**For the SHORT leg (receiving funding):**
1. **Hyperliquid** -- Highest rates due to bullish user base, hourly settlement, lowest fees
2. **Bybit** -- Often slightly higher rates than Binance, good liquidity
3. **Binance** -- Most liquid, most stable, built-in bot
4. **dYdX** -- Good rates, hourly settlement, but lower volume

**For the LONG leg (hedging):**
1. **Binance** -- Lowest slippage due to massive liquidity, built-in arb tools
2. **Spot market** (any exchange) -- Simplest: buy actual BTC/ETH
3. **Bybit** -- Good alternative if Binance unavailable

**Overall Best Setup for ~$5K capital:**
- Short BTC/ETH perp on **Hyperliquid** (receive high funding, hourly)
- Long BTC/ETH spot on **Binance** (deep liquidity, low fees)
- Lock yield via **Pendle Boros** when available
- Run **Polymarket market-making** separately on event markets
