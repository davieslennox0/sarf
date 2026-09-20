"""REST surface for the single-asset zap. The same ZapEngine methods the MCP
tools call (providers/zap_tools.py), so chat and website can't disagree
about a split, an IL figure or a transition.

GET /api/zap/position/{id} is public on purpose: a position page is meant to
be shared and bookmarked, and it shows what the chain already shows (the
wallet's LP and Aave balances), with the owner's address shortened. Anything
that changes a position, or builds a transaction for it, needs the owner's
session.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from .. import auth
from ..db import Database
from ..providers.zap_tools import position_url
from ..validation import ValidationError
from .zap import ZapEngine

PUBLIC_VIEW_TTL = 10.0
PUBLIC_PER_MINUTE = 60


def build_zap_api(db: Database, engine: ZapEngine) -> APIRouter:
    r = APIRouter(prefix="/api/zap")
    cache: dict[str, tuple[float, dict[str, Any]]] = {}
    hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def _addr(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        addr, state = auth.resolve_session_state(db, authorization[7:])
        if not addr:
            raise HTTPException(401, "session expired. Sign in with your wallet again"
                                if state == "expired" else "invalid session")
        return addr

    async def _run(coro_or_fn):
        """Engine errors -> HTTP errors with the engine's own message, which
        is written for the person reading it."""
        try:
            res = coro_or_fn()
            return await res if hasattr(res, "__await__") else res
        except LookupError as e:
            raise HTTPException(404, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except (ValidationError, ValueError) as e:
            raise HTTPException(400, str(e))

    async def _owner_view(pos: dict[str, Any]) -> dict[str, Any]:
        return await engine.view(pos, public_url=position_url(pos["position_id"]), for_owner=True)

    @r.get("/pools")
    async def pools() -> dict[str, Any]:
        out = []
        for p in engine.pools.values():
            try:
                st = await engine.pool_state(p)
                price = st["price"]
            except Exception:
                price = None
            out.append({**p.public(), "zap_with": [p.underlying.symbol, p.rwa.symbol],
                        "price": price,
                        "buy_tax_pct": p.other.buy_tax_bps / 100,
                        "sell_tax_pct": p.other.sell_tax_bps / 100,
                        "exit_and_reentry_cost_bps": engine.cycle_cost_bps(p)})
        return {"pools": out, "incentive_program": engine.incentive}

    @r.get("/position/{position_id}")
    async def public_position(position_id: str, request: Request) -> dict[str, Any]:
        ip = request.client.host if request.client else "?"
        q, now = hits[ip], time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PUBLIC_PER_MINUTE:
            raise HTTPException(429, "too many requests")
        q.append(now)
        pos = db.get_zap_position(position_id.strip())
        if not pos:
            raise HTTPException(404, "unknown zap position")
        # Keyed on the row's updated_at as well as time: a watcher transition
        # or a confirmed step shows up on the next load, not ten seconds on.
        key = f"{pos['position_id']}:{pos['updated_at']}"
        hit = cache.get(key)
        if hit and now - hit[0] < PUBLIC_VIEW_TTL:
            return hit[1]
        v = await engine.view(pos, public_url=position_url(pos["position_id"]))
        if len(cache) > 500:
            cache.clear()
        cache[key] = (now, v)
        return v

    @r.get("/positions")
    async def my_positions(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        ps = [p for p in db.zap_positions_for(addr) if p["state"] != "cancelled"]
        return {"positions": [await _owner_view(p) for p in ps]}

    @r.post("/deposit")
    async def deposit(body: dict[str, Any],
                      authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        pos = await _run(lambda: engine.create(
            addr, str(body.get("asset") or ""), str(body.get("amount") or ""),
            body.get("il_threshold_bps"), body.get("reentry_threshold_bps"),
            body.get("pool") or None))
        return await _owner_view(pos)

    @r.post("/{position_id}/threshold")
    async def threshold(position_id: str, body: dict[str, Any],
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        pos = await _run(lambda: engine.set_thresholds(
            position_id, addr, body.get("il_threshold_bps"), body.get("reentry_threshold_bps")))
        return await _owner_view(pos)

    @r.post("/{position_id}/exit")
    async def exit_now(position_id: str,
                       authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _owner_view(await _run(lambda: engine.request_exit(position_id, addr)))

    @r.post("/{position_id}/reenter")
    async def reenter_now(position_id: str,
                          authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _owner_view(await _run(lambda: engine.request_reentry(position_id, addr)))

    @r.post("/{position_id}/close")
    async def close_now(position_id: str,
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _owner_view(await _run(lambda: engine.request_close(position_id, addr)))

    @r.post("/{position_id}/cancel")
    async def cancel(position_id: str,
                     authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _owner_view(await _run(lambda: engine.cancel(position_id, addr)))

    @r.post("/{position_id}/step")
    async def next_step(position_id: str,
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _run(lambda: engine.next_step(position_id, addr))

    @r.post("/{position_id}/step/submitted")
    async def step_submitted(position_id: str, body: dict[str, Any],
                             authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _addr(authorization)
        return await _run(lambda: engine.submit_step(position_id, addr, str(body.get("tx_hash") or "")))

    return r
