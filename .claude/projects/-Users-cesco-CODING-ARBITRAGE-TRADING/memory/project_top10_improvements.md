---
name: Top 10 HFT Improvements
description: Priorisierte Liste der wichtigsten Optimierungen nach PnL-Impact, aus Profi-HFT-Analyse
type: project
---

## Top 10 Verbesserungen (nach PnL-Impact)

1. **Polymarket WebSocket** statt REST 3s Polling → +30-50% Fill-Qualität
2. **Maker Pivot** (Limit statt Market Orders) → Fee 3.15% → 0% + Rebate
3. **Logistic Model kalibrieren** mit echten Daten → +15-25% Signal
4. **Event-driven Signale** statt 500ms Sleep → +200ms Latenz
5. **Multi-Timeframe Momentum** (5s+10s+60s+300s) → +10-20% Signal
6. **Volume-weighted Signals** (Bid/Ask Qty bereits im WS vorhanden) → +10-15%
7. **Intra-Hour Drawdown Breaker** (-5% Rolling 1h) → verhindert Katastrophe
8. **Korrelations-adjustiertes Sizing** BTC↔ETH → verhindert 2x Risiko
9. **Dynamische Schwelle** nach Volatilitäts-Regime → +10-15% Sharpe
10. **Early Exit** (Position vor Resolution verkaufen) → -20% avg Verlust

## Drei strukturelle Probleme

- Orderbuch 3s stale (REST) vs 2.7s Edge-Fenster
- 3.15% Taker Fee frisst fast den ganzen Edge
- Probability Model geraten (scaling=2.0), nicht kalibriert

## Kostenlose Alpha-Quelle bereits im Code

binance_ws.py parsed msg["B"] (Bid Qty) und msg["A"] (Ask Qty) aber
verwirft sie. Order-Flow-Imbalance = bid_qty/(bid_qty+ask_qty) als
Signal-Multiplikator nutzen.

## Backtest-Caveat

85-97% Win Rate basiert auf simulated_ask=0.50. In Realität ist der Ask
bereits bei 0.52-0.55 wenn wir einsteigen. Echte WR wahrscheinlich 70-80%.
Trotzdem profitabel (Break-Even bei ~52%).
