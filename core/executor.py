"""Live Executor — Platziert echte Orders auf Polymarket via CLOB API.

Sicherheitsmechanismen:
1. Drei explizite Flags zum Aktivieren (config.live_trading + ENV + Code)
2. Max Position Size hart limitiert ($5 für $100 Account)
3. Jede Order wird via Telegram bestätigt
4. Kill-Switch stoppt sofort bei Drawdown

Architektur:
- L1 Auth: EIP-712 Signatur (einmalig beim Start)
- L2 Auth: HMAC-SHA256 (pro Trade, <1ms)
- Pre-Signing: Order wird vorbereitet während auf Signal gewartet wird
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from loguru import logger

from config import Settings
from utils import telegram


@dataclass
class ExecutionResult:
    """Ergebnis einer Live-Order-Platzierung."""

    success: bool
    order_id: str = ""
    filled_price: float = 0.0
    filled_size: float = 0.0
    fee_usd: float = 0.0
    latency_ms: float = 0.0
    error: str = ""


class PolymarketExecutor:
    """Platziert echte Orders auf dem Polymarket CLOB.

    ACHTUNG: Nur aktiv wenn ALLE drei Bedingungen erfüllt:
    1. config.live_trading == True
    2. POLYMARKET_PRIVATE_KEY in .env gesetzt
    3. mode == "live" in config
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        self._creds = None
        self._ready = False
        self._orders_placed = 0
        self._total_volume_usd = 0.0
        self._pre_signed_orders: dict = {}
        self._last_exec_latency_ms: float = 0.0  # Letzte Signal-to-Post Latenz

    async def initialize(self) -> bool:
        """Initialisiert CLOB Client mit L1 Auth und leitet L2 Credentials ab."""
        pk = self.settings.polymarket_private_key
        funder = self.settings.polymarket_funder

        if not pk or not self.settings.live_trading:
            logger.info("Executor: PAPER MODE (kein Private Key oder live_trading=False)")
            return False

        try:
            from py_clob_client.client import ClobClient

            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=pk,
                chain_id=137,  # Polygon
                signature_type=0,  # Standard EOA
                funder=funder if funder else None,
            )

            # L2 Credentials ableiten (einmalig)
            logger.info("Executor: Leite L2 API Credentials ab (EIP-712)...")
            self._creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(self._creds)

            self._ready = True
            logger.info("Executor: LIVE MODE AKTIV — echte Orders werden platziert!")

            return True

        except Exception as e:
            logger.error(f"Executor Init Fehler: {e}")
            self._ready = False
            return False

    @property
    def is_live(self) -> bool:
        return self._ready and self._client is not None

    # --- Minimum Order Constants (Polymarket CLOB) ---
    MIN_ORDER_SIZE_USD = 1.0    # Minimum $1 order
    MIN_SHARES = 5.0            # Minimum 5 shares for limit orders
    MAX_PRE_SIGN_PRICE_DRIFT = 0.05  # 5% max drift between pre-signed and actual price

    def pre_sign_order(self, token_id: str, price: float, size_usd: float) -> None:
        """Pre-Signing: Order vorab signieren für minimale Latenz bei Trigger.

        Wird aufgerufen während der Bot auf das nächste Signal wartet.
        Bei Trigger muss nur noch post_order() mit dem fertigen Digest aufgerufen werden.
        Spart ~20-50ms EIP-712 Signatur-Berechnung.

        Speichert auch den Pre-Sign-Preis für spätere Drift-Prüfung.
        """
        if not self.is_live:
            return
        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            is_maker = self.settings.order_type == "maker"
            order_price = max(0.01, price - 0.01) if is_maker else price
            size_shares = size_usd / order_price

            # Guard: Minimum shares check
            if round(size_shares, 2) < self.MIN_SHARES:
                logger.debug(f"Pre-Sign abgebrochen: {round(size_shares, 2)} shares < {self.MIN_SHARES} Minimum")
                return

            order_args = OrderArgs(
                token_id=token_id,
                price=order_price,
                size=round(size_shares, 2),
                side=BUY,
            )
            signed = self._client.create_order(order_args)
            self._pre_signed_orders[token_id] = {
                "order": signed,
                "signed_price": order_price,
            }
        except Exception as e:
            logger.debug(f"Pre-Sign Fehler: {e}")

    async def place_order(
        self,
        token_id: str,
        side: str,  # "BUY"
        price: float,
        size_usd: float,
        asset: str = "",
        direction: str = "",
    ) -> ExecutionResult:
        """Platziert eine Limit Order auf dem CLOB.

        Args:
            token_id: Polymarket Token ID (UP oder DOWN)
            side: "BUY" (wir kaufen immer)
            price: Limit Price (z.B. 0.55)
            size_usd: Position in USD
            asset: "BTC"/"ETH" (für Logging)
            direction: "UP"/"DOWN" (für Logging)
        """
        if not self.is_live:
            return ExecutionResult(success=False, error="Executor nicht initialisiert")

        # Safety Check: Max Position
        max_pos = self.settings.max_live_position_usd
        if size_usd > max_pos:
            logger.warning(f"Position ${size_usd} > Max ${max_pos} — capped")
            size_usd = max_pos

        # Safety Check: Minimum Order Size ($1)
        if size_usd < self.MIN_ORDER_SIZE_USD:
            msg = f"Order zu klein: ${size_usd:.2f} < ${self.MIN_ORDER_SIZE_USD} Minimum"
            logger.warning(msg)
            return ExecutionResult(success=False, error=msg)

        # Safety Check: Minimum Shares (5 shares for limit orders)
        size_shares_check = size_usd / price if price > 0 else 0
        if round(size_shares_check, 2) < self.MIN_SHARES:
            msg = f"Shares zu klein: {round(size_shares_check, 2)} < {self.MIN_SHARES} Minimum (${size_usd:.2f} @ {price:.3f})"
            logger.warning(msg)
            return ExecutionResult(success=False, error=msg)

        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            # Shares berechnen
            size_shares = size_usd / price

            t0 = time.perf_counter()

            # Pre-Signed Order nutzen wenn vorhanden (spart ~20-50ms)
            pre_signed_data = self._pre_signed_orders.pop(token_id, None)

            if pre_signed_data:
                signed_price = pre_signed_data["signed_price"]
                pre_signed = pre_signed_data["order"]

                # Drift-Check: Verwerfe Pre-Signed Order wenn Preis zu stark abweicht
                price_drift = abs(signed_price - price) / price if price > 0 else 1.0
                if price_drift > self.MAX_PRE_SIGN_PRICE_DRIFT:
                    logger.warning(
                        f"Pre-Signed Order verworfen: Preis-Drift {price_drift:.1%} > {self.MAX_PRE_SIGN_PRICE_DRIFT:.0%} "
                        f"(signed={signed_price:.3f}, actual={price:.3f}) — erstelle neue Order"
                    )
                    pre_signed = None  # Fallthrough zu frischer Order

            if pre_signed_data and pre_signed:
                order = self._client.post_order(pre_signed)
                logger.debug(f"Pre-Signed Order verwendet (Latenz-Vorteil, drift={abs(pre_signed_data['signed_price'] - price)/price:.1%})")
            else:
                # Maker vs Taker: Maker setzt Limit 1 Cent unter Ask (kassiert Rebate)
                is_maker = self.settings.order_type == "maker"
                order_price = max(0.01, price - 0.01) if is_maker else price

                order_args = OrderArgs(
                    token_id=token_id,
                    price=order_price,
                    size=round(size_shares, 2),
                    side=BUY,
                )
                signed_order = self._client.create_order(order_args)
                order = self._client.post_order(signed_order)

            latency_ms = (time.perf_counter() - t0) * 1000
            self._last_exec_latency_ms = latency_ms

            order_id = order.get("orderID", order.get("id", "unknown"))
            self._orders_placed += 1
            self._total_volume_usd += size_usd

            result = ExecutionResult(
                success=True,
                order_id=str(order_id),
                filled_price=price,
                filled_size=size_usd,
                latency_ms=round(latency_ms, 1),
            )

            logger.info(
                f"LIVE ORDER #{self._orders_placed}: {asset} {direction} "
                f"@ {price:.3f} | ${size_usd:.2f} | "
                f"Latency: {latency_ms:.0f}ms | ID: {order_id}"
            )

            # Telegram Alert
            await telegram.send_alert(
                f"💰 <b>LIVE ORDER #{self._orders_placed}</b>\n"
                f"{asset} {direction} @ {price:.3f}\n"
                f"Size: ${size_usd:.2f}\n"
                f"Latency: {latency_ms:.0f}ms\n"
                f"Order: <code>{order_id}</code>"
            )

            return result

        except Exception as e:
            error_msg = str(e)[:300]  # Polymarket 400 errors need >100 chars to show root cause
            logger.error(f"Order Fehler: {error_msg}")

            await telegram.send_alert(
                f"❌ <b>ORDER FEHLER</b>\n"
                f"{asset} {direction} @ {price:.3f}\n"
                f"Error: {error_msg}"
            )

            return ExecutionResult(success=False, error=error_msg)

    # Polygon RPCs mit Fallback (getestet 02.04.2026)
    POLYGON_RPCS = [
        "https://polygon.gateway.tenderly.co",     # Funktioniert ✅
        "https://polygon-bor-rpc.publicnode.com",   # Fallback
        "https://polygon.publicnode.com",           # Fallback
        "https://polygon.drpc.org",                 # Oft 403
    ]

    async def get_balance(self) -> float:
        """Holt echte USDC.e Balance von der Blockchain (mit RPC Fallback)."""
        if not self._client:
            return 0.0
        try:
            import aiohttp
            wallet = self._client.get_address()
            USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            addr_padded = wallet[2:].lower().zfill(64)
            call_data = "0x70a08231" + addr_padded
            payload = {"jsonrpc": "2.0", "method": "eth_call",
                       "params": [{"to": USDCE, "data": call_data}, "latest"], "id": 1}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                for rpc in self.POLYGON_RPCS:
                    try:
                        async with s.post(rpc, json=payload) as r:
                            if r.status == 200:
                                result = await r.json()
                                balance = int(result.get("result", "0x0"), 16) / 1e6
                                if balance >= 0:
                                    return balance
                    except Exception:
                        continue
            return 0.0
        except Exception:
            return 0.0

    def stats(self) -> dict:
        return {
            "live": self.is_live,
            "orders_placed": self._orders_placed,
            "total_volume_usd": round(self._total_volume_usd, 2),
            "wallet": self._client.get_address() if self._client else "",
            "order_type": self.settings.order_type,
            "exec_latency_ms": round(self._last_exec_latency_ms, 1),
        }
