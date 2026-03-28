# ARBITRAGE TRADING BOT — Vollständiges Projektbriefing für Claude Code
# =====================================================================
# Dieses Dokument enthält ALLES, was Claude Code braucht, um das Projekt
# im Ordner ~/CODING/ARBITRAGE TRADING/ aufzubauen.
# 
# ANWEISUNG AN CLAUDE CODE:
# 1. Lies dieses gesamte Dokument ZUERST
# 2. Erstelle ALLE Dateien NUR in: /Users/cesco/CODING/ARBITRAGE TRADING/
# 3. Beginne mit Phase 1 (Scanner) — kein Echtgeld
# =====================================================================

---

## 1. PROJEKTZIEL

Ein Python-basierter Cross-Exchange Spot-Arbitrage-Bot, der:
- Preisunterschiede desselben Coins zwischen zentralisierten Börsen (CEXs) erkennt
- Coins identifiziert, wo die Marge am höchsten ist (mit angemessenem Volumen)
- Auf der einen Börse kauft und auf der anderen verkauft
- Auf alle Kosten achtet (Gebühren, Slippage), damit der Trade profitabel bleibt
- Auf einem MacBook läuft (Python, keine spezielle Hardware nötig)

WICHTIG: Das ist KEINE MEV/Blockchain-Arbitrage. Der Bot handelt ausschließlich
über CEX-APIs (Binance, KuCoin, Bybit etc.) — keine Smart Contracts, keine
Builder, kein Gas-Optimierung.

---

## 2. STRATEGIE: "Pre-Funded Dual-Balance"

Der Bot transferiert KEINE Coins zwischen Börsen während des Tradings.
Stattdessen:

1. Guthaben wird VOR dem Start auf allen Börsen verteilt (z.B. 33% pro Börse)
2. Wenn BTC auf Binance billiger ist als auf KuCoin:
   - KAUFE BTC auf Binance (aus dem dortigen USDT-Bestand)
   - VERKAUFE BTC auf KuCoin (aus dem dortigen BTC-Bestand)
   - Beide Trades quasi-gleichzeitig (asyncio.gather)
3. Periodisch (alle 4-8h) werden Bestände manuell rebalanciert
   via günstige Chains (Tron für USDT, Solana, Arbitrum)

---

## 3. ARCHITEKTUR — 6 MODULE

### Modul 1: Market Data Collector (WebSocket + REST)
- Verbindet sich per WebSocket mit Orderbüchern aller Zielbörsen
- Empfängt Echtzeit best-bid/best-ask Daten
- Normalisiert in einheitliches internes Format
- Fallback: REST-Polling wenn WebSocket ausfällt
- Bibliothek: ccxt (mit ccxt.pro für WebSockets wenn verfügbar)

### Modul 2: Opportunity Scanner
- Vergleicht kontinuierlich: ask_A vs bid_B für jedes Paar auf jeder Börsenkombination
- Spread-Formel: spread_pct = (bid_higher - ask_lower) / ask_lower * 100
- Filter:
  - Mindest-Spread nach Gebühren (konfigurierbar, default 0.3%)
  - Mindest-24h-Volumen (default 50,000 USD auf beiden Seiten)
  - Orderbuch-Tiefe am besten Bid/Ask (genug für geplante Ordergröße)
  - Verfügbares Guthaben auf beiden Börsen prüfen

### Modul 3: Cost Calculator
- Berechnet für jeden potenziellen Trade:
  - Maker/Taker-Gebühren beider Börsen (aus ccxt exchange.fees)
  - Slippage-Schätzung basierend auf Orderbuch-Tiefe
  - Anteilige Rebalancing-Kosten
- Nur Trades mit positivem Netto-Profit werden weitergeleitet
- Break-Even-Spread typisch: 0.20-0.33%

### Modul 4: Order Executor
- Sendet zwei Limit-Orders gleichzeitig via asyncio.gather():
  - Kauf auf billigerer Börse
  - Verkauf auf teurerer Börse
- Timeout: Order nicht filled in 3 Sekunden → Cancel
- Partial Fill Handling: Wenn nur eine Seite filled → sofort Market-Order
  auf der anderen Seite zum Schließen (Failure Unwind Logic)
- Alle Orders als Limit-Orders (nicht Market) für Kostenkontrolle

### Modul 5: Portfolio & Rebalancing Manager
- Überwacht Bestände auf allen Börsen in Echtzeit
- Erkennt Drift (z.B. 80%/20% statt 50%/50%)
- Löst Alert aus wenn Rebalancing nötig ist
- Phase 1-3: Manuelle Rebalancing-Alerts
- Phase 4+: Automatisierung via LI.FI API (später)

### Modul 6: Dashboard & Monitoring
- Phase 1-2: Terminal-UI (Python rich/textual)
- Phase 3+: FastAPI Web-Dashboard
- Phase 4+: Prometheus + Grafana
- Zeigt: Spreads, Balances, P&L, Trade History, Errors

---

## 4. TECH-STACK

### Sprache: Python 3.11+
### Kern-Bibliotheken:
```
ccxt>=4.0          # Unified API für 100+ Krypto-Börsen
aiohttp>=3.9       # Async HTTP
websockets>=12.0   # WebSocket-Verbindungen
pandas>=2.0        # Datenanalyse
python-dotenv>=1.0 # Sichere API-Key-Verwaltung
rich>=13.0         # Terminal-UI
pydantic>=2.0      # Datenvalidierung & Settings
loguru>=0.7        # Strukturiertes Logging
```

### Spätere Erweiterungen (NICHT in Phase 1):
```
fastapi + uvicorn     # Web-Dashboard (Phase 3)
prometheus_client     # Monitoring (Phase 4)
```

---

## 5. PROJEKTSTRUKTUR

```
ARBITRAGE TRADING/
├── .env.example              # Template für API-Keys
├── .gitignore                # Python + .env
├── README.md                 # Projektbeschreibung
├── requirements.txt          # Python Dependencies
├── config.py                 # Zentrale Konfiguration (Pydantic Settings)
├── main.py                   # Entry Point
│
├── core/
│   ├── __init__.py
│   ├── market_data.py        # Modul 1: WebSocket/REST Datensammler
│   ├── scanner.py            # Modul 2: Opportunity Scanner
│   ├── cost_calculator.py    # Modul 3: Gebühren & Slippage
│   ├── executor.py           # Modul 4: Order Execution
│   ├── portfolio.py          # Modul 5: Balance & Rebalancing
│   └── risk_manager.py       # Kill-Switches, Circuit Breaker
│
├── strategies/
│   ├── __init__.py
│   ├── spot_arbitrage.py     # Cross-Exchange Spot-Arbitrage
│   └── funding_rate.py       # Delta-neutral Funding (Phase 4)
│
├── dashboard/
│   ├── __init__.py
│   └── terminal_ui.py        # Rich-basiertes Terminal-Dashboard
│
├── utils/
│   ├── __init__.py
│   ├── logger.py             # Loguru Setup
│   ├── rate_limiter.py       # Adaptive Throttling / Exponential Backoff
│   └── kelly.py              # Kelly-Kriterium Positionsgrößen
│
├── data/
│   ├── trades.csv            # Trade-History (wird generiert)
│   └── spreads.log           # Spread-Logs (wird generiert)
│
└── tests/
    ├── __init__.py
    ├── test_scanner.py
    ├── test_cost_calculator.py
    └── test_executor.py
```

---

## 6. KONFIGURATION (.env und config.py)

### .env.example:
```
# Binance
BINANCE_API_KEY=your_key_here
BINANCE_SECRET=your_secret_here

# KuCoin
KUCOIN_API_KEY=your_key_here
KUCOIN_SECRET=your_secret_here
KUCOIN_PASSPHRASE=your_passphrase_here

# Bybit
BYBIT_API_KEY=your_key_here
BYBIT_SECRET=your_secret_here

# Bot Settings
MODE=paper              # paper | live
LOG_LEVEL=INFO
```

### config.py Kernwerte:
```python
# Börsen
EXCHANGES = ["binance", "kucoin", "bybit"]

# Mindest-Spread nach Gebühren (in Prozent)
MIN_PROFITABLE_SPREAD = 0.30

# Mindest-24h-Volumen pro Börse (in USD)
MIN_VOLUME_USD = 50_000

# Maximale Ordergröße (in USD)
MAX_ORDER_SIZE_USD = 100  # Phase 1: Klein anfangen!

# Maximaler Anteil des Portfolios pro Trade
MAX_PORTFOLIO_PCT_PER_TRADE = 0.08  # 8% (Kelly-adjusted)

# Kill-Switch Limits
MAX_DAILY_LOSS_PCT = 0.20    # -20% → Stop
MAX_TOTAL_LOSS_PCT = 0.40    # -40% → Shutdown

# Order Timeout
ORDER_TIMEOUT_SECONDS = 3

# Rebalancing Alert Schwellenwert
REBALANCE_DRIFT_PCT = 0.30   # Alert wenn >30% Drift

# Quote Currency
QUOTE_CURRENCY = "USDT"
```

---

## 7. GEBÜHRENSTRUKTUR DER ZIELBÖRSEN

| Börse    | Spot Maker | Spot Taker | Discount        | Withdrawal USDT (TRC20) |
|----------|-----------|-----------|-----------------|------------------------|
| Binance  | 0.10%     | 0.10%     | -25% mit BNB    | 1.0 USDT               |
| KuCoin   | 0.10%     | 0.10%     | -20% mit KCS    | 0.5 USDT (manchmal 0)  |
| Bybit    | 0.10%     | 0.10%     | VIP-Stufen      | 1.0 USDT               |

Break-Even bei Taker/Taker: 0.10% + 0.10% = 0.20% Mindest-Spread
Mit Discounts: ~0.15% Mindest-Spread
Sicherheitsmarge: Mindestens 0.30% Spread targetieren

---

## 8. RISIKOMANAGEMENT (VOM ERSTEN TAG AN!)

### Kill-Switches (MUSS in risk_manager.py):
1. Max 8% des Portfolios pro einzelnem Trade
2. Automatischer Handelsstopp bei -20% Tagesverlust
3. Finaler Kill-Switch bei -40% Gesamtverlust seit Start
4. Kein Trading wenn Guthaben auf einer Börse < 100 USD

### Failure Unwind Logic (MUSS in executor.py):
- Trade-Seite A filled, Seite B NICHT filled innerhalb Timeout:
  → Sofortiger Market-Order-Close von Seite A
  → Logging des Vorfalls
  → 60-Sekunden-Cooldown bevor nächster Trade

### Adaptive Throttling (MUSS in rate_limiter.py):
- Bei "Rate Limit Exceeded" Error:
  → Exponential Backoff: 1s → 2s → 4s → 8s → 16s
  → Max 5 Retries, dann Skip
  → ccxt enableRateLimit=True als Basis

### Balance Drift Protection:
- Prüfe vor jedem Trade: Ist genug Guthaben auf BEIDEN Börsen?
- Wenn Drift > 30%: Warnung, keine neuen Trades bis rebalanciert

---

## 9. KELLY-KRITERIUM FÜR POSITIONSGRÖSSEN

Formel (Fractional Kelly, 50%):
```python
def calculate_position_size(win_rate, avg_win, avg_loss, portfolio_value):
    if avg_win == 0:
        return 0
    kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
    kelly = max(0, min(kelly, 0.25))  # Cap bei 25%
    fractional_kelly = kelly * 0.5     # Halbes Kelly
    position = portfolio_value * fractional_kelly
    max_position = portfolio_value * 0.08  # Hard Cap 8%
    return min(position, max_position)
```

Benötigt: Laufendes Trade-Log mit Win/Loss-Statistiken (min. 50 Trades
für zuverlässige Schätzung). Vorher: Feste Positionsgröße verwenden.

---

## 10. ENTWICKLUNGSPHASEN

### PHASE 1: Scanner (Woche 1-2) — WO WIR JETZT STARTEN
- [ ] Projektstruktur anlegen in /Users/cesco/CODING/ARBITRAGE TRADING/
- [ ] ccxt-Verbindung zu Binance, KuCoin, Bybit (read-only API Keys)
- [ ] Alle gemeinsamen USDT-Handelspaare identifizieren
- [ ] Echtzeit-Spread-Berechnung (ask vs bid über alle Börsenkombinationen)
- [ ] Gebühren-Berechnung pro Paar
- [ ] Netto-Spread nach Gebühren berechnen
- [ ] Filtern nach Mindest-Spread und Mindest-Volumen
- [ ] Terminal-Dashboard: Top-20 Opportunitäten, aktualisiert alle 5 Sekunden
- [ ] Spread-Logging in CSV für spätere Analyse
- [ ] Kill-Switches und Adaptive Throttling implementieren

### PHASE 2: Paper Trading (Woche 3-4)
- [ ] Simulierter Order-Executor (echte Preise, fake Orders)
- [ ] P&L-Tracking mit realistischen Gebühren
- [ ] Trade-History-Logging
- [ ] Kelly-Kriterium-Integration
- [ ] 200-Trade-Validierung: Win Rate muss >70% sein

### PHASE 3: Live mit Kleingeld (Woche 5-6)
- [ ] Echte API-Keys mit Trading-Berechtigung (KEIN Withdrawal!)
- [ ] 200-500 USD Startkapital, aufgeteilt auf 3 Börsen
- [ ] Max. 10-50 USD pro Trade
- [ ] Failure Unwind Logic live testen
- [ ] Tägliche Performance-Auswertung

### PHASE 4: Skalierung (Ab Monat 2)
- [ ] Kapital auf 5.000-20.000 USD erhöhen
- [ ] Funding-Rate-Arbitrage als Zweitstrategie
- [ ] OKX Unified Account für Kapitaleffizienz
- [ ] Prometheus + Grafana Monitoring
- [ ] 2-3 weitere Börsen (OKX, Gate.io, MEXC)

---

## 11. COIN-AUSWAHL STRATEGIE

Der Bot soll NICHT auf feste Coins setzen, sondern dynamisch scannen:

### Tier 1: Große Coins (BTC, ETH, SOL) — Basis
- Spreads: 0.05-0.3%
- Extrem liquide, kaum Slippage
- Für hohes Volumen, viele kleine Trades

### Tier 2: Mid-Cap Altcoins (AVAX, LINK, DOGE, XRP, ADA) — Sweet Spot
- Spreads: 0.2-1.0%
- Gute Liquidität, weniger Bot-Konkurrenz
- HIER LIEGT DER HAUPTFOKUS

### Tier 3: Small-Caps — Höchste Margen, höchstes Risiko
- Spreads: 1-15% (bei neuen Listings, Flash Events)
- Dünne Liquidität, hohes Slippage-Risiko
- Nur mit kleinen Positionen und striktem Risikomanagement

Der Scanner muss ALLE verfügbaren Paare scannen und die profitabelsten
automatisch identifizieren. Die Coin-Auswahl ändert sich stündlich.

---

## 12. WICHTIGE CCXT-PATTERNS

### Exchange-Initialisierung:
```python
import ccxt.async_support as ccxt

exchange = ccxt.binance({
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_SECRET'),
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})
```

### Gemeinsame Paare finden:
```python
async def find_common_pairs(exchanges):
    all_markets = {}
    for ex in exchanges:
        markets = await ex.load_markets()
        all_markets[ex.id] = set(
            s for s, m in markets.items()
            if m['quote'] == 'USDT' and m['active']
        )
    common = set.intersection(*all_markets.values())
    return common
```

### Spread-Berechnung mit Orderbuch:
```python
async def get_spread(exchange_a, exchange_b, symbol):
    ob_a, ob_b = await asyncio.gather(
        exchange_a.fetch_order_book(symbol, limit=5),
        exchange_b.fetch_order_book(symbol, limit=5)
    )
    # Kann ich auf A kaufen (ask) und auf B verkaufen (bid)?
    ask_a = ob_a['asks'][0][0] if ob_a['asks'] else None
    bid_b = ob_b['bids'][0][0] if ob_b['bids'] else None
    
    if ask_a and bid_b and bid_b > ask_a:
        spread_pct = (bid_b - ask_a) / ask_a * 100
        return {'buy_on': exchange_a.id, 'sell_on': exchange_b.id,
                'spread_pct': spread_pct, 'ask': ask_a, 'bid': bid_b}
    
    # Umgekehrt prüfen
    ask_b = ob_b['asks'][0][0] if ob_b['asks'] else None
    bid_a = ob_a['bids'][0][0] if ob_a['bids'] else None
    
    if ask_b and bid_a and bid_a > ask_b:
        spread_pct = (bid_a - ask_b) / ask_b * 100
        return {'buy_on': exchange_b.id, 'sell_on': exchange_a.id,
                'spread_pct': spread_pct, 'ask': ask_b, 'bid': bid_a}
    
    return None
```

---

## 13. MARKTKONTEXT (Recherche-Ergebnisse März 2026)

### Realistische Erwartungen:
- Typische profitabe Spreads 2026: 0.1-2% (gelegentlich 5-15% bei Altcoins)
- Break-Even nach Gebühren: ~0.20-0.33%
- Bei 10.000 USD Kapital, moderate Strategie: 200-600 USD/Monat (2-6%)
- Bei Volatilitätsspitzen: 5-15% möglich in wenigen Stunden
- In Seitwärtsmärkten: nahe Null oder leicht negativ

### Warum das MacBook reicht:
- API-Roundtrip zu Binance aus Europa: ~100-300ms
- Kein Mikrosekunden-Wettbewerb (das ist CEX-Spot, nicht MEV)
- 2-4 GB RAM für 5 Börsen × 200 Paare
- Bot wartet 99% der Zeit auf API-Responses

### Spätere Strategie-Erweiterungen (NICHT in Phase 1):
1. Delta-neutrale Funding-Rate-Arbitrage (Spot long + Perp short)
2. Triangulare Arbitrage (3 Paare auf einer Börse)
3. OKX/Binance Unified Account für Kapitaleffizienz
4. LI.FI API für automatisiertes Cross-Chain Rebalancing
5. Prometheus/Grafana für professionelles Monitoring

---

## 14. CLAUDE CODE ANWEISUNGEN

1. ALLE Dateien NUR in: /Users/cesco/CODING/ARBITRAGE TRADING/
2. Beginne mit Phase 1 (Scanner) — KEIN Echtgeld-Trading
3. Verwende async/await durchgängig (asyncio + ccxt.async_support)
4. Implementiere Kill-Switches und Throttling VOM ERSTEN COMMIT AN
5. Schreibe sauberen, gut dokumentierten Code mit Type Hints
6. Verwende Pydantic für Konfiguration und Datenvalidierung
7. Logge ALLES (Loguru) — Spreads, Fehler, Performance
8. Terminal-UI mit rich für Phase 1 Dashboard
9. Tests schreiben für kritische Logik (Scanner, Cost Calculator)
10. .env für alle API-Keys, NIEMALS hardcoded

STARTE MIT: Projektstruktur anlegen + Scanner (Modul 1 + 2) +
Terminal-Dashboard das die Top-20 Arbitrage-Opportunitäten
in Echtzeit anzeigt, gefiltert nach Profitabilität nach Gebühren.
