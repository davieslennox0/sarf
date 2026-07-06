"""SuiFlow MCP server (remote/streamable-HTTP).

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

import hmac
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from mcp.server.fastmcp import FastMCP

from .config import settings
from .db import Database
from .providers.current_finance import CurrentFinanceProvider
from .registry import AssetRegistry
from .txclient import txbuilder

mcp = FastMCP(
    "SuiFlow",
    instructions=(
        "Non-custodial Sui lending assistant (Current Finance). propose_* tools "
        "only BUILD transactions and return them for the user to sign in their "
        "own wallet — always show the user the human_summary and every "
        "risk_note before they decide. Never imply an action was executed "
        "until submit_signed_transaction returns a tx_digest."
    ),
    stateless_http=True,
    json_response=True,
)

db = Database(settings.db_path)
registry = AssetRegistry(txbuilder)
CurrentFinanceProvider(db, txbuilder, registry).register_tools(mcp)
# Extension point: AftermathProvider(db, aftermath_txbuilder, registry)
# .register_tools(mcp) once providers/aftermath.py exists.


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="SuiFlow", lifespan=lifespan)


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
    return {"ok": True, "service": "suiflow", "leverage_max": settings.leverage_max_multiplier}


app.mount("/", mcp.streamable_http_app())


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
