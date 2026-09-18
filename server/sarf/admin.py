"""Admin console API — operator-only reads, and three bounded actions.

WHO GETS IN

Two independent things must both hold on every route here:

  1. A valid Sarf session (the ordinary bearer token, minted only after a
     wallet signature over a server nonce). This is what the whole rest of the
     product runs on, and requiring it costs nothing — the console is a page
     inside the signed-in app, so the token is already in hand.
  2. A Privy identity token, presented in `X-Privy-Id-Token`, whose SIGNATURE
     this server verifies and whose Google-verified email is on
     SARF_ADMIN_EMAILS. See privy_auth.py for what verification covers.

Neither alone is sufficient, and that is the point. (1) alone would mean
"admin = whoever holds a session", i.e. everyone. (2) alone would make a Privy
outage or a Privy compromise the whole boundary. Together, an attacker needs
both a wallet that can sign for a Sarf session AND a Google account Privy will
mint an identity token for — and the second one is the operator's, by name.

Why an email gate at all, when this codebase's whole identity model is
addresses: because the operator asked for one, and because the trade-off is
acceptable HERE and nowhere else. Nothing in this file signs, transfers,
mints a session for another account, raises a cap, or reads key material. The
worst an illegitimate admin does is see aggregates and cause some legitimate
users to have to sign in again. That is a real cost, not a nil one, which is
why the second factor is kept and why every action is written to admin_audit
before it runs.

WHAT IT SHOWS

Aggregates and recent-rows pages: user counts, live sessions by client, order
mix and quoted-vs-settled volume, deposit health (including the stuck count,
which nothing else in the product reports), relayer gas outflow, live session-
key grants, passkey coverage, and a config/health block. No balances, no
portfolio contents, no proposal bodies, no tokens, no keys.

WHAT IT CAN DO

Three actions, each reversible-by-the-user or self-healing:
  * revoke an account's sessions + refresh families (they sign in again)
  * revoke an account's session-key grant LOCALLY (stops Sarf using the key;
    the on-chain revoke is still the user's own, and is what binds)
  * re-queue a stuck deposit for the sweeper (cannot misdeliver: the recipient
    is inside the message the user already signed on Base)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request

from . import auth, privy_auth
from .config import settings
from .db import Database

log = logging.getLogger("sarf.admin")

# How long a pending deposit may sit before the console calls it stuck.
# Attestation is normally seconds; an hour is not "slow", it is "look at me".
STUCK_DEPOSIT_SECONDS = 3600.0


def _identity_token(
    x_privy_id_token: str | None = Header(default=None),
    privy_id_token: str | None = Cookie(default=None, alias="privy-id-token"),
) -> str | None:
    """The Privy identity token, from the header or the cookie Privy sets.

    Two sources because the header alone proved unreliable in practice, and for
    a reason worth writing down: the header is filled by asking the Privy SDK
    for the token at request time, and that read intermittently answers null
    while the SDK is between states — so a request goes out with no token, the
    gate correctly refuses it, and a tab that fetches exactly once sticks on a
    refusal that had nothing to do with who was asking.

    The cookie has no such window. The SDK writes `privy-id-token` at login,
    the browser attaches it to same-origin requests on its own, and no timing
    on our side is involved.

    Accepting a cookie does not open a CSRF hole here, and it is worth being
    exact about why rather than trusting the shape of the thing. A cookie is
    attached by the browser whoever caused the request, so on its own it would
    be forgeable-by-navigation. It is never on its own: every admin route also
    requires the wallet session as a `Bearer` in the Authorization header, and
    a cross-site page cannot set that header on a request the browser will send
    with our cookies. The bearer remains the anti-CSRF factor it always was;
    the identity token only answers "which human", never "was this deliberate".

    The header wins when both are present, so an explicit fresh token is always
    preferred over whatever the browser happened to carry.
    """
    return x_privy_id_token or privy_id_token


class Actor:
    """The verified operator behind an admin request."""

    __slots__ = ("email", "address")

    def __init__(self, email: str, address: str) -> None:
        self.email = email
        self.address = address


def build_admin_api(db: Database, dex=None, registry=None) -> APIRouter:
    r = APIRouter(prefix="/api/admin", tags=["admin"])

    def _session_address(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        addr, state = auth.resolve_session_state(db, authorization[7:])
        if not addr:
            raise HTTPException(
                401,
                "session expired — sign in with your wallet again"
                if state == "expired" else "invalid session",
            )
        return addr

    def _actor(authorization: str | None, id_token: str | None) -> Actor:
        """Both factors, or a 403 that says nothing about which one failed.

        The refusal is deliberately uniform. An admin console that answers
        "your email is fine, your session is not" is a console that helps
        someone work out what they still need.
        """
        address = _session_address(authorization)
        try:
            email = privy_auth.admin_email(id_token)
        except privy_auth.PrivyError as e:
            log.warning("admin refused for session %s: %s", address, e)
            raise HTTPException(403, "not an administrator") from None
        return Actor(email, address)

    # ------------------------------------------------------------- identity

    @r.get("/whoami")
    async def whoami(authorization: str | None = Header(default=None),
                     x_privy_id_token: str | None = Depends(_identity_token)) -> dict[str, Any]:
        """Am I an admin? -> a boolean, never a 403.

        This one route answers instead of refusing, because the frontend calls
        it on every page load to decide whether to render the Admin tab, and a
        403 there would be an error in the console for every ordinary user.
        It still leaks nothing: a non-admin learns only that they are not one,
        which they can also tell from the absence of the tab.

        `configured` is reported separately from `is_admin` so a deployment
        that has the allow-list set but the verification key missing says so,
        rather than looking to the operator like their own account was
        rejected. Only an authenticated session sees that detail.
        """
        address = _session_address(authorization)
        ok, why = privy_auth.configured()
        if not ok:
            return {"is_admin": False, "configured": False, "reason": why,
                    "address": address}
        try:
            email = privy_auth.admin_email(x_privy_id_token)
        except privy_auth.PrivyError as e:
            # The reason travels with the "no", to an authenticated session
            # only, for the same reason `configured` does: an operator whose own
            # account is refused otherwise has nothing to go on but a page that
            # says "not you" — and the one running this deployment may well be
            # reading it on a phone, with no console to check. What it costs is
            # near nothing: the reasons name a check ("no identity token
            # presented", "no allow-listed email on this identity token"), all
            # of which SECURITY.md states publicly, and never name an
            # allow-listed address. The gated routes stay silent — see _actor.
            log.warning("admin whoami refused for session %s: %s", address, e)
            return {"is_admin": False, "configured": True, "reason": str(e),
                    "address": address}
        return {"is_admin": True, "configured": True, "email": email, "address": address}

    # ---------------------------------------------------------------- reads

    @r.get("/overview")
    async def overview(authorization: str | None = Header(default=None),
                       x_privy_id_token: str | None = Depends(_identity_token)) -> dict[str, Any]:
        _actor(authorization, x_privy_id_token)
        now = time.time()
        snap = db.get_stat("rwa")

        # Fee revenue is DERIVED, not stored: the fee is collected inside the
        # user's own swap by the aggregator, so this server never sees a fee
        # transfer to count. Settled orders x the flat fee is therefore an
        # estimate and is labelled one — reporting it as booked revenue would
        # be inventing a number the chain never confirmed to us.
        orders = db.order_stats(now)
        fee_estimate = orders["settled_count"] * settings.platform_fee_usd

        return {
            "generated_at": now,
            "users": db.user_stats(now),
            "sessions": db.session_stats(now),
            "orders": orders,
            "deposits": db.deposit_stats(now, STUCK_DEPOSIT_SECONDS),
            "gas": db.gas_stats(now),
            "grants": db.grant_stats(now),
            "passkeys": db.passkey_stats(),
            "fees": {
                "per_swap_usd": settings.platform_fee_usd,
                "estimated_total_usd": fee_estimate,
                "estimated_24h_usd": orders["settled_count_24h"] * settings.platform_fee_usd,
                "collected_by": "aggregator, inside the user's own transaction",
                "estimate": True,
            },
            "config": {
                "env": settings.env,
                "chain_id": 196,
                "tradable_assets": len(registry.assets) if registry else None,
                "session_ttl_seconds": settings.session_ttl_seconds,
                "max_order_usd": settings.max_order_usd,
                "max_price_impact_pct": settings.max_price_impact_pct,
                "delegated_auto_usd": settings.delegated_auto_usd,
                "passkey_required": settings.passkey_required,
                # Which path quotes are coming from. "http" means the OKX DEX
                # API credentials are working and the platform fee can be
                # attached inside the swap; "cli" is the local `onchainos`
                # fallback, which trades WITHOUT a fee and is worth noticing
                # from a dashboard rather than from a week of missing revenue.
                "quote_transport": dex.transport if dex else None,
                # Booleans and addresses, never the credentials themselves.
                "relayer_configured": bool(settings.relayer_private_key),
                "delegate_address": settings.delegate_address,
                "fee_address_set": bool(settings.platform_fee_address),
            },
            "snapshot": {
                "value": snap[0] if snap else None,
                "updated_at": snap[1] if snap else None,
                "age_seconds": (now - snap[1]) if snap else None,
                "refresh_seconds": settings.stats_refresh_seconds,
            },
            "clients": db.oauth_client_stats(),
        }

    @r.get("/users")
    async def users(limit: int = 50, offset: int = 0, q: str | None = None,
                    authorization: str | None = Header(default=None),
                    x_privy_id_token: str | None = Depends(_identity_token)) -> dict[str, Any]:
        _actor(authorization, x_privy_id_token)
        return {"users": db.recent_users(_page(limit), max(0, int(offset)), q)}

    @r.get("/orders")
    async def orders(limit: int = 50, status: str | None = None,
                     authorization: str | None = Header(default=None),
                     x_privy_id_token: str | None = Depends(_identity_token)) -> dict[str, Any]:
        _actor(authorization, x_privy_id_token)
        return {"orders": db.recent_orders(_page(limit), status)}

    @r.get("/deposits")
    async def deposits(limit: int = 50, status: str | None = None,
                       authorization: str | None = Header(default=None),
                       x_privy_id_token: str | None = Depends(_identity_token)) -> dict[str, Any]:
        _actor(authorization, x_privy_id_token)
        rows = db.recent_deposits(_page(limit), status)
        cutoff = time.time() - STUCK_DEPOSIT_SECONDS
        for row in rows:
            row["stuck"] = row["status"] == "pending" and row["created_at"] < cutoff
        return {"deposits": rows, "stuck_after_seconds": int(STUCK_DEPOSIT_SECONDS)}

    @r.get("/grants")
    async def grants(limit: int = 50,
                     authorization: str | None = Header(default=None),
                     x_privy_id_token: str | None = Depends(_identity_token)) -> dict[str, Any]:
        _actor(authorization, x_privy_id_token)
        return {"grants": db.live_grants(_page(limit))}

    @r.get("/audit")
    async def audit(limit: int = 100,
                    authorization: str | None = Header(default=None),
                    x_privy_id_token: str | None = Depends(_identity_token)) -> dict[str, Any]:
        _actor(authorization, x_privy_id_token)
        return {"entries": db.admin_audit_log(_page(limit, cap=500))}

    # -------------------------------------------------------------- actions

    @r.post("/sessions/revoke")
    async def revoke_sessions(body: dict[str, Any], request: Request,
                              authorization: str | None = Header(default=None),
                              x_privy_id_token: str | None = Depends(_identity_token),
                              ) -> dict[str, Any]:
        """Sign an account out everywhere. They sign in again with their wallet.

        Refresh families go with the sessions, for the same reason
        /api/auth/logout kills them: a connector holding a live refresh token
        would mint itself a new access token within the minute and the revoke
        would have meant nothing.
        """
        actor = _actor(authorization, x_privy_id_token)
        address = _address_arg(body)
        db.record_admin_action(actor_email=actor.email, actor_address=actor.address,
                               action="sessions.revoke", target=address,
                               detail={"ip": _client_ip(request)})
        sessions = db.revoke_sessions_for_address(address, reason="admin_revoke")
        refresh = db.revoke_refresh_for_address(address, "admin_revoke")
        log.warning("admin %s revoked sessions for %s (%d sessions, %d refresh)",
                    actor.email, address, sessions, refresh)
        return {"ok": True, "address": address,
                "sessions_revoked": sessions, "refresh_revoked": refresh}

    @r.post("/grants/revoke")
    async def revoke_grant(body: dict[str, Any], request: Request,
                           authorization: str | None = Header(default=None),
                           x_privy_id_token: str | None = Depends(_identity_token),
                           ) -> dict[str, Any]:
        """Stop Sarf using an account's session key.

        This is the LOCAL half only, and the response says so rather than
        implying the key is dead. The binding revocation is the on-chain one,
        which only the account's own wallet can send — see db.revoke_grant.
        Presenting this as a full revoke would be the console telling an
        operator a user is safe when the contract still says otherwise.
        """
        actor = _actor(authorization, x_privy_id_token)
        address = _address_arg(body)
        db.record_admin_action(actor_email=actor.email, actor_address=actor.address,
                               action="grants.revoke", target=address,
                               detail={"ip": _client_ip(request)})
        revoked = db.revoke_grant(address)
        log.warning("admin %s revoked grant for %s (%s)", actor.email, address,
                    "found" if revoked else "no live grant")
        return {
            "ok": True, "address": address, "revoked": revoked,
            "note": ("Local only: Sarf will no longer sign with this session key. "
                     "The on-chain revoke can only be sent by the account's own "
                     "wallet, and that is what binds."),
        }

    @r.post("/deposits/retry")
    async def retry_deposit(body: dict[str, Any], request: Request,
                            authorization: str | None = Header(default=None),
                            x_privy_id_token: str | None = Depends(_identity_token),
                            ) -> dict[str, Any]:
        """Hand a stuck deposit back to the sweeper.

        Safe by construction: the mint's recipient is written into the message
        the user signed on Base, so re-queueing decides only WHEN the money
        arrives, never where. Already-minted deposits are refused by the DB
        method rather than re-attempted.
        """
        actor = _actor(authorization, x_privy_id_token)
        burn_tx = str(body.get("burn_tx") or "").strip().lower()
        if not burn_tx.startswith("0x") or len(burn_tx) != 66:
            raise HTTPException(400, "burn_tx must be a 0x-prefixed 32-byte hash")
        db.record_admin_action(actor_email=actor.email, actor_address=actor.address,
                               action="deposits.retry", target=burn_tx,
                               detail={"ip": _client_ip(request)})
        moved = db.requeue_deposit(burn_tx)
        log.warning("admin %s re-queued deposit %s (%s)", actor.email, burn_tx,
                    "queued" if moved else "not eligible")
        return {
            "ok": True, "burn_tx": burn_tx, "requeued": moved,
            "note": None if moved else
            "Nothing to re-queue: unknown hash, or the deposit is already minted.",
        }

    return r


# --- small shared helpers ----------------------------------------------------

def _page(limit: Any, cap: int = 200) -> int:
    """Clamp a caller-supplied page size. An admin is trusted, not infallible:
    `?limit=1000000` should be a big page, not a stalled event loop."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 50
    return max(1, min(n, cap))


def _address_arg(body: dict[str, Any]) -> str:
    """Validate the address an action targets.

    Uses the same validator the rest of the server does rather than a local
    regex, so "what counts as an address" has one definition. Imported here
    to keep this module's import graph shallow — xlayer.evm pulls in the RPC
    layer, and the admin router should not be a reason to load it at startup.
    """
    from .xlayer.evm import validate_evm_address

    try:
        return validate_evm_address(body.get("address")).lower()
    except Exception:
        raise HTTPException(400, "a valid 0x address is required") from None


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
