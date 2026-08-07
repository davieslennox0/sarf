"""REST surface for the X Layer RWA build: wallet auth, passkeys, orders, prices.

Auth proof-of-ownership is EIP-191 (`personal_sign`) recovery, verified with
`eth-account` — no signature cryptography is hand-rolled here. The recovered
address must equal the claimed one, and the challenge is single-use.

The order submit path mirrors the invariant the Sui build shipped with: the
server only records a tx hash against an order the session owns, unexpired and
not already settled. It never broadcasts and never holds a signature.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import auth, passkey
from ..config import settings
from ..db import Database
from ..validation import ValidationError
from . import rpc
from .evm import validate_evm_address, validate_tx_hash
from .okx_dex import DexError, OkxDexClient
from .registry import CHAIN_ID, EXPLORER_TX, XStocksRegistry

CHALLENGE_TTL = 300
_challenges: dict[str, tuple[str, float]] = {}

# Featured-chart poll interval. The aggregator is the upstream rate limit, so
# one server-side poller fans out to every viewer over SSE rather than each
# browser hitting OKX directly.
PRICE_POLL_SECONDS = float(__import__("os").environ.get("PRICE_POLL_SECONDS", "2"))


def challenge_message(addr: str, nonce: str) -> str:
    return (
        f"Sarf login\n"
        f"address: {addr}\n"
        f"chain: X Layer ({CHAIN_ID})\n"
        f"nonce: {nonce}\n"
        f"This signature only proves address ownership to Sarf; "
        f"it authorizes no transaction and moves no funds."
    )


def issue_challenge(addr: str) -> str:
    nonce = secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, (_, exp) in _challenges.items() if exp < now]:
        _challenges.pop(k, None)
    _challenges[addr] = (nonce, now + CHALLENGE_TTL)
    return challenge_message(addr, nonce)


def verify_wallet_challenge(addr: str, signature: Any) -> None:
    """Consume the live challenge for addr iff an EIP-191 signature recovers to it."""
    if not isinstance(signature, str) or not signature.strip():
        raise HTTPException(400, "signature required")
    entry = _challenges.get(addr)
    if not entry or entry[1] < time.time():
        raise HTTPException(400, "no live challenge for this address; request a new one")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=challenge_message(addr, entry[0])),
            signature=signature.strip(),
        )
    except Exception:
        raise HTTPException(401, "signature could not be verified")
    if recovered.lower() != addr.lower():
        raise HTTPException(401, "signature does not match the claimed address")
    _challenges.pop(addr, None)  # single use


def build_xlayer_api(db: Database, dex: OkxDexClient, reg: XStocksRegistry) -> APIRouter:
    r = APIRouter(prefix="/api")

    def _session_addr(authorization: str | None) -> str:
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

    # ------------------------------------------------------------------ auth

    @r.get("/auth/challenge")
    async def auth_challenge(address: str) -> dict[str, Any]:
        return {"message": issue_challenge(validate_evm_address(address))}

    @r.post("/auth/verify")
    async def auth_verify(body: dict[str, Any]) -> dict[str, Any]:
        addr = validate_evm_address(body.get("address"))
        verify_wallet_challenge(addr, body.get("signature"))
        db.upsert_user(addr, "wallet")
        token, ttl = auth.mint_session(db, addr)
        return {
            "token": token, "address": addr, "expires_in": ttl,
            "chain_id": CHAIN_ID,
            "has_passkey": bool(db.passkeys_for_address(addr)),
            "mcp_url": (
                f"{settings.mcp_public_url}/mcp?key={token}" if settings.mcp_public_url else None
            ),
        }

    @r.post("/auth/logout")
    async def auth_logout(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "logout requires the session bearer token")
        token = authorization[7:]
        addr, _ = auth.resolve_session_state(db, token)
        if addr:
            db.revoke_sessions_for_address(addr, reason="user_logout")
        else:
            auth.revoke_token(db, token, reason="user_logout")
        return {"ok": True}

    # -------------------------------------------------------------- passkeys

    @r.get("/passkey/status")
    async def passkey_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _session_addr(authorization)
        creds = db.passkeys_for_address(addr)
        last = db.last_passkey_verification(addr)
        return {
            "address": addr,
            "registered": bool(creds),
            "credential_count": len(creds),
            "stepup_threshold_usd": settings.passkey_stepup_usd,
            "required": settings.passkey_required,
            "last_verified_at": int(last) if last else None,
            "stepup_valid_for_seconds": passkey.STEPUP_VALIDITY_SECONDS,
        }

    @r.post("/passkey/register/options")
    async def passkey_register_options(authorization: str | None = Header(default=None)):
        addr = _session_addr(authorization)
        try:
            return passkey.registration_options(db, addr)
        except passkey.PasskeyError as e:
            raise HTTPException(400, str(e))

    @r.post("/passkey/register/verify")
    async def passkey_register_verify(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ):
        addr = _session_addr(authorization)
        try:
            return passkey.verify_registration(db, addr, body)
        except passkey.PasskeyError as e:
            raise HTTPException(400, str(e))

    @r.post("/passkey/auth/options")
    async def passkey_auth_options(authorization: str | None = Header(default=None)):
        addr = _session_addr(authorization)
        try:
            return passkey.authentication_options(db, addr, "stepup")
        except passkey.PasskeyError as e:
            raise HTTPException(400, str(e))

    @r.post("/passkey/auth/verify")
    async def passkey_auth_verify(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ):
        addr = _session_addr(authorization)
        try:
            return passkey.verify_authentication(db, addr, body, "stepup")
        except passkey.PasskeyError as e:
            raise HTTPException(400, str(e))

    @r.delete("/passkey")
    async def passkey_delete(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _session_addr(authorization)
        return {"removed": db.delete_passkeys_for_address(addr)}

    # ---------------------------------------------------------------- orders

    @r.get("/order/{order_id}")
    async def get_order(order_id: str) -> dict[str, Any]:
        # Order ids are 128-bit capabilities: knowing one lets you VIEW it (the
        # sign link has to work from a chat window). Executing still requires
        # the owner's wallet signature over the exact unsigned transaction.
        o = db.get_order(order_id.strip())
        if not o:
            raise HTTPException(404, "unknown order")
        return o

    @r.post("/order/{order_id}/submitted")
    async def order_submitted(
        order_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """Record the tx hash the user's wallet produced for this order.

        The server does not broadcast — the wallet already did. This binds the
        resulting hash to a stored order so the audit trail and the settlement
        view mean something.
        """
        addr = _session_addr(authorization)
        o = db.get_order(order_id.strip())
        if not o:
            raise HTTPException(404, "unknown order")
        if o["address"] != addr:
            raise HTTPException(403, "this order belongs to a different account")
        if o["status"] not in ("proposed", "awaiting_signature"):
            raise HTTPException(400, f"order is not awaiting a signature (status: {o['status']})")
        if o["expired"]:
            db.mark_order(order_id, "expired")
            raise HTTPException(400, "order expired before it was signed; request a fresh quote")
        try:
            tx_hash = validate_tx_hash(body.get("tx_hash"))
        except ValidationError as e:
            raise HTTPException(400, str(e))
        db.mark_order(order_id, "submitted", tx_hash=tx_hash)
        return {
            "order_id": order_id, "tx_hash": tx_hash, "status": "submitted",
            "explorer_url": EXPLORER_TX.format(tx_hash),
        }

    @r.get("/order/{order_id}/status")
    async def order_status(order_id: str) -> dict[str, Any]:
        o = db.get_order(order_id.strip())
        if not o:
            raise HTTPException(404, "unknown order")
        if not o["tx_hash"]:
            return {"order_id": order_id, "status": o["status"], "state": "unsigned"}
        st = await rpc.tx_status(o["tx_hash"])
        if st.mined:
            db.mark_order(order_id, "confirmed" if st.success else "failed")
        return {
            "order_id": order_id, "tx_hash": o["tx_hash"],
            "state": "confirmed" if st.success else ("pending" if not st.mined else "failed"),
            "block_number": st.block_number,
            "explorer_url": EXPLORER_TX.format(o["tx_hash"]),
        }

    @r.get("/me/orders")
    async def my_orders(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _session_addr(authorization)
        return {"address": addr, "orders": db.orders_for_address(addr, 100)}

    # ---------------------------------------------------------------- prices

    @r.get("/rwa/list")
    async def rwa_list() -> dict[str, Any]:
        allow = settings.rwa_allowlist
        assets = [a for a in reg.assets if not allow or a.symbol.upper() in allow]
        return {
            "chain_id": CHAIN_ID,
            "count": len(assets),
            "assets": [
                {"symbol": a.symbol, "name": a.name, "address": a.address,
                 "cex_ticker": a.cex_ticker, "explorer_url": a.explorer_url}
                for a in assets
            ],
        }

    async def _price_of(symbol: str) -> tuple[str, float | None]:
        """-> (canonical on-chain symbol, price). Always echo the canonical
        x-suffix casing back, never the caller's — the UI and the model both
        read this, and showing 'AAPLX' would teach the CEX-style identifier."""
        asset = reg.resolve(symbol, allowlist=settings.rwa_allowlist)
        try:
            q = await dex.quote(asset.address, reg.quote.address, 10 ** asset.decimals)
        except DexError:
            return asset.symbol, None
        price = q.to_amount / (10 ** reg.quote.decimals) if q.to_amount > 0 else None
        return asset.symbol, price

    @r.get("/rwa/price/{symbol}")
    async def rwa_price(symbol: str) -> dict[str, Any]:
        try:
            canonical, price = await _price_of(symbol)
        except ValidationError as e:
            raise HTTPException(400, str(e))
        if price is None:
            raise HTTPException(503, "no route available to price this asset right now")
        return {"symbol": canonical, "price_usdt": price, "at": int(time.time())}

    @r.get("/rwa/stream/{symbol}")
    async def rwa_stream(symbol: str, request: Request) -> StreamingResponse:
        """Server-sent price ticks for the featured chart.

        One poller per connection is deliberate for now: the aggregator is the
        bottleneck, and a shared cache is only worth adding once concurrent
        viewers actually appear. Ticks carry an explicit `stale` flag so the UI
        can show a degraded feed instead of a frozen number pretending to be live.
        """
        try:
            asset = reg.resolve(symbol, allowlist=settings.rwa_allowlist)
        except ValidationError as e:
            raise HTTPException(400, str(e))

        async def gen():
            last: float | None = None
            while True:
                if await request.is_disconnected():
                    break
                try:
                    _, price = await _price_of(symbol)
                except Exception:
                    price = None
                payload = {
                    "symbol": asset.symbol,
                    "price_usdt": price if price is not None else last,
                    "at": int(time.time()),
                    "stale": price is None,
                }
                if price is not None:
                    last = price
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(PRICE_POLL_SECONDS)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @r.get("/stats")
    async def stats() -> dict[str, Any]:
        snap = db.get_stat("rwa")
        return {
            "total_users": db.count_users(),
            "chain": {"name": "X Layer", "chain_id": CHAIN_ID},
            "tradable_assets": len(reg.assets),
            "snapshot": snap[0] if snap else None,
            "last_updated": snap[1] if snap else None,
        }

    return r
