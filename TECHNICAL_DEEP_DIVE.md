# Polymarket Latency Arbitrage Bot -- Technische Dokumentation

---

## 1. Executive Summary

### Was die Software macht

Dieser Bot betreibt **Polymarket Latency/Oracle Arbitrage** auf kurzlebigen Krypto-Prediction-Markets (5-Minuten- und 15-Minuten-Fenster). Die Kernidee: Binance liefert Krypto-Preise in Echtzeit via WebSocket (Sub-50ms Latenz), waehrend die Orderbuecher auf Polymarket traeger reagieren. Wenn ein signifikantes Momentum auf Binance erkannt wird, kauft der Bot die entsprechende Position (UP oder DOWN) auf Polymarket, bevor der Markt den Preisshift einpreist.

### Kernstrategie in 3 Saetzen

1. **Oracle-Latenz-Ausnutzung**: Binance-WebSocket-Preise dienen als Fruehindikator fuer die Richtung kurzfristiger Krypto-Prediction-Markets auf Polymarket, deren Resolution-Oracle (Chainlink) den Preisbewegungen zeitverzoegert folgt.

2. **Mathematische Edge-Berechnung**: Eine logistische Funktion schaetzt aus dem Binance-Momentum die wahre Gewinnwahrscheinlichkeit, die gegen den Polymarket-Marktpreis, dynamische Gebuehren (max 3.15%) und Half-Kelly-Sizing abgewogen wird -- nur Trades mit positivem Net-Expected-Value nach Gebuehren werden ausgefuehrt.

3. **Defensive Architektur**: Kill-Switches (-20% Tages-Drawdown, -40% Gesamt), 8% Hard-Cap pro Position und Paper-Trading-Validation mit 200-Trade-Ziel vor Live-Einsatz sichern das Kapital ab.

---

## 2. Architektur-Uebersicht

### ASCII-Diagramm der Gesamtarchitektur

```
+---------------------+       +------------------------+
|   BINANCE EXCHANGE  |       |  POLYMARKET GAMMA API  |
|  (WebSocket Stream) |       |   (REST: Slug-Query)   |
+----------+----------+       +-----------+------------+
           |                              |
           | bookTicker (Sub-50ms)        | Slug Discovery + CLOB Books
           v                              v
+----------+----------+       +-----------+------------+
|  BinanceWSOracle    |       |   MarketDiscovery      |
|  - PriceTick Stream |       |   - Slug-Generierung   |
|  - PriceWindow      |       |   - Token-ID Fetch     |
|  - Momentum Calc    |       |   - Orderbook Refresh  |
|  - Latenz-Metriken  |       |   - Auto-Rollover      |
+----------+----------+       +-----------+------------+
           |                              |
           |   +------- Momentum ---------+---- Ask-Preis
           v   v                          v
+----------+---+---------------------------+
|       PolymarketLatencyStrategy          |
|  (Signal Loop: 500ms Takt)               |
|  - Momentum-Check (>0.15%)               |
|  - Zeitfenster-Check (30s-240s)          |
|  - Orderbook-Freshness                   |
+-------------------+----------------------+
                    |
                    v
+-------------------+----------------------+
|       PreTradeCalculator                 |
|  - Probability via Logistic Function     |
|  - Fee Curve: MAX_FEE x (4p(1-p))^2     |
|  - Net EV = p_true / effective_ask - 1   |
|  - Half-Kelly Sizing                     |
|  - EXECUTE / ABORT Entscheidung          |
+-------------------+----------------------+
                    |
       EXECUTE      |        ABORT
       +------------+------------+
       v                         v
+------+--------+        (Signal geloggt,
|  PaperTrader  |         kein Trade)
|  - Position   |
|    eroeffnen  |
|  - Resolution |
|    bei Ablauf |
|  - PnL Track  |
|  - CSV Log    |
+------+--------+
       |
       +----------+-----------+-----------+
       v          v           v           v
+------+--+ +----+-----+ +---+------+ +--+--------+
| Risk    | | Telegram  | | Web      | | CSV/Log   |
| Manager | | Alerts    | | Dashboard| | Files     |
+---------+ +----------+ +----+-----+ +-----------+
                               |
                          WebSocket 1Hz
                               v
                         +-----+------+
                         |  Browser   |
                         |  index.html|
                         |  5-Panel   |
                         |  Bloomberg |
                         |  Layout    |
                         +------------+
```

### Datenfluss von Tick zu Trade

```
Binance WS Tick (bookTicker)
    |
    v
PriceTick(symbol, bid, ask, mid, timestamp)
    |
    v
PriceWindow.add(tick) --> deque(maxlen=500)
    |
    v
PriceWindow.momentum(30s) --> % Aenderung
    |
    v
abs(momentum) >= 0.15%?  ---NO---> skip
    |YES
    v
MarketDiscovery.get_windows_for_asset("BTC")
    |
    v
Fuer jedes tradeable Fenster:
    30s <= seconds_remaining <= 240s?
    orderbook_fresh (<5s)?
    |YES
    v
PreTradeCalculator.evaluate()
    |
    +---> estimate_probability(momentum, time, dir) --> p_true
    +---> polymarket_fee_pct(ask_price) --> fee%
    +---> Net EV = (p_true / (ask * (1+fee/100)) - 1) * 100
    +---> kelly_fraction(p_true, ask) --> f
    +---> position_usd = min(f * capital, 8% * capital)
    |
    v
Net EV > 2.0%?  ---NO---> ABORT
    |YES
    v
PaperTrader.open_position()
    |
    v  (5-15 Min spaeter)
Position.is_expired --> resolve mit Binance-Preis
    |
    v
PnL berechnet, Kapital aktualisiert, CSV geschrieben
```

### Async Event Loop Architektur

Der Bot nutzt eine Single-Event-Loop-Architektur mit `asyncio`. Alle Komponenten laufen als koroutinenbasierte Tasks in derselben Event Loop:

```
asyncio.run() / uvicorn Event Loop
    |
    +-- Task: BinanceWSOracle._stream_symbol("BTC/USDT")
    +-- Task: BinanceWSOracle._stream_symbol("ETH/USDT")
    +-- Task: MarketDiscovery._refresh_loop()      [alle 3s]
    +-- Task: Strategy._signal_loop()               [alle 500ms]
    +-- Task: Strategy._position_resolver_loop()    [alle 5s]
    +-- Task: Strategy._status_loop()               [alle 60s]
    +-- Task: web_ui._broadcast_loop()              [alle 1s]
    +-- Task: web_ui._keep_alive_loop()             [alle 600s]
```

Keine Threads, keine Multiprocessing -- alles kooperatives Multitasking via `await`.

---

## 3. Tech-Stack

| Library | Version | Zweck |
|---------|---------|-------|
| **Python** | 3.11+ | Basis-Runtime; `asyncio`, Type Hints, `match/case` |
| **FastAPI** | >=0.115 | Web-Framework fuer Dashboard-Backend, REST + WebSocket |
| **uvicorn** | >=0.34 | ASGI-Server, startet FastAPI, HTTP/WebSocket-Handling |
| **websockets** | >=12.0 | Binance WebSocket-Client (bookTicker-Stream) |
| **aiohttp** | >=3.9 | Async HTTP-Client fuer Polymarket Gamma API, CLOB API, Telegram API, Keep-Alive |
| **ccxt** | >=4.0 | Unified Exchange API (fuer zukuenftige Cross-Exchange-Erweiterung, aktuell nicht aktiv genutzt) |
| **pydantic** | >=2.0 | Konfigurationsvalidierung, Typsicherheit fuer Settings |
| **pydantic-settings** | >=2.0 | Automatisches Laden von `.env`-Variablen in Settings-Klasse |
| **python-dotenv** | >=1.0 | `.env`-File-Parsing fuer API-Keys und Konfiguration |
| **loguru** | >=0.7 | Structured Logging mit Rotation, Kompression, Farben |
| **rich** | >=13.0 | Terminal-UI-Formatierung (fuer CLI-Modus) |
| **pandas** | >=2.0 | Datenanalyse (fuer `analyze_paper_trades.py`) |

### Warum diese Wahl?

- **FastAPI statt Flask**: Native async/await-Unterstuetzung, eingebautes WebSocket-Handling, automatische OpenAPI-Dokumentation.
- **websockets statt ccxt-WS**: Direkter Zugriff auf Binance bookTicker-Stream ohne Overhead einer Unified-API. Sub-50ms Latenz ist geschaeftskritisch.
- **aiohttp statt requests**: Non-blocking HTTP-Calls fuer Polymarket-API, die im gleichen Event Loop laufen wie der WebSocket-Stream.
- **loguru statt stdlib logging**: Zero-Config, automatische Rotation, `.gz`-Kompression, farbige Console-Ausgabe.
- **pydantic-settings**: Typ-sichere Konfiguration mit automatischem Environment-Variable-Mapping und Validierung.

---

## 4. Module im Detail

### 4.1 Binance WebSocket Oracle (`core/binance_ws.py`)

**Zweck**: Echtzeit-Preisstream von Binance als Momentum-Indikator fuer die Polymarket-Strategie. Liefert Sub-50ms Tick-Daten via WebSocket.

**Klassen und Datenstrukturen**:

```python
@dataclass
class PriceTick:
    symbol: str       # z.B. "BTC/USDT"
    bid: float
    ask: float
    mid: float        # (bid + ask) / 2
    timestamp: float  # Unix seconds (local)

@dataclass
class PriceWindow:
    symbol: str
    maxlen: int = 500
    _ticks: deque       # Rolling Buffer, maxlen=500
    _tick_intervals: deque  # Fuer Latenz-Metriken, maxlen=200
```

**Wichtigste Methoden**:

- `PriceWindow.momentum(window_s)`: Berechnet die prozentuale Preisaenderung ueber die letzten `window_s` Sekunden. Sucht den aeltesten Tick innerhalb des Fensters und berechnet `(current_mid - baseline_mid) / baseline_mid * 100`.
- `PriceWindow.latency_percentiles()`: Gibt P50 und P99 der Tick-Intervalle in Millisekunden zurueck -- essentiell fuer die Ueberwachung der Oracle-Latenz.
- `BinanceWebSocketOracle._stream_symbol(symbol)`: Haelt eine persistente WebSocket-Verbindung mit Auto-Reconnect und Exponential Backoff (1s bis 30s).

**Algorithmus -- Momentum-Berechnung**:

```python
def momentum(self, window_s: float) -> float | None:
    now = self._ticks[-1].timestamp
    cutoff = now - window_s
    # Aeltester Tick innerhalb des Fensters finden
    baseline = next((t for t in self._ticks if t.timestamp >= cutoff), None)
    if baseline is None:
        return None
    return (self._ticks[-1].mid - baseline.mid) / baseline.mid * 100
```

**WebSocket-Protokoll**: Nutzt den `bookTicker`-Stream (`wss://stream.binance.com:9443/ws/{symbol}@bookTicker`), der bei jedem Top-of-Book-Update ein JSON-Paket liefert:

```json
{"u":123, "s":"BTCUSDT", "b":"65000.10", "B":"1.5", "a":"65000.20", "A":"2.0"}
```

**Konfigurierbare Parameter**:
- `symbols`: Liste der zu ueberwachenden Handelspaare (Default: `["BTC/USDT", "ETH/USDT"]`)
- `window_size_s`: Maximale Groesse des Rolling Window (Default: 120s)
- `ping_interval`: WebSocket Ping-Intervall (20s)
- `ping_timeout`: WebSocket Ping-Timeout (10s)
- Reconnect Backoff: 1s initial, max 30s, exponentiell

---

### 4.2 Market Discovery Engine (`core/market_discovery.py`)

**Zweck**: Dynamische Erkennung und Verwaltung ephemerer Polymarket 5m/15m Krypto-Prediction-Markets. Diese Maerkte sind nicht ueber Text-Suche auffindbar -- die Discovery basiert auf mathematischer Slug-Berechnung.

**Datenstrukturen**:

```python
@dataclass
class MarketWindow:
    slug: str                    # z.B. "btc-updown-5m-1711636200"
    asset: str                   # "btc" / "eth"
    timeframe: str               # "5m" / "15m"
    window_start_ts: int         # Unix timestamp Fenster-Start
    window_end_ts: int           # Unix timestamp Fenster-Ende
    condition_id: str            # Polymarket Condition ID
    question: str                # Marktfrage (max 80 Zeichen)
    up_token_id: str             # CLOB Token-ID fuer UP-Shares
    down_token_id: str           # CLOB Token-ID fuer DOWN-Shares
    up_best_bid/ask: float       # Live-Orderbuch (UP)
    down_best_bid/ask: float     # Live-Orderbuch (DOWN)
    up_bid_size/ask_size: float  # Depth am Best Bid/Ask
    orderbook_ts: float          # Letztes Orderbuch-Update
    gamma_up_price: float        # Mid-Price Referenz von Gamma API
    liquidity_usd: float         # Polymarket-Liquiditaet
    fetched: bool                # True wenn Token-IDs geladen
```

**Slug-Generierungs-Algorithmus**:

```python
@staticmethod
def generate_slugs(asset: str, timeframe: str, count: int = 3):
    interval_s = TIMEFRAMES[timeframe]  # 300 fuer 5m, 900 fuer 15m
    now = int(time.time())
    current_start = (now // interval_s) * interval_s  # Floor zum Fenster-Start

    result = []
    for i in range(count):
        start_ts = current_start + (i * interval_s)
        end_ts = start_ts + interval_s
        slug = f"{asset}-updown-{timeframe}-{start_ts}"
        result.append((slug, start_ts, end_ts))
    return result
```

Beispiel: Bei `time()=1711636350` und `timeframe="5m"` (300s):
- `current_start = (1711636350 // 300) * 300 = 1711636200`
- Slug: `btc-updown-5m-1711636200`

**API-Anbindung**:
- **Gamma API** (`https://gamma-api.polymarket.com/events?slug=...`): Liefert Event-Daten, Market-Metadaten, Token-IDs, Outcome-Prices
- **CLOB API** (`https://clob.polymarket.com/book?token_id=...`): Liefert aktuelle Orderbuecher (Bids/Asks)

**Refresh-Loop** (alle 3 Sekunden):
1. Neue Fenster entdecken (Rollover zum naechsten Zeitfenster)
2. Orderbuecher fuer aktive Fenster parallel aktualisieren
3. Abgelaufene Fenster entfernen (30s nach Ablauf)

**Konfigurierbare Parameter**:
- `assets`: Target-Assets (Default: `["btc", "eth"]`)
- `timeframes`: Target-Timeframes (Default: `["5m", "15m"]`)
- `min_liquidity_usd`: Mindest-Liquiditaet (Default: $5000)
- Refresh-Intervall: 3 Sekunden (hardcoded)
- Cleanup-Grace-Period: 30 Sekunden nach Ablauf

---

### 4.3 PreTrade Cost Calculator (`core/pretrade_calculator.py`)

**Zweck**: Entscheidet fuer jeden potenziellen Trade: EXECUTE oder ABORT. Berechnet Gebuehren, Edge, Wahrscheinlichkeit und optimale Positionsgroesse.

**Datenstrukturen**:

```python
class TradeDecision(Enum):
    EXECUTE = "execute"
    ABORT = "abort"

@dataclass
class PreTradeResult:
    asset: str              # "BTC" / "ETH"
    direction: str          # "UP" / "DOWN"
    p_true: float           # Geschaetzte Gewinnwahrscheinlichkeit
    p_market: float         # Polymarket-Preis (Markt-Implied)
    raw_edge_pct: float     # (p_true - p_market) / p_market * 100
    fee_pct: float          # Polymarket-Gebuehr in %
    net_ev_pct: float       # Erwarteter Wert nach Gebuehren
    kelly_fraction: float   # Berechneter Kelly-Anteil
    position_usd: float     # Empfohlene Positionsgroesse
    decision: TradeDecision
    abort_reason: str       # Grund fuer ABORT (leer bei EXECUTE)
```

**Guard Checks** (in dieser Reihenfolge):
1. Zeitfenster: `30s <= seconds_to_expiry <= 240s`
2. Polymarket-Preis: `0 < ask < 1`
3. Verfuegbares Kapital: `>= $10`
4. Edge > 0 (p_true > p_market)
5. Net-EV > 2.0% (nach Gebuehren)
6. Kelly-Fraction > 0

**Konfigurierbare Parameter**:
- `min_seconds_to_expiry`: 30s (zu nah am Ablauf = zu riskant)
- `max_seconds_to_expiry`: 240s (zu weit weg = Momentum veraltet)
- `min_edge_pct`: 2.0% (Mindest-Net-EV nach Gebuehren)
- `polymarket_max_fee_pct`: 3.15% (Peak-Gebuehr bei p=0.50)
- `kelly_fraction`: 0.50 (Half-Kelly)
- `max_position_pct`: 8% (Hard Cap pro Trade)

Die mathematischen Modelle werden in Abschnitt 5 detailliert erlaeutert.

---

### 4.4 Paper Trader (`core/paper_trader.py`)

**Zweck**: Simuliert Polymarket-Trades ohne echtes Kapital. Verwaltet offene/geschlossene Positionen, trackt PnL, fuehrt Kill-Switch-Pruefungen durch und loggt alles in eine CSV-Datei.

**Datenstrukturen**:

```python
@dataclass
class PaperPosition:
    trade_id: str               # "PT-0001", "PT-0002", ...
    asset: str                  # "BTC" / "ETH"
    direction: str              # "UP" / "DOWN"
    market_condition_id: str
    market_question: str
    entry_price: float          # Polymarket-Ask bei Einstieg
    size_usd: float             # Investiertes Kapital
    shares: float               # size_usd / entry_price
    fee_usd: float              # Bezahlte Gebuehr
    entered_at: float           # Unix timestamp
    market_end_timestamp: float # Wann der Markt ablaeuft
    p_true_at_entry: float      # Geschaetzte p_true bei Einstieg
    momentum_at_entry: float    # Binance-Momentum bei Einstieg
    oracle_price_at_entry: float # Binance-Preis als Referenz
    resolved: bool = False
    outcome_correct: bool = False
    pnl_usd: float = 0.0
```

**Resolution-Logik**:

```python
def resolve_position(self, trade_id, oracle_price_now):
    ref_price = position.oracle_price_at_entry  # Binance bei Entry
    actual_up = oracle_price_now > ref_price     # Ist Preis gestiegen?

    if direction == "UP":
        outcome_correct = actual_up
    else:
        outcome_correct = not actual_up

    if outcome_correct:
        gross_payout = shares * 1.0  # $1 pro Share
        pnl = gross_payout - size_usd - fee_usd
    else:
        pnl = -(size_usd + fee_usd)  # Totalverlust + Gebuehr
```

**CSV-Logging-Format** (Datei: `data/paper_trades.csv`):

| Feld | Beispiel |
|------|----------|
| event | open / close |
| trade_id | PT-0001 |
| timestamp | 2026-03-28 14:05:32 |
| asset | BTC |
| direction | UP |
| entry_price | 0.5200 |
| size_usd | 45.00 |
| fee_usd | 1.42 |
| shares | 86.5385 |
| p_true | 0.6500 |
| momentum_pct | 0.2800 |
| pnl_usd | +39.58 (nur bei close) |

**Kill-Switch**: Prueft bei jedem `open_position()`-Aufruf, ob der Tages-Drawdown >= 20% des Initialkapitals erreicht hat.

---

### 4.5 Strategy Orchestrator (`strategies/polymarket_latency.py`)

**Zweck**: Zentraler Orchestrator, der alle Komponenten verbindet und die Haupt-Trading-Loops betreibt.

**Klassen**:

```python
@dataclass
class StrategySignal:
    asset: str
    direction: str
    momentum_pct: float
    binance_price: float
    polymarket_ask: float
    seconds_to_expiry: float
    market_question: str
    detected_at: float
```

**Drei parallele Loops**:

1. **Signal Loop** (500ms Takt):
   - Iteriert ueber BTC, ETH
   - Prueft Oracle-Freshness (max 5s alt)
   - Berechnet Momentum ueber konfiguriertes Fenster (Default: 30s)
   - Bei |momentum| >= 0.15%: evaluiert alle passenden Polymarket-Fenster
   - Fuehrt PreTrade-Analyse durch, loggt Signal (EXECUTE oder ABORT)
   - Bei EXECUTE: oeffnet Paper-Position, sendet Telegram-Alert

2. **Position Resolver Loop** (5s Takt):
   - Prueft alle offenen Positionen auf Ablauf
   - Loest abgelaufene Positionen mit aktuellem Binance-Preis auf
   - Sendet Telegram-Alerts fuer jede Resolution
   - Prueft Drawdown-Warnstufen (5%, 15%)

3. **Status Loop** (60s Takt):
   - Loggt komprimierten Status: Fenster, Signale, Trades, Win Rate, PnL, Kapital

**Komponenten-Initialisierung**:

```python
class PolymarketLatencyStrategy:
    def __init__(self, settings):
        self.oracle = BinanceWebSocketOracle(
            symbols=settings.oracle_symbols,           # ["BTC/USDT", "ETH/USDT"]
            window_size_s=max(momentum_window * 2, 120)
        )
        self.discovery = MarketDiscovery(
            assets=["btc", "eth"],
            timeframes=["5m", "15m"],
            min_liquidity_usd=5000.0
        )
        self.calculator = PreTradeCalculator(settings)
        self.paper_trader = PaperTrader(settings)
        self.risk_manager = RiskManager(settings)
```

**Signal-History**: Die letzten 30 Signal-Evaluationen werden in einer `deque` gespeichert und ans Dashboard weitergegeben (sowohl EXECUTE als auch ABORT).

---

### 4.6 Web Dashboard (`dashboard/web_ui.py` + `index.html`)

**Zweck**: Echtzeit-Monitoring-Dashboard im Bloomberg-Terminal-Stil. Zeigt alle relevanten Metriken auf einem Bildschirm.

**Backend (FastAPI)**:

- `GET /`: Liefert die Single-Page HTML-Datei
- `GET /api/status`: REST-Endpoint fuer Debugging (JSON-Payload)
- `WS /ws`: WebSocket-Endpoint fuer Live-Updates

**Broadcast-Architektur**:

```python
async def _broadcast_loop():
    while True:
        if connected_clients and strategy:
            payload = _build_payload()
            for ws in connected_clients:
                await ws.send_json(payload)
        await asyncio.sleep(1)  # 1Hz Push
```

Disconnected Clients werden automatisch erkannt und aus dem Set entfernt.

**Keep-Alive** (nur auf Render.com):

```python
async def _keep_alive_loop():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return  # Nur auf Render aktiv
    while True:
        await session.get(f"{url}/api/status")
        await asyncio.sleep(600)  # Alle 10 Minuten
```

**Frontend (Single HTML File -- 5-Panel Layout)**:

Das Dashboard verwendet ein CSS Grid Layout mit 5 Panels:

```
+--------------------+---------------------------+------------------+
|  P1: Oracle        |  P2: Active Markets       |  P4: Risk &      |
|  Latency           |  & Rollover               |  Kill Switches   |
|                    |                           |                  |
+--------------------+                           |                  |
|  P3: Live Signals  |                           |                  |
|  & EV Engine       |                           |                  |
+--------------------+---------------------------+------------------+
|  P5: Paper Trading History (Open / Resolved Tabs)                |
+------------------------------------------------------------------+
```

**CSS Grid Definition**:

```css
.grid {
    grid-template-columns: 4fr 5fr 3fr;
    grid-template-rows: auto 1fr auto;
}
.p1 { grid-column: 1; grid-row: 1; }
.p2 { grid-column: 2; grid-row: 1/3; }
.p3 { grid-column: 1; grid-row: 2; }
.p4 { grid-column: 3; grid-row: 1/3; }
.p5 { grid-column: 1/4; grid-row: 3; }
```

**Client-Side Countdown-Interpolation**:

Der Server pusht Daten mit 1Hz. Fuer fluessige Countdown-Animationen interpoliert der Client alle 500ms:

```javascript
setInterval(() => {
    if (lastData) {
        renderMarkets(lastData.discovery || {});
        renderPositions(lastData.trading || {});
    }
}, 500);

// Im Render: Korrektur fuer verstrichene Zeit seit letztem Push
const secs = Math.max(0, w.remaining_s - (Date.now() - (lastData?._t || Date.now())) / 1000);
```

**Design-System (CSS Custom Properties)**:

```css
:root {
    --bg: #0a0c10;           /* Dunkler Hintergrund */
    --bg-card: #13151c;      /* Karten-Hintergrund */
    --accent: #3b82f6;       /* Primaerfarbe (Blau) */
    --green: #22c55e;        /* Positiv/Aktiv */
    --red: #ef4444;          /* Negativ/Fehler */
    --yellow: #eab308;       /* Warnung */
    --font: 'SF Mono', 'JetBrains Mono', monospace;
}
```

---

### 4.7 Telegram Alerts (`utils/telegram.py`)

**Zweck**: Push-Benachrichtigungen ueber den Telegram Bot API fuer kritische Events (Signale, Resolutions, Drawdown-Warnungen, Kill-Switch).

**Architektur**: Modulares Design mit Singleton-Session. Nutzt `aiohttp` direkt statt eines separaten Telegram-Packages.

```python
_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

async def send_alert(message: str, silent: bool = False) -> bool:
    payload = {
        "chat_id": _chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }
    async with _session.post(url, json=payload) as resp:
        return resp.status == 200
```

**Vorgefertigte Alert-Templates**:

| Template | Trigger | Inhalt |
|----------|---------|--------|
| `alert_startup` | Bot-Start | Modus, Kapital, Strategie |
| `alert_shutdown` | Bot-Stop | Trades, PnL, Endkapital |
| `alert_signal` | EXECUTE-Entscheidung | Asset, Direction, Momentum, Ask, EV, Size |
| `alert_resolved` | Position aufgeloest | Trade-ID, Win/Loss, PnL, Kapital |
| `alert_drawdown` | Drawdown >= 5% | Drawdown-%, Kapital |
| `alert_kill_switch` | Drawdown >= 15% | Drawdown-%, Kapital, "Keine weiteren Trades!" |

**Konfiguration**: Ueber `.env`-Variablen `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID`. Wenn leer, werden Alerts automatisch deaktiviert (graceful degradation).

---

### 4.8 Risk Manager (`core/risk_manager.py`)

**Zweck**: Erzwingt Kill-Switches und Circuit Breaker. Schuetzt vor unkontrolliertem Kapitalverlust.

**Bot-Zustaende**:

```python
class BotState(Enum):
    RUNNING = "running"    # Normal
    PAUSED = "paused"      # Tages-Limit erreicht (Reset naechsten Tag)
    SHUTDOWN = "shutdown"  # Gesamt-Limit erreicht (finaler Stop)
```

**Kill-Switch-Hierarchie**:

```
Stufe 1: Max 8% des Portfolios pro Trade
    --> check_order_size(order_size, portfolio_value)

Stufe 2: -20% Tagesverlust --> PAUSED
    --> Automatischer Reset am naechsten Tag

Stufe 3: -40% Gesamtverlust --> SHUTDOWN
    --> Manueller Neustart erforderlich

Stufe 4: Min. Balance < $100
    --> check_balance(balance, exchange_id)
```

**Tagesstatistiken**:

```python
@dataclass
class DailyStats:
    date: str = ""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    peak_pnl_usd: float = 0.0
```

**Cooldown-Mechanismus**: Nach einem Failure Unwind kann ein Cooldown aktiviert werden (Default: 60s), waehrend dessen keine neuen Trades erlaubt sind.

**Tages-Reset-Logik**: Wenn sich das Datum aendert, werden die Tagesstatistiken zurueckgesetzt. Ein PAUSED-Bot wird automatisch auf RUNNING gesetzt; ein SHUTDOWN-Bot bleibt gestoppt.

---

## 5. Mathematische Modelle

### 5.1 Polymarket Fee Curve

**Formel**:

```
fee(p) = MAX_FEE * (4 * p * (1 - p))^2
```

wobei:
- `p` = Preis/Wahrscheinlichkeit des Tokens (0 bis 1)
- `MAX_FEE` = 3.15% (konfigurierbar)
- `4 * p * (1 - p)` = Normalisierung auf 1.0 bei p = 0.50

**Herleitung**: Polymarket berechnet Gebuehren mit `feeRate * (p * (1-p))^exponent`. Fuer kurzfristige Krypto-Maerkte: `feeRate = 0.50`, `exponent = 2`.

Bei `p = 0.50`:
```
0.50 * (0.50 * 0.50)^2 = 0.50 * 0.0625 = 0.03125 = 3.125%
```

Durch Normalisierung mit Faktor 4:
```
fee(p) = MAX_FEE * (4 * p * (1-p))^2
fee(0.50) = 3.15% * (4 * 0.25)^2 = 3.15% * 1.0 = 3.15%
fee(0.80) = 3.15% * (4 * 0.16)^2 = 3.15% * 0.4096 = 1.29%
fee(0.95) = 3.15% * (4 * 0.0475)^2 = 3.15% * 0.0361 = 0.11%
```

**Eigenschaft**: Die Gebuehr ist eine quartische Funktion mit Maximum bei p = 0.50 und schnellem Abfall zu den Raendern. Trades nahe p = 0.50 sind am teuersten, Trades bei extremen Wahrscheinlichkeiten nahezu gebuehrenfrei.

### 5.2 Probability Estimation (Logistische Funktion mit Time-Decay)

**Modell**:

```python
# Zeit-Daempfung
time_factor = min(1.0, 120.0 / max(seconds_to_expiry, 1.0))
effective_momentum = momentum_pct * time_factor

# Logistische Funktion
scaling = 2.0
raw_signal = effective_momentum * scaling
p_up = 1 / (1 + exp(-raw_signal))
```

**Warum logistische Funktion?**
- Bildet reelle Zahlen (Momentum) auf das Intervall (0, 1) ab
- Symmetrisch um 0.50 (kein Bias)
- Steigung kalibrierbar ueber `scaling`-Parameter
- Natuerliche Saettigung: Extreme Momentums fuehren nicht zu unrealistischen Wahrscheinlichkeiten

**Kalibrierungstabelle** (bei `scaling = 2.0`, vollem Time-Factor):

| Momentum | p_up | Interpretation |
|----------|------|----------------|
| +0.15% | 0.574 | Schwellenwert, leichte Ueberzeugung |
| +0.30% | 0.646 | Moderate Edge |
| +0.50% | 0.731 | Starke Edge |
| +1.00% | 0.881 | Sehr starke Edge |
| +2.00% | 0.982 | Nahezu sicher |

**Time-Decay**: Der `time_factor` skaliert das Momentum basierend auf der verbleibenden Zeit:
- Bei 60s verbleibend: `time_factor = min(1.0, 120/60) = 1.0` (volle Staerke)
- Bei 120s verbleibend: `time_factor = min(1.0, 120/120) = 1.0` (volle Staerke)
- Bei 240s verbleibend: `time_factor = min(1.0, 120/240) = 0.5` (halbe Staerke)

Rationale: Je naeher am Marktablauf, desto relevanter ist das aktuelle Momentum fuer den Ausgang.

### 5.3 Half-Kelly Criterion

**Formel**:

```
f_raw = (p_true - p_market) / (1 - p_market)
f_capped = min(f_raw, 0.25)           # Hard Cap bei 25%
f_final = f_capped * kelly_fraction    # * 0.50 = Half-Kelly
```

**Herleitung fuer Binary Markets**: In einem Prediction Market mit Preis `p_market`:
- Gewinn bei korrekter Vorhersage: `(1 - p_market)` pro investiertem Dollar
- Verlust bei falscher Vorhersage: `p_market` pro investiertem Dollar
- Odds: `b = (1 - p_market) / p_market`

Kelly-Formel: `f = (b * p - q) / b` wobei `p = p_true`, `q = 1 - p_true`

Vereinfacht: `f = (p_true - p_market) / (1 - p_market)`

**Beispiel**:
- p_true = 0.70 (wir schaetzen 70% Chance)
- p_market = 0.55 (Polymarket-Preis)
- f_raw = (0.70 - 0.55) / (1 - 0.55) = 0.333 = 33.3%
- f_capped = min(0.333, 0.25) = 0.25
- f_final = 0.25 * 0.50 = 12.5% (aber durch 8% Hard Cap weiter begrenzt)

**Warum Half-Kelly statt Full-Kelly?**
- Full-Kelly maximiert den logarithmischen Nutzen, aber mit hoher Varianz
- Half-Kelly reduziert die Varianz um 75% bei nur 25% weniger Wachstum
- Schuetzt gegen Modell-Unsicherheit (die p_true-Schaetzung ist nicht perfekt)
- In der Praxis: Position = min(Half-Kelly * Kapital, 8% * Kapital)

### 5.4 Net Expected Value Berechnung

**Formel**:

```
effective_ask = polymarket_ask * (1 + fee_pct / 100)
net_ev_pct = (p_true / effective_ask - 1) * 100
```

**Interpretation**: Der `effective_ask` ist der tatsaechliche Preis inklusive Gebuehren. Wenn `p_true / effective_ask > 1`, dann ist der Trade im Erwartungswert profitabel.

**Beispiel**:
- p_true = 0.65
- polymarket_ask = 0.52
- fee_pct = 2.8%
- effective_ask = 0.52 * 1.028 = 0.5346
- net_ev_pct = (0.65 / 0.5346 - 1) * 100 = 21.6%

**Mindest-Edge**: Der Bot handelt nur bei `net_ev_pct > 2.0%` (konfigurierbar). Diese Schwelle beruecksichtigt:
- Modell-Unsicherheit in der p_true-Schaetzung
- Slippage (Orderbuch koennte sich zwischen Evaluierung und Ausfuehrung aendern)
- Latenz der Polymarket-API

---

## 6. Sicherheit & Risk Management

### Kill-Switches

| Kill-Switch | Schwelle | Aktion | Reset |
|-------------|----------|--------|-------|
| Position Size | 8% pro Trade | Trade abgelehnt | Sofort |
| Tages-Drawdown | -20% | Bot PAUSED | Automatisch am naechsten Tag |
| Gesamt-Drawdown | -40% | Bot SHUTDOWN | Manueller Neustart |
| Min. Balance | < $100 | Kein Trading auf dieser Boerse | Wenn Balance wieder > $100 |
| Cooldown | Nach Failure | 60s Pause | Automatisch nach Ablauf |

### Position Sizing Limits

```
position_usd = min(
    kelly_fraction * capital,      # Half-Kelly Empfehlung
    max_position_pct * capital,    # 8% Hard Cap
    max_order_size_usd             # $100 absolutes Limit
)
```

Dreifache Absicherung: Kelly-Mathematik, prozentualer Cap, absoluter Cap.

### API Key Handling

- Alle API-Keys in `.env` (nie im Code)
- `.env` in `.gitignore` (nie im Repository)
- `.env.example` als Template ohne echte Werte
- Pydantic-Settings laedt Keys automatisch aus Environment
- Telegram-Token in Render.com als `sync: false` markiert (kein Plaintext im YAML)

### Telegram Token Sicherheit

```yaml
# render.yaml
envVars:
  - key: TELEGRAM_BOT_TOKEN
    sync: false  # Nicht im YAML gespeichert, manuell in Render-Dashboard setzen
  - key: TELEGRAM_CHAT_ID
    sync: false
```

### .gitignore Strategie

Ignoriert werden:
- `.env`, `.env.local` (API-Keys, Secrets)
- `data/` (Runtime-Daten: CSV-Logs, Bot-Logs)
- `venv/`, `.venv/` (Virtual Environments)
- `__pycache__/` (Python Bytecode)
- `.vscode/`, `.idea/` (IDE-Konfiguration)
- `*.log` (Log-Dateien)

---

## 7. Deployment-Architektur

### Lokale Entwicklung

```bash
# Paper Trading mit Web-Dashboard
python main.py              # Startet auf http://localhost:8000

# Paper Trading CLI-only (ohne Dashboard)
python main.py --cli
```

### Render.com Deployment (Free Tier)

**Build & Start**:
```yaml
buildCommand: pip install -r requirements.txt
startCommand: uvicorn dashboard.web_ui:app --host 0.0.0.0 --port $PORT
```

**Environment Variables** (im Render-Dashboard):
- `MODE=paper`
- `LOG_LEVEL=INFO`
- `TELEGRAM_BOT_TOKEN=...` (manuell, sync: false)
- `TELEGRAM_CHAT_ID=...` (manuell, sync: false)
- `PYTHON_VERSION=3.11.11`

**Procfile** (fuer Heroku-kompatible Plattformen):
```
web: uvicorn dashboard.web_ui:app --host 0.0.0.0 --port $PORT
```

### Keep-Alive Self-Ping

Render Free Tier legt Instanzen nach 15 Minuten Inaktivitaet schlafen. Der Bot pingt sich selbst alle 10 Minuten:

```python
async def _keep_alive_loop():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return  # Nur auf Render aktiv
    while True:
        await session.get(f"{url}/api/status")
        await asyncio.sleep(600)
```

Wird nur aktiviert wenn `RENDER_EXTERNAL_URL` gesetzt ist (automatisch von Render).

### GitHub Integration

- Private Repository
- Auto-Deploy auf Render bei Push auf `main`
- `.gitignore` schuetzt Secrets und Runtime-Daten

---

## 8. Dashboard (5-Panel Bloomberg Layout)

### Panel 1: Oracle Latency

Zeigt den Status der Binance-WebSocket-Verbindungen:

| Spalte | Beschreibung |
|--------|-------------|
| Feed | BTC / ETH mit Status-Dot (gruen/rot) |
| Status | OK / DOWN |
| Price | Letzter Mid-Price |
| P50 | Median Tick-Intervall in ms |
| P99 | 99. Perzentil Tick-Intervall in ms |
| Ticks | Gesamtzahl empfangener Ticks |

Zusaetzlich: Polymarket CLOB Status, Anzahl aktiver Maerkte, Orderbook-Freshness.

**Latenz-Farbkodierung**:
- Gruen: < 50ms (optimal)
- Gelb: 50-200ms (akzeptabel)
- Rot: > 200ms (problematisch)

### Panel 2: Active Markets & Rollover

Zeigt alle entdeckten Polymarket-Fenster:

| Spalte | Beschreibung |
|--------|-------------|
| Market | Asset-Tag (BTC/ETH mit Farbe) |
| TF | Zeitfenster (5m / 15m) |
| Countdown | MM:SS mit Fortschrittsbalken |
| UP | Best Bid/Ask fuer UP-Shares |
| DOWN | Best Bid/Ask fuer DOWN-Shares |
| Liq | Liquiditaet in USD |
| Status | LIVE (handelbar) / QUEUED (wartend) |

Sortierung: Handelbare Fenster zuerst, dann nach verbleibender Zeit aufsteigend.

**Countdown-Farbkodierung**:
- Gruen: > 60s verbleibend
- Gelb: 30-60s verbleibend
- Rot: < 30s verbleibend (nahe Ablauf)

### Panel 3: Signals & EV Engine

**Hero-Section** (grosse Anzeige des letzten Signals):
- Momentum mit Vorzeichen und Farbe
- p_true, p_market, Fee, Net EV
- Decision-Badge: EDGE (gruen) oder NO EDGE (grau)
- Position Size oder Abort-Reason

**Signal-Tabelle** (letzte 15 Evaluationen):

| Spalte | Beschreibung |
|--------|-------------|
| Time | Uhrzeit (HH:MM:SS) |
| Asset | BTC/ETH Tag |
| Dir | UP/DOWN Tag |
| Mom% | Momentum-Prozent |
| EV% | Net Expected Value |
| Fee% | Polymarket-Gebuehr |
| Size | Positionsgroesse (bei EXECUTE) |
| Decision | EXEC (gruen) / SKIP (grau) |

EXECUTE-Signale werden mit gruener linker Rahmenlinie hervorgehoben. ABORT-Signale werden abgedunkelt.

### Panel 4: Risk & Kill Switches

- **Paper Capital**: Aktuelles Kapital mit Farbe (gruen wenn >= Start, rot wenn darunter)
- **Daily P&L**: Tages-PnL mit Farbe
- **Drawdown**: Prozentualer Drawdown mit Fortschrittsbalken (0% bis -20% HALT)
  - Gruen: < 5%
  - Gelb: 5-10%
  - Orange: 10-15%
  - Rot: > 15%
- **Position Sizing**: Aktueller Kelly-Wert, 8% Hard Cap
- **Win Rate**: Prozent mit Farbe (gruen >= 70%, gelb >= 55%, rot darunter)
- **Wins/Losses**: Zaehler
- **Kill Switch**: Status-Dot (gruen = OFF, rot = ACTIVE)

### Panel 5: Paper Trading History

Zwei Tabs:

**Open-Tab**:
| ID | Asset | Side | Entry | Size | p_true | Expires |
|----|-------|------|-------|------|--------|---------|

**Resolved-Tab**:
| ID | Asset | Side | Entry | Size | PnL | Fee | Result | Time |
|----|-------|------|-------|------|-----|-----|--------|------|

### WebSocket 1Hz Push Architecture

```
Server (FastAPI)                Client (Browser)
      |                              |
      |---- JSON payload ----------->|  (1Hz)
      |                              |
      |                              +-- update() aufgerufen
      |                              +-- Alle 5 Panels aktualisiert
      |                              |
      |                              +-- setInterval(500ms)
      |                              |   Countdown-Interpolation
      |                              |   (renderMarkets + renderPositions)
      |                              |
      |<--- receive_text() ----------|  (Keepalive)
```

**Reconnect-Verhalten**: Bei Verbindungsabbruch versucht der Client nach 2 Sekunden automatisch eine neue Verbindung:

```javascript
ws.onclose = () => {
    rt = setTimeout(connect, 2000);
};
```

---

## 9. Design-Entscheidungen & Trade-offs

### Warum REST fuer Orderbuecher statt WebSocket?

Polymarket bietet zwar einen WebSocket-Endpoint fuer das CLOB (Central Limit Order Book), aber:
- Die 5m/15m-Maerkte sind **ephemer** -- alle paar Minuten aendern sich die Token-IDs
- Ein WebSocket-Reconnect mit neuen Subscriptions alle 5 Minuten wuerde die Komplexitaet erhoehen
- Die REST-Latenz von ~100-300ms fuer Orderbook-Fetches ist akzeptabel, da die **Entscheidungslatenz** (Momentum-Erkennung) im Sekundenbereich liegt
- Parallele REST-Calls (`asyncio.gather`) fuer UP + DOWN sind effizient

### Warum Binance als Oracle statt Chainlink direkt?

- **Latenz**: Binance WebSocket liefert Preise in < 50ms. Chainlink on-chain Updates kommen alle ~20-60 Sekunden (Heartbeat) oder bei 0.5% Deviation.
- **Die Strategie nutzt den Latenz-Unterschied**: Binance-Preise eilen Chainlink vor. Wir wetten darauf, dass Chainlink (das Resolution-Oracle) dem Binance-Trend folgt.
- **Kosten**: On-chain Chainlink-Reads erfordern RPC-Provider; Binance WS ist kostenlos.
- **Annahme**: Binance-Chainlink Korrelation > 99.99% fuer BTC/ETH in Minutenzeitfenstern.

### Warum Half-Kelly statt Full-Kelly?

Full-Kelly maximiert den erwarteten logarithmischen Vermoegenszuwachs, aber:
- Unsere `p_true`-Schaetzung ist ein **Modell**, keine exakte Wahrscheinlichkeit
- Full-Kelly fuehrt bei Modell-Fehlern zu **Overbetting** mit katastrophalen Verlusten
- Half-Kelly reduziert die Varianz um ~75% bei nur ~25% weniger Wachstum
- In Kombination mit dem 8% Hard Cap ergibt sich ein konservatives Sizing

### Warum 3.15% Max-Fee statt dynamisch von API?

- Polymarket hat kein oeffentliches API-Endpoint fuer die aktuelle Gebuehrenstruktur
- Die Fee-Formel (`feeRate * (p * (1-p))^exponent`) ist aus der Polymarket-Dokumentation bekannt
- Fuer kurzfristige Krypto-Maerkte: `feeRate = 0.50`, `exponent = 2` ergibt ~3.125% Peak
- Wir nutzen 3.15% als konservativen Aufschlag (leicht ueber der theoretischen Gebuehr)
- Wenn Polymarket die Gebuehrenstruktur aendert, muss nur `polymarket_max_fee_pct` angepasst werden

### Warum min_momentum 0.15% als Schwelle?

- Bei 0.15% Momentum ergibt die logistische Funktion `p_up = 0.574` -- gerade genug Edge fuer einen profitablen Trade nach Gebuehren
- Niedrigere Schwellen (< 0.10%) generieren zu viele Signale mit minimaler Edge -- die 3.15% Gebuehr frisst den Gewinn
- Hoehere Schwellen (> 0.30%) reduzieren die Trade-Frequenz drastisch
- 0.15% ist ein Kompromiss zwischen Signalqualitaet und -frequenz

### Single HTML File vs. React/Vue

- **Zero Build-Toolchain**: Kein npm, kein Webpack, kein Build-Step
- **Deployment-Einfachheit**: Eine Datei, direkt von FastAPI serviert
- **Performance**: Vanilla JavaScript ist schneller als ein Framework-Bundle fuer diesen Use Case
- **Magie unnoetig**: 5 Tabellen und ein paar Kennzahlen rechtfertigen kein SPA-Framework
- **Trade-off**: Keine Komponenten-Wiederverwendung, kein State-Management -- akzeptabel bei dieser Groesse (~430 Zeilen inkl. CSS)

---

## 10. Bekannte Limitierungen

### Paper Trading vs. Live Trading Gaps

- **Slippage**: Paper Trading nimmt den Best Ask als Einstiegspreis an -- in der Realitaet kann der Preis sich zwischen Evaluierung und Order-Ausfuehrung aendern
- **Liquiditaet**: Der Bot prueft `ask_depth_usd`, aber simuliert keine partielle Ausfuehrung (Partial Fill)
- **Latenz**: Die Orderplatzierung auf Polymarket CLOB dauert 200-500ms -- in dieser Zeit kann sich das Orderbuch aendern
- **Resolution**: Paper Trading nutzt Binance-Preis zur Resolution statt Chainlink. In 99.9% der Faelle identisch, aber bei Flash-Crashes koennen Abweichungen auftreten
- **Gebuehren**: Die Fee-Kurve ist statisch konfiguriert (3.15% Peak), nicht live von der API abgefragt

### Chainlink vs. Binance Price Divergenz

- **Normal**: < 0.01% Abweichung fuer BTC/ETH
- **Risiko**: Chainlink aktualisiert bei 0.5% Deviation oder Heartbeat (20-60s). Bei schnellen Preisbewegungen kann Chainlink bis zu 0.5% hinter Binance liegen
- **Konsequenz fuer Paper Trading**: Die Win/Loss-Bestimmung basiert auf Binance, nicht Chainlink. Eine Position koennte im Paper Trading gewinnen, aber auf Chainlink verlieren (oder umgekehrt)
- **Mitigierung**: Nur Trades mit > 2% Net EV, was den 0.5% Chainlink-Lag bei weitem uebersteigt

### Render Free Tier Constraints

- **15-Minuten Sleep**: Ohne den Self-Ping wird die Instanz nach 15 Minuten Inaktivitaet eingeschlafen. Der Keep-Alive-Loop alle 10 Minuten verhindert das, aber:
  - Beim ersten Cold Start gehen 30-60 Sekunden verloren (Container-Boot)
  - WebSocket-Verbindungen werden beim Sleep getrennt
- **750 Free Hours/Monat**: Bei 24/7-Betrieb (720h/Monat) knapp ausreichend
- **512 MB RAM**: Fuer den Bot ausreichend, aber kein Spielraum fuer Memory Leaks
- **Kein Persistent Storage**: `data/paper_trades.csv` geht bei jedem Deploy verloren

### WebSocket Reconnect-Verhalten

**Binance WebSocket (Server-Side)**:
- Auto-Reconnect mit Exponential Backoff (1s bis 30s)
- Waehrend des Reconnects gehen Ticks verloren
- Momentum-Berechnung kann nach Reconnect falsch sein (Luecke im PriceWindow)
- Mitigierung: Tick-Intervalle > 5s werden bei der Latenz-Messung ignoriert

**Dashboard WebSocket (Client-Side)**:
- Auto-Reconnect nach 2 Sekunden
- Dashboard-Dot wechselt sofort auf rot bei Disconnect
- Countdowns frieren waehrend des Reconnects ein
- Nach Reconnect wird sofort ein frischer Payload gepusht

### Weitere Limitierungen

- **Keine Persistenz**: Alle Positionen und Statistiken sind im Arbeitsspeicher. Bei einem Crash gehen offene Positionen verloren (CSV hat nur den Eintrag, keine Auto-Resolution)
- **Kein Retry fuer Polymarket API**: Wenn ein Orderbook-Fetch fehlschlaegt, wird das Fenster beim naechsten 3s-Refresh erneut versucht -- aber ein einzelner Signal-Zyklus koennte eine Opportunity verpassen
- **Keine Rate-Limiting fuer Polymarket**: Theoretisch koennten zu viele parallele Orderbook-Requests (bei vielen aktiven Fenstern) zu Rate Limits fuehren
- **Statische Asset-Liste**: Nur BTC und ETH werden ueberwacht. Neue Assets erfordern Code-Aenderungen im `_STREAM_MAP`
- **Kein Backtesting-Framework**: Historische Daten koennen nicht direkt durch den Bot-Code simuliert werden -- nur Live-Paper-Trading

---

## Anhang: Weitere Module

### `utils/rate_limiter.py`

Generischer Exponential-Backoff-Mechanismus fuer Exchange-API-Calls:

```python
@dataclass
class RateLimiter:
    max_retries: int = 5
    base_delay: float = 1.0
    max_delay: float = 16.0

    def _get_delay(self, exchange_id):
        failures = self._failures.get(exchange_id, 0)
        return min(base_delay * 2^(failures-1), max_delay)
```

Backoff-Sequenz: 1s -> 2s -> 4s -> 8s -> 16s (dann Reset).

### `utils/logger.py`

Konfiguriert Loguru mit zwei Outputs:

1. **Console** (stderr): Kurzformat mit Farben, Level >= INFO
2. **File** (`data/bot.log`): Detailformat mit Zeilennummern, Level >= DEBUG, Rotation bei 10 MB, Retention 7 Tage, gzip-Kompression

### `analyze_paper_trades.py`

Standalone-Analyse-Tool fuer Paper-Trade-Ergebnisse:

- **Momentum-Korrelation**: Gruppiert Trades nach Momentum-Bins (0.15-0.25%, 0.25-0.50%, 0.50-1.00%, >1.00%) und berechnet Win Rate und Avg PnL pro Bin
- **Asset-Breakdown**: Win Rate und PnL getrennt nach BTC/ETH
- **Timeframe-Breakdown**: Win Rate und PnL getrennt nach 5m/15m
- **Equity Curve**: Kumulierte PnL ueber alle Trades
- **Drawdown-Analyse**: Max Drawdown in USD und %, Proximity zum Kill-Switch-Level
- **Validierungskriterium**: >= 200 Trades mit >= 70% Win Rate = bereit fuer Live-Trading

Optional: Matplotlib-Charts (Equity Curve, Win Rate vs. Momentum, PnL Distribution, Cumulative PnL).

### `config.py`

Pydantic-Settings mit allen konfigurierbaren Parametern:

| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `mode` | "paper" | Modus: paper / live |
| `oracle_symbols` | ["BTC/USDT", "ETH/USDT"] | Binance-Symbole fuer Oracle |
| `momentum_window_s` | 30.0 | Sekunden fuer Momentum-Berechnung |
| `min_momentum_pct` | 0.15 | Mindest-Preisbewegung fuer Signal |
| `min_edge_pct` | 2.0 | Mindest-Edge nach Gebuehren (%) |
| `polymarket_max_fee_pct` | 3.15 | Peak-Gebuehr bei p=0.50 (%) |
| `kelly_fraction` | 0.50 | Fractional Kelly (Half-Kelly) |
| `paper_capital_usd` | 1000.0 | Virtuelles Startkapital |
| `max_position_pct` | 0.08 | 8% Hard Cap pro Trade |
| `max_daily_loss_pct` | 0.20 | -20% Tages-Kill-Switch |
| `max_total_loss_pct` | 0.40 | -40% Gesamt-Kill-Switch |
| `min_seconds_to_expiry` | 30.0 | Kein Trade < 30s vor Ablauf |
| `max_seconds_to_expiry` | 240.0 | Kein Trade > 240s vor Ablauf |
| `scan_interval_seconds` | 5.0 | Dashboard-Refresh-Intervall |

---

*Dokumentation generiert am 28.03.2026. Basierend auf der aktuellen Codebase im Verzeichnis `/Users/cesco/CODING/ARBITRAGE TRADING/`.*
