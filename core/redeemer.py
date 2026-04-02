"""Auto-Redeemer — Löst gewonnene Polymarket Conditional Tokens automatisch ein.

Nach jeder Market Resolution werden die Tokens über den CTF Contract
zurück in USDC.e konvertiert. Läuft als periodischer Task alle 5 Minuten.

Architektur:
1. Fragt Polymarket Data API nach redeemable Positionen
2. Ruft redeemPositions() auf dem CTF Contract auf
3. USDC.e fliesst zurück ins EOA Trading Wallet
"""

from __future__ import annotations

import json
import time
from urllib.request import urlopen, Request

from loguru import logger

# Working Polygon RPCs (tested 02.04.2026)
POLYGON_RPCS = [
    "https://polygon.publicnode.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.gateway.tenderly.co",
]

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class AutoRedeemer:
    """Periodisch gewonnene Positionen redeemen."""

    def __init__(self, private_key: str, wallet_address: str):
        self._key = private_key
        self._wallet = wallet_address
        self._w3 = None
        self._ctf = None
        self._last_redeem_time = 0
        self._total_redeemed = 0
        self._total_redeemed_usd = 0.0

    def _connect(self):
        """Lazy Web3 Connection mit Fallback RPCs."""
        if self._w3 is not None:
            return True

        try:
            from web3 import Web3
            for rpc in POLYGON_RPCS:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                    if w3.is_connected():
                        self._w3 = w3
                        self._ctf = w3.eth.contract(
                            address=Web3.to_checksum_address(CTF_ADDRESS),
                            abi=CTF_ABI,
                        )
                        logger.info(f"AutoRedeemer: Connected to {rpc}")
                        return True
                except Exception:
                    continue
        except ImportError:
            logger.warning("AutoRedeemer: web3 not installed — redeem disabled")
        return False

    def get_redeemable_positions(self) -> list[dict]:
        """Holt alle redeemable Positionen von der Polymarket Data API."""
        try:
            url = f"https://data-api.polymarket.com/positions?user={self._wallet}&limit=50"
            req = Request(url, headers={"User-Agent": "polymarket-arb/2.0"})
            with urlopen(req, timeout=10) as resp:
                positions = json.loads(resp.read())

            return [
                p for p in positions
                if p.get("redeemable") and p.get("currentValue", 0) > 0
            ]
        except Exception as e:
            logger.error(f"AutoRedeemer: Position fetch error: {e}")
            return []

    def redeem_all(self) -> dict:
        """Redeemed alle gewonnenen Positionen. Returns stats."""
        from web3 import Web3

        if not self._connect():
            return {"redeemed": 0, "failed": 0, "error": "no web3 connection"}

        positions = self.get_redeemable_positions()
        if not positions:
            return {"redeemed": 0, "failed": 0, "positions": 0}

        wallet = Web3.to_checksum_address(self._wallet)
        redeemed = 0
        failed = 0
        total_value = 0.0

        try:
            current_nonce = self._w3.eth.get_transaction_count(wallet)
        except Exception as e:
            return {"redeemed": 0, "failed": 0, "error": f"nonce error: {e}"}

        for pos in positions:
            cid_hex = pos.get("conditionId", "")
            value = pos.get("currentValue", 0)
            title = pos.get("title", "")[:40]

            if not cid_hex:
                continue

            try:
                cid_bytes = bytes.fromhex(cid_hex[2:] if cid_hex.startswith("0x") else cid_hex)

                # Check if resolved
                payout = self._ctf.functions.payoutDenominator(cid_bytes).call()
                if payout == 0:
                    continue

                gas_price = int(self._w3.eth.gas_price * 1.2)

                tx = self._ctf.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_E),
                    b"\x00" * 32,
                    cid_bytes,
                    [1, 2],
                ).build_transaction({
                    "from": wallet,
                    "nonce": current_nonce,
                    "gas": 300000,
                    "gasPrice": gas_price,
                    "chainId": 137,
                })

                signed = self._w3.eth.account.sign_transaction(tx, self._key)
                tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

                if receipt.status == 1:
                    redeemed += 1
                    total_value += value
                    logger.info(f"AutoRedeemer: ✅ Redeemed ${value:.2f} from {title}")
                else:
                    failed += 1
                    logger.warning(f"AutoRedeemer: ❌ Reverted for {title}")

                current_nonce += 1
                time.sleep(2)

            except Exception as e:
                logger.error(f"AutoRedeemer: Error redeeming {title}: {e}")
                failed += 1
                if "nonce" in str(e).lower():
                    current_nonce += 1

        self._total_redeemed += redeemed
        self._total_redeemed_usd += total_value
        self._last_redeem_time = time.time()

        return {
            "redeemed": redeemed,
            "failed": failed,
            "value_usd": round(total_value, 2),
            "total_lifetime_redeemed": self._total_redeemed,
            "total_lifetime_usd": round(self._total_redeemed_usd, 2),
        }

    def stats(self) -> dict:
        return {
            "total_redeemed": self._total_redeemed,
            "total_usd": round(self._total_redeemed_usd, 2),
            "last_redeem": self._last_redeem_time,
            "connected": self._w3 is not None,
        }
