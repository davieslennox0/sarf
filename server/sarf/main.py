"""Sarf MCP server (remote/streamable-HTTP).

Wiring only — policy lives in config.py, enforcement in validation.py and
auth.py, tools in providers/. Runs behind Caddy (TLS) under pm2; binds
loopback by default.

Auth model (see SECURITY.md): tool calls run under a verified session. The
user proves address ownership with a wallet/zkLogin signature over a server
nonce (verified via @mysten/sui in the sidecar); the server mints a
short-lived session token; the middleware below resolves that token on every
/mcp request and binds the proven address to the request context. Tools take
no user_address argument — they act on the session's address only. In
production mode, no valid session means 401.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import auth
from .api import build_api, tvl_refresher
from .config import settings
from .db import Database
from .providers.current_finance import CurrentFinanceProvider
from .registry import AssetRegistry
from .txclient import txbuilder

# Host-header validation for the MCP transport (DNS-rebinding protection).
# Loopback always allowed for dev; public hostnames come from
# SARF_ALLOWED_HOSTS since Host arrives verbatim through the Caddy proxy.
_transport_security = TransportSecuritySettings(
    allowed_hosts=["127.0.0.1:*", "localhost:*", *settings.allowed_hosts],
    allowed_origins=[
        "http://127.0.0.1:*",
        "http://localhost:*",
        *(f"https://{h}" for h in settings.allowed_hosts),
    ],
)

mcp = FastMCP(
    "Sarf",
    instructions=(
        "Non-custodial Sui lending assistant (Current Finance). All tools act "
        "on the wallet-verified account bound to this connector's session "
        "token — there is no way (and no need) to pass an address. If tools "
        "return an authentication error, the session expired: the user signs "
        "in again on the Sarf dashboard and updates the connector token. "
        "propose_* tools only BUILD transactions and return them for the user "
        "to sign in their own wallet — always show the user the human_summary "
        "and every risk_note before they decide, and share the sign_url so "
        "they can review and sign on the Sarf signer page. Never imply an "
        "action was executed until submit_signed_transaction (or the signer "
        "page) returns a tx_digest."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security,
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


# --- Hardening + session middleware ------------------------------------------
# Sliding-window rate limit per client IP (uvicorn trusts Caddy's
# X-Forwarded-For, so this is the real client). In-memory on purpose: one
# uvicorn worker, and the state is advisory — correctness never depends on it.
_hits: dict[str, deque[float]] = defaultdict(deque)
_log = logging.getLogger("sarf.auth")


def _extract_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("x-api-key", "") or request.query_params.get("key", "")).strip()


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

        # Session gate: the token was minted only after a verified
        # wallet/zkLogin signature (auth.py). Tools act on the address bound
        # here — never on an address supplied in tool arguments.
        #
        # Expired-but-authentic tokens deliberately PASS the transport: an
        # HTTP 401 is consumed by the MCP client's OAuth machinery (we run
        # none) and reaches the model as an opaque failed call, killing even
        # the handshake. Letting the request through means initialize/
        # tools/list keep working and require_address() raises an in-band
        # "session_expired: …reconnect at…" tool error the model can relay.
        # Nothing executes on an expired session — tools have no address.
        address, state = auth.resolve_session_state(db, _extract_token(request))
        if address is None and state != "expired" and settings.env == "production":
            return Response(
                "unauthorized: sign in with your wallet at "
                f"{settings.public_url or 'the Sarf dashboard'} to mint a session token, "
                "then use ?key=<token> (or a Bearer header) on this endpoint. "
                f"Tokens expire after {settings.session_ttl_seconds // 60} minutes.",
                status_code=401,
            )
        if address is None and settings.env != "production":
            # Dev mode only — deliberately loud on EVERY request so a dev
            # deployment can't quietly masquerade as production.
            _log.warning(
                "SARF DEV MODE: unauthenticated /mcp request from %s allowed; "
                "set SARF_ENV=production to fail closed", ip,
            )
        auth.bind_session(address, state)

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
class _SPAStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback: unknown paths (deep links like
    /dashboard/sign, /dashboard/activity) serve index.html so the client
    router can take over. Real assets still 404 honestly if missing."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        # Starlette signals "not found" either by raising HTTPException(404)
        # or (older versions) returning a 404 response — handle both.
        looks_like_route = "." not in path.rsplit("/", 1)[-1]
        try:
            resp = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not looks_like_route:
                raise
            return await super().get_response("index.html", scope)
        if resp.status_code == 404 and looks_like_route:
            resp = await super().get_response("index.html", scope)
        return resp


_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/dashboard", _SPAStaticFiles(directory=_FRONTEND_DIST, html=True), name="dashboard")

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
    import copy

    import uvicorn

    # MCP connector URLs carry the session token as ?key=..., and uvicorn's
    # access log records the full request line including the query string — a
    # live bearer credential must not sit in log files. Filters must ride in
    # uvicorn's log_config: uvicorn applies dictConfig at startup, which
    # would drop a filter attached to the logger earlier.
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config.setdefault("filters", {})["redact_session_tokens"] = {
        "()": auth.RedactSessionTokens,
    }
    for handler in log_config["handlers"].values():
        handler.setdefault("filters", []).append("redact_session_tokens")

    # proxy_headers: Caddy is the only client on loopback; trusting its
    # X-Forwarded-For gives the rate limiter real client IPs instead of one
    # shared 127.0.0.1 bucket for everyone.
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_config=log_config,
    )


if __name__ == "__main__":
    run()
