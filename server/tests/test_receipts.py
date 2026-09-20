"""Trade receipts: signed, tamper-evident, anchored, issued once.

The property under test is not "a receipt exists" but "a receipt that says
something other than what happened cannot be made to verify".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sarf import receipts
from sarf.db import Database

ADDR = "0xf0139a53d77246ee6cc506753784034a3da74039"
KEY = "0x" + "11" * 32          # a throwaway signer for the tests
TX = "0x" + "ab" * 32


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def signing(monkeypatch):
    # Settings is frozen, so the module's reference to it is what gets swapped.
    monkeypatch.setattr(receipts, "settings",
                        SimpleNamespace(relayer_private_key=KEY, public_url=""))
    return receipts.signer_address()


def _order(**over):
    """The shape /api/order/{id} actually returns: the readable terms sit at
    the top level. An earlier version of the receipt builder looked for them
    under `display`, found nothing, and fell back to base units — a live
    receipt went out reading "paid: 1000000"."""
    o = {"order_id": "sarf_ord_test", "address": ADDR, "tx_hash": TX, "est_usd": 12.34,
         "created_at": 1789900000, "side": "buy", "symbol": "SPCXx",
         "spending": "12.34 USDT", "amount_in": "12340000",
         "receiving_estimated": "0.0810 SPCXx", "minimum_received": "0.0806 SPCXx"}
    o.update(over)
    return o


def test_a_receipt_verifies_only_as_issued(signing):
    msg = receipts.build(_order(), block_number=71_000_000, settled_at=1789900100)
    dig, sig, signer = receipts.sign(msg)
    assert signer == signing
    assert receipts.verify(msg, sig, signer)

    # Every field is load-bearing: change any one and the signature is no
    # longer over the thing it is attached to.
    for field, worse in [("paid", "0.01 USDT"),
                         ("receivedAtLeast", "99 SPCXx"),
                         ("account", "0x000000000000000000000000000000000000dEaD"),
                         ("quotedValueUsdCents", 1),
                         ("txHash", "0x" + "cd" * 32),
                         ("settledAt", 1789999999)]:
        tampered = dict(msg, **{field: worse})
        assert receipts.digest(tampered) != dig, field
        assert not receipts.verify(tampered, sig, signer), field

    # And somebody else's signature does not pass as ours.
    other = receipts.Account.from_key("0x" + "22" * 32)
    assert not receipts.verify(msg, sig, other.address)


def test_an_unsigned_order_has_nothing_to_attest(signing):
    with pytest.raises(receipts.ReceiptError):
        receipts.build(_order(tx_hash=None), block_number=1, settled_at=1.0)


def test_the_receipt_records_the_terms_the_user_was_shown(signing):
    msg = receipts.build(_order(), block_number=71_000_000, settled_at=1789900100)
    # Never base units: "12340000" is not a term anybody agreed to.
    assert msg["paid"] != "12340000"
    assert msg["paid"] == "12.34 USDT"
    assert msg["receivedAtLeast"] == "0.0806 SPCXx"      # the promise, not the estimate
    assert msg["quotedValueUsdCents"] == 1234            # integer cents, not a float
    assert msg["quotedAt"] == 1789900000                 # quoted before settled
    assert msg["settledAt"] == 1789900100

    # The same fields nested under `display`, as some paths store them.
    nested = {"order_id": "o", "address": ADDR, "tx_hash": TX, "est_usd": 12.34,
              "created_at": 1789900000,
              "display": {"side": "buy", "symbol": "SPCXx", "spending": "12.34 USDT",
                          "minimum_received": "0.0806 SPCXx"}}
    m2 = receipts.build(nested, block_number=1, settled_at=2)
    assert m2["paid"] == "12.34 USDT" and m2["receivedAtLeast"] == "0.0806 SPCXx"


def test_issuing_is_once_per_order_and_survives_a_failed_anchor(signing, monkeypatch):
    db = Database(":memory:")
    calls = []

    async def anchor_ok(d):
        calls.append(d)
        return "0x" + "ee" * 32

    monkeypatch.setattr(receipts, "anchor", anchor_ok)
    first = run(receipts.issue(db, _order(), block_number=71_000_000, settled_at=1789900100))
    assert first["anchor_tx"] == "0x" + "ee" * 32
    assert receipts.verify(first["payload"], first["signature"], first["signer"])

    # A second settlement poll must not mint a second receipt over the same
    # trade, nor anchor it again.
    again = run(receipts.issue(db, _order(), block_number=71_000_000, settled_at=1789999999))
    assert again["digest"] == first["digest"] and len(calls) == 1

    # An anchor that cannot be sent costs the anchor, never the receipt.
    async def anchor_fails(d):
        raise RuntimeError("node down")

    monkeypatch.setattr(receipts, "anchor", anchor_fails)
    db2 = Database(":memory:")
    r = run(receipts.issue(db2, _order(order_id="sarf_ord_two"),
                           block_number=1, settled_at=1.0))
    assert r is not None and r["anchor_tx"] is None
    assert receipts.verify(r["payload"], r["signature"], r["signer"])


def test_without_a_signing_key_nothing_is_issued_and_nothing_breaks(monkeypatch):
    monkeypatch.setattr(receipts, "settings",
                        SimpleNamespace(relayer_private_key="", public_url=""))
    db = Database(":memory:")
    assert receipts.signer_address() is None
    assert run(receipts.issue(db, _order(), block_number=1, settled_at=1.0)) is None


def test_the_anchor_carries_the_digest_itself(signing, monkeypatch):
    """What lands on-chain must BE the digest — an anchor over anything else
    proves nothing about the receipt. Checked on the raw signed transaction
    rather than by spying on the signer."""
    raw_seen = {}

    async def transaction_count(a):
        return 7

    async def gas_price():
        return 20_000_000

    async def send_raw_transaction(raw):
        raw_seen["raw"] = raw
        return "0x" + "ff" * 32

    import sarf.xlayer.rpc as real_rpc
    monkeypatch.setattr(real_rpc, "transaction_count", transaction_count)
    monkeypatch.setattr(real_rpc, "gas_price", gas_price)
    monkeypatch.setattr(real_rpc, "send_raw_transaction", send_raw_transaction)

    msg = receipts.build(_order(), block_number=1, settled_at=1.0)
    dig = receipts.digest(msg)
    assert run(receipts.anchor(dig)) == "0x" + "ff" * 32

    # The digest sits in the transaction's calldata, so anyone reading that
    # transaction can match it against the receipt.
    assert dig[2:] in raw_seen["raw"].lower()

    from eth_account import Account
    from eth_account._utils.legacy_transactions import Transaction  # noqa: F401
    signer = receipts.signer_address()
    # And it is a self-send of zero value: an attestation, not a payment.
    from eth_utils import keccak  # noqa: F401
    decoded = Account.recover_transaction(raw_seen["raw"])
    assert decoded.lower() == signer.lower()
