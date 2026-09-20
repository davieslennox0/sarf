"""MCP tools for the single-asset zap. Thin wrappers over xlayer/zap.py's
ZapEngine, which the REST endpoints (xlayer/zap_api.py) call too. Both
surfaces run the same code for splitting, IL and exit/re-entry.

Like every Sarf tool, these act on the session's verified wallet. None of them
takes an address argument, so a tool call can never read or act on somebody
else's position by naming it.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..auth import require_address
from ..config import settings
from ..xlayer.zap import ZapEngine


def position_url(position_id: str) -> str:
    return f"{settings.public_url}/zap/{position_id}"


def register_zap_tools(mcp: FastMCP, engine: ZapEngine) -> None:

    async def _view(pos: dict[str, Any]) -> dict[str, Any]:
        v = await engine.view(pos, public_url=position_url(pos["position_id"]), for_owner=True)
        if v["action_needed"]:
            v["sign_url"] = v["position_url"]
        return v

    def _owned_errors(fn):
        # The engine raises LookupError/PermissionError for a position that is
        # missing or someone else's. Both reach the model as ordinary errors.
        try:
            return fn()
        except (LookupError, PermissionError) as e:
            raise ValueError(str(e))

    @mcp.tool()
    async def get_zap_pools() -> dict[str, Any]:
        """List the X Layer RWA incentive pools the zap can deposit into.

        These are Uniswap V2 pools from X Layer's RWA Liquidity Incentive
        Program (current round), each pairing a wrapped xStock with an ecosystem
        token. Every pair address was checked on-chain against Uniswap's
        official factory. Includes live reserves and the current pool price."""
        out = []
        for p in engine.pools.values():
            st = await engine.pool_state(p)
            usd = await engine.rwa_usd(p)
            out.append({
                **p.public(),
                "zap_with": [p.underlying.symbol, p.rwa.symbol],
                "price": f"{st['price']:,.2f} {p.other.symbol} per {p.rwa.symbol}",
                "tvl_usd_estimate": (round(2 * st["reserve_rwa"] / 10 ** p.rwa.decimals * usd)
                                     if usd else None),
            })
        return {
            "pools": out,
            "incentive_program": engine.incentive,
            "exit_parking": "Aave V3 USDT on X Layer",
            "note": ("No RWA+stablecoin pair is incentivised in the current round, so every "
                     "pool pairs the xStock with a volatile ecosystem token. IL here is driven "
                     "by that token's price as much as the stock's."),
        }

    @mcp.tool()
    async def zap_deposit(
        asset: Annotated[str, Field(description="What the user is depositing: 'SPCXx' (or 'wSPCXx'), or 'NVDAx' (or 'wNVDAx')")],
        amount: Annotated[str, Field(description="Amount of `asset` in whole tokens, e.g. '0.5'")],
        il_threshold_bps: Annotated[int, Field(description="Exit when impermanent loss exceeds this, in basis points (100 = 1%). 1-5000")],
        reentry_threshold_bps: Annotated[int | None, Field(
            default=None, description="Re-enter when IL falls back to or below this. Default: half of il_threshold_bps")] = None,
        pool: Annotated[str | None, Field(
            default=None, description="Pool key from get_zap_pools, e.g. 'LAIKA-wSPCXx'. Default: the deepest pool for the asset")] = None,
    ) -> dict[str, Any]:
        """Deposit ONE asset into an X Layer RWA incentive pool with IL protection.

        Sarf wraps the xStock into the token the pool lists (SPCXx to wSPCXx,
        ERC-4626), swaps the fee-exact optimal share of it into the pool's
        other token through the same pool, and adds both sides as Uniswap V2
        liquidity. From then on it tracks impermanent loss against the entry
        price. Past il_threshold_bps it moves the position to Aave V3 as USDT,
        and when IL is back under reentry_threshold_bps it re-enters and resets
        the entry price.

        Non-custodial: this returns a position and a position_url. The user
        signs each step in their own wallet on that page (approve, wrap, swap,
        add liquidity). Give them the link. The position is not live until
        those are signed, and nothing has moved before that. Exits and
        re-entries are decided automatically but also signed by the wallet.

        Always relay the headline: current IL next to any yield figure, and
        the disclosure."""
        address = require_address()
        pos = await engine.create(address, asset, amount, il_threshold_bps,
                                  reentry_threshold_bps, pool)
        v = await _view(pos)
        v["next_step"] = (f"Open {v['position_url']} and sign the deposit steps in your wallet. "
                          "Only an approval is needed when the allowance is short; the page "
                          "builds each step from live state as you go.")
        return v

    @mcp.tool()
    async def get_zap_position(
        position_id: Annotated[str | None, Field(
            default=None, description="A zap_... id. Omit to list all of this wallet's zap positions")] = None,
    ) -> dict[str, Any]:
        """State of the user's zap position(s): in the pool or parked in Aave,
        current IL in bps against the entry price, entry price, current value
        vs a 50/50 hold and vs holding the original asset, measured pool-fee
        return and Aave APY (always beside the IL), and the full exit/re-entry
        history. Each has a position_url, a shareable page with the same data.
        If action_needed is set, give the user sign_url."""
        address = require_address()
        if position_id:
            pos = _owned_errors(lambda: engine._owned(position_id, address))
            return await _view(pos)
        positions = [p for p in engine.db.zap_positions_for(address) if p["state"] != "cancelled"]
        return {"positions": [await _view(p) for p in positions],
                "count": len(positions),
                "auto_watch": settings.zap_autoexit_enabled}

    @mcp.tool()
    async def set_zap_threshold(
        position_id: Annotated[str, Field(description="A zap_... id")],
        il_threshold_bps: Annotated[int, Field(description="New exit threshold in bps (100 = 1%)")],
        reentry_threshold_bps: Annotated[int | None, Field(
            default=None, description="New re-entry threshold in bps. Default: half the exit threshold")] = None,
    ) -> dict[str, Any]:
        """Change when a zap position exits and re-enters. Takes effect on the
        watcher's next pass."""
        address = require_address()
        pos = _owned_errors(lambda: engine.set_thresholds(
            position_id, address, il_threshold_bps, reentry_threshold_bps))
        return await _view(pos)

    @mcp.tool()
    async def zap_exit(
        position_id: Annotated[str, Field(description="A zap_... id that is in the pool")],
    ) -> dict[str, Any]:
        """Exit a zap position now, whatever the IL: withdraw the liquidity,
        convert to USDT and park it in Aave V3. Returns the sign_url for the
        exit steps."""
        address = require_address()
        pos = _owned_errors(lambda: engine.request_exit(position_id, address))
        return await _view(pos)

    @mcp.tool()
    async def zap_close(
        position_id: Annotated[str, Field(description="A zap_... id in the pool or parked in Aave")],
    ) -> dict[str, Any]:
        """Close a zap position for good: unwind it and send the proceeds back
        to the user's own wallet as USDT, instead of parking them in Aave.

        This is how a position is REALISED. zap_exit only moves the money to
        Aave and keeps watching; this ends the position, and Sarf stops
        watching it and will not re-enter. Works from the pool or from Aave.
        Returns the sign_url for the closing steps.
        """
        address = require_address()
        pos = _owned_errors(lambda: engine.request_close(position_id, address))
        return await _view(pos)

    @mcp.tool()
    async def zap_reenter(
        position_id: Annotated[str, Field(description="A zap_... id that is parked in Aave")],
    ) -> dict[str, Any]:
        """Re-enter a parked zap position now, without waiting for IL to fall
        under the re-entry threshold. Resets the entry price. Returns the
        sign_url for the re-entry steps."""
        address = require_address()
        pos = _owned_errors(lambda: engine.request_reentry(position_id, address))
        return await _view(pos)
