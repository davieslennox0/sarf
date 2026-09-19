"""REST for the parts of an account that were chat-only: xPoints, and
stop-loss / take-profit levels.

Each endpoint runs the MCP tool's own function under the caller's session,
so the website and the assistant apply the same validation and give the same
answers. Setting a level moves nothing. Whether levels are watched and acted
on is the server's RISK_WATCH_ENABLED setting, and the response says which.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException

from .. import auth
from ..config import settings
from ..db import Database
from ..validation import ValidationError
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
