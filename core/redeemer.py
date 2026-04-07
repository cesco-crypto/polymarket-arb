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
from pathlib import Path
from urllib.request import urlopen, Request

from loguru import logger

# Working Polygon RPCs (tested 02.04.2026, tenderly first — publicnode often 403)
POLYGON_RPCS = [
    "https://polygon.gateway.tenderly.co",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.publicnode.com",
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

    # Gas Pacing: Max baseFee in Gwei bevor Redeemer pausiert
    MAX_BASE_FEE_GWEI = 100
    # Batch Thresholds
    BATCH_MIN_USD = 15.0    # Minimum $15 bevor Flush
    BATCH_TTL_S = 4 * 3600  # Max 4h bevor Force-Flush
    # Cooldown Eskalation
    COOLDOWN_BASE_S = 2 * 3600   # 2h Basis
    COOLDOWN_MAX_S = 48 * 3600   # 48h Cap

    def __init__(self, private_key: str, wallet_address: str):
        self._key = private_key
        self._wallet = wallet_address
        self._w3 = None
        self._ctf = None
        self._last_redeem_time = 0
        self._total_redeemed = 0
        # Dynamischer Cooldown: cid -> {"until": float, "attempts": int}
        self._cooldown_cache: dict[str, dict] = {}
        # Pending Batch: Positions die Pre-Check bestanden, warten auf Flush
        self._pending_batch: list[dict] = []
        self._total_redeemed_usd = 0.0
        self._total_merged = 0
        self._total_merged_usd = 0.0
        self._last_check_time = 0

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

            # redeemable=True genuegt — currentValue kann 0 sein obwohl
            # die Position gewonnen hat (Polymarket API Bug bei resolved markets)
            return [
                p for p in positions
                if p.get("redeemable") and float(p.get("size", 0)) > 0
            ]
        except Exception as e:
            logger.error(f"AutoRedeemer: Position fetch error: {e}")
            return []

    def _get_cooldown(self, cid: str) -> float:
        """Gibt den Cooldown-Zeitpunkt fuer eine condition_id zurueck (0 = kein Cooldown)."""
        entry = self._cooldown_cache.get(cid)
        return entry["until"] if entry else 0

    def _set_cooldown(self, cid: str, title: str, reason: str) -> None:
        """Setzt dynamischen eskalierenden Cooldown fuer eine condition_id."""
        entry = self._cooldown_cache.get(cid, {"until": 0, "attempts": 0})
        attempts = entry["attempts"] + 1
        # Eskalation: 2h → 6h → 18h → 48h (cap)
        cooldown_s = min(self.COOLDOWN_BASE_S * (3 ** (attempts - 1)), self.COOLDOWN_MAX_S)
        self._cooldown_cache[cid] = {"until": time.time() + cooldown_s, "attempts": attempts}
        hours = cooldown_s / 3600
        logger.warning(f"AutoRedeemer: {reason} ({title}) — {hours:.0f}h cooldown (attempt #{attempts})")

    def redeem_all(self) -> dict:
        """Institutioneller Redeemer: Pre-Check → Batch → Gas-Pacing → Flush."""
        from web3 import Web3

        if not self._connect():
            return {"redeemed": 0, "failed": 0, "error": "no web3 connection"}

        # ── EIP-1559 GAS PACING: Pausiere bei Netzwerk-Ueberlast ──
        try:
            latest = self._w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas", 0)
            if base_fee > self.MAX_BASE_FEE_GWEI * 1e9:
                gwei = base_fee / 1e9
                logger.info(f"AutoRedeemer: Gas too high ({gwei:.0f} gwei > {self.MAX_BASE_FEE_GWEI}) — snoozing")
                return {"redeemed": 0, "failed": 0, "snoozed": True, "gas_gwei": round(gwei)}
        except Exception:
            pass  # Gas-Check ist optional, nicht blockierend

        positions = self.get_redeemable_positions()
        if not positions:
            return {"redeemed": 0, "failed": 0, "positions": 0}

        now = time.time()

        # ── PRE-CHECK PHASE: payoutDenominator pruefen, Batch befuellen ──
        for pos in positions:
            cid_hex = pos.get("conditionId", "")
            title = pos.get("title", "")[:40]
            if not cid_hex:
                continue

            # Dynamischer Cooldown Check
            if now < self._get_cooldown(cid_hex):
                continue

            # Bereits im Batch?
            if any(p.get("conditionId") == cid_hex for p in self._pending_batch):
                continue

            # payoutDenominator — isoliertes try/except
            payout = 0
            try:
                cid_bytes = bytes.fromhex(cid_hex[2:] if cid_hex.startswith("0x") else cid_hex)
                payout = self._ctf.functions.payoutDenominator(cid_bytes).call()
            except Exception as e:
                self._set_cooldown(cid_hex, title, f"RPC error: {str(e)[:60]}")
                continue

            if payout == 0:
                self._set_cooldown(cid_hex, title, "Not resolved")
                continue

            # Pre-Check bestanden → ab in den Batch
            pos["_checked_at"] = now
            pos["_cid_bytes"] = cid_bytes
            self._pending_batch.append(pos)

        # ── BATCH FLUSH CHECK ──
        batch_value = sum(float(p.get("currentValue", 0)) + float(p.get("size", 0)) * 0.5
                         for p in self._pending_batch)
        oldest = min((p.get("_checked_at", now) for p in self._pending_batch), default=now)
        batch_age = now - oldest

        should_flush = (
            len(self._pending_batch) > 0
            and (batch_value >= self.BATCH_MIN_USD or batch_age >= self.BATCH_TTL_S)
        )

        if not should_flush:
            pending_count = len(self._pending_batch)
            if pending_count > 0:
                logger.debug(
                    f"AutoRedeemer: {pending_count} pending (${batch_value:.1f}, age {batch_age/60:.0f}min) "
                    f"— waiting for ${self.BATCH_MIN_USD} or {self.BATCH_TTL_S/3600:.0f}h TTL"
                )
            return {"redeemed": 0, "failed": 0, "pending": pending_count, "pending_usd": round(batch_value, 2)}

        # ── FLUSH: Sende einzelne TXs (kein atomic MultiSend) ──
        logger.info(f"AutoRedeemer: FLUSH {len(self._pending_batch)} positions (${batch_value:.1f})")

        wallet = Web3.to_checksum_address(self._wallet)
        redeemed = 0
        failed = 0
        total_value = 0.0

        try:
            current_nonce = self._w3.eth.get_transaction_count(wallet)
        except Exception as e:
            return {"redeemed": 0, "failed": 0, "error": f"nonce error: {e}"}

        flushed = []
        for pos in self._pending_batch:
            cid_hex = pos.get("conditionId", "")
            cid_bytes = pos.get("_cid_bytes", b"")
            value = float(pos.get("currentValue", 0))
            title = pos.get("title", "")[:40]

            if not cid_bytes:
                flushed.append(pos)
                continue

            try:
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
                    tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
                    logger.info(f"AutoRedeemer: ✅ Redeemed ${value:.2f} from {title}")
                    self._cooldown_cache.pop(cid_hex, None)

                    self._log_redemption_to_journal(
                        condition_id=cid_hex,
                        title=pos.get("title", "")[:60],
                        outcome=pos.get("outcome", ""),
                        payout_usd=value,
                        initial_value=pos.get("initialValue", 0),
                        tx_hash_hex=tx_hash_hex,
                    )
                else:
                    failed += 1
                    self._set_cooldown(cid_hex, title, "TX reverted")

                flushed.append(pos)
                current_nonce += 1
                time.sleep(2)

            except Exception as e:
                self._set_cooldown(cid_hex, title, f"TX error: {str(e)[:60]}")
                flushed.append(pos)
                failed += 1
                if "nonce" in str(e).lower():
                    current_nonce += 1

        # Geflushed Positions aus Batch entfernen
        for pos in flushed:
            if pos in self._pending_batch:
                self._pending_batch.remove(pos)

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

    def _log_redemption_to_journal(
        self, condition_id: str, title: str, outcome: str,
        payout_usd: float, initial_value: float, tx_hash_hex: str,
    ) -> None:
        """Schreibt einen 'redeemed' Close-Event ins Trade Journal.

        Passiv: Verknüpft den Redeem mit dem Original-Trade über condition_id.
        Wenn kein matching trade_id gefunden wird, wird der Event trotzdem
        geloggt mit trade_id='REDEEM-UNKNOWN'.
        """
        JOURNAL_PATH = Path("data/trade_journal.jsonl")

        # 1. Sammle ALLE open-Events und bereits geschlossene trade_ids
        open_entries = []     # Alle offenen Trades
        closed_tids = set()   # Trade-IDs die bereits ein close/redeemed Event haben

        try:
            if JOURNAL_PATH.exists():
                with open(JOURNAL_PATH) as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ev = entry.get("event", "")
                        if ev == "open":
                            open_entries.append(entry)
                        elif ev in ("redeemed", "resolved_loss", "close"):
                            closed_tids.add(entry.get("trade_id", ""))
        except Exception as e:
            logger.debug(f"Journal read error: {e}")

        # 2. Finde den BESTEN Match: condition_id + live + noch nicht geschlossen
        matched_trade_id = "REDEEM-UNKNOWN"
        matched_order_type = "unknown"
        matched_source = ""
        matched_entry_price = 0.0

        # Prioritaet: live_order_success=True UND noch nicht geschlossen
        for entry in reversed(open_entries):  # Neueste zuerst
            if entry.get("condition_id") != condition_id:
                continue
            tid = entry.get("trade_id", "")
            if tid in closed_tids:
                continue  # Bereits geschlossen — nicht nochmal matchen
            if not entry.get("live_order_success"):
                continue  # Nur echte Live-Trades matchen
            matched_trade_id = tid
            matched_order_type = entry.get("order_type", "unknown")
            matched_source = entry.get("source_wallet_name", "")
            matched_entry_price = entry.get("executed_price", 0.0)
            break

        # Fallback: Auch nicht-live Trades (Paper) matchen wenn noetig
        if matched_trade_id == "REDEEM-UNKNOWN":
            for entry in reversed(open_entries):
                if entry.get("condition_id") != condition_id:
                    continue
                tid = entry.get("trade_id", "")
                if tid in closed_tids:
                    continue
                matched_trade_id = tid
                matched_order_type = entry.get("order_type", "unknown")
                matched_source = entry.get("source_wallet_name", "")
                matched_entry_price = entry.get("executed_price", 0.0)
                break

        # 3. $0-Payout ohne Match = Verlierer-Token Cleanup (kein falscher Verlust)
        if payout_usd <= 0.001 and matched_trade_id == "REDEEM-UNKNOWN":
            matched_trade_id = "REDEEM-CLEANUP"
            logger.debug(f"Redeemer: $0 payout cleanup for {title[:30]} (no matching trade)")

        # 4. Berechne PnL
        pnl_usd = payout_usd - initial_value if initial_value > 0 else payout_usd

        # 5. Schreibe Close-Event ins JSONL
        close_event = {
            "trade_id": matched_trade_id,
            "event": "redeemed",
            "exit_ts": time.time(),
            "condition_id": condition_id,
            "market_question": title,
            "direction": outcome,
            "order_type": matched_order_type,
            "source_wallet_name": matched_source,
            "executed_price": matched_entry_price,
            "size_usd": initial_value,
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_usd / max(0.01, initial_value) * 100, 2),
            "outcome_correct": payout_usd > 0.001,  # True nur bei echtem Payout
            "payout_usd": round(payout_usd, 4),
            "redeem_tx_hash": tx_hash_hex,
        }

        try:
            JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(JOURNAL_PATH, "a") as f:
                f.write(json.dumps(close_event) + "\n")
            logger.info(
                f"JOURNAL REDEEMED: {matched_trade_id} | "
                f"${payout_usd:.2f} payout, ${pnl_usd:+.2f} PnL | "
                f"{matched_order_type} | {title[:30]}"
            )
        except Exception as e:
            logger.error(f"Journal redeem write error: {e}")

        # 5. Validiere JSONL-Integrität nach Schreibvorgang
        self._validate_journal(JOURNAL_PATH)

    def _validate_journal(self, path: Path) -> bool:
        """Prüft ob die JSONL-Datei strukturell valide ist nach einem Schreibvorgang."""
        try:
            valid = 0
            invalid = 0
            with open(path) as f:
                for i, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            invalid += 1
                            logger.error(f"Journal validation: Line {i} is not a dict")
                        elif "trade_id" not in entry or "event" not in entry:
                            invalid += 1
                            logger.error(f"Journal validation: Line {i} missing trade_id or event")
                        else:
                            valid += 1
                    except json.JSONDecodeError:
                        invalid += 1
                        logger.error(f"Journal validation: Line {i} invalid JSON")
            if invalid > 0:
                logger.error(f"JOURNAL VALIDATION FAILED: {invalid} invalid lines (of {valid + invalid})")
                return False
            return True
        except Exception as e:
            logger.error(f"Journal validation error: {e}")
            return False

    def get_all_positions(self) -> list[dict]:
        """Holt ALLE Positionen (nicht nur redeemable)."""
        try:
            url = f"https://data-api.polymarket.com/positions?user={self._wallet}&limit=200"
            req = Request(url, headers={"User-Agent": "polymarket-arb/2.0"})
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.error(f"AutoRedeemer: Position fetch error: {e}")
            return []

    def get_mergeable_pairs(self) -> list[dict]:
        """Findet Positionen wo wir BEIDE Seiten haben (mergeable).

        Merge = Wir haben Yes UND No desselben Markets → tauschen für $1/Paar.
        Kein Oracle nötig, funktioniert immer sofort.
        """
        from collections import defaultdict
        positions = self.get_all_positions()

        # Group by conditionId
        by_cid = defaultdict(list)
        for p in positions:
            cid = p.get("conditionId", "")
            if cid and p.get("size", 0) > 0:
                by_cid[cid].append(p)

        pairs = []
        for cid, pos_list in by_cid.items():
            if len(pos_list) >= 2:
                # Beide Seiten vorhanden
                outcomes = {p.get("outcomeIndex"): p for p in pos_list}
                if 0 in outcomes and 1 in outcomes:
                    p0 = outcomes[0]
                    p1 = outcomes[1]
                    # Mergeable Pairs: min shares beider Seiten
                    shares_0 = p0.get("size", 0)
                    shares_1 = p1.get("size", 0)
                    merge_shares = min(shares_0, shares_1)
                    merge_value = merge_shares  # 1 share pair = $1 USDC.e
                    if merge_shares >= 0.1:
                        pairs.append({
                            "conditionId": cid,
                            "title": p0.get("title", ""),
                            "outcome_0": p0.get("outcome", ""),
                            "outcome_1": p1.get("outcome", ""),
                            "shares_0": shares_0,
                            "shares_1": shares_1,
                            "merge_shares": merge_shares,
                            "merge_value_usd": round(merge_value, 2),
                            "cost_0": round(p0.get("initialValue", 0), 2),
                            "cost_1": round(p1.get("initialValue", 0), 2),
                        })
        return pairs

    def merge_positions(self) -> dict:
        """Merged alle Positionen wo wir beide Seiten haben.

        CTF redeemPositions mit indexSets=[1,2] merged Yes+No → USDC.e.
        """
        from web3 import Web3

        if not self._connect():
            return {"merged": 0, "error": "no web3 connection"}

        pairs = self.get_mergeable_pairs()
        if not pairs:
            return {"merged": 0, "pairs": 0}

        wallet = Web3.to_checksum_address(self._wallet)
        merged = 0
        total_value = 0.0

        try:
            current_nonce = self._w3.eth.get_transaction_count(wallet)
        except Exception as e:
            return {"merged": 0, "error": f"nonce error: {e}"}

        for pair in pairs:
            cid_hex = pair["conditionId"]
            title = pair["title"][:40]
            value = pair["merge_value_usd"]

            try:
                cid_bytes = bytes.fromhex(cid_hex[2:] if cid_hex.startswith("0x") else cid_hex)
                gas_price = int(self._w3.eth.gas_price * 1.2)

                # redeemPositions mit [1,2] = merge beide Seiten
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
                    merged += 1
                    total_value += value
                    logger.info(f"AutoMerger: ✅ Merged ${value:.2f} from {title}")
                else:
                    logger.warning(f"AutoMerger: ❌ Reverted for {title}")

                current_nonce += 1
                time.sleep(2)

            except Exception as e:
                logger.error(f"AutoMerger: Error merging {title}: {e}")
                if "nonce" in str(e).lower():
                    current_nonce += 1

        self._total_merged += merged
        self._total_merged_usd += total_value

        return {
            "merged": merged,
            "value_usd": round(total_value, 2),
            "total_lifetime_merged": self._total_merged,
        }

    def check_and_collect(self) -> dict:
        """Kompletter Check: Redeem + Merge + Status. Alle 60s aufrufen."""
        self._last_check_time = time.time()

        result = {
            "timestamp": time.time(),
            "redeemed": 0,
            "redeem_usd": 0.0,
            "merged": 0,
            "merge_usd": 0.0,
            "pending_redeem": 0,
            "pending_merge_usd": 0.0,
            "stuck_usd": 0.0,
        }

        # 1. Redeem
        try:
            redeem_result = self.redeem_all()
            result["redeemed"] = redeem_result.get("redeemed", 0)
            result["redeem_usd"] = redeem_result.get("value_usd", 0)
        except Exception as e:
            logger.error(f"AutoCollect redeem error: {e}")

        # 2. Merge
        try:
            merge_result = self.merge_positions()
            result["merged"] = merge_result.get("merged", 0)
            result["merge_usd"] = merge_result.get("value_usd", 0)
        except Exception as e:
            logger.error(f"AutoCollect merge error: {e}")

        # 3. Status — was hängt noch?
        try:
            positions = self.get_all_positions()
            won_not_redeemable = [
                p for p in positions
                if p.get("curPrice", 0) >= 0.95 and not p.get("redeemable")
                and p.get("currentValue", 0) > 0.01
            ]
            result["stuck_usd"] = round(sum(p.get("currentValue", 0) for p in won_not_redeemable), 2)
            result["pending_redeem"] = len([
                p for p in positions
                if p.get("redeemable") and p.get("currentValue", 0) > 0
            ])
            mergeable = self.get_mergeable_pairs()
            result["pending_merge_usd"] = round(sum(p["merge_value_usd"] for p in mergeable), 2)
        except Exception as e:
            logger.error(f"AutoCollect status error: {e}")

        # Log summary
        if result["redeemed"] > 0 or result["merged"] > 0:
            logger.info(
                f"AUTO-COLLECT: Redeemed {result['redeemed']} (${result['redeem_usd']:.2f}) | "
                f"Merged {result['merged']} (${result['merge_usd']:.2f}) | "
                f"Stuck: ${result['stuck_usd']:.2f} | Pending merge: ${result['pending_merge_usd']:.2f}"
            )

        return result

    def stats(self) -> dict:
        return {
            "total_redeemed": self._total_redeemed,
            "total_redeemed_usd": round(self._total_redeemed_usd, 2),
            "total_merged": self._total_merged,
            "total_merged_usd": round(self._total_merged_usd, 2),
            "last_check": self._last_check_time,
            "last_redeem": self._last_redeem_time,
            "connected": self._w3 is not None,
        }
