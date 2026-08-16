"""OAuth 2.1 authorization server for the MCP connector.

Why this exists: MCP clients (Claude included) treat a transport 401 as
"run the OAuth flow" and render a Reconnect button on the connector. Until
now that channel was a dead end here (no authorization server to discover),
which forced two workarounds: session tokens riding in the connector URL
(?key=…, re-pasted every expiry) and expired sessions failing in-band
instead of at the transport. With this module the connector is added ONCE
at a stable URL; Claude discovers this server, the user approves with a
wallet signature, and tokens ride in Authorization headers. Ending a
session in Sarf revokes the token, the next MCP request 401s, and Claude
shows Reconnect on the same connector entry — no stale ✓, no re-adding.

The pieces (all standard, nothing hand-rolled conceptually):
- RFC 9728 protected-resource metadata → points clients at this AS
- RFC 8414 authorization-server metadata → advertises the endpoints below
- RFC 7591 dynamic client registration → public clients, no secrets
- Authorization-code grant with PKCE S256 (the only grant)

The user-facing step of /authorize is the SAME wallet challenge–response
used everywhere else (api.issue_challenge / api.verify_wallet_challenge,
sidecar zkLogin-aware verification) — OAuth changes how a session token is
DELIVERED, not how ownership is proven or what the token is. Access tokens
are ordinary 30-minute session rows.

Deliberately NOT implemented:
- refresh tokens: renewal means a fresh wallet signature, by design.
- client secrets: public clients + PKCE only.
- scopes: one audience, one privilege level (read + propose-unsigned).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import auth
from .xlayer.api import verify_wallet_challenge
from .config import settings
from .db import Database
from .xlayer.evm import validate_evm_address as validate_address

AUTH_CODE_TTL = 600  # seconds a mint-authorization code stays exchangeable


def _issuer() -> str:
    return settings.public_url or f"http://127.0.0.1:{settings.port}"


def _resource() -> str:
    return f"{settings.mcp_public_url or _issuer()}/mcp"


def www_authenticate() -> str:
    """Header value that tells an MCP client where to start the OAuth flow.
    Sent on every transport 401 from /mcp."""
    return (
        'Bearer resource_metadata='
        f'"{settings.mcp_public_url or _issuer()}/.well-known/oauth-protected-resource"'
    )


def _as_metadata() -> dict[str, Any]:
    base = _issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/api/oauth/token",
        "registration_endpoint": f"{base}/api/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [],
    }


def _resource_metadata() -> dict[str, Any]:
    return {
        "resource": _resource(),
        "authorization_servers": [_issuer()],
        "bearer_methods_supported": ["header"],
    }


def _valid_redirect(uri: str) -> bool:
    p = urlsplit(uri)
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost"))


def _redirect_err(redirect_uri: str, state: str | None, error: str, desc: str) -> RedirectResponse:
    q = {"error": error, "error_description": desc}
    if state:
        q["state"] = state
    sep = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


def _token_error(error: str, desc: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": desc},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def build_oauth(db: Database) -> APIRouter:
    r = APIRouter()

    # --------------------------------------------------------- discovery
    # Served host-agnostically: current-spec clients fetch the resource
    # metadata on the MCP host and the AS metadata on the issuer; older
    # clients probe the MCP host for AS metadata directly. Same answers
    # everywhere. The {_:path} suffix covers RFC 9728/8414 path-component
    # variants (…/oauth-protected-resource/mcp).

    @r.get("/.well-known/oauth-protected-resource{_:path}")
    async def protected_resource_metadata(_: str = "") -> dict[str, Any]:
        return _resource_metadata()

    @r.get("/.well-known/oauth-authorization-server{_:path}")
    async def authorization_server_metadata(_: str = "") -> dict[str, Any]:
        return _as_metadata()

    # ------------------------------------------------------ registration

    @r.post("/api/oauth/register")
    async def register(body: dict[str, Any]) -> JSONResponse:
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(
            isinstance(u, str) and _valid_redirect(u) for u in uris
        ):
            raise HTTPException(400, "redirect_uris must be https (or localhost) URLs")
        client_id = secrets.token_urlsafe(24)
        name = body.get("client_name") if isinstance(body.get("client_name"), str) else None
        db.create_oauth_client(client_id, name, uris)
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "redirect_uris": uris,
                "client_name": name,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            },
            status_code=201,
        )

    # ------------------------------------------------------- authorize
    # GET /authorize only validates and hands off to the SPA consent page,
    # which drives wallet connect → challenge → signature. The code is
    # minted by /api/oauth/approve below, never by this redirect.

    @r.get("/authorize")
    async def authorize(request: Request):
        q = request.query_params
        client = db.get_oauth_client(q.get("client_id", ""))
        redirect_uri = q.get("redirect_uri", "")
        # Per OAuth 2.1: an unknown client or unregistered redirect_uri must
        # NEVER be redirected to (open-redirect vector) — hard 400 instead.
        if not client or redirect_uri not in client["redirect_uris"]:
            raise HTTPException(400, "unknown client_id or unregistered redirect_uri")
        state = q.get("state")
        if q.get("response_type") != "code":
            return _redirect_err(redirect_uri, state, "unsupported_response_type", "use code")
        if not q.get("code_challenge") or q.get("code_challenge_method", "S256") != "S256":
            return _redirect_err(redirect_uri, state, "invalid_request", "PKCE S256 required")
        # /approve, not /connect: the SPA's /connect is the public MCP setup
        # page. The consent UI needs a route nothing else claims, or a user
        # arriving mid-OAuth lands on marketing with their params intact and
        # nothing to approve.
        return RedirectResponse(f"/approve?{urlencode(dict(q))}", status_code=307)

    @r.post("/api/oauth/approve")
    async def approve(body: dict[str, Any]) -> dict[str, Any]:
        addr = validate_address(body.get("address"))
        client = db.get_oauth_client(str(body.get("client_id") or ""))
        redirect_uri = str(body.get("redirect_uri") or "")
        code_challenge = str(body.get("code_challenge") or "")
        if not client or redirect_uri not in client["redirect_uris"] or not code_challenge:
            raise HTTPException(400, "unknown client_id, redirect_uri, or missing PKCE challenge")
        # The gate: same single-use wallet-signature challenge as sign-in.
        verify_wallet_challenge(addr, body.get("signature"))
        db.upsert_user(addr, "wallet")
        code = secrets.token_urlsafe(32)
        db.put_oauth_code(code, client["client_id"], redirect_uri, code_challenge,
                          addr, AUTH_CODE_TTL)
        q = {"code": code}
        if isinstance(body.get("state"), str) and body["state"]:
            q["state"] = body["state"]
        sep = "&" if urlsplit(redirect_uri).query else "?"
        return {"redirect": f"{redirect_uri}{sep}{urlencode(q)}"}

    # ----------------------------------------------------------- token

    @r.post("/api/oauth/token")
    async def token(request: Request):
        # OAuth mandates application/x-www-form-urlencoded; parsed by hand to
        # avoid a multipart dependency. JSON accepted as a courtesy.
        raw = (await request.body()).decode()
        if request.headers.get("content-type", "").startswith("application/json"):
            import json

            form = {k: str(v) for k, v in (json.loads(raw or "{}")).items()}
        else:
            form = {k: v[0] for k, v in parse_qs(raw).items()}

        if form.get("grant_type") != "authorization_code":
            return _token_error("unsupported_grant_type",
                                "authorization_code only (no refresh tokens, by design: "
                                "session renewal requires a fresh wallet signature)")
        rec = db.consume_oauth_code(form.get("code", ""))
        if rec is None:
            return _token_error("invalid_grant", "unknown, expired, or already-used code")
        if form.get("client_id") != rec["client_id"] or (
            form.get("redirect_uri") or rec["redirect_uri"]) != rec["redirect_uri"]:
            return _token_error("invalid_grant", "code was issued to a different client")
        verifier = form.get("code_verifier", "")
        digest = hashlib.sha256(verifier.encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != rec["code_challenge"]:
            return _token_error("invalid_grant", "PKCE verification failed")
        # Carry the client's registered name onto the session, so the account
        # page can say what is connected — "Claude", not "a session".
        client = db.get_oauth_client(rec["client_id"])
        token_str, ttl = auth.mint_session(
            db, rec["address"],
            client_name=(client or {}).get("client_name"),
            client_id=rec["client_id"],
        )
        return JSONResponse(
            {"access_token": token_str, "token_type": "Bearer", "expires_in": ttl},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    return r
