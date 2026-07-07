"""Session-layer authentication: proof of address ownership BEFORE tool calls.

The v1 model (identity = claimed Sui address, wallet signature only at the
final signing step) allowed anyone to read portfolios and spam proposal
generation for arbitrary addresses. This module closes that:

1. Session establishment requires a wallet signature over a one-time server
   nonce (see api.py). Verification happens in the sidecar via @mysten/sui's
   `verifyPersonalMessageSignature`, which for zkLogin signatures performs
   the full spec checks — Groth16 proof binding the OAuth JWT to the address
   seed, on-chain JWK lookup, maxEpoch freshness — and for plain wallets
   verifies the ed25519/secp signature against the address. Nothing here
   hand-rolls proof math.
2. On success the server mints its own short-lived token:

       sarf_sess_<128-bit-id-hex>.<hmac-sha256(secret, id)-hex>

   The HMAC (server signing secret) makes tokens unforgeable without a DB
   hit; the DB row (keyed by id) provides expiry and revocation. Both must
   pass. TTL default is 30 minutes (SESSION_TTL_SECONDS), hard-capped at 60
   in config.py: long enough for a propose→review→sign conversation, short
   enough that a leaked connector URL goes stale the same hour. Expiry means
   re-verification with the wallet — there is no silent renewal.
3. Every MCP tool call runs with the session's address bound to a
   ContextVar by the middleware in main.py; tools call require_address()
   instead of trusting a caller-supplied user_address parameter.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from contextvars import ContextVar
from typing import TYPE_CHECKING

from .config import settings

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database

log = logging.getLogger("sarf.auth")

_TOKEN_RE = re.compile(r"^sarf_sess_([0-9a-f]{32})\.([0-9a-f]{64})$")

# Address of the verified session for the current request, set by the /mcp
# middleware (and by REST handlers after bearer validation). None = no
# authenticated session in this context.
_session_address: ContextVar[str | None] = ContextVar("sarf_session_address", default=None)


class AuthError(Exception):
    """No or invalid session where one is required."""


def _sign(token_id: str) -> str:
    return hmac.new(settings.session_secret.encode(), token_id.encode(), hashlib.sha256).hexdigest()


def mint_session(db: "Database", address: str, ttl_seconds: int | None = None) -> tuple[str, int]:
    """Create a session for an address whose ownership was JUST proven.

    Returns (token, ttl_seconds). Only api.py's auth_verify may call this,
    after the sidecar accepted the wallet/zkLogin signature.
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.session_ttl_seconds
    token_id = secrets.token_hex(16)
    db.put_session(token_id, address, ttl)
    return f"sarf_sess_{token_id}.{_sign(token_id)}", ttl


def resolve_session(db: "Database", token: str | None) -> str | None:
    """Token -> verified address, or None. Constant-time HMAC check first
    (forged tokens never reach the DB), then the stored row decides expiry
    and revocation."""
    if not token:
        return None
    m = _TOKEN_RE.match(token.strip())
    if not m:
        return None
    token_id, sig = m.group(1), m.group(2)
    if not hmac.compare_digest(sig, _sign(token_id)):
        return None
    return db.session_address(token_id)


def revoke_token(db: "Database", token: str | None) -> None:
    m = _TOKEN_RE.match((token or "").strip())
    if m:
        db.revoke_session(m.group(1))


def bind_session(address: str | None):
    """Bind the verified address to the current request context."""
    return _session_address.set(address)


def current_address() -> str | None:
    return _session_address.get()


def require_address() -> str:
    """The address every tool acts on. Raises if the request carried no valid
    session — tools never accept an address from the caller instead."""
    addr = current_address()
    if not addr:
        raise AuthError(
            "No authenticated session. Sign in with your wallet at "
            f"{settings.public_url or 'the Sarf dashboard'} to mint a connector token, then "
            "configure the MCP connector URL with ?key=<token>. Tokens expire after "
            f"{settings.session_ttl_seconds // 60} minutes and require signing in again."
        )
    return addr
