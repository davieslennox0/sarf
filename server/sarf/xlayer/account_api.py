"""REST for the parts of an account that were chat-only: xPoints, and
stop-loss / take-profit levels.

Each endpoint runs the MCP tool's own function under the caller's session,
so the website and the assistant apply the same validation and give the same
answers. Setting a level moves nothing. Whether levels are watched and acted
on is the server's RISK_WATCH_ENABLED setting, and the response says which.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from .. import auth
from ..config import settings
from ..db import Database
from ..validation import ValidationError
from . import xstocks_points
from .registry import XStocksRegistry


def build_account_api(db: Database, reg: XStocksRegistry, provider) -> APIRouter:
    r = APIRouter(prefix="/api/me")

    def _addr(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        addr, state = auth.resolve_session_state(db, authorization[7:])
        if not addr:
            raise HTTPException(401, "session expired. Sign in with your wallet again"
                                if state == "expired" else "invalid session")
        auth.bind_session(addr, "valid")
        return addr

    @r.get("/xpoints")
    async def xpoints(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _addr(authorization)
        return await provider._get_xpoints()

    # Official xStocks xPoints. Registration is the user's own EIP-191
    # signature over xStocks' text; the page fetches the exact message here,
    # has the wallet sign it, and posts it back to be checked and relayed.
    # Nothing here can register a wallet the session does not belong to.
    def _xpoints_on() -> None:
        if not settings.xstocks_points_enabled:
            raise HTTPException(404, "official xPoints are not enabled on this server")

    @r.get("/xpoints/register-message")
    async def xpoints_register_message(
            authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _addr(authorization)
        _xpoints_on()
        ts = int(time.time())
        return {"message": xstocks_points.registration_message(ts), "timestamp": ts,
                "referral_code": settings.xstocks_referral_code or None}

    @r.post("/xpoints/register")
    async def xpoints_register(body: dict[str, Any],
                               authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        _xpoints_on()
        sig, ts = body.get("signature"), body.get("timestamp")
        if not isinstance(sig, str) or not isinstance(ts, int):
            raise HTTPException(400, "signature (hex string) and timestamp (int) are required")
        out = await xstocks_points.register(addr, sig, ts)
        if out["status"] in ("registered", "already_registered"):
            db.link_xpoints(addr, "registered")
        elif out["status"] == "rejected":
            raise HTTPException(400, out["reason"])
        return out

    @r.post("/xpoints/link")
    async def xpoints_link(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """For a wallet that already signed up on xStocks: the session proves
        ownership, so no second signature; this only records the opt-in."""
        addr = _addr(authorization)
        _xpoints_on()
        known = await xstocks_points.is_registered(addr)
        if known is None:
            raise HTTPException(503, "could not reach xStocks; try again")
        if not known:
            raise HTTPException(404, "this wallet is not registered on xStocks yet")
        db.link_xpoints(addr, "linked")
        return {"status": "linked"}

    @r.delete("/xpoints/link")
    async def xpoints_unlink(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Stop Sarf reading the balance. The xStocks account itself stays;
        only xStocks can remove that."""
        addr = _addr(authorization)
        return {"unlinked": db.unlink_xpoints(addr)}

    @r.get("/levels")
    async def levels(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        rows = db.risk_params_for(addr)
        assets = []
        for row in rows:
            a = reg.get(row["symbol"])
            if a:
                row["symbol"] = a.symbol
                assets.append(a)
        prices = await provider.display_prices(assets, budget=2.0) if assets else {}
        for row in rows:
            row["price"] = prices.get(row["symbol"])
        return {
            "levels": rows,
            "auto_execute": settings.risk_watch_enabled,
            "note": ("A breached level sells the whole position automatically when it fits "
                     "your session key; otherwise the sell is built for you to sign."
                     if settings.risk_watch_enabled else
                     "Levels are a watch list: Sarf flags a breached level when you ask about "
                     "that asset or your portfolio. It does not sell on its own."),
        }

    @r.post("/levels")
    async def set_level(body: dict[str, Any],
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _addr(authorization)

        def num(k):
            v = body.get(k)
            return None if v in (None, "") else float(v)
        try:
            return await provider._set_risk_params(
                symbol=str(body.get("symbol") or ""),
                stop_loss=num("stop_loss"), take_profit=num("take_profit"))
        except (ValidationError, ValueError, TypeError) as e:
            raise HTTPException(400, str(e))

    return r
