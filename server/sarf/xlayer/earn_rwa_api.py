"""REST surface for Earn's "RWA Fixed Yield" venue. The same earn_pendle.py
functions the MCP tools call (providers/earn_rwa_tools.py), so chat and
website can't disagree about a position's state.

GET /api/earn-rwa/{position_id} is public on purpose, same reasoning as the
zap's equivalent: a position page is meant to be shared and bookmarked, and
it shows what the chain already shows. Anything that changes a position, or
builds a transaction for it, needs the owner's session.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from .. import auth
from ..config import settings
from ..db import Database
from ..providers.earn_rwa_tools import position_url
from ..validation import ValidationError
from . import earn_pendle as earn_rwa_exec
from .okx_dex import OkxDexClient
from .registry import XStocksRegistry
from .zap import _units

PUBLIC_PER_MINUTE = 60


def build_earn_rwa_api(db: Database, reg: XStocksRegistry, dex: OkxDexClient) -> APIRouter:
    r = APIRouter(prefix="/api/earn-rwa")
    hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def _addr(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        addr, state = auth.resolve_session_state(db, authorization[7:])
        if not addr:
            raise HTTPException(401, "session expired. Sign in with your wallet again"
                                if state == "expired" else "invalid session")
        return addr

    async def _run(coro):
        try:
            return await coro
        except ValidationError as e:
            raise HTTPException(400, str(e))

    def _view(pos: dict[str, Any]) -> dict[str, Any]:
        v = {**pos, "position_url": position_url(pos["position_id"])}
        if pos["state"] in ("entering", "exiting", "buying_rwa"):
            v["sign_url"] = v["position_url"]
        return v

    @r.get("/markets")
    async def markets() -> dict[str, Any]:
        """Public, read-only: the fixed-yield markets this venue can deposit
        into, plus which stablecoins it accepts. Registered before
        /{position_id} on purpose — a literal path has to win over the
        catch-all or "markets" gets read as a position id."""
        return {
            "markets": [{**m, "deposit_tokens": list(earn_rwa_exec.DEPOSIT_TOKENS)}
                       for m in earn_rwa_exec.MARKETS],
            "principal_protected": True,
        }

    @r.get("/{position_id}")
    async def public_position(position_id: str, request: Request) -> dict[str, Any]:
        ip = request.client.host if request.client else "?"
        q, now = hits[ip], time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PUBLIC_PER_MINUTE:
            raise HTTPException(429, "too many requests")
        q.append(now)
        pos = db.get_earn_rwa_position(position_id.strip())
        if not pos:
            raise HTTPException(404, "unknown Earn RWA position")
        return _view(pos)

    @r.get("/positions/mine")
    async def my_positions(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        ps = db.earn_rwa_positions_for(addr)
        return {"positions": [_view(p) for p in ps]}

    @r.post("/deposit")
    async def deposit(body: dict[str, Any],
                      authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        amount_units = _units(str(body.get("amount") or ""), 6)
        pos = await _run(earn_rwa_exec.open_deposit(
            db, reg, settings.rwa_allowlist, addr, str(body.get("deposit_asset") or ""),
            amount_units, str(body.get("designated_rwa") or "")))
        return _view(pos)

    @r.post("/{position_id}/withdraw")
    async def withdraw(position_id: str,
                       authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        pos = await _run(earn_rwa_exec.request_withdraw(db, addr, position_id))
        return _view(pos)

    @r.post("/{position_id}/step")
    async def next_step(position_id: str,
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _run(earn_rwa_exec.next_step(db, reg, dex, addr, position_id))

    @r.post("/{position_id}/step/submitted")
    async def step_submitted(position_id: str, body: dict[str, Any],
                             authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _run(earn_rwa_exec.submit_step(
            db, reg, addr, position_id, str(body.get("tx_hash") or "")))

    return r
