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
# Why there is no address: "missing" (no/forged token) vs "expired" (a token
# we provably minted whose session lapsed or was ended). The distinction
# drives the error the model can relay to the user.
_session_state: ContextVar[str] = ContextVar("sarf_session_state", default="missing")


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
    return resolve_session_state(db, token)[0]


def resolve_session_state(db: "Database", token: str | None) -> tuple[str | None, str]:
    """Token -> (verified address | None, state).

    States: "valid" | "expired" | "invalid" | "missing". A passing HMAC
    proves we minted the token, so HMAC-ok + no live DB row = a real user
    whose session lapsed (or who ended it) -> "expired", which callers turn
    into an actionable "sign in again" error. Anything unverifiable is
    "invalid"/"missing" and stays a generic transport-level rejection —
    forged tokens don't deserve a helpful error, and it's not a state a
    legitimate user ever sees.
    """
    if not token:
        return None, "missing"
    m = _TOKEN_RE.match(token.strip())
    if not m:
        return None, "invalid"
    token_id, sig = m.group(1), m.group(2)
    if not hmac.compare_digest(sig, _sign(token_id)):
        return None, "invalid"
    address = db.session_address(token_id)
    return (address, "valid") if address else (None, "expired")


def revoke_token(db: "Database", token: str | None) -> None:
    m = _TOKEN_RE.match((token or "").strip())
    if m:
        db.revoke_session(m.group(1))


def bind_session(address: str | None, state: str | None = None):
    """Bind the verified address (and why there isn't one) to the current
    request context."""
    _session_state.set(state or ("valid" if address else "missing"))
    return _session_address.set(address)


def current_address() -> str | None:
    return _session_address.get()


def require_address() -> str:
    """The address every tool acts on. Raises if the request carried no valid
    session — tools never accept an address from the caller instead.

    Raised inside a tool, the message becomes an in-band MCP tool error
    (result.isError + text) — the channel the spec routes to the model — so
    the assistant can tell the user exactly how to reconnect. A transport
    401 would instead be eaten by the client's OAuth machinery and surface
    as an opaque failed call.
    """
    addr = current_address()
    if addr:
        return addr
    dashboard = (
        f"{settings.public_url}/dashboard/activity" if settings.public_url else "the Sarf dashboard"
    )
    minutes = settings.session_ttl_seconds // 60
    if _session_state.get() == "expired":
        raise AuthError(
            f"session_expired: Your Sarf session has expired (sessions last {minutes} minutes; "
            "there is no silent renewal, by design). Tell the user to: (1) open "
            f"{dashboard} and sign in with their wallet, (2) copy the fresh connector URL "
            "shown there, (3) update this connector's URL/key with it — then retry the request."
        )
    raise AuthError(
        f"not_authenticated: No Sarf session on this connection. Tell the user to sign in "
        f"with their wallet at {dashboard}, copy the personal connector URL shown there "
        f"(…/mcp?key=sarf_sess_…), and configure this connector with it. Tokens expire after "
        f"{minutes} minutes."
    )
