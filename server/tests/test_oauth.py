"""OAuth authorization-server tests.

The flow under test: a client registers (RFC7591), sends the user to
/authorize, the user approves with a wallet signature (the same
challenge–response gate as dashboard sign-in), the client exchanges the
code + PKCE verifier for an ordinary 30-minute session token. Also the
transport policy that makes this matter: Bearer clients get 401 +
WWW-Authenticate for dead sessions (→ Reconnect in Claude), legacy ?key=
clients keep the in-band expired error.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sarf import auth
from sarf.api import build_api
from sarf.db import Database
from sarf.oauth import build_oauth, www_authenticate
from sarf.providers.current_finance import CurrentFinanceProvider

from test_auth import ADDR_A, ADDR_B, FakeTx

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture()
def db():
    return Database(":memory:")


@pytest.fixture()
def faketx():
    return FakeTx()


@pytest.fixture()
def client(db, faketx):
    import sarf.api as api_module

    api_module._challenges.clear()
    provider = CurrentFinanceProvider(db, faketx, None)
    app = FastAPI()
    app.include_router(build_api(db, faketx, provider))
    app.include_router(build_oauth(db, faketx))
    return TestClient(app)


def _register(client, redirect=REDIRECT) -> str:
    res = client.post(
        "/api/oauth/register",
        json={"client_name": "TestClaude", "redirect_uris": [redirect]},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["token_endpoint_auth_method"] == "none"  # public client, no secret
    return body["client_id"]


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _approve(client, client_id, challenge, address=ADDR_A, state="st8") -> str:
    """Run the consent flow as the SPA would; returns the auth code."""
    client.get(f"/api/auth/challenge?address={address}")
    res = client.post(
        "/api/oauth/approve",
        json={
            "address": address,
            "signature": "AAAA",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "state": state,
        },
    )
    assert res.status_code == 200, res.text
    redirect = res.json()["redirect"]
    assert redirect.startswith(REDIRECT + "?")
    assert f"state={state}" in redirect
    q = dict(p.split("=", 1) for p in redirect.split("?", 1)[1].split("&"))
    return q["code"]


def _exchange(client, client_id, code, verifier):
    return client.post(
        "/api/oauth/token",
        content=(
            f"grant_type=authorization_code&code={code}&client_id={client_id}"
            f"&redirect_uri={REDIRECT}&code_verifier={verifier}"
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# ---------------------------------------------------------------- discovery

def test_metadata_documents_the_flow(client):
    rs = client.get("/.well-known/oauth-protected-resource").json()
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert rs["authorization_servers"] == [asm["issuer"]]
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["grant_types_supported"] == ["authorization_code"]  # no refresh, by design
    for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert asm[k].startswith(asm["issuer"])
    # path-suffix variants (RFC 9728/8414) answer too
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


def test_www_authenticate_points_at_resource_metadata():
    assert "resource_metadata=" in www_authenticate()
    assert www_authenticate().startswith("Bearer ")


# ------------------------------------------------------------- registration

def test_register_rejects_non_https_redirects(client):
    res = client.post("/api/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]})
    assert res.status_code == 400


# ---------------------------------------------------------------- authorize

def test_authorize_hands_off_to_consent_page(client):
    cid = _register(client)
    _, challenge = _pkce()
    res = client.get(
        "/authorize",
        params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
        },
        follow_redirects=False,
    )
    assert res.status_code == 307
    assert res.headers["location"].startswith("/dashboard/authorize?")


def test_authorize_never_redirects_unknown_clients(client):
    """Unknown client/redirect_uri must 400, not redirect (open-redirect vector)."""
    res = client.get(
        "/authorize",
        params={"response_type": "code", "client_id": "nope",
                "redirect_uri": "https://evil.example/cb", "code_challenge": "x"},
        follow_redirects=False,
    )
    assert res.status_code == 400


def test_approve_requires_valid_signature(client, faketx):
    faketx.valid = False
    cid = _register(client)
    _, challenge = _pkce()
    client.get(f"/api/auth/challenge?address={ADDR_A}")
    res = client.post(
        "/api/oauth/approve",
        json={"address": ADDR_A, "signature": "AAAA", "client_id": cid,
              "redirect_uri": REDIRECT, "code_challenge": challenge},
    )
    assert res.status_code == 401  # bad proof -> no code, no session


# -------------------------------------------------------------------- token

def test_full_flow_mints_working_session(client, db):
    cid = _register(client)
    verifier, challenge = _pkce()
    code = _approve(client, cid, challenge)
    res = _exchange(client, cid, code, verifier)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token_type"] == "Bearer"
    assert 0 < body["expires_in"] <= 3600
    assert "refresh_token" not in body  # renewal = wallet signature, always
    assert auth.resolve_session_state(db, body["access_token"]) == (ADDR_A, "valid")


def test_wrong_pkce_verifier_rejected(client, db):
    cid = _register(client)
    _, challenge = _pkce()
    code = _approve(client, cid, challenge)
    res = _exchange(client, cid, code, "not-the-verifier")
    assert res.status_code == 400 and res.json()["error"] == "invalid_grant"


def test_code_is_single_use(client):
    cid = _register(client)
    verifier, challenge = _pkce()
    code = _approve(client, cid, challenge)
    assert _exchange(client, cid, code, verifier).status_code == 200
    assert _exchange(client, cid, code, verifier).status_code == 400


def test_code_bound_to_client(client):
    cid = _register(client)
    other = _register(client)
    verifier, challenge = _pkce()
    code = _approve(client, cid, challenge)
    assert _exchange(client, other, code, verifier).status_code == 400


def test_expired_code_rejected(client, monkeypatch):
    monkeypatch.setattr("sarf.oauth.AUTH_CODE_TTL", -1)
    cid = _register(client)
    verifier, challenge = _pkce()
    code = _approve(client, cid, challenge)
    assert _exchange(client, cid, code, verifier).status_code == 400


# ------------------------------------------------- transport policy (the point)

def test_transport_policy_splits_by_credential_channel():
    deny = auth.transport_denies
    # OAuth/Bearer clients: every dead state 401s -> Claude shows Reconnect
    assert deny("expired", via_header=True, env="production")
    assert deny("invalid", via_header=True, env="production")
    assert deny("missing", via_header=True, env="production")
    assert not deny("valid", via_header=True, env="production")
    # legacy ?key= clients: expired stays in-band (401 is opaque to them)
    assert not deny("expired", via_header=False, env="production")
    assert deny("invalid", via_header=False, env="production")
    assert deny("missing", via_header=False, env="production")
    # dev mode never hard-denies
    assert not deny("missing", via_header=True, env="dev")


def test_terminate_in_sarf_kills_every_session_for_the_wallet(client, db):
    """The user story: End session on the dashboard must also void the
    OAuth token Claude holds for the same wallet."""
    cid = _register(client)
    verifier, challenge = _pkce()
    code = _approve(client, cid, challenge)
    mcp_token = _exchange(client, cid, code, verifier).json()["access_token"]
    dash_token, _ = auth.mint_session(db, ADDR_A)   # the dashboard bearer
    other_token, _ = auth.mint_session(db, ADDR_B)  # someone else, untouched

    res = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {dash_token}"})
    assert res.status_code == 200
    assert auth.resolve_session_state(db, dash_token) == (None, "expired")
    assert auth.resolve_session_state(db, mcp_token) == (None, "expired")
    assert auth.resolve_session_state(db, other_token) == (ADDR_B, "valid")
