"""earn_pendle.py: the Pendle "RWA Fixed Yield" venue's position lifecycle.

Everything here is fake chain/fake Pendle API — no network. The real
on-chain mechanics (Pendle's convert API, the router, split_proceeds'
floor-at-zero behaviour) were already proven for real on mainnet this
session (1.5 USDT0 round trip, 21 transactions, zero reverts); these tests
pin the lifecycle/bookkeeping layer built on top of that proof: does a
"sign" step actually advance state, does a receipt with no relevant
Transfer correctly read as "that was just the approve", does a bad RWA
choice or deposit token get refused before any chain call.
"""
from __future__ import annotations

import asyncio

import pytest

from sarf.db import Database
from sarf.validation import ValidationError
from sarf.xlayer import earn_pendle as ep
from sarf.xlayer import pendle_rwa as pendle
from sarf.xlayer.zap import Step, TRANSFER_TOPIC, _addr_topic

ADDR = "0x" + "11" * 20
PT = pendle.PT_USDG_OCT2026
USDT0 = ep.DEPOSIT_TOKENS["USDT0"]
RWA_ADDR = "0x" + "44" * 20


def run(coro):
    return asyncio.run(coro)


def _transfer_log(token: str, to: str, amount: int) -> dict:
    return {"address": token, "topics": [TRANSFER_TOPIC, _addr_topic("0x" + "99" * 20),
                                         _addr_topic(to)], "data": f"0x{amount:064x}"}


class FakeAsset:
    def __init__(self, symbol, address):
        self.symbol = symbol
        self.address = address


class FakeReg:
    def resolve(self, symbol, *, allowlist=frozenset()):
        if symbol.upper() == "SPCXX":
            return FakeAsset("SPCXx", RWA_ADDR)
        raise ValidationError(f"{symbol!r} is not a tokenized stock available on X Layer.")


class FakeDex:
    async def build_swap(self, *, from_address, to_address, amount_min_units,
                         user_address, slippage_pct):
        class Tx:
            to = "0x" + "77" * 20
            data = "0xbeef"
            value = "0"
        return Tx(), None, False


@pytest.fixture()
def db():
    return Database(":memory:")


@pytest.fixture()
def reg():
    return FakeReg()


# --------------------------------------------------------------- open_deposit

def test_open_deposit_refuses_an_unlisted_rwa_symbol(db, reg):
    with pytest.raises(ValidationError, match="not a tokenized stock"):
        run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_000_000, "NOTREAL"))


def test_open_deposit_refuses_an_unaccepted_deposit_token(db, reg):
    with pytest.raises(ValidationError, match="only accepts"):
        run(ep.open_deposit(db, reg, frozenset(), ADDR, "DOGE", 1_000_000, "SPCXx"))


def test_open_deposit_refuses_a_non_positive_amount(db, reg):
    with pytest.raises(ValidationError, match="positive"):
        run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 0, "SPCXx"))


def test_open_deposit_records_the_users_rwa_choice(db, reg):
    pos = run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_500_000, "spcxx"))
    assert pos["state"] == "entering"
    assert pos["designated_rwa_symbol"] == "SPCXx"  # canonical casing from the registry
    assert pos["principal_units"] == "1500000"


# -------------------------------------------------------------------- entry

def test_next_step_entering_asks_pendle_and_returns_its_first_step(monkeypatch, db, reg):
    pos = run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_500_000, "SPCXx"))

    async def fake_entry_steps(address, token_in, amount_in):
        assert address == ADDR and token_in == USDT0 and amount_in == 1_500_000
        return [Step("swap", "Buy PT-USDG (Pendle)", "0x" + "88" * 20, "0xabcd")]
    monkeypatch.setattr(pendle, "entry_steps", fake_entry_steps)

    step = run(ep.next_step(db, reg, FakeDex(), ADDR, pos["position_id"]))
    assert step["status"] == "sign" and step["tx"]["to"] == "0x" + "88" * 20


def test_submit_step_approve_only_receipt_keeps_state_entering(monkeypatch, db, reg):
    pos = run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_500_000, "SPCXx"))

    async def fake_receipt(tx_hash):
        return {"status": "0x1", "logs": []}  # an approve has no Transfer log
    monkeypatch.setattr(ep, "_receipt", fake_receipt)

    out = run(ep.submit_step(db, reg, ADDR, pos["position_id"], "0xhash"))
    assert out == {"status": "confirmed", "state": "entering"}
    assert db.get_earn_rwa_position(pos["position_id"])["state"] == "entering"


def test_submit_step_pt_transfer_moves_to_open_and_records_pt_units(monkeypatch, db, reg):
    pos = run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_500_000, "SPCXx"))

    async def fake_receipt(tx_hash):
        return {"status": "0x1", "logs": [_transfer_log(PT, ADDR, 1_502_681)]}
    monkeypatch.setattr(ep, "_receipt", fake_receipt)

    out = run(ep.submit_step(db, reg, ADDR, pos["position_id"], "0xhash"))
    assert out == {"status": "confirmed", "state": "open"}
    final = db.get_earn_rwa_position(pos["position_id"])
    assert final["state"] == "open" and final["pt_units"] == "1502681"


def test_submit_step_reverted_tx_raises_and_credits_nothing(monkeypatch, db, reg):
    pos = run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_500_000, "SPCXx"))

    async def fake_receipt(tx_hash):
        return {"status": "0x0", "logs": []}
    monkeypatch.setattr(ep, "_receipt", fake_receipt)

    with pytest.raises(ValidationError, match="reverted"):
        run(ep.submit_step(db, reg, ADDR, pos["position_id"], "0xhash"))
    assert db.get_earn_rwa_position(pos["position_id"])["state"] == "entering"


# --------------------------------------------------------- withdraw / exit

def test_request_withdraw_refuses_a_position_that_is_not_open(db, reg):
    pos = run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_500_000, "SPCXx"))
    with pytest.raises(ValidationError, match="entering"):
        run(ep.request_withdraw(db, ADDR, pos["position_id"]))


def _opened_position(db, reg, pt_units=1_502_681):
    pos = run(ep.open_deposit(db, reg, frozenset(), ADDR, "USDT0", 1_500_000, "SPCXx"))
    db.update_earn_rwa_position(pos["position_id"], state="open", pt_units=str(pt_units))
    return pos["position_id"]


def test_exit_with_proceeds_moves_to_buying_rwa_not_closed(monkeypatch, db, reg):
    position_id = _opened_position(db, reg)
    run(ep.request_withdraw(db, ADDR, position_id))

    async def fake_receipt(tx_hash):
        # redeemed MORE than principal: real proceeds
        return {"status": "0x1", "logs": [_transfer_log(USDT0, ADDR, 1_600_000)]}
    monkeypatch.setattr(ep, "_receipt", fake_receipt)

    out = run(ep.submit_step(db, reg, ADDR, position_id, "0xhash"))
    assert out == {"status": "confirmed", "state": "buying_rwa"}
    final = db.get_earn_rwa_position(position_id)
    assert final["proceeds_units"] == "100000"  # 1,600,000 - 1,500,000


def test_exit_at_a_loss_closes_directly_with_zero_proceeds(monkeypatch, db, reg):
    """The whole point of split_proceeds' floor: a same-day or unlucky exit
    must not manufacture a negative RWA purchase — it just closes with
    proceeds_units=0, exactly what the real mainnet test produced."""
    position_id = _opened_position(db, reg)
    run(ep.request_withdraw(db, ADDR, position_id))

    async def fake_receipt(tx_hash):
        return {"status": "0x1", "logs": [_transfer_log(USDT0, ADDR, 1_499_817)]}
    monkeypatch.setattr(ep, "_receipt", fake_receipt)

    out = run(ep.submit_step(db, reg, ADDR, position_id, "0xhash"))
    assert out == {"status": "confirmed", "state": "closed"}
    final = db.get_earn_rwa_position(position_id)
    assert final["proceeds_units"] == "0" and final["rwa_units"] == "0"


def test_next_step_buying_rwa_builds_a_real_okx_swap_for_the_chosen_asset(db, reg):
    position_id = _opened_position(db, reg)
    db.update_earn_rwa_position(position_id, state="buying_rwa", proceeds_units="100000")

    step = run(ep.next_step(db, reg, FakeDex(), ADDR, position_id))
    assert step["status"] == "sign" and step["kind"] == "swap"
    assert "SPCXx" in step["title"]


def test_submit_step_buying_rwa_credits_only_the_designated_asset(monkeypatch, db, reg):
    position_id = _opened_position(db, reg)
    db.update_earn_rwa_position(position_id, state="buying_rwa", proceeds_units="100000")

    async def fake_receipt(tx_hash):
        return {"status": "0x1", "logs": [_transfer_log(RWA_ADDR, ADDR, 628)]}
    monkeypatch.setattr(ep, "_receipt", fake_receipt)

    out = run(ep.submit_step(db, reg, ADDR, position_id, "0xhash"))
    assert out == {"status": "confirmed", "state": "closed"}
    assert db.get_earn_rwa_position(position_id)["rwa_units"] == "628"
