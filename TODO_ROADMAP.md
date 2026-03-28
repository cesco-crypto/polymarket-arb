# Polymarket Latency Arb — Roadmap & TODO

## Phase 1: JETZT AKTIV (Paper Trading läuft auf Render)
- [x] Binance WS Oracle (Sub-50ms)
- [x] Market Discovery (Slug-basiert, 5m/15m)
- [x] PreTrade Calculator (3.15% Fee, Half-Kelly, ROI-basierter EV)
- [x] Paper Trader + CSV Logging
- [x] Bloomberg-Style Dashboard (5 Panels)
- [x] Quant Lab (Heatmaps, Grid Search, KPIs)
- [x] Live Pulse Momentum-Meter
- [x] Telegram Alerts + Trade Backup
- [x] Render Deployment + Keep-Alive
- [x] Backtest Engine (6 Monate, 300K Klines, 85-97% WR validiert)
- [x] Mark-out Recording (T+1s, T+5s, T+10s, T+30s, T+60s)

## Phase 2: MONTAG — Paper Trading Auswertung
- [ ] Montag Paper-Trading-Daten sammeln (~279 erwartete Trades)
- [ ] Mark-out Analyse: Ist der Preis T+5s nach Signal noch in unsere Richtung?
  - Positiv = echtes Alpha, weiter
  - Negativ = Toxic Flow, Schwelle erhöhen auf 0.20%+
- [ ] analyze_paper_trades.py ausführen mit echten Daten
- [ ] Kill-Switch Telegram-Testverifizierung (manuell -20% simulieren)
- [ ] Entscheidung: Sind wir profitabel nach Fees?

## Phase 3: DASHBOARD-ERWEITERUNGEN
- [ ] Mark-out Decay Graph ("Todeskurve") im Quant Lab
  - Visualisiert Preisentwicklung 1s/5s/10s nach Trade
  - Warnlampe: "LATENCY ARB FAILING" wenn Mark-out negativ
- [ ] Hurdle Visualization im Live Pulse
  - Total Friction = 3.15% Taker Fee + echter Bid-Ask Spread (1-2 Cent)
  - Zeigt dass die echte Hürde oft > 5% ist
- [ ] Regime Detection Label
  - "Weekend Low Vol" / "US Session High Vol" / "Asian Session"
  - Bot pausiert automatisch in Low-Vol Regimes
- [ ] Monte Carlo Stress Test (nach 50+ Trades)
  - 1000 Permutationen der Trade-History
  - Probability Distribution der Account-Outcomes

## Phase 4: EXECUTION LAYER (Voraussetzung für Live-Trading)
- [ ] EIP-712 Wallet Signing für Polymarket CLOB
- [ ] HMAC-SHA256 L2 Authentifizierung
- [ ] Order Placement via CLOB API
- [ ] py-clob-client Integration
- [ ] Private Key Management (ENV Variable, nie im Code)

### Trick A: EIP-712 Pre-Signing (Latenz-Killer)
- [ ] Order-Signatur vorab berechnen während auf Signal gewartet wird
- [ ] Bei Trigger nur noch fertiges Paket senden (spart 50-100ms)

### Trick B: Maker Pivot (Gebühren eliminieren)
- [ ] Statt Taker-Order (3.15% Fee): Limit-Order knapp unter Marktpreis
- [ ] Bei Fill: Maker Rebate ERHALTEN statt Taker Fee ZAHLEN
- [ ] Trade-off: Niedrigere Fill-Rate, aber massiv höherer Profit/Trade
- [ ] Backtest: Win Rate sinkt von 90% auf ~70%, aber Net PnL steigt

### Trick C: Liquiditäts-Gating
- [ ] Hard Limit: Nie traden wenn Markt-Liquidität < $20,000
- [ ] Aktuell: ETH 5m Märkte haben nur $4K Liq — zu dünn
- [ ] Nur BTC 15m Märkte ($13-50K) und BTC 5m ($7K+) qualifizieren

## Phase 5: INFRASTRUKTUR (für Live-Trading)
### Trick D: Geografische Co-Location
- [ ] Von Render Frankfurt → AWS Tokio (ap-northeast-1)
- [ ] Bare-Metal oder EC2 c5.xlarge (~$150/Monat)
- [ ] Binance Matching Engine ist in Tokio → P99 Latenz von 788ms auf <10ms
- [ ] Oder: Singapur als Kompromiss (Render bietet Singapur)

### Infrastruktur-Upgrades
- [ ] Render Starter Tier ($7/Mo) für persistentes Filesystem
- [ ] Oder: Eigener VPS mit Docker
- [ ] Polymarket WS für Orderbücher (statt REST alle 3s)
- [ ] 1-Sekunden Binance Tick-Daten von data.binance.vision

## Phase 6: GO-LIVE AUDIT (alle müssen "JA" sein)
- [ ] 3.15% Fees + 1-2 Cent Spreads fest im PreTradeCalculator?
- [ ] Mark-out nach 5s positiv (echtes Alpha, nicht Toxic Flow)?
- [ ] Kill-Switch -20% Daily scharf + Telegram-verifiziert?
- [ ] 200+ Paper Trades mit >70% Win Rate?
- [ ] Execution Layer getestet (Paper Order → echte Order)?
- [ ] Server-Latenz < 100ms P99 zum Polymarket CLOB?
- [ ] Wallet funded mit USDC auf Polygon?
- [ ] Rechtliche Klärung (CH: gewerbsmässiger Handel?)

## Notizen
- 0x8dxd hatte 0% Fees am Anfang — wir haben 3.15%. Andere Ära.
- 0x8dxd: 52.7% WR × 541K Trades = Masse. Wir: 90%+ WR × weniger Trades = Qualität.
- Samstag/Sonntag: ~16 Signale/Tag. Montag: ~429 Signale/Tag (27x mehr).
- Backtest validiert: Jede Konfiguration profitabel über 6 Monate.
