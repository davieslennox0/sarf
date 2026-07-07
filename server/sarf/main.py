"""Sarf MCP server (remote/streamable-HTTP).

Wiring only — policy lives in config.py, enforcement in validation.py, tools
in providers/. Runs behind Caddy (TLS) under pm2; binds loopback by default.

Auth model (assumption, documented in SECURITY.md): identity is the Sui
address. There was no reusable server-side zkLogin session code to inherit
(the referenced pattern is a frontend wallet-popup flow), and unsigned
proposals for an address you don't control are unusable — the wallet
signature is the real authorization. Optional MCP_AUTH_TOKEN gates the whole
endpoint for private deployments.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP

from .api import build_api, tvl_refresher
from .config import settings
from .db import Database
from .providers.current_finance import CurrentFinanceProvider
from .registry import AssetRegistry
from .txclient import txbuilder

mcp = FastMCP(
    "Sarf",
    instructions=(
        "Non-custodial Sui lending assistant (Current Finance). propose_* tools "
        "only BUILD transactions and return them for the user to sign in their "
        "own wallet — always show the user the human_summary and every "
        "risk_note before they decide, and share the sign_url so they can "
        "review and sign on the Sarf signer page. Never imply an action was "
        "executed until submit_signed_transaction (or the signer page) "
        "returns a tx_digest."
    ),
    stateless_http=True,
    json_response=True,
)

db = Database(settings.db_path)
registry = AssetRegistry(txbuilder)
provider = CurrentFinanceProvider(db, txbuilder, registry)
provider.register_tools(mcp)
# Extension point: AftermathProvider(db, aftermath_txbuilder, registry)
# .register_tools(mcp) once providers/aftermath.py exists.


@asynccontextmanager
async def lifespan(app: FastAPI):
    tvl_task = asyncio.create_task(tvl_refresher(db, txbuilder))
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        tvl_task.cancel()


app = FastAPI(title="Sarf", lifespan=lifespan)


# --- Minimal hardening middleware -------------------------------------------
# Sliding-window rate limit per client IP. In-memory on purpose: one uvicorn
# worker (pm2 runs the process singleton), and the state is advisory —
# correctness never depends on it.
_hits: dict[str, deque[float]] = defaultdict(deque)
_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        ip = request.client.host if request.client else "?"
        now = time.time()
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= settings.rate_limit_per_minute:
            return Response("rate limited", status_code=429)
        q.append(now)

        if _AUTH_TOKEN:
            supplied = ""
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:]
            supplied = supplied or request.headers.get("x-api-key", "") or request.query_params.get("key", "")
            if not hmac.compare_digest(supplied, _AUTH_TOKEN):
                return Response("unauthorized", status_code=401)

    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "sarf", "leverage_max": settings.leverage_max_multiplier}


app.include_router(build_api(db, txbuilder, provider))

# Dashboard + signer static bundle (built by `npm run build` in frontend/).
# Explicit routes/mounts win over the catch-all MCP mount below; /dashboard
# serves the SPA and /sign deep-links into it (SPA router handles the path).
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/dashboard", StaticFiles(directory=_FRONTEND_DIST, html=True), name="dashboard")

    @app.get("/")
    async def index():
        return RedirectResponse("/dashboard/")

    @app.get("/sign")
    async def sign_page(request: Request):
        # The SPA is served from /dashboard/; forward the ?p=sarf_... param.
        qs = request.url.query
        return RedirectResponse(f"/dashboard/sign{'?' + qs if qs else ''}", status_code=307)


app.mount("/", mcp.streamable_http_app())


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
