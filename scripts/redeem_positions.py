"""Redeem winning Polymarket Conditional Token positions.

Calls redeemPositions() directly on the CTF contract for each winning condition.
"""
import os
import sys
import json
import time
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")

from web3 import Web3

KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
WALLET = os.getenv("POLYMARKET_FUNDER", "")

# Working Polygon RPC endpoints
RPCS = [
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

# All condition IDs from our 7 positions (5 wins + 2 losses)
CONDITIONS = [
    ("0xff1b2f10296fc90eb9ddebed084011ca140362763f7ea6abe02671b387604066", "BTC UP +$8.51"),
    ("0x96373c1d0d2f26d39a59b4dd7fc4922df63fd53cf4160400eb6f49bbd350a6df", "ETH DOWN +$6.90"),
    ("0x7ee32335fa654790aec78e0f9c5bf90d73988f61a428898e6d6fc2c730ed02cf", "ETH DOWN +$5.00"),
    ("0x2addf59f8e85213e69c117e5e5c67a390c451c2b8fa014f1dbc6cfa22a411037", "BTC DOWN +$5.39"),
    ("0xc8b77aaff713460159e7ced2d438eb15b69934584480e39dd0a3bd2dda89c552", "ETH DOWN +$4.73"),
    # Losses (redeem returns $0 but cleans up the position)
    ("0xdd81226e992f73c294039f89c7c5cbf4240b6ba1957e1c28ab6dfae13561d317", "ETH UP -$4.99"),
    ("0x18e7c3f0cd5b81d5bb6b2060ce02def40d3e9e41e23a9dd2f5cd9e63c7d62e9f", "ETH DOWN -$2.92"),
]


def connect():
    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                print(f"Connected to {rpc} (chain {w3.eth.chain_id})")
                return w3
        except Exception:
            continue
    print("ERROR: Could not connect to any RPC")
    sys.exit(1)


def main():
    if not KEY or not WALLET:
        print("ERROR: POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER not set")
        sys.exit(1)

    w3 = connect()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    wallet = Web3.to_checksum_address(WALLET)

    bal_before = w3.eth.get_balance(wallet)
    print(f"Wallet: {wallet}")
    print(f"POL Balance: {bal_before / 1e18:.4f}")
    print(f"Conditions to redeem: {len(CONDITIONS)}")
    print()

    redeemed = 0
    failed = 0
    current_nonce = w3.eth.get_transaction_count(wallet)
    print(f"Starting nonce: {current_nonce}")
    print()

    for cid_hex, label in CONDITIONS:
        cid_bytes = bytes.fromhex(cid_hex[2:])

        # Check if resolved
        try:
            payout = ctf.functions.payoutDenominator(cid_bytes).call()
            if payout == 0:
                print(f"  SKIP {label}: not yet resolved")
                continue
            print(f"  {label}: resolved (payout_denom={payout})")
        except Exception as e:
            print(f"  SKIP {label}: check error: {e}")
            continue

        # Redeem
        try:
            gas_price = w3.eth.gas_price
            # Add 20% to gas price for faster confirmation
            gas_price = int(gas_price * 1.2)

            tx = ctf.functions.redeemPositions(
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

            signed = w3.eth.account.sign_transaction(tx, KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  TX sent: {tx_hash.hex()[:20]}... (nonce={current_nonce})")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                print(f"  ✅ SUCCESS: {label} | Gas: {receipt.gasUsed}")
                redeemed += 1
            else:
                print(f"  ❌ REVERTED: {label}")
                failed += 1

            current_nonce += 1
            time.sleep(2)  # Wait between TXs

        except Exception as e:
            print(f"  ❌ ERROR: {label} | {type(e).__name__}: {str(e)[:150]}")
            failed += 1
            # Still increment nonce if TX was sent
            if "already known" in str(e).lower() or "nonce" in str(e).lower():
                current_nonce += 1

    print(f"\nDone: {redeemed} redeemed, {failed} failed")


if __name__ == "__main__":
    main()
