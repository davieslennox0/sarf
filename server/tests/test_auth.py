"""Auth boundary tests (offline).

Covers the session layer that replaced address-as-identity:
- production mode fails closed at startup without a signing secret
- session tokens: mint/resolve, tampering, expiry, revocation
- challenge–response: rejected proofs (verifier says no), nonce single-use,
  nonce expiry
- address binding: tools take no user_address and act on the session's
  address; a caller-supplied address is ignored/rejected, never honored;
  submitting a proposal that belongs to a different session is refused

The cryptographic verification of wallet/zkLogin signatures itself lives in
the sidecar (@mysten/sui verifyPersonalMessageSignature with the claimed
address — covering the ZK proof, JWK lookup and maxEpoch freshness); these
tests mock the verifier's verdict and assert the server honors it, plus every
invariant the server owns on top.
"""

from __future__ import annotations

import base64
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

from sarf import auth
from sarf.api import build_api
from sarf.config import SESSION_TTL_ABSOLUTE_MAX, Settings
from sarf.db import Database
from sarf.providers.current_finance import CurrentFinanceProvider

ADDR_A = "0x" + "a" * 64
ADDR_B = "0x" + "b" * 64
PTB = base64.b64encode(b"unsigned transaction bytes").decode()
SIG = base64.b64encode(b"x" * 97).decode()


# --------------------------------------------------------------- test doubles

class FakeTx:
    """Sidecar stand-in: scripted signature verdicts, recorded portfolio calls."""

    def __init__(self, valid: bool = True):
        self.valid = valid
        self.portfolio_called_with: str | None = None

    async def _req(self, method, path, body=None):
        assert path == "/verify-signature"
        return {"valid": self.valid, "reason": "" if self.valid else "bad signature"}

    async def portfolio(self, addr):
        self.portfolio_called_with = addr
        return {"positions": []}


@pytest.fixture()
def db():
    return Database(":memory:")


@pytest.fixture()
def faketx():
    return FakeTx()


@pytest.fixture()
def client(db, faketx):
    import sarf.api as api_module

    api_module._challenges.clear()  # module-level nonce store; isolate tests
    provider = CurrentFinanceProvider(db, faketx, None)
    app = FastAPI()
    app.include_router(build_api(db, faketx, provider))
    return TestClient(app)


def _sign_in(client, address=ADDR_A) -> str:
    client.app.state  # noqa: B018 - touch to keep TestClient warm
    challenge = client.get(f"/api/auth/challenge?address={address}").json()
    assert "nonce" in challenge["message"]
    res = client.post("/api/auth/verify", json={"address": address, "signature": "AAAA"})
    assert res.status_code == 200, res.text
    return res.json()["token"]


# ------------------------------------------------- production fails closed

def test_production_without_secret_refuses_start(monkeypatch):
    monkeypatch.setenv("SARF_ENV", "production")
    monkeypatch.delenv("SARF_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SARF_SESSION_SECRET"):
        Settings()


def test_production_with_weak_secret_refuses_start(monkeypatch):
    monkeypatch.setenv("SARF_ENV", "production")
    monkeypatch.setenv("SARF_SESSION_SECRET", "tooshort")
    with pytest.raises(RuntimeError):
        Settings()


def test_production_with_secret_starts(monkeypatch):
    monkeypatch.setenv("SARF_ENV", "production")
    monkeypatch.setenv("SARF_SESSION_SECRET", "s" * 64)
    assert Settings().env == "production"


def test_dev_mode_generates_ephemeral_secret(monkeypatch):
    monkeypatch.setenv("SARF_ENV", "dev")
    monkeypatch.delenv("SARF_SESSION_SECRET", raising=False)
    s = Settings()
    assert len(s.session_secret) >= 32  # sessions still unforgeable in dev


def test_session_ttl_hard_capped(monkeypatch):
    monkeypatch.setenv("SARF_ENV", "dev")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "999999")
    assert Settings().session_ttl_seconds == SESSION_TTL_ABSOLUTE_MAX


# ----------------------------------------------------------- session tokens

def test_token_roundtrip(db):
    token, ttl = auth.mint_session(db, ADDR_A)
    assert ttl > 0
    assert auth.resolve_session(db, token) == ADDR_A


def test_tampered_signature_rejected(db):
    token, _ = auth.mint_session(db, ADDR_A)
    flipped = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert auth.resolve_session(db, flipped) is None


def test_tampered_id_rejected(db):
    token, _ = auth.mint_session(db, ADDR_A)
    head, sig = token.rsplit(".", 1)
    forged = "sarf_sess_" + "0" * 32 + "." + sig
    assert auth.resolve_session(db, forged) is None


@pytest.mark.parametrize("junk", [None, "", "Bearer x", "sarf_sess_zz.zz", "sarf_" + "0" * 32])
def test_malformed_tokens_rejected(db, junk):
    assert auth.resolve_session(db, junk) is None


def test_expired_session_rejected(db):
    token, _ = auth.mint_session(db, ADDR_A, ttl_seconds=-1)
    assert auth.resolve_session(db, token) is None


def test_revoked_session_rejected(db):
    token, _ = auth.mint_session(db, ADDR_A)
    auth.revoke_token(db, token)
    assert auth.resolve_session(db, token) is None


def test_session_states_distinguished(db):
    """Expired-but-authentic is distinguishable from forged/missing — it
    drives the actionable in-band error instead of a transport 401."""
    live, _ = auth.mint_session(db, ADDR_A)
    stale, _ = auth.mint_session(db, ADDR_A, ttl_seconds=-1)
    assert auth.resolve_session_state(db, live) == (ADDR_A, "valid")
    assert auth.resolve_session_state(db, stale) == (None, "expired")
    assert auth.resolve_session_state(db, None) == (None, "missing")
    assert auth.resolve_session_state(db, "garbage") == (None, "invalid")
    tampered = live[:-1] + ("0" if live[-1] != "0" else "1")
    assert auth.resolve_session_state(db, tampered) == (None, "invalid")
    # ended-by-user reads as expired too: same user story, same remedy
    auth.revoke_token(db, live)
    assert auth.resolve_session_state(db, live) == (None, "expired")


# ------------------------------------------- challenge–response via the API

def test_login_happy_path_mints_short_lived_session(client, db):
    token = _sign_in(client)
    assert auth.resolve_session(db, token) == ADDR_A


def test_rejected_proof_no_session(client, faketx):
    faketx.valid = False  # sidecar verdict: signature/proof does not verify
    client.get(f"/api/auth/challenge?address={ADDR_A}")
    res = client.post("/api/auth/verify", json={"address": ADDR_A, "signature": "AAAA"})
    assert res.status_code == 401


def test_verify_without_challenge_rejected(client):
    res = client.post("/api/auth/verify", json={"address": ADDR_A, "signature": "AAAA"})
    assert res.status_code == 400


def test_nonce_is_single_use(client):
    _sign_in(client)
    # replaying the (verified) signature against the consumed nonce fails
    res = client.post("/api/auth/verify", json={"address": ADDR_A, "signature": "AAAA"})
    assert res.status_code == 400


def test_expired_nonce_rejected(client, monkeypatch):
    monkeypatch.setattr("sarf.api.CHALLENGE_TTL", -1)
    client.get(f"/api/auth/challenge?address={ADDR_A}")
    res = client.post("/api/auth/verify", json={"address": ADDR_A, "signature": "AAAA"})
    assert res.status_code == 400


def test_activity_requires_live_session(client, db):
    assert client.get("/api/me/activity").status_code == 401
    token = _sign_in(client)
    ok = client.get("/api/me/activity", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and ok.json()["address"] == ADDR_A
    # expiry is enforced server-side, not just by the browser dropping it
    stale, _ = auth.mint_session(db, ADDR_A, ttl_seconds=-1)
    assert (
        client.get("/api/me/activity", headers={"Authorization": f"Bearer {stale}"}).status_code
        == 401
    )


def test_logout_revokes_session(client):
    token = _sign_in(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/auth/logout", headers=headers)
    assert client.get("/api/me/activity", headers=headers).status_code == 401


# -------------------------------------- revocation audit trail (internal only)

def _token_id(token: str) -> str:
    return token.split(".", 1)[0].removeprefix("sarf_sess_")


def test_revocation_recorded_server_side(db):
    """An explicit revoke must be distinguishable internally from natural
    expiry (when and why), while the token holder sees exactly the same
    'expired' behavior — the reason never leaves the DB."""
    token, _ = auth.mint_session(db, ADDR_A)
    auth.revoke_token(db, token, reason="compromise_suspected")
    rec = db.session_record(_token_id(token))
    assert rec is not None and rec["revoked_at"] is not None
    assert rec["revocation_reason"] == "compromise_suspected"
    # holder-facing state is byte-for-byte the natural-expiry story
    assert auth.resolve_session_state(db, token) == (None, "expired")
    # ...whereas a naturally-expired row carries no revocation record
    stale, _ = auth.mint_session(db, ADDR_A, ttl_seconds=-1)
    rec2 = db.session_record(_token_id(stale))
    assert rec2["revoked_at"] is None and rec2["revocation_reason"] is None


def test_logout_records_reason(client, db):
    token = _sign_in(client)
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    rec = db.session_record(_token_id(token))
    assert rec is not None and rec["revocation_reason"] == "user_logout"


def test_logout_without_token_fails_loudly(client):
    """A tokenless logout is a client bug (it revokes nothing while looking
    successful — the App.jsx clear-before-logout regression). 401, not ok."""
    assert client.post("/api/auth/logout").status_code == 401


def test_logout_voids_mcp_connector_credential(client, db):
    """End session in the dashboard must disconnect Claude: the revoked
    token — the same string in the connector URL — resolves as expired,
    so tools refuse to act on it."""
    token = _sign_in(client)
    res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert auth.resolve_session_state(db, token) == (None, "expired")


def test_revoked_row_survives_pruning(db):
    """put_session prunes stale rows, but a revoked row is audit evidence:
    it must outlive its own expiry (for the retention window)."""
    revoked, _ = auth.mint_session(db, ADDR_A, ttl_seconds=-1)  # already past expiry
    auth.revoke_token(db, revoked, reason="test")
    plain_stale, _ = auth.mint_session(db, ADDR_A, ttl_seconds=-1)
    auth.mint_session(db, ADDR_B)  # insert triggers the prune
    assert db.session_record(_token_id(revoked)) is not None
    assert db.session_record(_token_id(plain_stale)) is None


# ---------------------------------------------- access-log token redaction

def test_access_log_filter_redacts_tokens(db):
    """uvicorn's access log records the full request line including
    ?key=<token>; the filter must scrub it before the line is written."""
    token, _ = auth.mint_session(db, ADDR_A)
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:0", "POST", f"/mcp?key={token}", "1.1", 200),
        exc_info=None,
    )
    assert auth.RedactSessionTokens().filter(record) is True  # never drops lines
    line = record.getMessage()
    assert token not in line
    assert 'POST /mcp?key=sarf_sess_[redacted] HTTP/1.1' in line
    # non-token lines pass through untouched
    plain = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:0", "GET", "/healthz", "1.1", 200),
        exc_info=None,
    )
    auth.RedactSessionTokens().filter(plain)
    assert '"GET /healthz HTTP/1.1" 200' in plain.getMessage()


# ------------------------------------------------ address binding on submit

def _proposal_for(db, address):
    return db.create_proposal(
        user_address=address, tool="propose_deposit", params={},
        ptb_base64=PTB, simulation=None, risk=None, ttl_seconds=600,
    )


def test_submit_requires_session(client, db):
    prop = _proposal_for(db, ADDR_A)
    res = client.post("/api/submit", json={
        "proposal_id": prop.proposal_id, "signed_tx_bytes_base64": PTB, "signatures": [SIG],
    })
    assert res.status_code == 401


def test_submit_foreign_proposal_rejected(client, db):
    """Session A cannot execute (or probe) a proposal that belongs to B."""
    prop = _proposal_for(db, ADDR_B)
    token = _sign_in(client, ADDR_A)
    res = client.post(
        "/api/submit",
        json={"proposal_id": prop.proposal_id, "signed_tx_bytes_base64": PTB, "signatures": [SIG]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400
    assert "does not belong" in res.json()["detail"]
    # and the proposal was NOT consumed/marked by the failed attempt
    assert db.get_proposal(prop.proposal_id).status == "proposed"


def test_submit_byte_mismatch_still_rejected(client, db):
    """The pre-existing byte-match invariant survives the session change."""
    prop = _proposal_for(db, ADDR_A)
    token = _sign_in(client, ADDR_A)
    tampered = base64.b64encode(b"different bytes entirely").decode()
    res = client.post(
        "/api/submit",
        json={"proposal_id": prop.proposal_id, "signed_tx_bytes_base64": tampered, "signatures": [SIG]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400
    assert "do not match" in res.json()["detail"]


# --------------------------------------------- MCP tools bound to the session

@pytest.fixture()
def mcp_with_tools(db, faketx):
    mcp = FastMCP("sarf-test")
    CurrentFinanceProvider(db, faketx, None).register_tools(mcp)
    return mcp


@pytest.mark.asyncio
async def test_no_tool_accepts_user_address(mcp_with_tools):
    """The old trusted input is gone from every tool schema."""
    tools = await mcp_with_tools.list_tools()
    assert len(tools) == 9
    for t in tools:
        props = (t.inputSchema or {}).get("properties", {})
        assert "user_address" not in props, f"{t.name} still accepts user_address"


@pytest.mark.asyncio
async def test_get_portfolio_uses_session_address(mcp_with_tools, faketx):
    auth.bind_session(ADDR_A)
    try:
        await mcp_with_tools.call_tool("get_portfolio", {})
    finally:
        auth.bind_session(None)
    assert faketx.portfolio_called_with == ADDR_A


@pytest.mark.asyncio
async def test_caller_supplied_address_never_honored(mcp_with_tools, faketx):
    """Passing someone else's address must be rejected or ignored — the
    sidecar must never be asked about ADDR_B."""
    auth.bind_session(ADDR_A)
    try:
        try:
            await mcp_with_tools.call_tool("get_portfolio", {"user_address": ADDR_B})
        except Exception:
            pass  # rejecting the unexpected argument is fine
    finally:
        auth.bind_session(None)
    assert faketx.portfolio_called_with in (None, ADDR_A)


@pytest.mark.asyncio
async def test_tool_without_session_is_refused(mcp_with_tools, faketx):
    auth.bind_session(None)
    with pytest.raises(Exception, match="not_authenticated"):
        await mcp_with_tools.call_tool("get_portfolio", {})
    assert faketx.portfolio_called_with is None


@pytest.mark.asyncio
async def test_expired_session_tool_error_is_actionable(mcp_with_tools, faketx):
    """Expired session -> a distinct, labeled, in-band tool error telling the
    model to send the user back to the dashboard — not a generic failure.
    (Transport-level behavior is covered by the live e2e: expired tokens
    pass the middleware so initialize/tools/list still work.)"""
    auth.bind_session(None, "expired")
    try:
        with pytest.raises(Exception) as exc:
            await mcp_with_tools.call_tool("get_portfolio", {})
    finally:
        auth.bind_session(None)
    msg = str(exc.value)
    assert "session_expired" in msg
    assert "sign in" in msg
    assert faketx.portfolio_called_with is None
