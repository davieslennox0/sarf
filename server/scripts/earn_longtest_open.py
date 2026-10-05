#!/usr/bin/env python3
"""Open the 48h Earn long-test positions for real, on mainnet, with the zap
test wallet. Writes to data/earn_longtest.db — durable, isolated from the
live sarf.db/earn.db the running pm2 apps use.

    .venv/bin/python scripts/earn_longtest_open.py scoped 0.02099632
    .venv/bin/python scripts/earn_longtest_open.py full   0.02099632

Positions are opened and LEFT OPEN — nothing here closes anything. A
separate script (earn_longtest_report.py) reads them back after 48h.
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
from sarf.xlayer import earn_execution as ex                     # noqa: E402
from sarf.xlayer import rpc as xrpc                               # noqa: E402
from sarf.xlayer.okx_dex import OkxDexClient                     # noqa: E402
from sarf.xlayer.registry import registry                        # noqa: E402
from sarf.xlayer.zap import ZapEngine                             # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "earn_longtest.db")
VENUES_CFG = json.load(open(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sarf", "xlayer", "earn_venues.json")))

KEY = "0x" + next(l.split("=", 1)[1].strip() for l in open("/root/.sarf-zap-testwallet")
                  if l.startswith("SARF_ZAP_TEST_PRIVATE_KEY=")).removeprefix("0x")
ACCT = Account.from_key(KEY)
OWNER = ACCT.address.lower()


def engine() -> ZapEngine:
    return ZapEngine(Database(DB_PATH), OkxDexClient(), registry())


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


async def drive_deposit(e: ZapEngine, position_id: str) -> list[str]:
    hashes = []
    for _ in range(40):
        step = await ex.next_step(e, VENUES_CFG, xrpc, position_id, OWNER)
        if step["status"] == "sign":
            print(f"    [leg {step['leg_index']} {step['leg_kind']}] {step['kind']}")
            h = await send(step["tx"])
            hashes.append(h)
            await ex.submit_step(e, position_id, OWNER, h, step["leg_index"])
        elif step["status"] == "awaiting_confirmation":
            await asyncio.sleep(2)
        else:
            print(f"    deposit flow: {step['status']}")
            return hashes
    raise SystemExit("deposit did not complete in 40 steps")


async def main():
    mode, amount_str = sys.argv[1], sys.argv[2]
    e = engine()
    pos = await ex.open_deposit(e, VENUES_CFG, xrpc, OWNER, "SPCXx", amount_str,
                                only_top_leg=(mode == "scoped"))
    print(f"position {pos['position_id']}  mode={mode}  amount={amount_str} SPCXx")
    for leg in pos["legs"]:
        print(" ", leg)
    hashes = await drive_deposit(e, pos["position_id"])
    final = e.db.get_earn_position(pos["position_id"])
    print(f"\nstate: {final['state']}")
    for leg in final["legs"]:
        print(" ", leg)
    json.dump({"mode": mode, "position_id": pos["position_id"], "amount_in": amount_str,
              "hashes": hashes, "opened_at": __import__("time").time()},
              open(os.path.join(os.path.dirname(DB_PATH), f"earn_longtest_{mode}.json"), "w"),
              indent=1)


asyncio.run(main())
