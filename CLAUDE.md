# CLAUDE.md — Projektanweisungen für Claude Code

## Projekt
Cross-Exchange Spot-Arbitrage Bot (Python). Kauft Crypto auf der billigeren Börse, verkauft gleichzeitig auf der teureren. Pre-funded balances auf allen Börsen.

## Arbeitsverzeichnis
ALLE Dateien NUR in: /Users/cesco/CODING/ARBITRAGE TRADING/

## Aktueller Stand
Phase 1: Scanner — Echtzeit-Spread-Erkennung über Binance, KuCoin, Bybit. KEIN Echtgeld.

## Tech-Stack
- Python 3.11+, async/await durchgängig
- ccxt (async_support) für Börsen-APIs
- rich für Terminal-UI
- pydantic für Config/Validation
- loguru für Logging
- python-dotenv für API-Keys

## Architektur
6 Module: market_data.py, scanner.py, cost_calculator.py, executor.py, portfolio.py, risk_manager.py

## Kritische Regeln
1. Kill-Switches VOM ERSTEN COMMIT: max 8% Portfolio/Trade, -20% Tageslimit, -40% Gesamtlimit
2. Adaptive Throttling: Exponential Backoff bei Rate Limits
3. Failure Unwind: Einseitiger Fill → sofort Market-Order-Close
4. NIEMALS API-Keys hardcoden → .env
5. Break-Even Spread ~0.3% → nur Trades darüber
6. Alle Orders als Limit-Orders, Timeout 3 Sekunden

## Vollständiges Briefing
Siehe CLAUDE_CODE_BRIEFING.md im Projektroot für alle Details zu Strategie, Gebühren, Architektur, Phasenplan und Code-Patterns.
