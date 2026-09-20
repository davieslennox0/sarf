"""Single-asset zap: IL math, the optimal split, receipt parsing, the full
enter -> exit -> park -> re-enter cycle, the watcher's transitions, and the
REST/auth boundary.

Offline: the chain is a fake that mines whatever the test "signs" and emits
the ERC-20 Transfer / Sync logs a real receipt would carry, so the engine's
receipt-driven bookkeeping is exercised for real.
"""

from __future__ import annotations

import asyncio
import itertools
import math
from types import SimpleNamespace

import pytest
from eth_abi import encode
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sarf import auth
from sarf.db import Database
from sarf.validation import ValidationError
from sarf.xlayer import zap as zapmod
from sarf.xlayer.registry import registry
from sarf.xlayer.zap import (
    FLOWS, LIVE_STATES, SYNC_TOPIC, TRANSFER_TOPIC, WATCHED_STATES, ZapEngine, il_bps,
    last_sync, optimal_swap_in, transfers_between, transfers_in,
)
from sarf.xlayer.zap_api import build_zap_api

OWNER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
OTHER_USER = "0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a"
OKX_ROUTER = "0x" + "0c" * 20
E18 = 10 ** 18


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------- math

def test_il_formula_matches_textbook_values():
    assert il_bps(1.0) == 0
    assert round(il_bps(2.0), 1) == 571.9     # 2x -> 5.72%
    assert round(il_bps(0.5), 1) == 571.9     # symmetric in log-price
    assert round(il_bps(4.0), 1) == 2000.0    # 4x -> 20%
    assert round(il_bps(1.25), 1) == 61.9
    with pytest.raises(ValueError):
        il_bps(0)
    with pytest.raises(ValueError):
        il_bps(float("inf"))


def _v2_out(a, r_in, r_out, fee_bps=30):
    a_fee = a * (10000 - fee_bps)
    return a_fee * r_out // (r_in * 10000 + a_fee)


@pytest.mark.parametrize("amount,r_in,r_out", [
    (E18, 1545 * E18, 76_000_000 * E18),
    (37 * E18, 1149 * E18, 23_000_000 * E18),
    (123456789, 10 ** 12, 5 * 10 ** 20),
])
def test_optimal_split_leaves_nothing_behind(amount, r_in, r_out):
    """After swapping s through the pair, the remainder and the output must
    sit in the pair's new ratio, so addLiquidity consumes both in full."""
    s = optimal_swap_in(amount, r_in)
    out = _v2_out(s, r_in, r_out)
    remain = amount - s
    ratio_left = remain / out
    ratio_pool = (r_in + s) / (r_out - out)
    assert math.isclose(ratio_left, ratio_pool, rel_tol=1e-6)
    # Near half: the fee pushes it above, the swap's own price impact below.
    assert 0.45 * amount < s < 0.55 * amount


@pytest.mark.parametrize("tax", [100, 200, 300])
def test_taxed_split_matches_what_actually_arrives(tax):
    """With a buy tax on the output token, the split must leave the post-tax
    output and the remainder in the post-swap ratio, or addLiquidity pairs
    less than the remainder and reverts on its minimums."""
    from sarf.xlayer.zap import pairable, zap_split
    amount, r_in, r_out = E18, 1545 * E18, 76_000_000 * E18
    s = zap_split(amount, r_in, r_out, 30, tax)
    out = _v2_out(s, r_in, r_out)
    got = out * (10000 - tax) // 10000
    a, b = pairable(amount - s, got, r_in + s, r_out - out)
    assert (amount - s) - a <= amount // 10 ** 12   # essentially nothing left over
    assert got - b <= got // 10 ** 12
    assert s > optimal_swap_in(amount, r_in)         # swaps more than untaxed


def test_cost_model_matches_the_mainnet_measurement():
    """LAIKA-wSPCXx, 2026-09-18 mainnet cycle: entry 3.1-3.4%, exit 2.1%,
    exit + re-entry 5.4% ($9.96 -> $9.42). The disclosed figures must not be
    rosier than what the chain did."""
    e = ZapEngine(Database(":memory:"), None, registry())
    p = e.pool("LAIKA-wSPCXx")
    assert e.entry_cost_bps(p) == 315
    assert e.exit_cost_bps(p) == 225
    assert e.cycle_cost_bps(p) == 550


# ------------------------------------------------------------ receipt parsing

def _topic(a):
    return "0x" + "0" * 24 + a.lower()[2:]


def _transfer(token, frm, to, amount):
    return {"address": token, "topics": [TRANSFER_TOPIC, _topic(frm), _topic(to)],
            "data": hex(amount)}


def _sync(pair, r0, r1):
    return {"address": pair, "topics": [SYNC_TOPIC],
            "data": "0x" + encode(["uint112", "uint112"], [r0, r1]).hex()}


def test_receipt_parsing_counts_only_what_reached_the_owner():
    tok, pair = "0x" + "aa" * 20, "0x" + "bb" * 20
    rc = {"logs": [
        _transfer(tok, pair, OWNER, 5), _transfer(tok, pair, OWNER, 7),
        _transfer(tok, pair, OTHER_USER, 1000),          # not ours
        _transfer(tok, OWNER, pair, 3),
        _sync(pair, 11, 22), _sync(pair, 33, 44),
    ]}
    assert transfers_in(rc, OWNER) == {tok: 12}
    assert transfers_between(rc, OWNER, pair) == {tok: 3}
    assert last_sync(rc, pair) == (33, 44)  # the LAST sync is the settled state


# ----------------------------------------------------------------- fake chain

class Chain:
    """Just enough of X Layer to run the zap: one V2 pair, balances,
    allowances, an Aave income index and a transaction/receipt store."""

    def __init__(self, engine: ZapEngine, pool):
        self.e, self.p = engine, pool
        self.r_rwa, self.r_other, self.ts = 1500 * E18, 75_000_000 * E18, 10_000 * E18
        self.bal: dict[tuple[str, str], int] = {}
        self.allow: dict[tuple[str, str, str], int] = {}
        self.income = 10 ** 27
        self.txs, self.receipts = {}, {}
        self._n = itertools.count(1)
        self.okx_calls = []
        e = engine

        async def pool_state(pool):
            return {"rwa_is_token0": True, "reserve_rwa": self.r_rwa,
                    "reserve_other": self.r_other, "total_supply": self.ts,
                    "price": e._price(pool, self.r_rwa, self.r_other),
                    "lp_index": math.sqrt(self.r_rwa * self.r_other) / self.ts}

        async def rwa_usd(pool):
            return 150.0

        async def aave_state():
            return {"supply_apy_pct": 3.478, "income_index": self.income}

        async def balance(token, owner):
            return self.bal.get((token.lower(), owner.lower()), 0)

        async def allowance(token, owner, spender):
            return self.allow.get((token.lower(), owner.lower(), spender.lower()), 0)

        async def wrap_rate(pool):
            return 1.0

        self.float_ = 0

        async def token_float(pool):
            return self.float_

        e.pool_state, e.rwa_usd, e.aave_state = pool_state, rwa_usd, aave_state
        e._balance, e._allowance, e._wrap_rate = balance, allowance, wrap_rate
        e._float = token_float

        async def build_swap(*, from_address, to_address, amount_min_units, user_address,
                             slippage_pct, **_):
            self.okx_calls.append((from_address, to_address, amount_min_units))
            data = "0x" + encode(["uint256", "uint256"], [amount_min_units, next(self._n)]).hex()
            return (SimpleNamespace(to=OKX_ROUTER, data=data, value="0"),
                    SimpleNamespace(to_amount=amount_min_units, price_impact_pct=0.05), True)

        e.dex = SimpleNamespace(build_swap=build_swap)

    # the parts of the node the engine talks to directly
    async def rpc_call(self, method, params, **_):
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0])
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        raise AssertionError(f"unexpected rpc {method}")

    async def eth_call(self, to, sig, types=None, args=None, out=None):
        assert sig.startswith("getAmountsOut"), sig
        amount, path = args
        rwa_in = path[0].lower() == self.p.rwa.address.lower()
        r_in, r_out = (self.r_rwa, self.r_other) if rwa_in else (self.r_other, self.r_rwa)
        return ([amount, _v2_out(amount, r_in, r_out)],)

    def mine(self, step, logs, *, sender=OWNER, status=1, data=None):
        h = "0x" + f"{next(self._n):064x}"
        self.txs[h] = {"from": sender, "to": step["tx"]["to"], "input": data or step["tx"]["data"]}
        self.receipts[h] = {"status": hex(status), "logs": logs}
        if status == 1:
            for lg in logs:
                if lg["topics"][0] == TRANSFER_TOPIC:
                    tok, amt = lg["address"].lower(), int(lg["data"], 16)
                    frm, to = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:]
                    self.bal[(tok, frm)] = self.bal.get((tok, frm), 0) - amt
                    self.bal[(tok, to)] = self.bal.get((tok, to), 0) + amt
        return h


@pytest.fixture()
def env(monkeypatch):
    db = Database(":memory:")
    e = ZapEngine(db, None, registry())
    pool = e.pool("LAIKA-wSPCXx")
    ch = Chain(e, pool)
    monkeypatch.setattr(zapmod.rpc, "_call", ch.rpc_call)
    monkeypatch.setattr(zapmod, "_eth_call", ch.eth_call)
    return SimpleNamespace(db=db, e=e, pool=pool, ch=ch)


def _sign_through(env, pid, answers):
    """Drive a flow: for each step the engine asks for, look up how the fake
    chain responds (the logs its receipt carries), mine it, submit it."""
    seen = []
    while True:
        st = run(env.e.next_step(pid, OWNER))
        if st["status"] != "sign":
            return seen, st
        kind = env.e.db.get_zap_position(pid)["flow"], st["kind"]
        seen.append(st["kind"] if st["kind"] != "approve" else "approve")
        logs = answers(st)
        h = env.ch.mine(st, logs)
        if st["kind"] == "approve":
            # apply the allowance so a later approve for the same pair is skipped
            tok, spender_amt = st["tx"]["to"], bytes.fromhex(st["tx"]["data"][10:])
            spender = "0x" + spender_amt[12:32].hex()
            env.ch.allow[(tok.lower(), OWNER, spender)] = int.from_bytes(spender_amt[32:], "big")
        res = run(env.e.submit_step(pid, OWNER, h))
        assert res["status"] in ("confirmed", "complete"), (kind, res)


def _liq_amounts(st, pool):
    """(rwa, other) desired amounts from an addLiquidity step, whichever
    order the tokens are in."""
    words = bytes.fromhex(st["tx"]["data"][10:])
    token_a = "0x" + words[12:32].hex()
    x, y = int.from_bytes(words[64:96], "big"), int.from_bytes(words[96:128], "big")
    return (x, y) if token_a == pool.rwa.address.lower() else (y, x)


def _enter(env, amount=E18):
    ch, p = env.ch, env.pool
    ch.bal[(p.underlying.address.lower(), OWNER)] = 10 * E18
    pos = run(env.e.create(OWNER, "SPCXx", str(amount / E18), 200))
    pid = pos["position_id"]

    def answers(st):
        k = st["tx"]["data"][:10]
        if st["kind"] == "wrap":
            return [_transfer(p.rwa.address, "0x" + "00" * 20, OWNER, amount)]
        if st["kind"] == "swap":
            swapped = int.from_bytes(bytes.fromhex(st["tx"]["data"][10:74]), "big")
            got = _v2_out(swapped, ch.r_rwa, ch.r_other)
            ch.r_rwa += swapped
            ch.r_other -= got
            return [_transfer(p.other.address, p.pair, OWNER, got)]
        if st["kind"] == "add_liquidity":
            a, b = _liq_amounts(st, p)
            lp = a * ch.ts // ch.r_rwa
            ch.r_rwa += a
            ch.r_other += b
            ch.ts += lp
            return [_transfer(p.rwa.address, OWNER, p.pair, a),
                    _transfer(p.other.address, OWNER, p.pair, b),
                    _transfer(p.pair, "0x" + "00" * 20, OWNER, lp),
                    _sync(p.pair, ch.r_rwa, ch.r_other)]
        assert st["kind"] == "approve", k
        return []

    seen, end = _sign_through(env, pid, answers)
    return pid, seen, end


def test_full_enter_records_entry_from_the_receipt(env):
    pid, seen, end = _enter(env)
    assert end["status"] == "nothing_to_sign"
    assert seen == ["approve", "wrap", "approve", "swap", "approve", "add_liquidity"]
    pos = env.db.get_zap_position(pid)
    assert pos["state"] == "in_pool"
    ch = env.ch
    assert math.isclose(pos["p_initial"], ch.r_other / ch.r_rwa, rel_tol=1e-12)
    assert int(pos["lp_amount"]) > 0 and int(pos["hold_rwa"]) > 0 and int(pos["hold_other"]) > 0
    kinds = [ev["kind"] for ev in env.db.zap_events(pid)]
    assert kinds[0] == "created" and kinds[-1] == "entered"


def test_view_always_pairs_yield_with_il(env):
    pid, _, _ = _enter(env)
    v = run(env.e.view(env.db.get_zap_position(pid), public_url="u"))
    assert v["il"]["current_bps"] == 0
    assert "il_bps_now" in v["yield"] and "aave_usdt_supply_apy_pct" in v["yield"]
    assert v["headline"].startswith("IL ")
    assert v["value"]["current_usd"] and v["value"]["hold_50_50_usd"]
    assert v["owner"] != OWNER and "…" in v["owner"]  # shortened when public


def _move_price(env, factor):
    """Someone else trades the pool: price of the RWA leg times `factor`,
    k held constant."""
    ch = env.ch
    k = ch.r_rwa * ch.r_other
    ch.r_rwa = int(ch.r_rwa / math.sqrt(factor))
    ch.r_other = k // ch.r_rwa


def test_watcher_triggers_exit_and_clears_it_with_hysteresis(env):
    pid, _, _ = _enter(env)
    _move_price(env, 1.2)  # ~41 bps, under 200
    assert run(env.e.tick()) == []
    _move_price(env, 1.4 / 1.2)  # 1.4x -> ~140 bps, still under 200
    assert run(env.e.tick()) == []
    _move_price(env, 2.0 / 1.4)  # 2x -> ~572 bps, over 200
    assert run(env.e.tick()) == [(pid, "exit_triggered")]
    assert env.db.get_zap_position(pid)["state"] == "exit_pending"
    _move_price(env, 1.0 / 2.0)  # back to entry -> ~0 bps, under the 100 re-entry line
    assert run(env.e.tick()) == [(pid, "exit_trigger_cleared")]
    assert env.db.get_zap_position(pid)["state"] == "in_pool"


def _exit(env, pid):
    ch, p, park = env.ch, env.pool, env.e.park
    pos = env.db.get_zap_position(pid)

    def answers(st):
        if st["kind"] == "remove_liquidity":
            lp = int(pos["lp_amount"])
            a, b = lp * ch.r_rwa // ch.ts, lp * ch.r_other // ch.ts
            ch.r_rwa -= a
            ch.r_other -= b
            ch.ts -= lp
            return [_transfer(p.rwa.address, p.pair, OWNER, a),
                    _transfer(p.other.address, p.pair, OWNER, b),
                    _sync(p.pair, ch.r_rwa, ch.r_other)]
        if st["kind"] == "swap" and st["tx"]["to"] == env.e.router:
            amt = int.from_bytes(bytes.fromhex(st["tx"]["data"][10:74]), "big")
            got = _v2_out(amt, ch.r_other, ch.r_rwa)
            ch.r_other += amt
            ch.r_rwa -= got
            return [_transfer(p.rwa.address, p.pair, OWNER, got)]
        if st["kind"] == "swap":  # OKX: rwa -> USDT at $150
            amt = int.from_bytes(bytes.fromhex(st["tx"]["data"][2:66]), "big")
            return [_transfer(park.address, OKX_ROUTER, OWNER, amt * 150 // 10 ** 12)]
        if st["kind"] == "aave_supply":
            return [_transfer(env.e.a_token, "0x" + "00" * 20, OWNER, 1)]
        assert st["kind"] == "approve"
        return []

    return _sign_through(env, pid, answers)


def test_exit_parks_in_aave_and_logs_the_exit(env):
    pid, _, _ = _enter(env)
    _move_price(env, 2.0)
    run(env.e.tick())
    seen, end = _exit(env, pid)
    assert seen == ["approve", "remove_liquidity", "approve", "swap", "approve", "swap",
                    "approve", "aave_supply"]
    pos = env.db.get_zap_position(pid)
    assert pos["state"] == "parked" and pos["lp_amount"] is None
    assert int(pos["parked_amount"]) > 0
    ev = [x for x in env.db.zap_events(pid) if x["kind"] == "exited"][-1]
    assert ev["reason"] == "il_threshold"
    assert ev["il_bps_at_exit"] > 200
    assert ev["p_initial"] and ev["p_at_exit"]


def test_parked_position_reenters_and_resets_entry_price(env):
    pid, _, _ = _enter(env)
    p_first = env.db.get_zap_position(pid)["p_initial"]
    _move_price(env, 2.0)
    run(env.e.tick())
    _exit(env, pid)
    env.ch.income = 10 ** 27 * 101 // 100  # 1% interest accrued while parked
    env.ch.bal[(env.e.a_token.lower(), OWNER)] = 10 ** 12
    # still far from entry: stays parked
    assert run(env.e.tick()) == []
    _move_price(env, p_first / (env.ch.r_other / env.ch.r_rwa))  # back to entry
    assert run(env.e.tick()) == [(pid, "reentry_triggered")]

    ch, p, park = env.ch, env.pool, env.e.park
    parked = int(env.db.get_zap_position(pid)["parked_amount"])

    def answers(st):
        if st["kind"] == "aave_withdraw":
            amt = int.from_bytes(bytes.fromhex(st["tx"]["data"][10:])[32:64], "big")
            assert amt == parked * 101 // 100  # principal + interest, not "max"
            return [_transfer(park.address, env.e.a_token, OWNER, amt)]
        if st["kind"] == "swap" and st["tx"]["to"] == OKX_ROUTER:
            amt = int.from_bytes(bytes.fromhex(st["tx"]["data"][2:66]), "big")
            return [_transfer(p.rwa.address, OKX_ROUTER, OWNER, amt * 10 ** 12 // 150)]
        if st["kind"] == "swap":
            swapped = int.from_bytes(bytes.fromhex(st["tx"]["data"][10:74]), "big")
            got = _v2_out(swapped, ch.r_rwa, ch.r_other)
            ch.r_rwa += swapped
            ch.r_other -= got
            return [_transfer(p.other.address, p.pair, OWNER, got)]
        if st["kind"] == "add_liquidity":
            a, b = _liq_amounts(st, p)
            lp = a * ch.ts // ch.r_rwa
            ch.r_rwa, ch.r_other, ch.ts = ch.r_rwa + a, ch.r_other + b, ch.ts + lp
            return [_transfer(p.rwa.address, OWNER, p.pair, a),
                    _transfer(p.other.address, OWNER, p.pair, b),
                    _transfer(p.pair, "0x" + "00" * 20, OWNER, lp),
                    _sync(p.pair, ch.r_rwa, ch.r_other)]
        assert st["kind"] == "approve"
        return []

    seen, _ = _sign_through(env, pid, answers)
    assert seen[0] == "aave_withdraw" and seen[-1] == "add_liquidity"
    pos = env.db.get_zap_position(pid)
    assert pos["state"] == "in_pool"
    assert math.isclose(pos["p_initial"], ch.r_other / ch.r_rwa, rel_tol=1e-12)
    assert [x["kind"] for x in env.db.zap_events(pid)][-1] == "reentered"


def test_taxed_token_goes_first_in_add_liquidity(env):
    """If the RWA leg entered the pair first, a swap-back fired by the taxed
    token's transfer would sync it into the reserves before our mint and the
    whole leg would be donated. The taxed token must be tokenA."""
    pid, _, _ = _enter(env)
    ev = [e for e in env.db.zap_events(pid) if e["kind"] == "step_confirmed"
          and e["step"] == "add_liquidity"]
    tx = env.ch.txs[ev[-1]["tx_hash"]]
    token_a = "0x" + bytes.fromhex(tx["input"][10:])[12:32].hex()
    assert token_a == env.pool.other.address.lower()


def test_selling_the_taxed_token_prices_in_its_swap_back(env):
    """A token float that will be sold into the pair first must lower the
    swap's minimum: the fork measurement that motivated this matched the
    model to the wei."""
    from sarf.xlayer.zap import v2_out
    pid, _, _ = _enter(env)
    ch = env.ch
    amount = 1000 * E18
    ch.float_ = 0
    s0 = run(env.e._v2_swap(env.pool, env.pool.other, env.pool.rwa, amount, OWNER))
    ch.float_ = ch.r_other // 50  # 2% of the reserve waiting to be sold
    s1 = run(env.e._v2_swap(env.pool, env.pool.other, env.pool.rwa, amount, OWNER))
    min0 = int.from_bytes(bytes.fromhex(s0.data[10:])[32:64], "big")
    min1 = int.from_bytes(bytes.fromhex(s1.data[10:])[32:64], "big")
    arrives = amount * (10000 - env.pool.other.sell_tax_bps) // 10000
    x_out = v2_out(ch.float_, ch.r_other, ch.r_rwa)
    expect = v2_out(arrives, ch.r_other + ch.float_, ch.r_rwa - x_out)
    assert min1 == env.e._min(expect)
    assert min1 < min0 * 0.97  # ~2x the float, as the fork showed


# ----------------------------------------------------------- tamper checks

def test_a_different_transaction_cannot_complete_a_step(env):
    env.ch.bal[(env.pool.underlying.address.lower(), OWNER)] = 10 * E18
    pid = run(env.e.create(OWNER, "SPCXx", "1", 200))["position_id"]
    st = run(env.e.next_step(pid, OWNER))
    wrong_data = env.ch.mine(st, [], data=st["tx"]["data"][:-2] + "ff")
    with pytest.raises(ValueError, match="not the step"):
        run(env.e.submit_step(pid, OWNER, wrong_data))
    wrong_sender = env.ch.mine(st, [], sender=OTHER_USER)
    with pytest.raises(ValueError, match="not the step"):
        run(env.e.submit_step(pid, OWNER, wrong_sender))
    with pytest.raises(PermissionError):
        run(env.e.submit_step(pid, OTHER_USER, wrong_sender))


def test_reverted_step_is_logged_and_rebuilt(env):
    env.ch.bal[(env.pool.underlying.address.lower(), OWNER)] = 10 * E18
    pid = run(env.e.create(OWNER, "SPCXx", "1", 200))["position_id"]
    st = run(env.e.next_step(pid, OWNER))
    h = env.ch.mine(st, [], status=0)
    assert run(env.e.submit_step(pid, OWNER, h))["status"] == "failed"
    assert env.db.get_zap_position(pid)["flow_step"] == 0
    assert run(env.e.next_step(pid, OWNER))["status"] == "sign"
    assert "step_failed" in [x["kind"] for x in env.db.zap_events(pid)]


def test_create_refuses_what_the_wallet_does_not_hold_and_bad_thresholds(env):
    with pytest.raises(ValidationError, match="holds"):
        run(env.e.create(OWNER, "SPCXx", "1", 200))
    env.ch.bal[(env.pool.underlying.address.lower(), OWNER)] = 10 * E18
    for il, re_ in [(0, None), (6000, None), (200, 200), (200, -1), (True, None)]:
        with pytest.raises(ValidationError):
            run(env.e.create(OWNER, "SPCXx", "1", il, re_))
    with pytest.raises(ValidationError, match="no incentivised pool"):
        run(env.e.create(OWNER, "AAPLx", "1", 200))


# ------------------------------------------------------------------- REST

@pytest.fixture()
def client(env):
    app = FastAPI()
    app.include_router(build_zap_api(env.db, env.e))
    return TestClient(app)


def _hdr(db, addr=OWNER):
    tok, _ = auth.mint_session(db, addr)
    return {"authorization": f"Bearer {tok}"}


def test_rest_public_view_needs_no_login_but_actions_do(env, client):
    env.ch.bal[(env.pool.underlying.address.lower(), OWNER)] = 10 * E18
    body = {"asset": "SPCXx", "amount": "1", "il_threshold_bps": 200}
    assert client.post("/api/zap/deposit", json=body).status_code == 401
    r = client.post("/api/zap/deposit", json=body, headers=_hdr(env.db))
    assert r.status_code == 200, r.text
    pid = r.json()["position_id"]
    assert r.json()["position_url"].endswith(f"/zap/{pid}")

    pub = client.get(f"/api/zap/position/{pid}")
    assert pub.status_code == 200 and pub.json()["state"] == "entering"
    assert OWNER not in pub.text  # address shortened on the shareable page

    other = _hdr(env.db, OTHER_USER)
    assert client.post(f"/api/zap/{pid}/step", headers=other).status_code == 403
    assert client.post(f"/api/zap/{pid}/threshold", json={"il_threshold_bps": 300},
                       headers=other).status_code == 403
    r = client.post(f"/api/zap/{pid}/threshold", json={"il_threshold_bps": 300},
                    headers=_hdr(env.db))
    assert r.status_code == 200 and r.json()["il"]["exit_threshold_bps"] == 300
    assert client.post(f"/api/zap/{pid}/exit", headers=_hdr(env.db)).status_code == 400
    assert client.post(f"/api/zap/{pid}/step", headers=_hdr(env.db)).json()["status"] == "sign"
    assert client.get("/api/zap/position/zap_nope").status_code == 404
    mine = client.get("/api/zap/positions", headers=_hdr(env.db)).json()["positions"]
    assert [p["position_id"] for p in mine] == [pid]


def test_mcp_tools_register():
    from mcp.server.fastmcp import FastMCP

    from sarf.providers.zap_tools import register_zap_tools

    m = FastMCP("t")
    register_zap_tools(m, ZapEngine(Database(":memory:"), None, registry()))
    names = {t.name for t in run(m.list_tools())}
    assert names == {"get_zap_pools", "zap_deposit", "get_zap_position", "set_zap_threshold",
                     "zap_exit", "zap_reenter", "zap_close"}


def test_closing_a_position_puts_the_money_back_in_the_wallet():
    """zap_exit parks in Aave and keeps watching; closing is the only way a
    position is ever realised. It must end terminal: proceeds recorded, the
    watcher off it, and no way back in."""
    db = Database(":memory:")
    eng = ZapEngine(db, None, registry())
    pid = db.create_zap_position(
        address=OWNER, pool_key="LAIKA-wSPCXx", deposit_symbol="SPCXx",
        deposit_amount=str(E18 // 2), deposit_usd=92.4, il_threshold_bps=800,
        reentry_bps=400, state="in_pool", lp_amount="1000", p_initial=50081.0,
        lp_index_initial=1.0)

    # A close gives back what went in. It must NOT carry the exit's conversion
    # to USDT: that exists so Aave has a stablecoin to hold, and handing
    # somebody a stablecoin when they deposited a stock token is a trade they
    # never asked for.
    assert "swap_rwa_to_park" not in FLOWS["close"]
    assert "approve_park_aave" not in FLOWS["close"] and "aave_supply" not in FLOWS["close"]
    assert FLOWS["close"][-1] == "unwrap"
    # Out of Aave the position is already a stablecoin; buying the stock back
    # would be a new position rather than a withdrawal.
    assert FLOWS["close_parked"] == ["aave_withdraw"]

    eng.request_close(pid, OWNER)
    assert db.get_zap_position(pid)["state"] == "close_pending"

    db.update_zap_position(pid, state="closed", realized_usd=90.15,
                           realized_amount="90150000", closed_at=1.0)
    assert "closed" not in LIVE_STATES and "closed" not in WATCHED_STATES
    for fn in (eng.request_exit, eng.request_reentry, eng.request_close):
        with pytest.raises(ValueError):
            fn(pid, OWNER)


def test_a_closed_position_reports_what_was_banked_not_a_live_mark():
    db = Database(":memory:")
    eng = ZapEngine(db, None, registry())
    pid = db.create_zap_position(
        address=OWNER, pool_key="LAIKA-wSPCXx", deposit_symbol="SPCXx",
        deposit_amount=str(E18 // 2), deposit_usd=92.4, il_threshold_bps=800,
        reentry_bps=400, state="closed", realized_usd=90.15,
        realized_amount="90150000", closed_at=1.0)
    v = run(eng.view(db.get_zap_position(pid)))
    assert v["value"]["current_usd"] == 90.15
    assert v["earnings"]["realized_usd"] == 90.15
    assert "Closed" in v["headline"] and "90.15" in v["headline"]


def test_incentives_are_counted_from_arrivals_never_estimated():
    """What is OWED cannot be read: the pot is split across pools X Layer
    picks, on its side. What ARRIVED can be, from the token's transfer log —
    so the page reports payments, each one a hash, and never a projection."""
    db = Database(":memory:")
    eng = ZapEngine(db, None, registry())
    pid = db.create_zap_position(
        address=OWNER, pool_key="LAIKA-wSPCXx", deposit_symbol="SPCXx",
        deposit_amount=str(E18 // 2), deposit_usd=92.4, il_threshold_bps=800,
        reentry_bps=400, state="in_pool", entered_at=100.0)

    v = run(eng.view(db.get_zap_position(pid)))
    paid = v["earnings"]["paid_separately"]
    assert paid["received_usdg"] == 0 and paid["drops"] == []
    assert paid["amount_owed_usdg"] is None
    assert paid["claimable_here"] is False
    assert paid["claim"]["url"].startswith("https://")
    assert "il_bps_now" in v["earnings"]   # no yield figure without its IL

    # Two arrivals land; both are counted, and a rescan of the same logs does
    # not pay anybody twice.
    rows = [{"tx_hash": "0x" + "ab" * 32, "log_index": 4, "address": OWNER.lower(),
             "token": eng.REWARD_TOKEN, "symbol": "USDG", "amount": "1250000",
             "decimals": 6, "sender": "0x" + "cd" * 20, "block": 10, "at": 200.0},
            {"tx_hash": "0x" + "ef" * 32, "log_index": 1, "address": OWNER.lower(),
             "token": eng.REWARD_TOKEN, "symbol": "USDG", "amount": "750000",
             "decimals": 6, "sender": "0x" + "cd" * 20, "block": 20, "at": 300.0}]
    assert db.record_zap_rewards(rows) == 2
    assert db.record_zap_rewards(rows) == 0

    paid = run(eng.view(db.get_zap_position(pid)))["earnings"]["paid_separately"]
    assert paid["received_usdg"] == 2.0 and paid["drop_count"] == 2
    assert paid["drops"][0]["tx_hash"].startswith("0x")

    # Arrivals from before the position existed are not its earnings.
    db.record_zap_rewards([{**rows[0], "tx_hash": "0x" + "11" * 32, "at": 50.0,
                            "amount": "9000000"}])
    assert run(eng.view(db.get_zap_position(pid)))["earnings"]["paid_separately"][
        "received_usdg"] == 2.0


def test_steps_carry_a_gas_limit_wide_enough_for_the_token_s_heavy_path():
    """A taxed token sells its collected tax into the pair on some transfers
    and not others. A wallet estimating for itself measures whichever path the
    current state implies and sends exactly that, so the transfer that trips
    the swap-back runs out of gas: mainnet 0xfd0440b5 reverted at 99.2% of its
    limit with no logs, and the retry survived by 6,500 gas."""
    eng = ZapEngine(Database(":memory:"), None, registry())

    async def estimate(*, from_address, to, data, value=0):
        return 215_000

    zapmod.rpc.estimate_gas = estimate
    heavy = run(eng._gas_for(zapmod.Step("swap_in", "t", "0x" + "11" * 20, "0xabcd"), OWNER))
    light = run(eng._gas_for(zapmod.Step("approve", "t", "0x" + "11" * 20, "0xabcd"), OWNER))
    assert heavy >= 215_000 * 1.5          # room for the swap-back path
    assert light >= 215_000 + 30_000       # every step gets some headroom
    assert heavy > light

    # An estimate that cannot be made must not block the step; the wallet
    # still gets its own chance.
    async def boom(**_):
        raise RuntimeError("node said no")

    zapmod.rpc.estimate_gas = boom
    assert run(eng._gas_for(zapmod.Step("swap_in", "t", "0x" + "11" * 20, "0xabcd"), OWNER)) is None


def _close(env, pid):
    """Drive a close the way a wallet would, on the fake chain."""
    ch, p = env.ch, env.pool
    pos = env.db.get_zap_position(pid)

    def answers(st):
        if st["kind"] == "remove_liquidity":
            lp = int(pos["lp_amount"])
            a, b = lp * ch.r_rwa // ch.ts, lp * ch.r_other // ch.ts
            ch.r_rwa -= a
            ch.r_other -= b
            ch.ts -= lp
            return [_transfer(p.rwa.address, p.pair, OWNER, a),
                    _transfer(p.other.address, p.pair, OWNER, b),
                    _sync(p.pair, ch.r_rwa, ch.r_other)]
        if st["kind"] == "swap":
            amt = int.from_bytes(bytes.fromhex(st["tx"]["data"][10:74]), "big")
            got = _v2_out(amt, ch.r_other, ch.r_rwa)
            ch.r_other += amt
            ch.r_rwa -= got
            return [_transfer(p.rwa.address, p.pair, OWNER, got)]
        if st["kind"] == "unwrap":
            shares = int.from_bytes(bytes.fromhex(st["tx"]["data"][10:74]), "big")
            return [_transfer(p.underlying.address, p.rwa.address, OWNER, shares)]
        assert st["kind"] == "approve", st["kind"]
        return []

    return _sign_through(env, pid, answers)


def test_close_hands_back_the_asset_that_was_deposited(env):
    """Someone who brings SPCXx gets SPCXx back. The exit converts to USDT
    because Aave needs a stablecoin to hold; a close has no such reason, and
    a stablecoin is not what they deposited."""
    pid, _, _ = _enter(env)
    env.e.request_close(pid, OWNER)
    seen, end = _close(env, pid)

    # No aggregator swap to USDT, and no Aave leg.
    # The other-token approval survives from the entry, so that step is
    # skipped rather than paid for again.
    assert seen == ["approve", "remove_liquidity", "swap", "unwrap"]
    # The driver asks once more after the last step; by then there is nothing
    # left to sign because the position is finished.
    assert end["status"] == "nothing_to_sign" and end["state"] == "closed"

    pos = env.db.get_zap_position(pid)
    assert pos["state"] == "closed"
    assert pos["realized_symbol"] == env.pool.underlying.symbol == "SPCXx"
    assert int(pos["realized_amount"]) > 0
    assert pos["lp_amount"] is None and pos["parked_amount"] is None

    ev = [x for x in env.db.zap_events(pid) if x["kind"] == "closed"][-1]
    assert "SPCXx" in ev["proceeds"] and "USDT" not in ev["proceeds"]

    v = run(env.e.view(env.db.get_zap_position(pid)))
    assert "SPCXx" in v["headline"] and v["state"] == "closed"
