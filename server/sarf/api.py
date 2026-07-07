"""Dashboard + signer REST API.

Serves the React frontend's data needs. Custody rules are identical to the
MCP path: this API returns unsigned bytes and accepts signed bytes; signing
happens only in the user's browser wallet. The signer page POSTs to
/api/submit, which shares execute_submit() with the MCP tool, so every
invariant (byte-match, TTL, single-use) is enforced in exactly one place.

Auth: challenge–response over a wallet personal-message signature. The
browser asks for a nonce, the wallet signs it (works for zkLogin wallets too
— verification handles zk signatures via on-chain JWK lookup in the sidecar),
and a bearer session token is minted. No key material ever reaches us.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from .config import settings
from .db import Database
from .providers.current_finance import CurrentFinanceProvider
from .txclient import TxBuilder, TxBuilderError
from .validation import ValidationError, validate_address, validate_base64

CHALLENGE_TTL = 300  # seconds a login nonce stays valid
_challenges: dict[str, tuple[str, float]] = {}  # address -> (nonce, expires)


def build_api(db: Database, tx: TxBuilder, provider: CurrentFinanceProvider) -> APIRouter:
    r = APIRouter(prefix="/api")

    # ------------------------------------------------------------- stats

    @r.get("/stats")
    async def stats() -> dict[str, Any]:
        """Public landing-page numbers. Served from the background snapshot —
        never computed inline — so this endpoint is O(1) and honest about
        freshness via last_updated."""
        snap = db.get_stat("tvl")
        return {
            "total_users": db.count_users(),
            "tvl": snap[0] if snap else None,
            "last_updated": snap[1] if snap else None,
            "note": (
                "TVL counts assets supplied through Sarf-tracked positions only, "
                "not Current Finance protocol-wide TVL."
            ),
        }

    # -------------------------------------------------------------- auth

    @r.get("/auth/challenge")
    async def auth_challenge(address: str) -> dict[str, Any]:
        addr = validate_address(address)
        nonce = secrets.token_urlsafe(24)
        now = time.time()
        # prune + store; one live challenge per address
        for k in [k for k, (_, exp) in _challenges.items() if exp < now]:
            _challenges.pop(k, None)
        _challenges[addr] = (nonce, now + CHALLENGE_TTL)
        message = (
            f"Sarf login\naddress: {addr}\nnonce: {nonce}\n"
            f"This signature only proves address ownership to sarf; it authorizes no transaction."
        )
        return {"message": message}

    @r.post("/auth/verify")
    async def auth_verify(body: dict[str, Any]) -> dict[str, Any]:
        addr = validate_address(body.get("address"))
        signature = body.get("signature")
        if not isinstance(signature, str) or not signature:
            raise HTTPException(400, "signature required")
        entry = _challenges.get(addr)
        if not entry or entry[1] < time.time():
            raise HTTPException(400, "no live challenge for this address; request a new one")
        nonce = entry[0]
        message = (
            f"Sarf login\naddress: {addr}\nnonce: {nonce}\n"
            f"This signature only proves address ownership to sarf; it authorizes no transaction."
        )
        import base64 as b64

        res = await tx._req(  # noqa: SLF001 - thin internal client
            "POST", "/verify-signature",
            {
                "address": addr,
                "messageBase64": b64.b64encode(message.encode()).decode(),
                "signature": signature,
            },
        )
        if not res.get("valid"):
            raise HTTPException(401, f"signature verification failed: {res.get('reason', '')}")
        _challenges.pop(addr, None)  # single use
        db.upsert_user(addr, "wallet")
        token = db.create_session(addr)
        return {"token": token, "address": addr, "expires_in": 24 * 3600}

    def _session_addr(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        addr = db.session_address(authorization[7:])
        if not addr:
            raise HTTPException(401, "invalid or expired session")
        return addr

    @r.post("/auth/logout")
    async def auth_logout(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if authorization and authorization.lower().startswith("bearer "):
            db.revoke_session(authorization[7:])
        return {"ok": True}

    # ----------------------------------------------------- authed activity

    @r.get("/me/activity")
    async def my_activity(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _session_addr(authorization)
        return {"address": addr, "proposals": db.audit_trail(addr, limit=100)}

    # ------------------------------------------------------ signer support

    @r.get("/proposal/{proposal_id}")
    async def proposal_view(proposal_id: str) -> dict[str, Any]:
        view = db.proposal_view(proposal_id.strip())
        if view is None:
            raise HTTPException(404, "unknown proposal")
        view["expired"] = time.time() > view["expires_at"]
        return view

    @r.post("/submit")
    async def submit(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await provider.execute_submit(
                body.get("proposal_id", ""),
                body.get("signed_tx_bytes_base64", ""),
                body.get("signatures", []),
            )
        except ValidationError as e:
            raise HTTPException(400, str(e))
        except TxBuilderError as e:
            raise HTTPException(502, str(e))

    return r


async def tvl_refresher(db: Database, tx: TxBuilder) -> None:
    """Background snapshot of app-tracked TVL.

    Iterates the distinct addresses that have obligation caps recorded via
    Sarf, reads their live positions (SDK reads + Pyth oracle prices — never
    hardcoded prices), and sums supplied USD. Cached in the stats table so
    page loads never trigger on-chain scans.
    """
    import asyncio

    while True:
        started = time.time()
        try:
            addresses = db.distinct_position_addresses()
            tvl_usd = 0.0
            positions = 0
            errors = 0
            for addr in addresses:
                try:
                    pf = await tx.portfolio(addr)
                    for p in pf.get("positions", []):
                        if "deposits" not in p:
                            continue
                        positions += 1
                        tvl_usd += sum(d.get("usdValue", 0.0) for d in p["deposits"])
                except TxBuilderError:
                    errors += 1
            db.set_stat("tvl", {
                "usd": round(tvl_usd, 2),
                "addresses_tracked": len(addresses),
                "positions": positions,
                "scan_errors": errors,
                "scan_seconds": round(time.time() - started, 1),
            })
        except Exception:  # noqa: BLE001 - refresher must never die
            pass
        await asyncio.sleep(settings.stats_refresh_seconds)
