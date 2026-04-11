---
name: Monday Action Plan
description: Konkrete Schritte für Montag 30. März basierend auf Paper Trading Daten
type: project
---

## Montag 30. März 2026 — Action Plan

Bot läuft auf Render (https://polymarket-arb-coht.onrender.com) seit Samstagabend.
Erwartete Trades Montag: ~279 (basierend auf 10-Montag-Analyse).
Config: 0.10% Momentum, 60s Fenster, Half-Kelly, 3.15% Fee.

### Morgens: Daten prüfen
- Telegram-History checken: Trades über Nacht/Wochenende?
- Dashboard öffnen: Live Pulse Meter, Scan Counter
- `python analyze_paper_trades.py --plot` lokal ausführen

### Nach 50+ Trades: Auswertung
- Win Rate checken (Ziel: >65%)
- Mark-out Analyse: T+1s, T+5s, T+10s Preisbewegung
  - Positiv = echtes Alpha → weiter
  - Negativ = Toxic Flow → Schwelle auf 0.20% erhöhen
- Momentum-Korrelation: Höhere Schwelle = höhere WR?
- Drawdown vs. Kill-Switch Distanz

### Entscheidung basierend auf Daten:
- WR >65%: Quick Wins einbauen (Event-driven, Volume-Signal, Drawdown-Breaker)
- WR 50-65%: Maker Pivot + Polymarket WS priorisieren
- WR <50%: Strategie fundamental überarbeiten

### Quick Wins (nur wenn WR >65%):
1. Event-driven Signale statt 500ms Sleep (Latenz -200ms)
2. Volume als Signal-Multiplikator (Bid/Ask Qty bereits im WS)
3. Intra-Hour Drawdown Breaker (-5% Rolling 1h)

### Grössere Optimierungen (Phase 3+):
- Polymarket WebSocket statt REST (3s → Sub-100ms)
- Maker Pivot (3.15% Fee → 0% + Rebate)
- Logistic Model kalibrieren mit echten Trade-Daten
- Multi-Timeframe Momentum (5s+10s+60s+300s)
