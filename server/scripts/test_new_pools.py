#!/usr/bin/env python3
"""Real mainnet test of the two newly-added zap pools (XDOG-wSPCXx,
TAPEOUTX-wNVDAx): deposit, confirm in_pool, close, confirm funds return.
Same ZapEngine.create/next_step/submit_step/request_close path every other
pool this session was proven on — nothing pool-specific in the test, which
is the point: these two must work through the exact same, already-trusted
machinery, not a special case.

    .venv/bin/python scripts/test_new_pools.py <pool_key> <asset> <amount>
"""
import asyncio
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
from sarf.xlayer import rpc as xrpc                               # noqa: E402
from sarf.xlayer.okx_dex import OkxDexClient                     # noqa: E402
from sarf.xlayer.registry import registry                        # noqa: E402
from sarf.xlayer.zap import Step, ZapEngine                      # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "new_pools_test.db")

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


async def drive(e: ZapEngine, position_id: str, label: str) -> None:
    for _ in range(40):
        step = await e.next_step(position_id, OWNER)
        if step["status"] == "sign":
            print(f"  [{label}] {step['kind']}: {step['title']}")
            h = await send(step["tx"])
            await e.submit_step(position_id, OWNER, h)
        elif step["status"] == "awaiting_confirmation":
            await asyncio.sleep(2)
        else:
            print(f"  [{label}] flow: {step['status']}")
            return
    raise SystemExit(f"{label} did not complete in 40 steps")


async def main():
    pool_key, asset, amount = sys.argv[1], sys.argv[2], sys.argv[3]
    e = ZapEngine(Database(DB_PATH), OkxDexClient(), registry())

    print(f"=== {pool_key}: deposit {amount} {asset} ===")
    pos = await e.create(OWNER, asset, amount, 1500, pool_key=pool_key)
    position_id = pos["position_id"]
    print("position:", position_id)
    await drive(e, position_id, "enter")

    final = e.db.get_zap_position(position_id)
    print(f"state after entry: {final['state']}  lp_amount={final.get('lp_amount')}")
    v = await e.view(final, public_url="x")
    print("IL:", v["il"]["current_bps"], "bps   pool fee return:", v["yield"]["pool_fee_return_since_entry_pct"], "%")
    print("has incentive_program in pool view:", "incentive_program" in v["pool"])
    print("has paid_separately in earnings:", "paid_separately" in v["earnings"])

    print(f"\n=== {pool_key}: close ===")
    e.request_close(position_id, OWNER)
    await drive(e, position_id, "close")
    closed = e.db.get_zap_position(position_id)
    print(f"final state: {closed['state']}  realized_amount={closed.get('realized_amount')} {closed.get('realized_symbol')}")


asyncio.run(main())
