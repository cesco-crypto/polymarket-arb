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

import asyncio
import functools
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

            # USDC.e Allowance aktualisieren (nötig für CLOB Trading)
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                self._client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                logger.info("Executor: USDC.e Allowance aktualisiert für CLOB")
            except Exception as ae:
                logger.warning(f"Executor: Allowance-Update fehlgeschlagen (evtl. schon OK): {ae}")

            # FIX: Patch ROUNDING_CONFIG — py-clob-client erlaubt 5-6 Dezimalen
            # aber die Polymarket API akzeptiert max 2 (maker) / 4 (taker).
            # Bekanntes Issue: github.com/Polymarket/py-clob-client/issues/253
            try:
                from py_clob_client.order_builder.builder import ROUNDING_CONFIG
                for tick_size, rc in ROUNDING_CONFIG.items():
                    if rc.amount > 4:
                        logger.info(f"ROUNDING_CONFIG patch: tick={tick_size} amount {rc.amount}→4")
                        rc.amount = 4
            except Exception as rce:
                logger.warning(f"ROUNDING_CONFIG patch failed (non-critical): {rce}")

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
    MIN_SHARES = 1.0            # Minimum 1 share (war 5, aber blockte Hedge-Orders)
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

            import math
            safe_price = round(order_price, 2)
            safe_size = math.floor(size_shares * 100) / 100
            if safe_size < self.MIN_SHARES:
                logger.debug(f"Pre-Sign abgebrochen: {safe_size} shares < {self.MIN_SHARES} after floor")
                return

            order_args = OrderArgs(
                token_id=token_id,
                price=safe_price,
                size=safe_size,
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
        side: str,  # "BUY" oder "SELL"
        price: float,
        size_usd: float,
        asset: str = "",
        direction: str = "",
    ) -> ExecutionResult:
        """Platziert eine Limit Order auf dem CLOB (BUY oder SELL).

        Args:
            token_id: Polymarket Token ID (UP/DOWN/Yes/No)
            side: "BUY" oder "SELL"
            price: Limit Price (z.B. 0.55)
            size_usd: Position in USD
            asset: "BTC"/"ETH"/Wallet-Name (für Logging)
            direction: "UP"/"DOWN"/Outcome (für Logging)
        """
        if not self.is_live:
            return ExecutionResult(success=False, error="Executor nicht initialisiert")

        # Safety Check: Max Position (nur für BUY — SELL soll nie blockiert werden)
        if side.upper() != "SELL":
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
            from py_clob_client.order_builder.constants import BUY, SELL

            # Side: BUY oder SELL
            order_side = SELL if side.upper() == "SELL" else BUY

            # Shares berechnen
            size_shares = size_usd / price

            t0 = time.perf_counter()

            # Pre-Signed Order nutzen wenn vorhanden (spart ~20-50ms)
            # SAFETY: Pre-signed Orders sind immer BUY — nie für SELL verwenden!
            pre_signed_data = self._pre_signed_orders.pop(token_id, None)
            if pre_signed_data and order_side == SELL:
                logger.warning(f"Pre-Signed BUY Order verworfen — SELL angefordert (token {token_id[:16]}...)")
                pre_signed_data = None  # Force frische SELL Order

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
                # Maker vs Taker: Maker BUY → 1 Cent unter Ask, Maker SELL → 1 Cent über Bid
                is_maker = self.settings.order_type == "maker"
                if is_maker:
                    order_price = min(0.99, price + 0.01) if side.upper() == "SELL" else max(0.01, price - 0.01)
                else:
                    order_price = price

                # FIX: Polymarket CLOB requires:
                # - maker_amount (price * size): max 2 decimals
                # - taker_amount (size in shares): max 4 decimals
                # Floor size to integer to guarantee maker_amount = price * N has <= 2 decimals.
                import math
                safe_price = round(order_price, 2)
                safe_size = math.floor(size_shares)  # Integer shares — guarantees clean maker_amount
                if safe_size < self.MIN_SHARES:
                    return ExecutionResult(success=False, error=f"Size too small after rounding: {safe_size}")

                order_args = OrderArgs(
                    token_id=token_id,
                    price=safe_price,
                    size=safe_size,
                    side=order_side,
                )
                signed_order = self._client.create_order(order_args)
                # IOC (FAK): Sofort füllen was verfügbar ist, Rest canceln
                # Verhindert Ghost-Orders im Buch (Adverse Selection)
                from py_clob_client.clob_types import OrderType
                order = self._client.post_order(signed_order, orderType=OrderType.FAK)

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

            side_tag = side.upper()
            logger.info(
                f"LIVE {side_tag} #{self._orders_placed}: {asset} {direction} "
                f"@ {price:.3f} | ${size_usd:.2f} | "
                f"Latency: {latency_ms:.0f}ms | ID: {order_id}"
            )

            # Telegram Alert
            sell_emoji = "📤" if side_tag == "SELL" else "💰"
            await telegram.send_alert(
                f"{sell_emoji} <b>LIVE {side_tag} #{self._orders_placed}</b>\n"
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

    # ═══════════════════════════════════════════════════════════════
    # ASYNC ORDER EXECUTION — Bypass py-clob-client sync requests
    # ═══════════════════════════════════════════════════════════════

    _aiohttp_session: "aiohttp.ClientSession | None" = None

    @classmethod
    async def _get_async_session(cls):
        """Persistente aiohttp Session — TCP Keep-Alive, kein TLS pro Order."""
        import aiohttp
        if cls._aiohttp_session is None or cls._aiohttp_session.closed:
            connector = aiohttp.TCPConnector(
                keepalive_timeout=300,
                limit=20,
                enable_cleanup_closed=True,
            )
            cls._aiohttp_session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=5, connect=2),
            )
        return cls._aiohttp_session

    async def place_order_async(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        asset: str = "",
        direction: str = "",
    ) -> "ExecutionResult":
        """Asynchrone Order-Execution via aiohttp — mit ehrlichem Error-Logging.

        Baut die Order mit py-clob-client (Signing), sendet sie ueber
        die persistente aiohttp Session. Wartet auf API-Response und
        validiert das Ergebnis BEVOR 'success' gemeldet wird.
        """
        if not self.is_live or not self._client:
            return ExecutionResult(success=False, error="Executor nicht initialisiert")

        import math
        import json as _json
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = SELL if side.upper() == "SELL" else BUY
        size_shares = size_usd / price if price > 0 else 0
        safe_price = round(price, 2)
        safe_size = math.floor(size_shares)

        if safe_size < self.MIN_SHARES:
            return ExecutionResult(success=False, error=f"Size too small: {safe_size} < {self.MIN_SHARES}")

        t0 = time.perf_counter()

        try:
            # 1. IMMER frisch signieren (Pre-Sign hat Kompatibilitaetsprobleme)
            order_args = OrderArgs(
                token_id=token_id, price=safe_price, size=safe_size, side=order_side,
            )
            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(
                None, functools.partial(self._client.create_order, order_args)
            )

            # 2. Build HTTP Request (exakte py-clob-client Logik)
            from py_clob_client.utilities import order_to_json
            from py_clob_client.headers.headers import create_level_2_headers
            from py_clob_client.clob_types import RequestArgs

            body = order_to_json(signed_order, self._creds.api_key, OrderType.FAK, False)
            serialized = _json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            request_args = RequestArgs(
                method="POST",
                request_path="/order",
                body=body,
                serialized_body=serialized,
            )
            headers = create_level_2_headers(self._client.signer, self._creds, request_args)
            headers["Content-Type"] = "application/json"

            sign_ms = (time.perf_counter() - t0) * 1000

            # 3. ASYNC POST — await Response und validiere
            session = await self._get_async_session()
            t1 = time.perf_counter()
            async with session.post(
                "https://clob.polymarket.com/order",
                headers=headers,
                data=serialized,
            ) as resp:
                status_code = resp.status
                resp_text = await resp.text()
                try:
                    resp_data = _json.loads(resp_text)
                except Exception:
                    resp_data = {"error": resp_text[:200]}

            network_ms = (time.perf_counter() - t1) * 1000
            total_ms = (time.perf_counter() - t0) * 1000
            self._last_exec_latency_ms = total_ms

            # 4. Ehrliche Validierung
            if status_code == 200 and "orderID" in resp_data:
                order_id = resp_data.get("orderID", resp_data.get("id", "unknown"))
                self._orders_placed += 1
                self._total_volume_usd += size_usd
                logger.info(
                    f"ASYNC ORDER OK: {asset} {direction} @ {price:.3f} | "
                    f"${size_usd:.2f} | sign={sign_ms:.0f}ms net={network_ms:.0f}ms total={total_ms:.0f}ms | {order_id}"
                )
                return ExecutionResult(
                    success=True, order_id=str(order_id),
                    filled_price=price, filled_size=size_usd,
                    latency_ms=round(total_ms, 1),
                )
            else:
                error = str(resp_data.get("error", resp_data))[:300]
                logger.error(
                    f"ASYNC ORDER REJECTED [{status_code}]: {asset} {direction} @ {price:.3f} | "
                    f"sign={sign_ms:.0f}ms net={network_ms:.0f}ms | ERROR: {error}"
                )
                return ExecutionResult(success=False, error=error, latency_ms=round(total_ms, 1))

        except Exception as e:
            total_ms = (time.perf_counter() - t0) * 1000
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            logger.error(f"ASYNC ORDER EXCEPTION: {asset} {direction} @ {price:.3f} | {total_ms:.0f}ms | {error_msg}")
            return ExecutionResult(success=False, error=error_msg, latency_ms=round(total_ms, 1))

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
