#!/usr/bin/env python3
"""Close the open Earn long-test positions for real, on mainnet, and report
the REALIZED numbers (what actually came back, read from receipts) rather
than the unrealized mark-to-market figures earn_longtest_report.py prints.

    .venv/bin/python scripts/earn_longtest_close.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for line in open("/root/sarf/.env", errors="ignore"):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from eth_account import Account                                  # noqa: E402
from eth_utils import to_checksum_address                        # noqa: E402

from sarf.db import Database                                     # noqa: E402
from sarf.xlayer import earn_execution as ex                      # noqa: E402
from sarf.xlayer import rpc as xrpc                               # noqa: E402
from sarf.xlayer.okx_dex import OkxDexClient                     # noqa: E402
from sarf.xlayer.registry import registry                        # noqa: E402
from sarf.xlayer.zap import ZapEngine                             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "earn_longtest.db")
VENUES_CFG = json.load(open(os.path.join(ROOT, "sarf", "xlayer", "earn_venues.json")))

KEY = "0x" + next(l.split("=", 1)[1].strip() for l in open("/root/.sarf-zap-testwallet")
                  if l.startswith("SARF_ZAP_TEST_PRIVATE_KEY=")).removeprefix("0x")
ACCT = Account.from_key(KEY)
OWNER = ACCT.address.lower()


async def send(tx: dict) -> str:
    nonce = int(await xrpc._call("eth_getTransactionCount", [ACCT.address, "pending"]), 16)
    gas_price = int(await xrpc._call("eth_gasPrice", []), 16)
    try:
        gas = int(int(await xrpc._call("eth_estimateGas", [
            {"from": ACCT.address, "to": to_checksum_address(tx["to"]), "data": tx["data"],
             "value": hex(int(tx.get("value") or 0))}]), 16) * 1.5)
    except Exception as e:
        print(f"      !! gas estimate failed: {e}")
        gas = 900_000
    body = {"chainId": 196, "to": to_checksum_address(tx["to"]), "data": tx["data"],
            "value": int(tx.get("value") or 0), "nonce": nonce, "gasPrice": gas_price, "gas": gas}
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


async def drive_withdraw(e: ZapEngine, position_id: str) -> list[str]:
    hashes = []
    for _ in range(60):
        step = await ex.next_step(e, VENUES_CFG, xrpc, position_id, OWNER)
        if step["status"] == "sign":
            print(f"    [leg {step['leg_index']} {step['leg_kind']}] {step['kind']}")
            h = await send(step["tx"])
            hashes.append(h)
            await ex.submit_step(e, position_id, OWNER, h, step["leg_index"])
        elif step["status"] == "awaiting_confirmation":
            await asyncio.sleep(2)
        else:
            print(f"    withdraw flow: {step['status']}")
            return hashes
    raise SystemExit("withdraw did not complete in 60 steps")


async def close_one(e: ZapEngine, position_id: str) -> None:
    pos = e.db.get_earn_position(position_id)
    label = "SCOPED" if len(pos["legs"]) == 1 else "FULL"
    print(f"\n=== closing {label} {position_id} ===")
    ex.request_withdraw(e, position_id, OWNER)
    await drive_withdraw(e, position_id)
    final = e.db.get_earn_position(position_id)
    deposit_units = int(final["deposit_units"])
    out_units = ex.total_out_units(final)
    pct = (out_units / deposit_units - 1) * 100 if deposit_units else 0
    age_h = (time.time() - final["created_at"]) / 3600
    print(f"  REALIZED: {deposit_units/1e18:.8f} in -> {out_units/1e18:.8f} SPCXx out "
         f"({pct:+.2f}% real round-trip), {age_h:.1f}h open")
    for leg in final["legs"]:
        tag = leg.get("pool_key") or f"Aave {leg.get('reserve_symbol')}"
        print(f"    {tag:22} out_units={leg.get('out_units')}")


async def main():
    e = ZapEngine(Database(DB_PATH), OkxDexClient(), registry())
    rows = e.db.earn_positions_for(OWNER)
    active = [p for p in rows if p["state"] == "active"]
    if not active:
        print("no active positions to close")
        return
    for pos in active:
        await close_one(e, pos["position_id"])

    print("\n--- final wallet SPCXx balance ---")
    from sarf.xlayer.registry import registry as reg_fn
    asset = reg_fn().resolve("SPCXx")
    bal = await xrpc.erc20_balance(asset.address, OWNER)
    print(f"{bal/1e18:.8f} SPCXx")


asyncio.run(main())
