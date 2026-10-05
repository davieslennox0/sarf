"""Pendle PT fixed-yield deposits inside Earn — the "RWA Fixed Yield" venue,
where the USER names which RWA their proceeds buy.

WHY THIS IS SEPARATE FROM earn_execution.py
    earn_execution.py's positions are SPCXx-denominated, multi-leg blends
    across LP + Aave lending (see earn_allocator.py). This venue's deposit
    asset is a stablecoin, not SPCXx, and its one leg is the PT itself — a
    different currency and a different shape, not a drop-in extra leg.
    Keeping it in its own module and its own table (pendle_positions) means
    neither the allocator's SPCXx math nor the proven multi-leg dispatcher
    in earn_execution.py has to learn a second deposit currency.

THE STRUCTURE (see pendle_rwa.py for the chain-level mechanics and the case
for calling this "RWA" — Pendle itself tags the USDG market "rwa")
    deposit:  stablecoin -> PT-USDG-29OCT2026 (Pendle, aggregator-routed)
    withdraw: PT -> stablecoin; principal returned as-is; only the PROCEEDS
              (redeemed - principal, floored at 0) buy the user's chosen
              RWA asset, via the same OKX aggregator every other Sarf trade
              uses. The user's choice is validated against the exact same
              registry + RWA_ALLOWLIST every other RWA purchase in Sarf goes
              through at deposit time — nothing here gets to name an
              unvetted token, and it can't be changed after the fact.

STATE
    entering -> open -> exiting -> (buying_rwa if proceeds > 0) -> closed.
    Each call to next_step re-asks Pendle for the current route (its own
    allowance check skips an approve that already landed on chain), so
    there is no separate step-index to keep in sync with chain truth — the
    same statelessness pendle_rwa.py's entry_steps/exit_steps already give
    it. Credited only from what a mined receipt's own Transfer logs show,
    the same discipline earn_execution.py uses for its own legs.
"""

from __future__ import annotations

from typing import Any

from ..validation import ValidationError
from . import pendle_rwa as pendle
from .earn_execution import _receipt
from .okx_dex import OkxDexClient
from .registry import XStocksRegistry
from .zap import transfers_in

# Stablecoins this venue accepts as a deposit asset -> their X Layer address.
# Kept local rather than reaching into zap_pools.json's aave section, which
# names only one park asset — this venue can take any of the three Sarf
# already treats as stable.
DEPOSIT_TOKENS = {
    "USDT0": "0x779ded0c9e1022225f8e0630b35a9b54be713736",
    "USDC": "0xb6ceceab302e2e4948951ee7843fc24e92933061",
    "USDG": pendle.USDG,
}

# One entry per fixed-yield market this venue can deposit into. The single
# source of truth for both the MCP tool (get_earn_rwa_markets) and the public
# REST listing (GET /api/earn-rwa/markets) — kept here, not duplicated in
# each surface, so the two can never quote a different rate.
MARKETS = [
    {"label": "USDG-29OCT2026", "maturity": "2026-10-29T00:00:00Z", "implied_apy_pct": 3.19},
]


def _token_address(symbol: str) -> str:
    addr = DEPOSIT_TOKENS.get(symbol.upper())
    if not addr:
        raise ValidationError(
            f"this venue only accepts {', '.join(DEPOSIT_TOKENS)} as a deposit asset")
    return addr


async def open_deposit(db, reg: XStocksRegistry, allowlist, address: str,
                       deposit_symbol: str, amount_units: int,
                       designated_rwa_symbol: str) -> dict[str, Any]:
    """Validate everything up front — bad RWA symbol, bad deposit asset, or a
    non-positive amount all fail here, before any chain call."""
    _token_address(deposit_symbol)  # raises if not accepted
    if amount_units <= 0:
        raise ValidationError("amount must be positive")
    asset = reg.resolve(designated_rwa_symbol, allowlist=allowlist)  # raises if bad/disallowed
    position_id = db.create_earn_rwa_position(
        address=address.lower(), deposit_symbol=deposit_symbol.upper(),
        principal_units=str(amount_units), designated_rwa_symbol=asset.symbol,
        pt_units="0", state="entering")
    return db.get_earn_rwa_position(position_id)


def _load(db, address: str, position_id: str) -> dict[str, Any]:
    pos = db.get_earn_rwa_position(position_id)
    if not pos or pos["address"] != address.lower():
        raise ValidationError("no such Pendle position for this account")
    return pos


async def next_step(db, reg: XStocksRegistry, dex: OkxDexClient, address: str,
                    position_id: str) -> dict[str, Any]:
    pos = _load(db, address, position_id)
    state = pos["state"]

    if state == "entering":
        steps = await pendle.entry_steps(
            address, _token_address(pos["deposit_symbol"]), int(pos["principal_units"]))
    elif state == "exiting":
        steps = await pendle.exit_steps(
            address, int(pos["pt_units"]), _token_address(pos["deposit_symbol"]))
    elif state == "buying_rwa":
        asset = reg.resolve(pos["designated_rwa_symbol"])
        tx, _quote, _fee = await dex.build_swap(
            from_address=_token_address(pos["deposit_symbol"]), to_address=asset.address,
            amount_min_units=int(pos["proceeds_units"]), user_address=address, slippage_pct=1.0)
        return {"status": "sign", "kind": "swap", "title": f"Buy {asset.symbol} with proceeds",
               "tx": {"chainId": 196, "to": tx.to, "data": tx.data, "value": tx.value}}
    else:
        return {"status": state, "position": pos}

    step = steps[0]
    return {"status": "sign", "kind": step.kind, "title": step.title,
           "tx": {"chainId": 196, "to": step.to, "data": step.data, "value": step.value}}


async def submit_step(db, reg: XStocksRegistry, address: str, position_id: str,
                      tx_hash: str) -> dict[str, Any]:
    pos = _load(db, address, position_id)
    receipt = await _receipt(tx_hash)
    if receipt is None:
        return {"status": "pending", "tx_hash": tx_hash}
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise ValidationError("that transaction reverted on-chain; nothing was credited")
    got = transfers_in(receipt, address)
    state = pos["state"]

    if state == "entering":
        pt_in = got.get(pendle.PT_USDG_OCT2026.lower(), 0)
        if not pt_in:
            return {"status": "confirmed", "state": "entering"}  # that was just the approve
        db.update_earn_rwa_position(position_id, state="open",
                                  pt_units=str(int(pos["pt_units"] or 0) + pt_in))
        return {"status": "confirmed", "state": "open"}

    if state == "exiting":
        token = _token_address(pos["deposit_symbol"])
        redeemed_in = got.get(token.lower(), 0)
        if not redeemed_in:
            return {"status": "confirmed", "state": "exiting"}  # that was just the approve
        result = pendle.split_proceeds(redeemed_in, int(pos["principal_units"]))
        next_state = "buying_rwa" if result.has_proceeds else "closed"
        db.update_earn_rwa_position(
            position_id, state=next_state, redeemed_units=str(result.redeemed_units),
            proceeds_units=str(result.proceeds_units),
            **({} if result.has_proceeds else {"rwa_units": "0"}))
        return {"status": "confirmed", "state": next_state}

    if state == "buying_rwa":
        asset = reg.resolve(pos["designated_rwa_symbol"])
        rwa_in = got.get(asset.address.lower(), 0)
        if not rwa_in:
            return {"status": "confirmed", "state": "buying_rwa"}  # that was just the approve
        db.update_earn_rwa_position(position_id, state="closed", rwa_units=str(rwa_in))
        return {"status": "confirmed", "state": "closed"}

    return {"status": "confirmed", "state": state}


async def request_withdraw(db, address: str, position_id: str) -> dict[str, Any]:
    pos = _load(db, address, position_id)
    if pos["state"] != "open":
        raise ValidationError(f"cannot withdraw from a position in state {pos['state']!r}")
    if not db.update_earn_rwa_position(position_id, expect_state="open", state="exiting"):
        raise ValidationError("the position changed state; reload it")
    return db.get_earn_rwa_position(position_id)
