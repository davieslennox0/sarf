"""MCP tools for Earn's "RWA Fixed Yield" venue. Thin wrappers over
xlayer/earn_pendle.py's position lifecycle, which the REST endpoints
(xlayer/earn_rwa_api.py) call too — same split as zap_tools.py / zap_api.py.

Named and branded as Earn throughout, on purpose: Pendle is the fixed-yield
protocol underneath, disclosed honestly where it's technically relevant
(the PT token, the market), but it is never the product name a user or the
model-facing tool surface sees.

Like every Sarf tool, these act on the session's verified wallet. None of
them takes an address argument, so a tool call can never read or act on
somebody else's position by naming it.

These tools never sign a transaction themselves — they create or change a
position and return a position_url, same as zap_deposit. The actual
approve/swap steps are signed by the user's own wallet on that page, which
drives next_step/submit_step through the REST API. earn_pendle.py's own
functions already raise ValidationError (a ValueError subclass) for every
user-facing failure, so nothing here needs to translate exception types the
way zap_tools.py does for the zap engine's LookupError/PermissionError.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..auth import require_address
from ..config import settings
from ..db import Database
from ..xlayer import earn_pendle as earn_rwa_exec
from ..xlayer.okx_dex import OkxDexClient
from ..xlayer.registry import XStocksRegistry
from ..xlayer.zap import _units

# One entry per fixed-yield market this venue can deposit into — matches
# earn_venues.json's "pendle_rwa.markets" exactly; kept here too so this
# read-only listing tool has no dependency on earn_pendle.py carrying
# display data it has no other use for.
_MARKETS = [
    {"label": "USDG-29OCT2026", "maturity": "2026-10-29T00:00:00Z", "implied_apy_pct": 3.19},
]


def position_url(position_id: str) -> str:
    return f"{settings.public_url}/earn-rwa/{position_id}"


def register_earn_rwa_tools(mcp: FastMCP, db: Database, reg: XStocksRegistry,
                            dex: OkxDexClient) -> None:

    def _view(pos: dict[str, Any]) -> dict[str, Any]:
        v = {**pos, "position_url": position_url(pos["position_id"])}
        if pos["state"] in ("entering", "exiting", "buying_rwa"):
            v["sign_url"] = v["position_url"]
        return v

    @mcp.tool()
    async def get_earn_rwa_markets() -> dict[str, Any]:
        """List the fixed-yield markets Earn's "RWA Fixed Yield" venue can
        deposit into on X Layer.

        USDG's own reserves are cash and short-term US Treasury bills (Paxos,
        attested monthly) — the underlying market is tagged "stables" AND
        "rwa" by the venue that runs it, which is why this is called an RWA
        product. Depositing is principal-protected by construction: the
        deposit always comes back as plain stablecoin, and ONLY the proceeds
        actually earned (never the principal) buy the RWA asset the user
        names at deposit time.

        implied_apy_pct is the live fixed rate quoted right now, not a
        forecast — relay it as "what locking in today pays", not a promise
        about tomorrow's rate on a NEW deposit.
        """
        return {
            "markets": [{**m, "deposit_tokens": list(earn_rwa_exec.DEPOSIT_TOKENS)}
                       for m in _MARKETS],
            "how_it_works": (
                "Deposit a stablecoin, it buys a fixed-yield Principal Token (PT) that "
                "redeems 1:1 to the stablecoin at maturity for a fixed, already-known "
                "yield. On withdrawal, principal returns as stablecoin; only the proceeds "
                "(redeemed minus principal, never negative) buy the RWA asset named at "
                "deposit time."
            ),
            "principal_protected": True,
            "designated_rwa": "chosen by the user at deposit time; must be an xStock Sarf "
                              "already lists and allows — nothing else.",
        }

    @mcp.tool()
    async def earn_rwa_deposit(
        deposit_asset: Annotated[str, Field(description="Stablecoin to deposit: 'USDT0', 'USDC', or 'USDG'")],
        amount: Annotated[str, Field(description="Amount of `deposit_asset` in whole tokens, e.g. '10'")],
        designated_rwa: Annotated[str, Field(description="Which xStock the PROCEEDS (not the principal) buy at withdrawal, e.g. 'SPCXx' or 'NVDAx'")],
    ) -> dict[str, Any]:
        """Deposit into Earn's RWA Fixed Yield venue.

        Principal-protected: the deposited stablecoin always comes back as
        stablecoin. Only the proceeds actually earned by maturity (or by an
        early exit) go on to buy `designated_rwa` — worst case the proceeds
        are zero and nothing is bought, never a loss beyond what the fixed
        position itself already accounts for.

        Returns a position and a position_url. The user signs each step
        (approve, then the entry swap) in their own wallet on that page —
        nothing moves before that."""
        address = require_address()
        amount_units = _units(amount, 6)  # every accepted deposit token here is 6dp
        pos = await earn_rwa_exec.open_deposit(
            db, reg, settings.rwa_allowlist, address, deposit_asset, amount_units, designated_rwa)
        v = _view(pos)
        v["next_step"] = (f"Open {v['position_url']} and sign the deposit steps in your "
                          "wallet. Only an approval is needed when the allowance is short.")
        return v

    @mcp.tool()
    async def get_earn_rwa_position(
        position_id: Annotated[str | None, Field(
            default=None, description="An earnrwa_... id. Omit to list all of this wallet's Earn RWA positions")] = None,
    ) -> dict[str, Any]:
        """State of the user's Earn RWA Fixed Yield position(s): principal,
        PT held, state, and — once withdrawn — the realized proceeds and how
        much of the designated RWA they bought. Each has a position_url and,
        while action is needed, a sign_url."""
        address = require_address()
        if position_id:
            pos = db.get_earn_rwa_position(position_id)
            if not pos or pos["address"] != address.lower():
                raise ValueError("no such Earn RWA position for this account")
            return _view(pos)
        positions = db.earn_rwa_positions_for(address)
        return {"positions": [_view(p) for p in positions], "count": len(positions)}

    @mcp.tool()
    async def earn_rwa_withdraw(
        position_id: Annotated[str, Field(description="An earnrwa_... id that is open")],
    ) -> dict[str, Any]:
        """Withdraw an Earn RWA Fixed Yield position: redeem the PT back to
        the deposit stablecoin, return the principal, and — only if there are
        real proceeds — buy the designated RWA with them. Returns the
        sign_url for the redeem (and, if there are proceeds, the RWA buy)
        steps."""
        address = require_address()
        pos = await earn_rwa_exec.request_withdraw(db, address, position_id)
        return _view(pos)
