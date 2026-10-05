#!/usr/bin/env python3
"""Read back the 48h Earn long-test positions WITHOUT closing them, value
every leg at today's live state, compare to what went in, and send the
result to Telegram via /root/ryu/auto/notify.sh.

Read-only end to end: no transaction is built or signed. LP legs are valued
from the pool's live reserves and the position's own lp_amount (never
touched); lending legs are valued by reading the aToken balance directly —
Aave's aTokens rebase, so balanceOf() already is principal + accrued
interest, no index math needed.

    .venv/bin/python scripts/earn_longtest_report.py
"""
import asyncio
import json
import math
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for line in open("/root/sarf/.env", errors="ignore"):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from sarf.db import Database                                    # noqa: E402
from sarf.xlayer import earn_execution as ex                    # noqa: E402
from sarf.xlayer import rpc as xrpc                              # noqa: E402
from sarf.xlayer.okx_dex import OkxDexClient                    # noqa: E402
from sarf.xlayer.registry import registry                       # noqa: E402
from sarf.xlayer.zap import ZapEngine, _eth_call                # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "earn_longtest.db")
OWNER = "0xf0139a53d77246ee6cc506753784034a3da74039"

# SPCXx/USD at deposit time, measured 0.2h after both positions opened (the
# very first run of this script, 2026-09-30 13:14 UTC). Not persisted anywhere
# else when the positions were opened, so this is hardcoded rather than
# fabricated-but-wrong: it is the real measured price, just recorded here
# instead of on the position row. Needed because converting everything to
# "SPCXx terms" at TODAY's SPCXx price silently mixes SPCXx's own price move
# into every leg's reported return — including the Aave legs, which hold
# flat USD stablecoins and should never show a "loss" just because SPCXx
# itself got more expensive. That bug shipped in the first two runs of this
# script (reported -7%..-16% "losses" that were mostly SPCXx rallying, not
# any leg actually losing money) before being caught against real USD values.
ENTRY_SPCXX_USD = 149.6380
A_TOKENS = {  # from zap_pools.json (USDT0) and read live earlier this session (USDG)
    "0x779ded0c9e1022225f8e0630b35a9b54be713736": "0xF356ae412dB5df43BD3a10746f7ad4e1C4De4297",
    "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8": "0x228765a3c18065c923f23a0ccb6c7cefb3ea2223",
}


def spcxx_price() -> float:
    o = subprocess.run(["/root/.local/bin/onchainos", "token", "price-info", "--chain", "196",
                        "--address", "0x68fa48b1c2fe52b3d776e1953e0e782b5044ce28"],
                       capture_output=True, text=True)
    d = json.loads(o.stdout)["data"]
    d = d[0] if isinstance(d, list) else d
    return float(d["price"])


async def value_lp_leg(engine: ZapEngine, leg: dict) -> tuple[float, float]:
    """-> (current USD value, entry USD value at deposit)."""
    zp = engine.db.get_zap_position(leg["zap_position_id"])
    pool = engine.pool(leg["pool_key"])
    st = await engine.pool_state(pool)
    lp = int(zp["lp_amount"] or 0)
    ts = st["total_supply"] or 1
    my_rwa = lp * st["reserve_rwa"] // ts
    my_other = lp * st["reserve_other"] // ts
    price = await engine.rwa_usd(pool)  # RWA (wrapped) in USD
    # Value both legs of the LP in USD directly — no SPCXx conversion here,
    # so SPCXx's own price move never leaks into this leg's reported return.
    rwa_usd_val = (my_rwa / 10 ** pool.rwa.decimals) * price
    # other-token USD: derive from the pool's own ratio (other per rwa) x rwa price
    other_per_rwa = st["price"]  # other tokens per 1 whole rwa, from pool_state
    other_usd_price = price / other_per_rwa if other_per_rwa else 0
    other_usd_val = (my_other / 10 ** pool.other.decimals) * other_usd_price
    current_usd = rwa_usd_val + other_usd_val
    entry_usd = (leg["target_units"] / 1e18) * ENTRY_SPCXX_USD
    return current_usd, entry_usd


async def value_lending_leg(leg: dict) -> tuple[float, float]:
    a_token = A_TOKENS[leg["reserve"]]
    bal = await xrpc.erc20_balance(a_token, OWNER)
    current_usd = bal / 1e6  # both reserves are 6dp, ~$1
    entry_usd = (leg["target_units"] / 1e18) * ENTRY_SPCXX_USD
    return current_usd, entry_usd


async def report_position(engine: ZapEngine, label: str, position_id: str) -> str:
    pos = engine.db.get_earn_position(position_id)
    lines = [f"\n{label} — {position_id}"]
    total_now, total_in = 0.0, 0.0
    for leg in pos["legs"]:
        if leg["kind"] == "lp":
            now, entry = await value_lp_leg(engine, leg)
            tag = leg["pool_key"]
        else:
            now, entry = await value_lending_leg(leg)
            tag = f"Aave {leg['reserve_symbol']}"
        pct = (now / entry - 1) * 100 if entry else 0
        lines.append(f"  {tag:22} in ${entry:.4f}  now ${now:.4f}  ({pct:+.2f}% real USD)")
        total_now += now
        total_in += entry
    pct = (total_now / total_in - 1) * 100 if total_in else 0
    age_h = (time.time() - pos["created_at"]) / 3600
    apr = pct / age_h * 24 * 365 if age_h else 0
    lines.append(f"  TOTAL: ${total_in:.4f} in -> ${total_now:.4f} now "
                 f"({pct:+.2f}% real USD, {age_h:.1f}h open, "
                 f"{apr:+.1f}%/yr annualized at this rate)")
    return "\n".join(lines), total_in, total_now, pct, age_h


async def main():
    engine = ZapEngine(Database(DB_PATH), OkxDexClient(), registry())
    rows = engine.db.earn_positions_for(OWNER)
    if not rows:
        msg = "Earn 48h long-test: no positions found in data/earn_longtest.db"
        print(msg)
        subprocess.run(["/root/ryu/auto/notify.sh", msg])
        return

    parts = [f"📊 Sarf Earn — 48h long-test report ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})",
            "Test wallet, real mainnet funds. Positions left OPEN (this is a report, not a withdrawal)."]
    for pos in rows:
        label = "SCOPED (single LAIKA leg)" if len(pos["legs"]) == 1 else "FULL (4-leg blend)"
        text, total_in, total_now, pct, age_h = await report_position(engine, label, pos["position_id"])
        parts.append(text)

    px = spcxx_price()
    parts.append(f"\nSPCXx: ${ENTRY_SPCXX_USD:.4f} at deposit -> ${px:.4f} now "
                 f"({(px / ENTRY_SPCXX_USD - 1) * 100:+.2f}%)")
    parts.append("All returns above are real USD, not SPCXx-denominated — converting to "
                 "SPCXx at today's price would mix SPCXx's own move (shown on the line "
                 "above) into every leg's number, including the flat Aave legs. Figures "
                 "are unrealized mark-to-market (LP legs valued from live pool reserves, "
                 "lending legs read directly from the aToken balance) — not what closing "
                 "today would actually return after slippage and the pool's own round-trip "
                 "cost.")

    msg = "\n".join(parts)
    print(msg)
    subprocess.run(["/root/ryu/auto/notify.sh", msg])
    json.dump({"sent_at": time.time(), "message": msg},
              open(os.path.join(ROOT, "data", "earn_longtest_last_report.json"), "w"), indent=1)


asyncio.run(main())
