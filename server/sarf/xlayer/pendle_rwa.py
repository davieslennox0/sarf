"""Pendle PT-USDG fixed yield, tagged as Sarf's RWA product.

USDG's reserves are cash and short-term US Treasury bills (Paxos, attested
monthly) — Pendle's own platform categorises this exact market under
"stables" AND "rwa" (its own category tags, not Sarf's framing). Tokenized
Treasuries are the single largest slice of the real-world-asset market, so
this is a defensible RWA label, unlike trying to get an xStock itself listed
anywhere (see earn_venues.json's PAXGy note and this session's Aave/Timeswap/
Morpho research — none of those panned out).

THE STRUCTURE (principal-protected, by construction)
    entry:  deposit asset -> (Pendle, aggregator-routed) -> PT-USDG
    exit:   PT-USDG -> (Pendle) -> deposit asset
            proceeds = redeemed - principal
            if proceeds > 0: proceeds -> (OKX aggregator) -> designated RWA
            principal is ALWAYS returned as plain stablecoin, never put at
            RWA price risk. Worst case is proceeds of zero — the RWA leg
            can go to zero gain, never negative, because only money already
            earned ever buys it.

Pendle's own hosted API (api-v2.pendle.finance) builds the actual swap
calldata for both legs — this module never hand-builds Pendle calldata, the
same way okx_dex.py never hand-builds OKX aggregator calldata. Both are
read-and-forward: ask the specialist, sign what it returns, verify the
shape before trusting it.

VERIFIED ON CHAIN, NOT ASSUMED (2026-10-04, X Layer RPC)
    - Router 0x8888888888…F946 has real code (a proxy; 287 bytes, consistent
      with Pendle's known EIP-1967-style router proxy pattern everywhere).
    - Market 0xcfb506cb…6032 answers readTokens() -> (SY, PT, YT) that match
      exactly what Pendle's API claims for this market — the API is not
      trusted blindly, it is cross-checked against the chain it describes.
    - PT 0x9a09a9e4…fd362 answers expiry() = 2026-10-29T00:00:00Z.
    - PT is also a live Aave V3 X Layer reserve (confirmed via
      getReservesList()) — Pendle's own ecosystem designed this market to
      be loopable there; this module does not loop it, by choice (see the
      no-leverage recommendation this session already made).
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..validation import ValidationError
from .registry import CHAIN_ID
from .zap import Step, _eth_call

PENDLE_API = "https://api-v2.pendle.finance/core"

ROUTER = "0x888888888889758F76e7103c6CbF23ABbF58F946"
MARKET_USDG_OCT2026 = "0xcfb506cb34dd340e80d3df8764182a5187636032"
PT_USDG_OCT2026 = "0x9a09a9e491db3dd8ada5b1b889991ac9ad5fd362"
USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"

SLIPPAGE_DEFAULT = 0.02


class PendleError(RuntimeError):
    """Pendle API or on-chain read failure. Message is safe to surface."""


def _approve_calldata(spender: str, amount: int) -> str:
    return ("0x095ea7b3"
            + spender.lower().removeprefix("0x").rjust(64, "0")
            + f"{amount:064x}")


async def _convert(*, tokens_in: str, tokens_out: str, amount_in: int,
                   receiver: str, slippage: float = SLIPPAGE_DEFAULT) -> dict[str, Any]:
    """One call to Pendle's hosted SDK: builds the full swap, auto-routing
    through an aggregator when the input/output isn't the market's own
    SY/underlying token (e.g. USDT0 -> PT, not just USDG -> PT).
    """
    url = (f"{PENDLE_API}/v2/sdk/{CHAIN_ID}/convert"
          f"?receiver={receiver}&slippage={slippage}&enableAggregator=true"
          f"&tokensIn={tokens_in}&tokensOut={tokens_out}&amountsIn={amount_in}")
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "sarf/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
    except Exception as e:
        raise PendleError(f"Pendle API did not answer: {type(e).__name__}")
    routes = body.get("routes") or []
    if not routes:
        raise PendleError(str(body.get("message") or body.get("error")
                             or "Pendle returned no route for this conversion"))
    route = routes[0]
    return {"tx": route["tx"], "method": route["contractParamInfo"]["method"],
           "approvals": body.get("requiredApprovals") or []}


async def _allowance(token: str, owner: str, spender: str) -> int:
    (a,) = await _eth_call(token, "allowance(address,address)",
                           ["address", "address"], [owner, spender])
    return a


async def maturity() -> int:
    """Unix timestamp this PT redeems at. Read live, never hardcoded — a
    hardcoded maturity that drifts from the real contract is exactly the
    kind of silent-wrong-number bug this session has been hunting all day."""
    (exp,) = await _eth_call(PT_USDG_OCT2026, "expiry()", out=["uint256"])
    return int(exp)


async def is_mature() -> bool:
    return time.time() >= await maturity()


# ------------------------------------------------------------------- entry

async def entry_steps(address: str, token_in: str, amount_in: int) -> list[Step]:
    """Approve (if needed) + swap `token_in` into PT-USDG. 1 or 2 steps."""
    if amount_in <= 0:
        raise ValidationError("amount must be positive")
    conv = await _convert(tokens_in=token_in, tokens_out=PT_USDG_OCT2026,
                          amount_in=amount_in, receiver=address)
    steps = []
    for appr in conv["approvals"]:
        have = await _allowance(appr["token"], address, ROUTER)
        if have < int(appr["amount"]):
            steps.append(Step("approve", "Approve Pendle router", appr["token"],
                              _approve_calldata(ROUTER, int(appr["amount"]))))
    tx = conv["tx"]
    steps.append(Step("swap", "Buy PT-USDG (Pendle)", tx["to"], tx["data"],
                      str(tx.get("value") or "0")))
    return steps


# -------------------------------------------------------------------- exit

async def exit_steps(address: str, pt_units: int, token_out: str) -> list[Step]:
    """Approve (if needed) + convert PT-USDG back to `token_out`.

    Works both before maturity (sells on Pendle's AMM, subject to slippage)
    and after (Pendle's own API switches to a 1:1 redeem automatically — it
    picks the action from tokensIn/tokensOut, same endpoint either way).
    """
    if pt_units <= 0:
        raise ValidationError("nothing to redeem")
    conv = await _convert(tokens_in=PT_USDG_OCT2026, tokens_out=token_out,
                          amount_in=pt_units, receiver=address)
    steps = []
    for appr in conv["approvals"]:
        have = await _allowance(appr["token"], address, ROUTER)
        if have < int(appr["amount"]):
            steps.append(Step("approve", "Approve Pendle router", appr["token"],
                              _approve_calldata(ROUTER, int(appr["amount"]))))
    tx = conv["tx"]
    steps.append(Step("swap", "Redeem PT-USDG (Pendle)", tx["to"], tx["data"],
                      str(tx.get("value") or "0")))
    return steps


@dataclass(frozen=True)
class ExitResult:
    redeemed_units: int        # stablecoin received for the whole PT position
    principal_units: int       # what originally went in
    proceeds_units: int        # redeemed - principal, floored at 0
    has_proceeds: bool


def split_proceeds(redeemed_units: int, principal_units: int) -> ExitResult:
    """Never let a rounding/slippage wobble manufacture negative proceeds —
    the whole point of this structure is principal protection, so a result
    a few units under principal is "zero proceeds", not "negative RWA buy"."""
    proceeds = max(0, redeemed_units - principal_units)
    return ExitResult(redeemed_units=redeemed_units, principal_units=principal_units,
                      proceeds_units=proceeds, has_proceeds=proceeds > 0)
