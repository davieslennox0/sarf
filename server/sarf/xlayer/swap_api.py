"""REST surface for swapping on the website, without a chat.

Both endpoints sit on the code the MCP `swap` tool runs. The quote is a
read-only aggregator quote for the form's live preview. The build calls the
tool function itself under the caller's session, so a swap built here and one
built in Claude are the same order: same balance check, USD cap, price-impact
limit, fee and risk notes. The result is an unsigned order the existing /sign
page signs in the user's wallet. Nothing here can execute or broadcast.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from .. import auth
from ..db import Database
from ..validation import ValidationError, validate_amount
from .okx_dex import DexError, OkxDexClient
from .registry import XStocksRegistry

# Symbols the form offers besides the xStocks: the stable, the gas coin, and
# USDC (what a card deposit mints).
EXTRA = ["USDT", "USDC", "OKB"]


def build_swap_api(db: Database, dex: OkxDexClient, reg: XStocksRegistry, provider) -> APIRouter:
    from ..providers.xlayer_rwa import _resolve_any  # same resolver as the tool

    r = APIRouter(prefix="/api/swap")

    def _addr(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        addr, state = auth.resolve_session_state(db, authorization[7:])
        if not addr:
            raise HTTPException(401, "session expired. Sign in with your wallet again"
                                if state == "expired" else "invalid session")
        return addr

    @r.get("/tokens")
    async def tokens() -> dict[str, Any]:
        out = []
        for s in EXTRA:
            a = _resolve_any(reg, s)
            out.append({"symbol": a.symbol if s != "OKB" else "OKB", "name": getattr(a, "name", s),
                        "decimals": a.decimals, "logo_url": getattr(a, "logo_url", ""), "kind": "base"})
        for a in reg.assets:
            out.append({"symbol": a.symbol, "name": a.name, "decimals": a.decimals,
                        "logo_url": a.logo_url, "kind": "xstock"})
        return {"tokens": out}

    @r.get("/quote")
    async def quote(from_symbol: str, to_symbol: str, amount: str) -> dict[str, Any]:
        """Display-only quote for the form. The order the user signs is
        re-quoted and re-checked when it is built."""
        try:
            sell, buy = _resolve_any(reg, from_symbol), _resolve_any(reg, to_symbol)
            if sell.address.lower() == buy.address.lower():
                raise ValidationError("pick two different assets")
            units = validate_amount(amount, sell.decimals, what=f"amount ({sell.symbol})")
        except ValidationError as e:
            raise HTTPException(400, str(e))
        try:
            q = await dex.quote(sell.address, buy.address, units)
        except DexError as e:
            raise HTTPException(503, f"could not price this swap on X Layer: {e}")
        out_amount = q.to_amount / 10 ** buy.decimals
        in_amount = units / 10 ** sell.decimals
        return {
            "from_symbol": sell.symbol, "to_symbol": buy.symbol,
            "amount_in": in_amount, "amount_out": out_amount,
            "rate": out_amount / in_amount if in_amount else None,
            "price_impact_percent": q.price_impact_pct, "route": q.route,
        }

    @r.post("/build")
    async def build(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        auth.bind_session(addr, "valid")
        slip = body.get("slippage_percent")
        try:
            res = await provider._swap(
                from_symbol=str(body.get("from_symbol") or ""),
                to_symbol=str(body.get("to_symbol") or ""),
                amount=str(body.get("amount") or ""),
                slippage_percent=float(slip) if slip not in (None, "") else None,
            )
        except (ValidationError, ValueError) as e:
            raise HTTPException(400, str(e))
        text = next(c.text for c in res if getattr(c, "type", "") == "text")
        payload = json.loads(text)
        payload.pop("unsigned_transaction", None)  # the signer page loads the order itself
        payload.pop("card", None)
        return payload

    return r
