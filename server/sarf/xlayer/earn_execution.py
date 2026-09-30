"""Execute an Earn deposit and withdraw: real transactions, built the same
way the zap engine already proves its own steps — one at a time, from live
chain state, and only ever credited on what a mined receipt actually shows.

WHY THIS EXISTS SEPARATELY FROM earn_allocator.py
    earn_allocator.py is read-only: it decides WHAT the split should be. This
    module is what actually MOVES the funds once a split has been decided,
    and it is new, unaudited-by-use code — none of it has run against real
    money before the test this was built for. Keeping it apart from the
    allocator means the (already trusted, already measured) decision logic
    is never touched by adding execution.

VENUES THIS CAN ACTUALLY EXECUTE
    - LP legs: reuses ZapEngine.create / next_step / submit_step /
      request_close UNCHANGED. This is the exact mechanism already proven on
      X Layer mainnet this session (22 confirmed transactions, LAIKA and
      IGNIX, deposit and close). No new swap or router code for this leg.
    - Lending legs: NEW here. A lending leg's asset is a stablecoin the user
      did not deposit, so it is a swap (via the OKX aggregator, the same
      _okx_swap already used elsewhere in zap.py) followed by Aave V3
      supply, and reversed on the way out. Both reserves this uses (USDT0,
      USDG) were confirmed in Aave's own getReservesList on 2026-09-29.
    - PAXGy is never executed here. The allocator never gives it a weight —
      it has no on-chain accrual and fails the 21-day history floor — so
      there is nothing for this module to do with it yet.

SHAPE OF THE FLOW
    Sequential, not a mixed queue: legs open one at a time, in a fixed
    order, and next_step always names which leg it is building for. A
    frontend does not exist yet for this (see EarnHome.jsx — Earn is a
    "coming soon" page), so there is no UI this needs to interleave with;
    sequential is easier to reason about correctly, and correctness is what
    matters with real funds. Each step is still the same shape zap.py's own
    next_step/submit_step return, so a future frontend drives it exactly
    like it already drives Zap.

STATE PER LEG (stored in earn_positions.legs_json)
    LP leg:      {kind:"lp", pool_key, target_units, threshold_bps,
                  zap_position_id, status}
    Lending leg: {kind:"lending", reserve, reserve_symbol, target_units,
                  status, swapped_units, supplied_units}
    status for either: "pending" -> ... -> "open" (deposit done) -> ... ->
    "closed" (withdrawal done). A leg never skips a state; next_step always
    resumes wherever a leg actually stopped, from the chain's own truth
    (balances, receipts), never from an assumption about what "should" have
    happened.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..validation import ValidationError, validate_amount
from . import rpc
from .zap import (
    Step,
    Token,
    ZapEngine,
    _calldata,
    _eth_call,
    _fmt,
    transfers_in,
)

log = logging.getLogger("sarf.earn.execution")

RECEIPT_WAIT_SECONDS = 90


async def _token_decimals(address: str) -> int:
    (dec,) = await _eth_call(address, "decimals()", [], [], out=["uint8"])
    return int(dec)


async def _receipt(tx_hash: str) -> dict[str, Any] | None:
    deadline = time.monotonic() + RECEIPT_WAIT_SECONDS
    while True:
        r = await rpc._call("eth_getTransactionReceipt", [tx_hash])
        if r is not None or time.monotonic() > deadline:
            return r
        import asyncio
        await asyncio.sleep(1.5)


async def plan_deposit(engine: ZapEngine, venues_cfg: dict[str, Any], rpc_mod,
                       asset_symbol: str, amount_units: int) -> list[dict[str, Any]]:
    """The allocator's split, turned into concrete legs with real unit
    amounts and enough detail to execute each one. Reads only — no
    transaction is built here."""
    from . import earn_allocator as EA

    venues = await EA.build_venues(engine, {**venues_cfg, "deposit_asset": {"symbol": asset_symbol}},
                                   rpc_mod)
    allocated = EA.allocate(venues)
    legs: list[dict[str, Any]] = []
    for leg in allocated:
        if leg.kind == "cash" or leg.weight <= 0:
            continue
        units = int(amount_units * leg.weight)
        if units <= 0:
            continue
        if leg.kind == "lp":
            pool_key = leg.detail.get("pool")
            pool = engine.pools.get(pool_key.upper()) if pool_key else None
            if pool is None:
                continue
            v = await engine.pool_viability(pool)
            threshold = v.get("suggested_threshold_bps") or engine.DEFAULT_EXIT_THRESHOLD_BPS
            legs.append({"kind": "lp", "pool_key": pool.key, "target_units": units,
                        "threshold_bps": int(threshold), "zap_position_id": None,
                        "status": "pending"})
        elif leg.kind == "lending":
            legs.append({"kind": "lending", "reserve": leg.detail["reserve"],
                        "reserve_symbol": leg.detail["asset"], "target_units": units,
                        "status": "pending", "swapped_units": 0, "supplied_units": 0})
    return legs


async def open_deposit(engine: ZapEngine, venues_cfg: dict[str, Any], rpc_mod,
                       address: str, asset_symbol: str, amount_str: str, *,
                       only_top_leg: bool = False) -> dict[str, Any]:
    """Create the earn_positions row for a new deposit. Opens nothing yet —
    the caller drives next_step/submit_step to actually move funds, exactly
    like a Zap position.

    only_top_leg restricts the split to the single highest-rated leg,
    spending the whole deposit there. It exists for testing the mechanism
    at the smallest honest scope before trusting the full multi-venue split
    with real money — not a mode a real deposit would use.
    """
    db = engine.db
    asset = engine.reg.resolve(asset_symbol)
    amount_units = validate_amount(amount_str, asset.decimals, what=f"amount ({asset_symbol})")
    held = await rpc.erc20_balance(asset.address, address)
    if held < amount_units:
        raise ValidationError(
            f"insufficient {asset_symbol}: you hold {_fmt(held, asset.decimals)} but this "
            f"deposits {_fmt(amount_units, asset.decimals)}")

    legs = await plan_deposit(engine, venues_cfg, rpc_mod, asset_symbol, amount_units)
    if not legs:
        raise ValidationError(f"no eligible venue for {asset_symbol} right now")
    if only_top_leg:
        top = max(legs, key=lambda l: l["target_units"])
        if top["kind"] != "lp":
            raise ValidationError("the top-rated venue for this asset is not an LP leg; "
                                  "only_top_leg only supports that path today")
        top["target_units"] = amount_units
        legs = [top]

    position_id = db.create_earn_position(
        address=address.lower(), asset_symbol=asset_symbol, deposit_units=str(amount_units),
        state="entering", legs=legs)
    db.log_zap_event(position_id, "earn_deposit_planned",
                     {"asset": asset_symbol, "amount_units": amount_units, "legs": legs})
    return db.get_earn_position(position_id)


async def _lp_leg_step(engine: ZapEngine, leg: dict[str, Any], address: str) -> Step | dict | None:
    """None means this leg needs nothing more; a dict {"complete": True}
    means it just finished; a Step needs a signature.

    LP legs delegate entirely to ZapEngine.next_step, which returns its own
    plain dict (not a Step) on {"status": "sign"} — it is a different, older
    return shape than the Step dataclass the lending-leg builders below use.
    That mismatch was the bug the mainnet test caught: this function used to
    hand that dict straight back, the caller's `isinstance(result, Step)`
    check never matched it, and every LP leg silently read as "awaiting
    confirmation" forever without a single real step ever being built.
    Wrapping it in a genuine Step here is the fix — one shape, everywhere.
    """
    if leg["zap_position_id"] is None:
        pool = engine.pools[leg["pool_key"].upper()]
        pos = await engine.create(address, pool.underlying.symbol,
                                  _fmt(leg["target_units"], pool.underlying.decimals, places=18),
                                  leg["threshold_bps"], pool_key=leg["pool_key"])
        leg["zap_position_id"] = pos["position_id"]
    step = await engine.next_step(leg["zap_position_id"], address)
    if step["status"] == "sign":
        tx = step["tx"]
        return Step(step["kind"], step["title"], tx["to"], tx["data"], str(tx.get("value") or "0"))
    if step["status"] in ("nothing_to_sign", "complete"):
        return {"complete": True}
    return None  # awaiting_confirmation: a submitted step is still mining


async def _lp_leg_close_step(engine: ZapEngine, leg: dict[str, Any], address: str) -> Step | dict | None:
    pos = engine.db.get_zap_position(leg["zap_position_id"])
    if pos["state"] not in ("close_pending", "closing", "closed"):
        engine.request_close(leg["zap_position_id"], address)
    step = await engine.next_step(leg["zap_position_id"], address)
    if step["status"] == "sign":
        tx = step["tx"]
        return Step(step["kind"], step["title"], tx["to"], tx["data"], str(tx.get("value") or "0"))
    if step["status"] in ("nothing_to_sign", "complete"):
        # _finish() clears flow_ctx to {} the moment a flow completes, so
        # "closed_amount" is already gone by the time we get here — that is
        # exactly the wrong place this looked at first, and it read back 0
        # every time despite the withdrawal genuinely landing in the wallet
        # (caught on a real $2 mainnet run, not in a test). realized_amount
        # is what _finish() actually leaves on the row afterwards.
        final = engine.db.get_zap_position(leg["zap_position_id"])
        leg["out_units"] = int(final.get("realized_amount") or 0)
        leg["out_symbol"] = final.get("realized_symbol")
        return {"complete": True}
    return None


async def _lending_leg_deposit_step(engine: ZapEngine, leg: dict[str, Any], address: str,
                                    asset_symbol: str) -> Step | dict | None:
    asset = engine.reg.resolve(asset_symbol)
    reserve = leg["reserve"]
    reserve_dec = await _token_decimals(reserve)
    sell = Token(asset_symbol, asset.address, asset.decimals)
    buy = Token(leg["reserve_symbol"], reserve, reserve_dec)

    if leg["status"] == "pending":
        # The aggregator does not pull tokens itself; it needs an allowance
        # first, exactly like every other OKX swap in this file (see
        # dex_approve_address's own docstring in registry.py). This step was
        # missing on the first mainnet run of this path and the swap
        # reverted on an allowance of exactly 0 — a real, on-chain failure,
        # not a simulation. Status stays "pending" until the allowance is
        # actually sufficient, so a retry after the approve lands walks
        # straight through to the swap below.
        approve = await engine._approve(sell, engine.reg.dex_approve_address,
                                        leg["target_units"], address,
                                        "for the OKX DEX aggregator")
        if approve is not None:
            return approve
        step = await engine._okx_swap(sell, buy, leg["target_units"], address)
        leg["status"] = "swapping_in"
        leg["_pending_swap_to"] = reserve
        return step
    if leg["status"] == "supplying_needs_approve":
        step = await engine._approve(buy, engine.aave_pool, leg["swapped_units"], address,
                                     "for Aave V3")
        if step is None:
            leg["status"] = "supplying"
        else:
            return step
    if leg["status"] == "supplying":
        return Step(
            "aave_supply", f"Supply {_fmt(leg['swapped_units'], reserve_dec)} "
                          f"{leg['reserve_symbol']} to Aave V3",
            engine.aave_pool, _calldata(
                "supply(address,uint256,address,uint16)",
                ["address", "uint256", "address", "uint16"],
                [reserve, leg["swapped_units"], address, 0]))
    return None


async def _lending_leg_withdraw_step(engine: ZapEngine, leg: dict[str, Any], address: str,
                                     asset_symbol: str) -> Step | dict | None:
    asset = engine.reg.resolve(asset_symbol)
    reserve = leg["reserve"]
    reserve_dec = await _token_decimals(reserve)
    sell = Token(leg["reserve_symbol"], reserve, reserve_dec)
    buy = Token(asset_symbol, asset.address, asset.decimals)

    if leg["status"] == "open":
        leg["status"] = "withdrawing"
    if leg["status"] == "withdrawing":
        return Step(
            "aave_withdraw", f"Withdraw {_fmt(leg['supplied_units'], reserve_dec)} "
                            f"{leg['reserve_symbol']} (principal + interest) from Aave V3",
            engine.aave_pool, _calldata(
                "withdraw(address,uint256,address)", ["address", "uint256", "address"],
                [reserve, 2 ** 256 - 1, address]))  # max uint = "all of it", Aave's own idiom
    if leg["status"] == "swapping_out":
        # Same allowance requirement as the entry swap — the reserve tokens
        # just withdrawn from Aave have never been approved to the
        # aggregator. This has to be its OWN status, not a plain re-check
        # inside "swapping_out": submit_step tells an approve's receipt from
        # the swap's by status alone, and the first mainnet run of this path
        # left it unable to — an approve confirmed, submit_step read "0
        # tokens transferred to me" off ITS receipt and declared the leg
        # closed, and the actual swap back to SPCXx never ran at all. The
        # funds were never at risk (they sat as USDT0/USDG in the user's own
        # wallet the whole time), but the position said "closed" over money
        # that was still one transaction from being what was promised.
        approve = await engine._approve(sell, engine.reg.dex_approve_address,
                                        leg["_withdrawn_units"], address,
                                        "for the OKX DEX aggregator")
        if approve is not None:
            leg["status"] = "swapping_out_needs_approve"
            return approve
        return await engine._okx_swap(sell, buy, leg["_withdrawn_units"], address)
    if leg["status"] == "swapping_out_needs_approve":
        # The approve is in; nothing left to check before the real swap.
        leg["status"] = "swapping_out"
        return await engine._okx_swap(sell, buy, leg["_withdrawn_units"], address)
    return None


async def next_step(engine: ZapEngine, venues_cfg: dict[str, Any], rpc_mod,
                    position_id: str, address: str) -> dict[str, Any]:
    """The next real transaction to sign for this Earn position, across
    whichever legs still have work outstanding. Advances legs that are
    waiting on nothing (e.g. an underlying zap flow between steps) without
    the caller having to know that detail."""
    db = engine.db
    pos = db.get_earn_position(position_id)
    if not pos or pos["address"] != address.lower():
        raise ValidationError("no such Earn position for this account")
    legs = pos["legs"]
    entering = pos["state"] == "entering"
    exiting = pos["state"] == "exiting"
    if not (entering or exiting):
        return {"status": pos["state"], "position": pos}

    # "open" is a leg's terminal status while entering, but its STARTING
    # point while exiting — a leg has to have finished depositing before it
    # can be withdrawn. Skipping on "open" unconditionally treated every leg
    # as already-withdrawn the instant a withdrawal began, closing the whole
    # position without moving anything: the mainnet test caught this too.
    already_done = "closed" if exiting else "open"
    for i, leg in enumerate(legs):
        if leg["status"] == already_done:
            continue
        if leg["kind"] == "lp":
            result = await (_lp_leg_step(engine, leg, address) if entering
                            else _lp_leg_close_step(engine, leg, address))
        else:
            result = await (_lending_leg_deposit_step(engine, leg, address, pos["asset_symbol"])
                            if entering else
                            _lending_leg_withdraw_step(engine, leg, address, pos["asset_symbol"]))
        if isinstance(result, dict) and result.get("complete"):
            leg["status"] = "open" if entering else "closed"
            db.update_earn_position(position_id, legs=legs)
            continue
        if isinstance(result, Step):
            db.update_earn_position(position_id, legs=legs)
            tx = {"chainId": 196, "to": result.to, "data": result.data, "value": result.value}
            return {"status": "sign", "leg_index": i, "leg_kind": leg["kind"],
                    "kind": result.kind, "title": result.title, "tx": tx}
        # result is None: this leg's underlying zap flow is between steps
        # (awaiting_confirmation) — nothing new to sign, but not stuck either.
        return {"status": "awaiting_confirmation", "leg_index": i}

    # every leg is open/closed
    db.update_earn_position(position_id, state=("active" if entering else "closed"))
    return {"status": "complete", "position": db.get_earn_position(position_id)}


async def submit_step(engine: ZapEngine, position_id: str, address: str, tx_hash: str,
                      leg_index: int) -> dict[str, Any]:
    """Bind a broadcast hash to the leg it was built for, wait for it to
    mine, and credit only what the receipt actually shows arrived."""
    db = engine.db
    pos = db.get_earn_position(position_id)
    if not pos or pos["address"] != address.lower():
        raise ValidationError("no such Earn position for this account")
    legs = pos["legs"]
    leg = legs[leg_index]

    if leg["kind"] == "lp":
        # The underlying zap position's own submit_step does the crediting;
        # nothing here duplicates that accounting.
        out = await engine.submit_step(leg["zap_position_id"], address, tx_hash)
        return out

    receipt = await _receipt(tx_hash)
    if receipt is None:
        return {"status": "pending", "tx_hash": tx_hash}
    if int(receipt.get("status", "0x0"), 16) != 1:
        db.log_zap_event(position_id, "earn_step_failed",
                         {"leg_index": leg_index, "tx_hash": tx_hash})
        raise ValidationError("that transaction reverted on-chain; nothing was credited")

    got = transfers_in(receipt, address)
    if leg["status"] == "swapping_in":
        credited = got.get(leg["_pending_swap_to"].lower(), 0)
        leg["swapped_units"] = credited
        leg["status"] = "supplying_needs_approve"
    elif leg["status"] == "supplying":
        leg["supplied_units"] = leg["swapped_units"]
        leg["status"] = "open"
    elif leg["status"] == "withdrawing":
        withdrawn = got.get(leg["reserve"].lower(), 0)
        leg["_withdrawn_units"] = withdrawn
        leg["status"] = "swapping_out"
    elif leg["status"] == "swapping_out":
        asset = engine.reg.resolve(pos["asset_symbol"])
        leg["out_units"] = got.get(asset.address.lower(), 0)
        leg["status"] = "closed"
    db.update_earn_position(position_id, legs=legs)
    db.log_zap_event(position_id, "earn_step_confirmed",
                     {"leg_index": leg_index, "tx_hash": tx_hash, "leg_status": leg["status"]})
    return {"status": "confirmed", "leg_status": leg["status"]}


def request_withdraw(engine: ZapEngine, position_id: str, address: str) -> dict[str, Any]:
    db = engine.db
    pos = db.get_earn_position(position_id)
    if not pos or pos["address"] != address.lower():
        raise ValidationError("no such Earn position for this account")
    if pos["state"] != "active":
        raise ValidationError(f"cannot withdraw from a position in state {pos['state']!r}")
    if not db.update_earn_position(position_id, expect_state="active", state="exiting"):
        raise ValidationError("the position changed state; reload it")
    return db.get_earn_position(position_id)


def total_out_units(pos: dict[str, Any]) -> int:
    """Sum of every leg's final amount, in the deposit asset's own units —
    the number that answers "how much SPCXx did I get back". Each leg
    records its own out_units when it finishes closing; see
    _lp_leg_close_step and the "swapping_out" branch of submit_step."""
    return sum(int(leg.get("out_units") or 0) for leg in pos["legs"])
