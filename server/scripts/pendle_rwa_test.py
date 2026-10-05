#!/usr/bin/env python3
"""Real mainnet test of the Pendle PT-USDG "RWA" venue, end to end, with the
zap test wallet. Writes to data/pendle_rwa_test.db — durable, isolated from
sarf.db/earn.db.

This is an EARLY-exit test (same day), not a maturity redemption (the
market matures 2026-10-29) — it exists to prove the entry/exit/proceeds-
split/RWA-purchase mechanics actually work on chain, not to produce a real
yield number. A same-day round trip is expected to return proceeds very
close to zero (or clipped to zero by split_proceeds), since no time has
passed for the fixed yield to accrue.

    .venv/bin/python scripts/pendle_rwa_test.py <usdt0_amount>
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for line in open("/root/sarf/.env", errors="ignore"):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from eth_account import Account                                  # noqa: E402
from eth_utils import to_checksum_address                        # noqa: E402

from sarf.db import Database                                     # noqa: E402
from sarf.xlayer import pendle_rwa as pr                         # noqa: E402
from sarf.xlayer import rpc as xrpc                               # noqa: E402
from sarf.xlayer.okx_dex import OkxDexClient                     # noqa: E402
from sarf.xlayer.registry import registry                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "pendle_rwa_test.db")
USDT0 = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
DESIGNATED_RWA = "SPCXx"

KEY = "0x" + next(l.split("=", 1)[1].strip() for l in open("/root/.sarf-zap-testwallet")
                  if l.startswith("SARF_ZAP_TEST_PRIVATE_KEY=")).removeprefix("0x")
ACCT = Account.from_key(KEY)
OWNER = ACCT.address.lower()


async def send(tx) -> str:
    nonce = int(await xrpc._call("eth_getTransactionCount", [ACCT.address, "pending"]), 16)
    gas_price = int(await xrpc._call("eth_gasPrice", []), 16)
    try:
        gas = int(int(await xrpc._call("eth_estimateGas", [
            {"from": ACCT.address, "to": to_checksum_address(tx.to), "data": tx.data,
             "value": hex(int(tx.value or 0))}]), 16) * 1.5)
    except Exception as e:
        print(f"      !! gas estimate failed: {e}")
        gas = 900_000
    body = {"chainId": 196, "to": to_checksum_address(tx.to), "data": tx.data,
            "value": int(tx.value or 0), "nonce": nonce, "gasPrice": gas_price, "gas": gas}
    signed = ACCT.sign_transaction(body)
    h = await xrpc._call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
    for _ in range(60):
        r = await xrpc._call("eth_getTransactionReceipt", [h])
        if r:
            ok = int(r["status"], 16) == 1
            print(f"      {'ok ' if ok else 'REVERTED'} {h}  gas {int(r['gasUsed'], 16):,}")
            if not ok:
                raise SystemExit(f"step reverted: {h}")
            return h
        await asyncio.sleep(2)
    raise SystemExit(f"no receipt for {h}")


async def drive(steps, label):
    hashes = []
    for s in steps:
        print(f"  [{label}] {s.kind}: {s.title}")
        hashes.append(await send(s))
    return hashes


async def balance_after(token: str, owner: str, floor: int, tries: int = 8) -> int:
    """A receipt confirms the block is mined, not that every RPC node behind
    the load balancer has caught up to it yet — one immediate read here came
    back stale (0 right after a real, successful 1.5-PT buy). Poll briefly
    rather than trust the first read."""
    for _ in range(tries):
        bal = await xrpc.erc20_balance(token, owner)
        if bal != floor:
            return bal
        await asyncio.sleep(2)
    return bal


async def main():
    amount_in = int(float(sys.argv[1]) * 1_000_000)  # USDT0, 6dp
    db = Database(DB_PATH)
    dex = OkxDexClient()
    reg = registry()

    pt_before = await xrpc.erc20_balance(pr.PT_USDG_OCT2026, OWNER)
    print(f"entering: {amount_in/1e6} USDT0 -> PT-USDG, PT balance before: {pt_before/1e6}")
    steps = await pr.entry_steps(OWNER, USDT0, amount_in)
    await drive(steps, "entry")

    pt_after = await balance_after(pr.PT_USDG_OCT2026, OWNER, pt_before)
    pt_bought = pt_after - pt_before
    print(f"PT bought: {pt_bought/1e6} (balance now {pt_after/1e6})")

    position_id = db.create_pendle_position(
        address=OWNER, deposit_symbol="USDT0", principal_units=str(amount_in),
        designated_rwa_symbol=DESIGNATED_RWA, pt_units=str(pt_bought), state="open")
    print(f"position {position_id}")

    print("\n--- early exit (same-day, mechanics test only) ---")
    usdt_before = await xrpc.erc20_balance(USDT0, OWNER)
    exit_steps = await pr.exit_steps(OWNER, pt_bought, USDT0)
    await drive(exit_steps, "exit")
    usdt_after = await balance_after(USDT0, OWNER, usdt_before)
    redeemed = usdt_after - usdt_before
    print(f"redeemed: {redeemed/1e6} USDT0")

    result = pr.split_proceeds(redeemed, amount_in)
    print(f"principal {result.principal_units/1e6}  redeemed {result.redeemed_units/1e6}  "
         f"proceeds {result.proceeds_units/1e6}  has_proceeds={result.has_proceeds}")

    rwa_units = 0
    rwa_hash = None
    if result.has_proceeds:
        asset = reg.resolve(DESIGNATED_RWA)
        spcxx_before = await xrpc.erc20_balance(asset.address, OWNER)
        tx, quote, fee_applied = await dex.build_swap(
            from_address=USDT0, to_address=asset.address,
            amount_min_units=result.proceeds_units, user_address=OWNER, slippage_pct=1.0)
        print(f"  [proceeds->RWA] swap {result.proceeds_units/1e6} USDT0 -> {DESIGNATED_RWA}")
        rwa_hash = await send(tx)
        spcxx_after = await balance_after(asset.address, OWNER, spcxx_before)
        rwa_units = spcxx_after - spcxx_before
        print(f"  RWA bought: {rwa_units/10**asset.decimals} {DESIGNATED_RWA}")
    else:
        print("  no proceeds this round (expected for a same-day exit) — nothing bought")

    db.update_pendle_position(
        position_id, state="closed", redeemed_units=str(result.redeemed_units),
        proceeds_units=str(result.proceeds_units), rwa_units=str(rwa_units))
    print(f"\nfinal position: {db.get_pendle_position(position_id)}")


asyncio.run(main())
