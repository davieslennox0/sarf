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
from collections import defaultdict, deque
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import auth, passkey
from ..config import settings
from ..db import Database
from ..validation import ValidationError
from . import analysis, delegation, rpc
from .evm import validate_evm_address, validate_tx_hash
from .okx_dex import DexError, OkxDexClient
from .registry import CHAIN_ID, EXPLORER_TX, XStocksRegistry

CHALLENGE_TTL = 300
_challenges: dict[str, tuple[str, float]] = {}

# Public (unauthenticated) portfolio lookups — see the /portfolio/{address}
# handler. 60s of staleness is invisible against a portfolio view and turns a
# refresh-happy visitor into one fan-out.
PUBLIC_PORTFOLIO_TTL = 60.0
PUBLIC_PORTFOLIO_PER_MINUTE = 10
PUBLIC_CACHE_MAX = 500
_public_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_public_hits: defaultdict[str, deque[float]] = defaultdict(deque)

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


def build_xlayer_api(db: Database, dex: OkxDexClient, reg: XStocksRegistry,
                     provider=None) -> APIRouter:
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
            "stepup_valid_for_seconds": passkey.stepup_validity_seconds(),
        }

    def _stable_units(usd: Any) -> int:
        """Dollars -> USDT minimal units. Rejects nonsense rather than
        int()-ing it into a cap the user did not choose."""
        if isinstance(usd, bool) or not isinstance(usd, (int, float, str)):
            raise ValidationError("caps must be numbers")
        try:
            v = float(usd)
        except (TypeError, ValueError):
            raise ValidationError("caps must be numbers")
        if v != v or v in (float("inf"), float("-inf")):
            raise ValidationError("caps must be finite")
        return int(round(v * 10 ** reg.quote.decimals))

    @r.post("/transfer/prepare")
    async def transfer_prepare(body: dict[str, Any],
                               authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Build an unsigned transfer for the site's Send page.

        Deliberately the same provider method the MCP tool calls — the passkey
        gate, the balance check and the gas reserve live in one place, so the
        website cannot end up with a weaker set of them than the assistant.
        """
        addr = _session_addr(authorization)
        if provider is None:  # pragma: no cover - wiring guard
            raise HTTPException(503, "transfers are unavailable on this server")
        try:
            return await provider.build_transfer(
                addr, body.get("symbol") or "", body.get("amount") or "",
                body.get("to_address") or "", source="web",
            )
        except ValidationError as e:
            raise HTTPException(400, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ---------------------------------------------------------------- grants
    # The session-key flow. Sarf prepares the transactions; the user's wallet
    # signs them. Every state-changing step here produces an UNSIGNED payload —
    # there is deliberately no endpoint that installs or removes a grant on the
    # user's behalf, because that is the one thing that must stay theirs.

    @r.get("/grant")
    async def grant_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        addr = _session_addr(authorization)
        row = db.get_grant(addr)
        # Read the chain, not just our row: a grant is only real if the
        # authorisation transaction actually landed, and a user can revoke
        # on-chain without telling us. The chain is the source of truth.
        installed = await rpc.delegated_to(addr)
        out: dict[str, Any] = {
            "address": addr,
            "delegate_contract": settings.delegate_address or None,
            "delegation_installed": bool(
                installed and settings.delegate_address
                and installed.lower() == settings.delegate_address.lower()
            ),
            "delegated_to": installed,
            "approval_mode": (row or {}).get("approval_mode") or "always_ask",
            "autonomous_limit_usd": round(
                int((row or {}).get("autonomous_limit") or 0)
                / 10 ** reg.quote.decimals, 2),
            "auto_execute_under_usd": settings.delegated_auto_usd,
            "max_grant_days": delegation.MAX_GRANT_SECONDS / 86400,
            "max_grant_seconds": delegation.MAX_GRANT_SECONDS,
            "passkey_registered": bool(db.passkeys_for_address(addr)),
        }
        if not row:
            out["grant"] = None
            return out
        g = delegation.Grant(
            address=row["address"], session_address=row["session_address"],
            delegate=row["delegate"], router=row["router"], stable=row["stable"],
            expiry=row["expiry"], per_trade_cap=row["per_trade_cap"],
            daily_cap=row["daily_cap"], created_at=row["created_at"],
            rotated_at=row["rotated_at"], revoked_at=row["revoked_at"],
        )
        out["grant"] = g.view(reg.quote.decimals)
        out["key_due_for_rotation"] = delegation.due_for_rotation(g)
        return out

    @r.post("/grant/prepare")
    async def grant_prepare(body: dict[str, Any],
                            authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Mint a session key and return the UNSIGNED authorize() transaction.

        Gated at BOTH ends by the passkey, which is the whole point of the
        design: a fresh assertion is required to obtain the key, and another one
        gates every transaction the key is later used for. Registration alone is
        not enough here — checking only that a credential exists would let
        anyone holding a live session token mint a spending key without ever
        proving they are the person the credential belongs to.
        """
        addr = _session_addr(authorization)
        if not settings.delegate_address:
            raise HTTPException(503, "in-chat execution is not configured on this server")
        if not db.passkeys_for_address(addr):
            raise HTTPException(
                400, "register a passkey first — it is what gates every trade this key makes"
            )
        last = db.last_passkey_verification(addr)
        validity = passkey.stepup_validity_seconds()
        if last is None or (time.time() - last) > validity:
            raise HTTPException(
                400,
                "verify your passkey before a session key is issued — it has to be you "
                f"asking, not just your browser. Assertions stay good for {validity // 60} "
                "minutes.",
            )
        try:
            # Default to the ceiling rather than to a week. A session key that
            # trades unattended should die with the passkey that bought it.
            expiry = delegation.requested_expiry(
                body.get("days", delegation.MAX_GRANT_SECONDS / 86400))
            per_trade = _stable_units(body.get("per_trade_cap_usd", 500))
            daily = _stable_units(body.get("daily_cap_usd", 2000))
            # Autonomous is the only mode. Always Ask was removed on 2026-08-11
            # because it could not be honoured where it mattered: a passkey
            # needs a top-level browsing context, an MCP widget is a sandboxed
            # iframe, so "approve every trade in chat" was unreachable and every
            # trade fell back to a link out to the website. Offering a mode the
            # platform cannot deliver is worse than not offering it.
            #
            # The passkey did not go away — it moved. It gates the SESSION: you
            # prove it is you to obtain the key, and the contract's per-trade
            # and daily caps plus the expiry bound everything that key can then
            # do without asking again.
            mode = "autonomous"
            autonomous_limit = _stable_units(body.get("autonomous_limit_usd", 0))
            # An unset limit must not mean "unlimited". Fall back to the
            # per-trade cap, which is enforced on-chain either way.
            if autonomous_limit <= 0:
                autonomous_limit = per_trade
        except ValidationError as e:
            raise HTTPException(400, str(e))
        if per_trade <= 0 or daily <= 0:
            raise HTTPException(400, "caps must be greater than zero")
        if per_trade > daily:
            raise HTTPException(400, "the per-trade cap cannot exceed the daily cap")
        if True:
            if autonomous_limit > per_trade:
                raise HTTPException(
                    400, "the autonomous limit cannot exceed the per-trade cap; the "
                         "cap is enforced on-chain and would reject the trade anyway")

        session_address, sealed = delegation.new_session_key()
        allow = settings.rwa_allowlist
        tokens = [a.address for a in reg.assets if not allow or a.symbol.upper() in allow]
        data = delegation.grant_calldata(
            session_key=session_address, expiry=expiry,
            # Two different contracts, and getting either wrong reverts every
            # trade on-chain at the user's expense. The ROUTER is what the swap
            # calls — the contract enforces target == grant.router, so naming
            # the approve address here reverted with TargetNotAllowed. The
            # SPENDER is who the sell-side allowance goes to — OKX's router
            # pulls through its TokenApprove, so naming the router here
            # reverted with SwapFailed. Both were observed on-chain.
            router=reg.dex_router_address, spender=reg.dex_approve_address,
            stable=reg.quote.address,
            per_trade_cap=per_trade, daily_cap=daily, tokens=tokens,
        )
        # Stored before the user signs. If they abandon the flow the key is
        # inert — it is only worth anything once the contract has been told
        # about it, and the contract only hears that from their wallet.
        db.put_grant(
            address=addr, session_address=session_address, sealed_key=sealed,
            # Must match the router in the calldata above, or the stored row
            # disagrees with what the contract was actually told.
            delegate=settings.delegate_address, router=reg.dex_router_address,
            stable=reg.quote.address, expiry=expiry,
            per_trade_cap=per_trade, daily_cap=daily,
            approval_mode=mode, autonomous_limit=autonomous_limit,
        )
        return {
            "session_key": session_address,
            # Needed by the browser only for the relayer fallback: EIP-7702
            # computes the authorization nonce differently when the authority
            # is not the sender, so a client re-signing for relay has to name
            # the account that will actually send it.
            "relayer": delegation.relayer_address(),
            "approval_mode": mode,
            "autonomous_limit_usd": round(
                autonomous_limit / 10 ** reg.quote.decimals, 2) if autonomous_limit else 0,
            "expires_at": expiry,
            "per_trade_cap_usd": round(per_trade / 10 ** reg.quote.decimals, 2),
            "daily_cap_usd": round(daily / 10 ** reg.quote.decimals, 2),
            "allowed_token_count": len(tokens),
            "authorization_required": {
                "type": "eip7702",
                "delegate": settings.delegate_address,
                "chain_id": CHAIN_ID,
                "note": (
                    "Sign an EIP-7702 authorisation for this delegate, then send the "
                    "transaction below to your own address. Both are signed by your "
                    "wallet; Sarf cannot do either."
                ),
            },
            "transaction": {"to": addr, "data": data, "value": "0x0", "chainId": CHAIN_ID},
            "what_this_grants": (
                f"Sarf may swap the listed tokens for you, up to "
                f"${per_trade / 10 ** reg.quote.decimals:,.0f} per trade and "
                f"${daily / 10 ** reg.quote.decimals:,.0f} per day, until this expires. "
                "It can never move funds to another address, spend your OKB, call any "
                "contract but the router, or raise these limits. You can revoke it at "
                "any time without Sarf's involvement."
            ),
        }

    @r.post("/grant/relay")
    async def grant_relay(body: dict[str, Any],
                          authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Broadcast a user-signed EIP-7702 authorization through the relayer.

        The fallback for wallets that sign a 7702 authorization but cannot send
        the type-4 transaction carrying it — Privy's embedded wallet being the
        one that forced this. The browser hands over the signed authorization
        and the unsigned call; the relayer pays gas and presses send.

        It is a relay, not an authority. The authorization is signed by the
        user's key, names this server's delegate (checked), and is bound to
        X Layer (checked). The relayer cannot forge one, point it at another
        contract, or change the caps — those are inside the signed payload.
        """
        addr = _session_addr(authorization)
        if not settings.delegate_address:
            raise HTTPException(503, "in-chat execution is not configured on this server")

        # Same passkey bar as /grant/prepare. Relaying is the step that actually
        # installs the delegation, so it must not be reachable on a weaker gate
        # than the one that minted the key.
        if not db.passkeys_for_address(addr):
            raise HTTPException(400, "register a passkey before installing a session key")
        last = db.last_passkey_verification(addr)
        if last is None or (time.time() - last) > passkey.stepup_validity_seconds():
            raise HTTPException(400, "verify your passkey before installing the session key")

        row = db.get_grant(addr)
        if not row or row.get("revoked_at"):
            raise HTTPException(400, "no prepared grant for this wallet — run setup again")

        tx = body.get("transaction") or {}
        auth = body.get("authorization")
        if not isinstance(auth, dict):
            raise HTTPException(400, "authorization object required")
        to = tx.get("to")
        data = tx.get("data")
        if not to or not data:
            raise HTTPException(400, "transaction with `to` and `data` required")
        # The call must target the user's OWN account: a 7702 install executes
        # against the authorising EOA, and relaying one aimed elsewhere would
        # spend the relayer's gas on a stranger's transaction.
        if str(to).lower() != addr.lower():
            raise HTTPException(400, "transaction must target the authorising account")

        try:
            tx_hash = await delegation.relay_authorization(
                authorization=auth, to=to, data=data,
            )
        except delegation.DelegationError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # pragma: no cover - upstream/RPC failure
            raise HTTPException(502, f"relay failed: {e}")

        return {
            "tx_hash": tx_hash,
            "relayer": delegation.relayer_address(),
            "explorer_url": EXPLORER_TX.format(tx_hash),
            "note": (
                "Broadcast by Sarf's relayer, which paid gas. The authorization "
                "it carried was signed by your wallet and names the limits you set."
            ),
        }

    @r.post("/grant/revoke")
    async def grant_revoke(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Stop using the key locally, and hand back the on-chain revoke().

        Both halves matter and only one is the security boundary: marking the
        row stops Sarf from signing, while the contract call is what stops
        anyone who has taken the key.
        """
        addr = _session_addr(authorization)
        db.revoke_grant(addr)
        return {
            "stopped_locally": True,
            "transaction": {
                "to": addr, "data": delegation.revoke_calldata(),
                "value": "0x0", "chainId": CHAIN_ID,
            },
            "note": (
                "Sarf has stopped using the key. Send this transaction from your wallet "
                "to revoke it on-chain — that is what makes it unusable by anyone, "
                "including us."
            ),
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

    @r.get("/me/portfolio")
    async def my_portfolio(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        # Same provider method the MCP get_portfolio tool uses, so the holdings
        # shown in chat and on the dashboard cannot disagree.
        addr = _session_addr(authorization)
        if provider is None:
            raise HTTPException(503, "portfolio view unavailable")
        return await provider.portfolio(addr)

    @r.get("/portfolio/{address}")
    async def public_portfolio(address: str, request: Request) -> dict[str, Any]:
        """Read-only analysis of any address. No session, no wallet, no signature.

        Safe to leave open because it is strictly a read of public chain state —
        it authorizes nothing and reveals nothing an explorer would not. What it
        is NOT is cheap: one call fans out to a balance read per tracked asset
        plus aggregator quotes to price them. So it is metered twice over — a
        per-IP rate limit, and a short cache keyed by address so a page that
        polls, or ten people looking at the same whale, costs one fan-out.
        """
        try:
            addr = validate_evm_address(address)
        except ValidationError as e:
            # This one is typed by hand off the front page, so a malformed
            # address is an ordinary user mistake, not a server fault.
            raise HTTPException(400, str(e)) from e
        if provider is None:
            raise HTTPException(503, "portfolio view unavailable")

        ip = request.client.host if request.client else "?"
        now = time.time()
        hits = _public_hits[ip]
        while hits and now - hits[0] > 60:
            hits.popleft()
        # Deliberately below the general /mcp limit: this is the most expensive
        # unauthenticated call on the server, so it gets the tightest budget.
        if len(hits) >= PUBLIC_PORTFOLIO_PER_MINUTE:
            raise HTTPException(429, "too many portfolio lookups; try again shortly")
        hits.append(now)

        cached = _public_cache.get(addr)
        if cached and now - cached[0] < PUBLIC_PORTFOLIO_TTL:
            return cached[1]

        try:
            portfolio = await provider.portfolio(addr, record=False)
        except DexError as e:
            raise HTTPException(502, f"price feed unavailable: {e}") from e
        except Exception as e:  # RPC flake, upstream timeout
            # Upstream being down is not this server being broken, and the
            # difference matters to whoever is looking at the page.
            raise HTTPException(503, "X Layer read failed; try again shortly") from e
        # analyze() is pure, so it costs nothing to run here and keeps the site
        # and the MCP tool reading from one implementation of the methodology.
        body = {**portfolio, "analysis": analysis.analyze(portfolio)}

        # Bound the cache rather than letting a scripted sweep of random
        # addresses grow it without limit.
        if len(_public_cache) > PUBLIC_CACHE_MAX:
            _public_cache.clear()
        _public_cache[addr] = (now, body)
        return body

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
                 "cex_ticker": a.cex_ticker, "logo_url": a.logo_url,
                 "explorer_url": a.explorer_url}
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
