"""Trade receipts: a signed, anchored record of what a trade actually did.

The problem this solves is not "did a transaction happen" — the chain answers
that. It is that a transaction alone does not say what was INTENDED. A swap's
calldata shows an aggregator call; it does not show the price Sarf quoted, the
minimum it promised, what the user was told they were paying, or when. If Sarf
later disagreed with a user about any of that, the chain would not settle it.

So a receipt binds the two together: the terms as Sarf stated them, the
settlement as the chain recorded it, and a signature over both. Three
properties follow.

  Attributable  It is signed with EIP-712 typed data by a key whose address is
                published, so anyone can check Sarf really issued it. A forged
                receipt fails signature recovery.

  Tamper-evident  Change any field and the digest changes, so the signature no
                longer recovers to the signer. There is no version of the
                receipt that says something else and still verifies.

  Anchored      The digest is written into a transaction on X Layer, so the
                receipt provably existed no later than that block. Sarf cannot
                backdate one, because it cannot backdate a block.

Deliberately EVM-only and dependency-free: eth_account for the signature, one
ordinary transaction for the anchor, no contract to deploy and nothing to
trust off this chain. The earlier approach reached for an external service and
a second chain to do the same job.

The signing key is the relayer's. That wallet is gas-only by design — what it
signs here is an attestation, never a transfer of anybody's funds — and its
address is public, which is exactly what a verifier needs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from .config import settings

log = logging.getLogger("sarf.receipts")

CHAIN_ID = 196

# The typed-data shape. Amounts travel as the strings the user was shown
# rather than base units: a receipt is a record of what somebody was told, and
# "0.00640788 SPCXx" is what they were told.
TYPES = {
    "Receipt": [
        {"name": "orderId", "type": "string"},
        {"name": "account", "type": "address"},
        {"name": "action", "type": "string"},
        {"name": "symbol", "type": "string"},
        {"name": "paid", "type": "string"},
        {"name": "receivedAtLeast", "type": "string"},
        {"name": "quotedValueUsdCents", "type": "uint256"},
        {"name": "txHash", "type": "bytes32"},
        {"name": "blockNumber", "type": "uint256"},
        {"name": "quotedAt", "type": "uint256"},
        {"name": "settledAt", "type": "uint256"},
    ]
}

DOMAIN = {"name": "Sarf Trade Receipt", "version": "1", "chainId": CHAIN_ID}


class ReceiptError(RuntimeError):
    pass


def signer_address() -> str | None:
    """Whose signature verifies these, or None if no key is configured."""
    key = settings.relayer_private_key
    return Account.from_key(key).address if key else None


def build(order: dict[str, Any], *, block_number: int | None,
          settled_at: float) -> dict[str, Any]:
    """The canonical message. Every field comes from what was recorded at the
    time — nothing is recomputed from live prices, because a receipt that
    changes when the market moves is not a receipt."""
    # The human-readable terms sit at the top level of a built order; the
    # nested `display` dict is a second home some paths use. Read both, in
    # that order, and never fall back to base units — "1000000" is not a term
    # anybody was shown, and a receipt that says it is a receipt of nothing.
    disp = order.get("display") or {}

    def term(*names: str) -> str:
        for n in names:
            v = order.get(n) if order.get(n) is not None else disp.get(n)
            if v not in (None, ""):
                return str(v)
        return ""

    tx = order.get("tx_hash") or ""
    if not tx:
        raise ReceiptError("an unsigned order has nothing to attest")
    usd = order.get("est_usd")
    return {
        "orderId": str(order["order_id"]),
        "account": to_checksum_address(order["address"]),
        "action": term("side") or "swap",
        "symbol": term("symbol"),
        "paid": term("spending"),
        "receivedAtLeast": term("minimum_received", "receiving_estimated"),
        # Cents, as an integer: a float in a hashed message is a different
        # hash on a different machine.
        "quotedValueUsdCents": int(round(float(usd) * 100)) if usd is not None else 0,
        "txHash": tx if tx.startswith("0x") else "0x" + tx,
        "blockNumber": int(block_number or 0),
        "quotedAt": int(order.get("created_at") or 0),
        "settledAt": int(settled_at),
    }


def digest(message: dict[str, Any]) -> str:
    """The EIP-712 digest — what the signature is over, and what gets anchored."""
    signable = encode_typed_data(domain_data=DOMAIN, message_types=TYPES,
                                 message_data=message)
    return "0x" + keccak(b"\x19" + signable.version + signable.header
                         + signable.body).hex()


def sign(message: dict[str, Any]) -> tuple[str, str, str]:
    """-> (digest, signature, signer). Raises if no key is configured."""
    key = settings.relayer_private_key
    if not key:
        raise ReceiptError("no signing key configured; receipts are disabled")
    acct = Account.from_key(key)
    signable = encode_typed_data(domain_data=DOMAIN, message_types=TYPES,
                                 message_data=message)
    signed = acct.sign_message(signable)
    return digest(message), "0x" + signed.signature.hex().removeprefix("0x"), acct.address


def verify(message: dict[str, Any], signature: str, expect_signer: str) -> bool:
    """True when this exact message was signed by that address.

    The check anyone auditing a receipt performs, and the one the tests use to
    prove a tampered field cannot survive.
    """
    try:
        signable = encode_typed_data(domain_data=DOMAIN, message_types=TYPES,
                                     message_data=message)
        got = Account.recover_message(signable, signature=signature)
    except Exception:
        return False
    return got.lower() == expect_signer.lower()


def verification_note(signer: str, anchor_tx: str | None) -> dict[str, Any]:
    """How to check this without trusting Sarf, stated on the receipt itself."""
    return {
        "standard": "EIP-712 typed data",
        "domain": DOMAIN,
        "types": TYPES,
        "signer": signer,
        "how": [
            "Recompute the EIP-712 digest from `domain`, `types` and `receipt`.",
            "Recover the signer from `signature` — it must equal `signer` above.",
            ("Read the anchor transaction's input data: it contains the same digest, "
             "so the receipt existed no later than that block.")
            if anchor_tx else
            "This receipt is signed but not yet anchored on-chain.",
        ],
        "anchor_tx": anchor_tx,
    }


async def anchor(digest_hex: str) -> str:
    """Write the digest into a transaction on X Layer. -> its hash.

    An ordinary self-send carrying the digest as calldata. No contract, no
    event, nothing to deploy or audit: the transaction's input data is public
    and permanent, and its block gives the receipt a time it cannot predate.
    At X Layer gas prices this costs about eight ten-thousandths of a cent.
    """
    from .xlayer import rpc  # local import: rpc pulls in config at module load

    key = settings.relayer_private_key
    if not key:
        raise ReceiptError("no relayer configured; receipts cannot be anchored")
    from .xlayer import delegation  # shares the relayer, so shares its nonce

    acct = Account.from_key(key)
    async with delegation.NONCE_LOCK:
        nonce = await rpc.transaction_count(acct.address)
        gas_price = await rpc.gas_price()
        tx = {
            "to": acct.address, "value": 0, "data": digest_hex,
            "chainId": CHAIN_ID, "nonce": nonce,
            # 21k for the send plus 16 per non-zero calldata byte; a round 40k
            # covers it with room and costs nothing extra when unused.
            "gas": 40_000,
            "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": min(gas_price, 10 ** 8),
        }
        signed = acct.sign_transaction(tx)
        return await rpc.send_raw_transaction(
            "0x" + signed.raw_transaction.hex().removeprefix("0x"))


async def issue(db, order: dict[str, Any], *, block_number: int | None,
                settled_at: float) -> dict[str, Any] | None:
    """Mint, store and anchor the receipt for a settled order.

    Idempotent on the order id: an order settles once, and a second call must
    return the receipt that already exists rather than signing a new one over
    the same trade.
    """
    existing = db.get_receipt(order["order_id"])
    if existing:
        return existing
    if not signer_address():
        return None
    try:
        message = build(order, block_number=block_number, settled_at=settled_at)
        dig, signature, signer = sign(message)
    except Exception:
        log.warning("could not build a receipt for %s", order.get("order_id"),
                    exc_info=True)
        return None
    # Stored before anchoring: a receipt that is signed but not yet anchored
    # is still worth something, and losing it to a failed send would be worse
    # than anchoring late.
    db.put_receipt(order_id=order["order_id"], address=order["address"],
                   payload=message, digest=dig, signature=signature, signer=signer)
    try:
        tx = await anchor(dig)
        db.set_receipt_anchor(order["order_id"], tx)
    except Exception:
        # Anchoring is the weakest link — it needs a live node and a funded
        # relayer — and it must never cost the user their receipt.
        log.warning("receipt %s signed but not anchored", order["order_id"],
                    exc_info=True)
    return db.get_receipt(order["order_id"])


def canonical_json(message: dict[str, Any]) -> str:
    """Byte-stable rendering, for anyone hashing the receipt by hand."""
    return json.dumps(message, sort_keys=True, separators=(",", ":"))
