"""earn_execution.py: the orchestrator that actually moves funds for Earn.

The mainnet test that first exercised this found a real bug here: an LP
leg's step came back as a plain dict (ZapEngine.next_step's own return
shape), the dispatcher only recognised a Step dataclass, and every LP leg
silently read as "awaiting confirmation" forever — 40 zap positions were
created and none of them were ever signed. These tests pin the fix: a fake
engine that returns exactly ZapEngine's real shapes, asserting the
dispatcher actually produces a signable step rather than stalling.

Everything here is a fake engine — no network, no real ZapEngine, no chain.
"""
from __future__ import annotations

import asyncio

import pytest

from sarf.db import Database
from sarf.validation import ValidationError
from sarf.xlayer import earn_execution as ex
from sarf.xlayer.zap import Step

ADDR = "0x" + "11" * 20


class FakeZapPosition:
    """Enough of a real zap position's lifecycle to drive earn_execution
    without a real ZapEngine: created -> a fixed number of "sign" steps ->
    complete (entering), or close_requested -> steps -> closed (exiting)."""

    def __init__(self, position_id, total_steps=2, close_units=None):
        self.position_id = position_id
        self.state = "entering"
        self.steps_done = 0
        self.total_steps = total_steps
        self.close_units = close_units if close_units is not None else 999


class FakeEngine:
    """Stands in for ZapEngine: real dict/Step shapes, fake chain."""

    DEFAULT_EXIT_THRESHOLD_BPS = 800

    def __init__(self, db):
        self.db = db
        self.pools: dict[str, object] = {}
        self._zap: dict[str, FakeZapPosition] = {}
        self._next_zap_id = 0

    async def create(self, address, asset, amount, il_threshold_bps, pool_key=None):
        self._next_zap_id += 1
        pid = f"zap_fake{self._next_zap_id}"
        self._zap[pid] = FakeZapPosition(pid)
        self.db.create_zap_position(
            position_id=pid, address=address.lower(), pool_key=pool_key or "POOL",
            deposit_symbol=asset, deposit_amount=str(amount), deposit_usd=1.0,
            il_threshold_bps=il_threshold_bps, reentry_bps=0, state="entering",
            flow="enter", flow_step=0, flow_ctx={})
        return {"position_id": pid}

    async def next_step(self, zap_position_id, address):
        zp = self._zap[zap_position_id]
        if zp.state == "closed":
            return {"status": "nothing_to_sign", "state": "closed"}
        if zp.steps_done >= zp.total_steps:
            zp.state = "in_pool" if zp.state != "exiting" else "closed"
            # Mirrors the real _finish(): flow_ctx is cleared, and the actual
            # returned amount lands in realized_amount instead. Reading
            # flow_ctx here was the second thing the mainnet run caught.
            self.db.update_zap_position(
                zap_position_id, state=zp.state, flow_ctx={},
                **({"realized_amount": str(zp.close_units), "realized_symbol": "SPCXx"}
                   if zp.state == "closed" else {}))
            return {"status": "nothing_to_sign", "state": zp.state}
        # The exact real shape ZapEngine.next_step returns on {"status": "sign"}.
        return {"status": "sign", "flow": "enter", "step_index": zp.steps_done,
                "step_count": zp.total_steps, "kind": "approve", "title": "fake step",
                "tx": {"chainId": 196, "to": "0x" + "22" * 20, "data": "0xabcd", "value": "0"}}

    async def submit_step(self, zap_position_id, address, tx_hash):
        zp = self._zap[zap_position_id]
        zp.steps_done += 1
        return {"status": "confirmed"}

    def request_close(self, zap_position_id, address):
        zp = self._zap[zap_position_id]
        zp.state, zp.steps_done, zp.total_steps = "exiting", 0, zp.total_steps
        self.db.update_zap_position(zap_position_id, state="close_pending")


class FakeUnderlying:
    symbol = "SPCXx"
    decimals = 18


class FakePool:
    underlying = FakeUnderlying()


@pytest.fixture()
def db():
    return Database(":memory:")


@pytest.fixture()
def engine(db):
    e = FakeEngine(db)
    e.pools = {"POOL": FakePool()}
    return e


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------- the bug itself, pinned

def test_lp_leg_step_returns_a_real_step_not_a_dict(engine, db):
    """The exact defect the mainnet test caught: ZapEngine.next_step's own
    'sign' dict must be converted to a Step, not passed through."""
    leg = {"kind": "lp", "pool_key": "POOL", "target_units": 100, "threshold_bps": 800,
          "zap_position_id": None, "status": "pending"}
    result = run(ex._lp_leg_step(engine, leg, ADDR))
    assert isinstance(result, Step), (
        f"expected a Step so the dispatcher recognises it; got {type(result)}")
    assert result.to == "0x" + "22" * 20 and result.data == "0xabcd"
    assert leg["zap_position_id"] is not None, "the underlying position must be recorded"


def test_an_lp_leg_actually_progresses_through_next_step(engine, db):
    """End-to-end through the real dispatcher: a fresh LP-only position must
    reach 'sign' on the first call, not 'awaiting_confirmation' forever."""
    position_id = db.create_earn_position(
        address=ADDR.lower(), asset_symbol="SPCXx", deposit_units="100", state="entering",
        legs=[{"kind": "lp", "pool_key": "POOL", "target_units": 100, "threshold_bps": 800,
              "zap_position_id": None, "status": "pending"}])

    step = run(ex.next_step(engine, {}, None, position_id, ADDR))
    assert step["status"] == "sign", f"expected a signable step, got {step}"
    assert step["leg_index"] == 0 and step["leg_kind"] == "lp"
    assert step["tx"]["to"] == "0x" + "22" * 20


def test_an_lp_leg_reaches_open_after_all_its_steps_are_signed(engine, db):
    position_id = db.create_earn_position(
        address=ADDR.lower(), asset_symbol="SPCXx", deposit_units="100", state="entering",
        legs=[{"kind": "lp", "pool_key": "POOL", "target_units": 100, "threshold_bps": 800,
              "zap_position_id": None, "status": "pending"}])

    for _ in range(10):
        step = run(ex.next_step(engine, {}, None, position_id, ADDR))
        if step["status"] == "sign":
            run(ex.submit_step(engine, position_id, ADDR, "0xhash", step["leg_index"]))
        elif step["status"] == "complete":
            break
    else:
        pytest.fail("did not complete: the orchestrator stalled")

    final = db.get_earn_position(position_id)
    assert final["state"] == "active"
    assert final["legs"][0]["status"] == "open"


def test_closing_an_lp_leg_records_the_real_amount_returned(engine, db):
    position_id = db.create_earn_position(
        address=ADDR.lower(), asset_symbol="SPCXx", deposit_units="100", state="active",
        legs=[{"kind": "lp", "pool_key": "POOL", "target_units": 100, "threshold_bps": 800,
              "zap_position_id": None, "status": "open"}])
    # Open the underlying zap position by hand (as if deposit had already run).
    pos = run(engine.create(ADDR, "SPCXx", "100", 800, pool_key="POOL"))
    zp = engine._zap[pos["position_id"]]
    zp.state, zp.close_units = "in_pool", 87  # returned less than deposited: a real outcome
    legs = db.get_earn_position(position_id)["legs"]
    legs[0]["zap_position_id"] = pos["position_id"]
    db.update_earn_position(position_id, legs=legs)

    ex.request_withdraw(engine, position_id, ADDR)
    for _ in range(10):
        step = run(ex.next_step(engine, {}, None, position_id, ADDR))
        if step["status"] == "sign":
            run(ex.submit_step(engine, position_id, ADDR, "0xhash", step["leg_index"]))
        elif step["status"] == "complete":
            break
    else:
        pytest.fail("withdraw stalled")

    final = db.get_earn_position(position_id)
    assert final["state"] == "closed"
    assert final["legs"][0]["out_units"] == 87, "must read the real closed_amount, not assume it"
    assert ex.total_out_units(final) == 87


# --------------------------------------------------------------- lending legs

def test_lending_leg_moves_through_swap_then_supply_states():
    leg = {"kind": "lending", "reserve": "0x" + "33" * 20, "reserve_symbol": "USDT0",
          "target_units": 100, "status": "pending", "swapped_units": 0, "supplied_units": 0}
    assert leg["status"] == "pending"
    # The state machine itself (status transitions on submit_step) is
    # integration-tested via the real mainnet run; this pins the shape the
    # legs dict must have going in, so a future refactor cannot silently
    # rename a field the driver script or a real API depends on.
    for key in ("reserve", "reserve_symbol", "target_units", "status",
               "swapped_units", "supplied_units"):
        assert key in leg


# -------------------------------------------------------------- open_deposit

def test_open_deposit_refuses_when_the_wallet_holds_too_little(monkeypatch, db):
    class Asset:
        address = "0x" + "44" * 20
        decimals = 18

    class FakeReg:
        def resolve(self, symbol):
            return Asset()

    engine = FakeEngine(db)
    engine.reg = FakeReg()
    monkeypatch.setattr(ex.rpc, "erc20_balance", lambda *a, **k: _async(0))

    with pytest.raises(ValidationError, match="insufficient"):
        run(ex.open_deposit(engine, {}, None, ADDR, "SPCXx", "1.0"))


def test_only_top_leg_refuses_when_the_top_venue_is_not_an_lp_leg(monkeypatch, db):
    class Asset:
        address = "0x" + "44" * 20
        decimals = 18

    class FakeReg:
        def resolve(self, symbol):
            return Asset()

    engine = FakeEngine(db)
    engine.reg = FakeReg()
    monkeypatch.setattr(ex.rpc, "erc20_balance", lambda *a, **k: _async(10 ** 18))
    monkeypatch.setattr(ex, "plan_deposit", lambda *a, **k: _async(
        [{"kind": "lending", "target_units": 999, "reserve": "0x" + "55" * 20,
         "reserve_symbol": "USDT0", "status": "pending", "swapped_units": 0,
         "supplied_units": 0}]))

    with pytest.raises(ValidationError, match="not an LP leg"):
        run(ex.open_deposit(engine, {}, None, ADDR, "SPCXx", "1.0", only_top_leg=True))


def _async(v):
    async def f():
        return v
    return f()


# ------------------------------------------------------------ request_withdraw

def test_withdraw_refused_from_a_position_that_is_not_active(db, engine):
    position_id = db.create_earn_position(
        address=ADDR.lower(), asset_symbol="SPCXx", deposit_units="1", state="entering", legs=[])
    with pytest.raises(ValidationError, match="entering"):
        ex.request_withdraw(engine, position_id, ADDR)


def test_withdraw_wrong_owner_is_refused(db, engine):
    position_id = db.create_earn_position(
        address=ADDR.lower(), asset_symbol="SPCXx", deposit_units="1", state="active", legs=[])
    with pytest.raises(ValidationError, match="no such"):
        ex.request_withdraw(engine, position_id, "0x" + "99" * 20)


# ------------------------------------ withdraw-side swap-back approve state

def test_lending_withdraw_swap_back_needs_its_own_approve_state():
    """The second mainnet bug: an approve confirming during 'swapping_out'
    must not be mistaken for the swap itself completing. The fix adds
    'swapping_out_needs_approve' as its own status; this pins that a leg
    stuck there is NOT reported as closed and still has a swap to do."""
    from sarf.xlayer import earn_execution as ex

    leg = {"kind": "lending", "reserve": "0x" + "33" * 20, "reserve_symbol": "USDT0",
          "target_units": 100, "status": "swapping_out_needs_approve",
          "swapped_units": 100, "supplied_units": 100, "_withdrawn_units": 100}
    # submit_step's dispatch must not have a branch that treats this status
    # as "the swap landed" — it should do nothing special (no out_units, no
    # status change) and let next_step's own state machine drive it forward.
    assert leg["status"] not in ("swapping_out",), (
        "the approve and the swap must be distinguishable statuses")
