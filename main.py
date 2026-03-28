"""Entry Point — Polymarket Latency Arbitrage Bot.

Verwendung:
  python main.py        → Web-Dashboard (Polymarket Strategy, Port 8000)
  python main.py --cli  → CLI-only Paper-Trading (ohne Dashboard)
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from config import settings
from utils.logger import setup_logger


def run_web_dashboard() -> None:
    """Startet den FastAPI Web-Dashboard-Server."""
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "dashboard.web_ui:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


async def run_polymarket_strategy() -> None:
    """Startet die Polymarket Latency Arbitrage Strategy (Paper Trading)."""
    from strategies.polymarket_latency import PolymarketLatencyStrategy

    setup_logger(level=settings.log_level, data_dir=settings.data_dir)
    logger.info("=" * 60)
    logger.info("POLYMARKET LATENCY ARBITRAGE — PAPER TRADING")
    logger.info(f"Startkapital: ${settings.paper_capital_usd:.0f} USDT (simuliert)")
    logger.info(f"Momentum-Fenster: {settings.momentum_window_s}s")
    logger.info(f"Min Momentum: {settings.min_momentum_pct}%")
    logger.info(f"Min Edge nach Gebühren: {settings.min_edge_pct}%")
    logger.info(f"Kill-Switch: -{settings.max_daily_loss_pct:.0%} Tages-Drawdown")
    logger.info("=" * 60)

    strategy = PolymarketLatencyStrategy(settings)
    shutdown_event = asyncio.Event()

    def handle_signal(sig: int, frame) -> None:
        logger.info(f"Signal {sig} — Shutdown...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    strategy_task = asyncio.create_task(strategy.run())

    await shutdown_event.wait()
    strategy_task.cancel()
    await asyncio.gather(strategy_task, return_exceptions=True)

    # Abschlussbericht
    status = strategy.get_status()
    pt = status["paper_trading"]
    logger.info("=" * 60)
    logger.info("ABSCHLUSSBERICHT PAPER TRADING")
    logger.info(f"Signale erkannt:  {status['signals_detected']}")
    logger.info(f"Trades total:     {pt['trades_total']}")
    logger.info(f"Trades closed:    {pt['trades_closed']}")
    logger.info(f"Win Rate:         {pt['win_rate_pct']:.1f}%")
    logger.info(f"Avg PnL/Trade:    ${pt['avg_pnl_usd']:+.4f}")
    logger.info(f"Tages-PnL:        ${pt['daily_pnl_usd']:+.2f}")
    logger.info(f"Return:           {pt['total_return_pct']:+.2f}%")
    logger.info(f"Endkapital:       ${pt['capital_usd']:.2f}")
    logger.info(f"Trades-Log:       {settings.data_dir}/paper_trades.csv")
    logger.info("=" * 60)


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(run_polymarket_strategy())
    else:
        run_web_dashboard()
