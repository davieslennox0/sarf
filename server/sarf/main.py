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
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, oauth
from .config import settings
from .db import Database
from .providers.xlayer_rwa import XLayerRwaProvider
from .xlayer.api import build_xlayer_api
from .xlayer.okx_dex import dex
from .xlayer.registry import registry as load_registry

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
        "Sarf — non-custodial tokenized-stock (xStocks) assistant on X Layer "
        "(EVM chain 196). All tools act on the wallet-verified account bound "
        "to this connector's session token — there is no way (and no need) to "
        "pass an address. If a tool returns an authentication error the "
        "session expired: the user reconnects this connector (Claude will "
        "prompt to re-authenticate) or signs in on the Sarf dashboard. "
        "IMPORTANT — identifiers: on-chain symbols carry an x SUFFIX (AAPLx, "
        "TSLAx, SPYx). OKX's centralized order book uses an X PREFIX (XAAPL) "
        "for the same underlying; that form is NOT tradable here. Use "
        "get_rwa_list to see what exists. place_order only BUILDS an unsigned "
        "transaction — print its `card` field verbatim, give the user "
        "sign_url, and let them review and sign in their own wallet. Never "
        "imply a trade happened until a tx_hash exists; confirm it with "
        "get_status. Always relay the synthetic-exposure "
        "disclosure: xStocks track a share price and convey no ownership, "
        "dividends, or voting rights. "
        "SETTLEMENT — submitting a transaction is not the end of the turn. "
        "Never finish on an unsigned or pending card: once a tx_hash exists, "
        "poll get_status(tx_hash=...) until it is confirmed or failed, then "
        "send a follow-up MESSAGE saying which. Success: name the asset, "
        "amount, price and the hash. Failed: say so plainly and why — "
        "reverted, slippage exceeded, insufficient balance — never silence. "
        "Still pending after a reasonable wait: say it is pending and that you "
        "will report back, rather than leaving the user wondering. The last "
        "thing the user sees must be readable text, never a bare card or image "
        "with nothing written under it. "
        "LISTS — when naming more than one asset (holdings, markets, search "
        "results, anything), put each on its OWN LINE. Never run them together "
        "as comma-separated prose. This applies in chat text and in card "
        "content alike. "
        "EXECUTION — the user's instruction is a command, not an opening "
        "position to negotiate. When they name an action and its parameters "
        "('swap $4 of USDT into AAPLx'), call the tool with those values. Do "
        "not re-ask for confirmation you already have, do not substitute a "
        "different amount or asset, and do not try to talk them out of it. "
        "Building an unsigned transaction commits nothing — the user reviews "
        "and signs in their own wallet, and that signature is the real "
        "decision point, not your assessment beforehand. "
        "Never state that a capability is unavailable unless you called its "
        "tool and got an error back. swap, place_order and transfer are all "
        "live here; answering 'swap is not active' in place of a tool call is "
        "a bug, not a safeguard. "
        "Never invent a limit. The real ones are enforced server-side and "
        "surface as tool errors carrying the actual numbers: slippage bounds, "
        "the price-impact ceiling, the OKB gas reserve, passkey step-up, and "
        "the fee-rate cap. There is NO minimum order size — any amount "
        "representable on-chain is accepted, and the fee cap keeps small "
        "orders proportionate, which is the job a floor used to do. Relay the "
        "error the tool actually returned; do not preempt one from memory or "
        "guesswork. "
        "The analysis boundary below governs UNSOLICITED recommendations. It "
        "does not restrict execution: carrying out an instruction the user "
        "gave is not advising them, so do not attach warnings or "
        "second-guessing to a trade they asked for beyond the disclosures "
        "named above. "
        "ANALYSIS BOUNDARY — analyze_portfolio applies standard "
        "portfolio-analysis methodology (concentration limits, "
        "diversification, allocation), but Sarf is NOT a licensed or "
        "registered financial adviser and nothing it outputs is personalised "
        "or regulated investment advice. Say so on every analysis. State "
        "findings as a fact next to the norm it is measured against and let "
        "the user conclude — 'NVDAx is 41% of holdings, above the 15-20% band "
        "commonly used as a single-name concentration limit', never 'you "
        "should sell NVDAx'. Do not instruct the user to buy, sell, trim, "
        "hold or rebalance, and do not predict prices or returns for any "
        "asset. Sarf sees on-chain holdings only — not income, other "
        "accounts, goals, time horizon or risk tolerance — so flag that gap "
        "rather than implying you have the user's full financial picture."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security,
)

db = Database(settings.db_path)
rwa_registry = load_registry()
provider = XLayerRwaProvider(db, dex, rwa_registry)
provider.register_tools(mcp)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to serve a trading surface pointed at the wrong chain: a
    # misconfigured RPC would otherwise price and build against some other
    # network's contracts at the same addresses.
    from .xlayer import rpc as _rpc

    try:
        await _rpc.assert_chain()
    except Exception as e:  # pragma: no cover - startup guard
        logging.getLogger("sarf").error("X Layer RPC check failed: %s", e)
        if settings.env == "production":
            raise
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Sarf", lifespan=lifespan)


# --- Hardening + session middleware ------------------------------------------
# Sliding-window rate limit per client IP (uvicorn trusts Caddy's
# X-Forwarded-For, so this is the real client). In-memory on purpose: one
# uvicorn worker, and the state is advisory — correctness never depends on it.
_hits: dict[str, deque[float]] = defaultdict(deque)
_log = logging.getLogger("sarf.auth")


def _extract_token(request: Request) -> tuple[str, bool]:
    """-> (token, via_header). Bearer means an OAuth-capable client whose
    401s render as a Reconnect prompt; ?key=/x-api-key are legacy clients
    that need in-band errors for the expired case (see auth.transport_denies)."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip(), True
    return (
        (request.headers.get("x-api-key", "") or request.query_params.get("key", "")).strip(),
        False,
    )


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
        # wallet/zkLogin signature (auth.py / oauth.py). Tools act on the
        # address bound here — never on an address in tool arguments.
        #
        # 401 policy lives in auth.transport_denies: OAuth (Bearer) clients
        # get a 401 + WWW-Authenticate for every non-valid state — their
        # client turns it into a Reconnect prompt, which is how ending a
        # session in Sarf visibly disconnects the connector. Legacy ?key=
        # clients keep the in-band path for expired-but-authentic tokens
        # (a 401 is opaque to them). Nothing executes without a valid
        # session either way — tools have no address to act on.
        token, via_header = _extract_token(request)
        address, state = auth.resolve_session_state(db, token)
        if address is None and auth.transport_denies(
            state, via_header=via_header, env=settings.env
        ):
            return Response(
                "unauthorized: connect this MCP client via OAuth (it will prompt you to "
                f"sign in with your wallet at {settings.public_url or 'the Sarf dashboard'}), "
                "or mint a ?key= token on the dashboard. Sessions expire after "
                f"{settings.session_ttl_seconds // 60} minutes.",
                status_code=401,
                headers={"WWW-Authenticate": oauth.www_authenticate()},
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
    # Asset filenames carry a content hash, so a given URL's bytes never
    # change and it can be cached hard. The SPA shell that names them must
    # not be (see _index) — otherwise a browser pins itself to a past deploy.
    if request.url.path.startswith("/assets/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "service": "sarf",
        "chain": "X Layer",
        "chain_id": 196,
        "tradable_assets": len(rwa_registry.assets),
        "quote_transport": dex.transport,
        "passkey_stepup_usd": settings.passkey_stepup_usd,
        # Whether in-chat execution is even possible. A boolean only: the
        # relayer's address and gas balance are readable on-chain by anyone
        # who wants them, but publishing the balance on an unauthenticated
        # endpoint turns "is the tank low" into a free signal for anyone
        # deciding when to spam the thing.
        "delegated_execution": bool(settings.delegate_address and settings.relayer_private_key),
    }


app.include_router(build_xlayer_api(db, dex, rwa_registry, provider))
app.include_router(oauth.build_oauth(db))

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
_SPA_ROUTES = [
    "/", "/portfolio", "/markets", "/how", "/security", "/connect",
    "/settings", "/activity", "/send", "/sign", "/approve", "/dashboard",
]

# Frozen snapshot of the pre-Privy site (git tag `pre-privy`), built with
# base=/legacy/ so it carries its own assets and its router stays inside the
# sub-path. It exists so a login restructure on the main bundle can never
# leave the demo surface unreachable — rebuild with:
#   VITE_ROUTER_BASE=/legacy npx vite build --base=/legacy/ --outDir dist-legacy
_LEGACY_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist-legacy"

if _LEGACY_DIST.is_dir():
    # Registered before the mount on purpose: Mount("/legacy") also matches the
    # bare path with an empty remainder and 404s on it, so the route has to win
    # first. A shared URL should not depend on the trailing slash.
    @app.get("/legacy", include_in_schema=False)
    async def _legacy_root():
        return RedirectResponse("/legacy/", status_code=308)

    app.mount("/legacy", _SPAStaticFiles(directory=_LEGACY_DIST, html=True), name="legacy")

if _FRONTEND_DIST.is_dir():
    # Assets get a real mount; SPA routes are declared explicitly rather than
    # mounting StaticFiles at "/", which would collide with the MCP app that
    # owns the catch-all below.
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    def _index() -> FileResponse:
        # no-cache means "revalidate", not "don't cache": the etag still turns
        # an unchanged shell into a 304 with no body. Without this header the
        # browser applies heuristic freshness and can serve a shell from a
        # previous deploy, which names hashed chunks that are no longer the
        # current build — the page then runs old code with no visible error.
        return FileResponse(
            _FRONTEND_DIST / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    for _path in _SPA_ROUTES:
        app.get(_path, include_in_schema=False)(lambda: _index())

    # Sub-paths of the SPA routes, so client-side routes with a parameter
    # (/dashboard/agents) serve index.html instead of redirecting to /. These
    # are registered as a catch-all per prefix rather than added by hand: the
    # exact-match list above was silently wrong for every nested route the
    # frontend grew, and the failure looks like a broken link, not a 404.
    for _path in _SPA_ROUTES:
        if _path == "/":
            continue
        app.get(f"{_path}/{{rest:path}}", include_in_schema=False)(lambda rest="": _index())

    @app.get("/favicon.ico", include_in_schema=False)
    async def _favicon():
        f = _FRONTEND_DIST / "favicon.ico"
        return FileResponse(f) if f.exists() else Response(status_code=404)

    # The site used to live under /dashboard. Old sign links and bookmarks
    # still arrive there, so keep them working rather than 404ing.
    @app.get("/dashboard{rest:path}", include_in_schema=False)
    async def _legacy_dashboard(rest: str, request: Request):
        target = (rest or "/").rstrip("/") or "/"
        if target == "/authorize":
            target = "/approve"
        qs = request.url.query
        return RedirectResponse(f"{target}{'?' + qs if qs else ''}", status_code=308)


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
