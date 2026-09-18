"""Tests for the admin console gate.

The gate is the whole point of this feature, so these tests are mostly about
what must be REFUSED. Every rejection path gets a case, because an admin
console that is accidentally open does not look broken from the outside — it
looks like it is working.

Fully offline: an ES256 keypair is generated here and identity tokens are
minted with it, so nothing contacts Privy.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sarf import admin, auth, privy_auth
from sarf.db import Database

ADMIN_EMAIL = "hollyhushz11@gmail.com"
APP_ID = "test-privy-app-id"
ADDR = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def keys():
    """An ES256 keypair standing in for Privy's signing key."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pem


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "admin.db"))


class _FakeSettings:
    """Only the fields the gate reads. A stand-in rather than a patched
    Settings() because Settings is frozen, and mutating it in place would
    leak into every other test in the session."""

    def __init__(self, pem, *, emails=(ADMIN_EMAIL,), app_id=APP_ID):
        self.privy_verification_key = pem
        self.privy_app_id = app_id
        self.admin_emails = frozenset(emails)


@pytest.fixture
def gate(monkeypatch, keys):
    """privy_auth wired to the test keypair."""
    _, pem = keys
    monkeypatch.setattr(privy_auth, "settings", _FakeSettings(pem))
    return pem


def token(priv, *, email=ADMIN_EMAIL, aud=APP_ID, iss="privy.io",
          exp_delta=600, acct_type="google_oauth", accounts=None,
          alg="ES256", key=None, stringify=True):
    """Mint an identity token shaped like Privy's."""
    linked = accounts if accounts is not None else (
        [{"type": acct_type, "email": email, "subject": "google-123"}]
        if email is not None else []
    )
    now = int(time.time())
    claims = {
        "sid": "sess-1", "iss": iss, "aud": aud, "sub": "did:privy:abc",
        "iat": now, "exp": now + exp_delta,
        "linked_accounts": json.dumps(linked) if stringify else linked,
    }
    return jwt.encode(claims, key if key is not None else priv, algorithm=alg)


# ------------------------------------------------------- token verification

class TestIdentityToken:
    def test_valid_token_yields_the_admin_email(self, keys, gate):
        priv, _ = keys
        assert privy_auth.admin_email(token(priv)) == ADMIN_EMAIL

    def test_linked_accounts_as_a_real_array_also_works(self, keys, gate):
        """Privy stringifies the claim today. The gate must not depend on it."""
        priv, _ = keys
        assert privy_auth.admin_email(token(priv, stringify=False)) == ADMIN_EMAIL

    def test_bare_base64_verification_key_is_accepted(self, monkeypatch, keys):
        """A PEM that lost its armour in an env var is the same key."""
        priv, pem = keys
        body = "".join(l for l in pem.splitlines() if "-----" not in l)
        monkeypatch.setattr(privy_auth, "settings", _FakeSettings(body))
        assert privy_auth.admin_email(token(priv)) == ADMIN_EMAIL

    def test_escaped_newline_verification_key_is_accepted(self, monkeypatch, keys):
        priv, pem = keys
        monkeypatch.setattr(privy_auth, "settings",
                            _FakeSettings(pem.replace("\n", "\\n")))
        assert privy_auth.admin_email(token(priv)) == ADMIN_EMAIL

    @pytest.mark.parametrize("missing", ["app_id", "emails"])
    def test_half_configured_gate_refuses(self, monkeypatch, keys, missing):
        _, pem = keys
        s = _FakeSettings(pem)
        setattr(s, {"app_id": "privy_app_id", "emails": "admin_emails"}[missing],
                "" if missing != "emails" else frozenset())
        monkeypatch.setattr(privy_auth, "settings", s)
        ok, why = privy_auth.configured()
        assert not ok and why
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email("anything")

    def test_a_pasted_app_id_that_is_not_id_shaped_is_refused(self, monkeypatch, keys):
        """It is interpolated into the JWKS URL, so a path separator in it
        would choose the endpoint."""
        _, pem = keys
        monkeypatch.setattr(privy_auth, "settings",
                            _FakeSettings(pem, app_id="../../evil"))
        ok, why = privy_auth.configured()
        assert not ok and "app id" in why
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.jwks_url("../../evil")

    def test_verification_key_is_optional(self, monkeypatch, keys):
        """Unset is the RECOMMENDED state — keys come from the JWKS."""
        _, pem = keys
        monkeypatch.setattr(privy_auth, "settings", _FakeSettings(""))
        ok, why = privy_auth.configured()
        assert ok and why is None

    def test_no_token_refused(self, keys, gate):
        for empty in (None, "", "   "):
            with pytest.raises(privy_auth.PrivyError):
                privy_auth.admin_email(empty)

    def test_token_signed_by_a_different_key_refused(self, keys, gate):
        """The whole gate: an attacker can mint any claims they like."""
        attacker = ec.generate_private_key(ec.SECP256R1())
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(attacker))

    def test_alg_none_refused(self, keys, gate):
        """Classic JWT break. algorithms=["ES256"] is what forecloses it."""
        unsigned = jwt.encode(
            {"iss": "privy.io", "aud": APP_ID, "sub": "x",
             "iat": int(time.time()), "exp": int(time.time()) + 600,
             "linked_accounts": json.dumps(
                 [{"type": "google_oauth", "email": ADMIN_EMAIL}])},
            key="", algorithm="none",
        )
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(unsigned)

    def test_token_for_another_privy_app_refused(self, keys, gate):
        """Anyone can create a Privy app and put any email in it. The audience
        check is the only thing standing between that and admin."""
        priv, _ = keys
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, aud="somebody-elses-app"))

    def test_wrong_issuer_refused(self, keys, gate):
        priv, _ = keys
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, iss="not-privy.io"))

    def test_expired_token_refused(self, keys, gate):
        priv, _ = keys
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, exp_delta=-60))

    def test_non_admin_email_refused(self, keys, gate):
        priv, _ = keys
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, email="someone.else@gmail.com"))

    def test_self_asserted_email_account_refused(self, keys, gate):
        """type='email' is not Google-verified, so it does not count even when
        the address matches."""
        priv, _ = keys
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, acct_type="email"))

    def test_email_case_and_whitespace_normalised(self, keys, gate):
        priv, _ = keys
        assert privy_auth.admin_email(
            token(priv, email=f"  {ADMIN_EMAIL.upper()}  ")) == ADMIN_EMAIL

    def test_gmail_dots_are_not_folded(self, keys, gate):
        """holly.hush@ and hollyhush@ are one Gmail inbox but two strings, and
        an allow-list should cover only what was written down."""
        priv, _ = keys
        dotted = ADMIN_EMAIL.replace("hollyhushz11", "holly.hushz11")
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, email=dotted))

    def test_no_linked_accounts_refused(self, keys, gate):
        priv, _ = keys
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, accounts=[]))

    def test_junk_linked_accounts_claim_does_not_crash(self, keys, gate):
        priv, _ = keys
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(token(priv, accounts=["not-an-object", 7]))


# --------------------------------------------------------------- the routes

@pytest.fixture
def client(db, monkeypatch, keys):
    _, pem = keys
    monkeypatch.setattr(privy_auth, "settings", _FakeSettings(pem))
    app = FastAPI()
    app.include_router(admin.build_admin_api(db))
    return TestClient(app)


def _session(db, address=ADDR):
    tok, _ = auth.mint_session(db, address.lower())
    return {"authorization": f"Bearer {tok}"}


class TestRoutes:
    def test_no_session_is_401(self, client):
        assert client.get("/api/admin/overview").status_code == 401

    def test_session_without_identity_token_is_403(self, client, db):
        """The case that matters most: an ordinary signed-in user, holding a
        perfectly valid session, hitting the admin API directly."""
        r = client.get("/api/admin/overview", headers=_session(db))
        assert r.status_code == 403
        assert r.json()["detail"] == "not an administrator"

    def test_identity_token_without_session_is_401(self, client, keys):
        """Privy alone is not enough either — both factors or nothing."""
        priv, _ = keys
        r = client.get("/api/admin/overview",
                       headers={"x-privy-id-token": token(priv)})
        assert r.status_code == 401

    def test_both_factors_admits(self, client, db, keys):
        priv, _ = keys
        r = client.get("/api/admin/overview",
                       headers={**_session(db), "x-privy-id-token": token(priv)})
        assert r.status_code == 200
        body = r.json()
        assert body["users"]["total"] == 0
        assert body["deposits"]["stuck"] == 0
        assert "fees" in body and body["fees"]["estimate"] is True

    def test_overview_never_carries_key_material(self, client, db, keys):
        """A regression guard for the widening SELECT: `grants` holds a sealed
        signing key and `passkeys` a public-key blob, and neither belongs in
        an operator's browser."""
        priv, _ = keys
        r = client.get("/api/admin/overview",
                       headers={**_session(db), "x-privy-id-token": token(priv)})
        blob = json.dumps(r.json())
        for leak in ("sealed_key", "public_key", "sarf_sess_", "sarf_refr_"):
            assert leak not in blob

    def test_whoami_answers_false_rather_than_erroring(self, client, db):
        """Every signed-in user calls this on page load."""
        r = client.get("/api/admin/whoami", headers=_session(db))
        assert r.status_code == 200
        assert r.json() == {"is_admin": False, "configured": True,
                            "reason": "no identity token presented",
                            "address": ADDR.lower()}

    def test_whoami_says_which_check_refused_but_never_who_would_pass(
            self, client, db, keys):
        """The reason is a debugging aid for the operator, not a directory.

        An operator locked out of their own console needs to know whether the
        token never arrived or arrived with the wrong address on it. What they
        must not be able to read off the page is the allow-list itself.
        """
        priv, _ = keys
        r = client.get("/api/admin/whoami",
                       headers={**_session(db),
                                "x-privy-id-token": token(priv, email="someone@else.com")})
        body = r.json()
        assert body["is_admin"] is False
        assert body["reason"] == "no allow-listed email on this identity token"
        assert ADMIN_EMAIL not in r.text

    def test_whoami_reports_a_half_configured_deployment(self, client, db,
                                                         monkeypatch, keys):
        _, pem = keys
        monkeypatch.setattr(privy_auth, "settings", _FakeSettings(pem, emails=()))
        r = client.get("/api/admin/whoami", headers=_session(db))
        assert r.json()["configured"] is False
        assert "SARF_ADMIN_EMAILS" in r.json()["reason"]

    def test_the_cookie_privy_sets_is_accepted_when_no_header_arrives(
            self, client, db, keys):
        """The browser carries the token even when the SDK read comes back null.

        This is the path that keeps a tab from sticking: fetching the token per
        request occasionally answers null while the SDK is between states, and
        a tab that loads once then shows whatever it got would sit on that
        refusal forever.
        """
        priv, _ = keys
        client.cookies.set("privy-id-token", token(priv))
        r = client.get("/api/admin/whoami", headers=_session(db))
        client.cookies.clear()
        assert r.json()["is_admin"] is True

    def test_the_cookie_alone_is_not_a_way_in(self, client, db, keys):
        """The bearer is what makes this safe to accept from a cookie.

        A cookie rides along on any request the browser is talked into making,
        so if it were sufficient, a cross-site page could drive the console.
        It is not sufficient: without the Authorization header — which no
        cross-site page can set on a cookie-bearing request — there is no
        session, and the route refuses before identity is even consulted.
        """
        priv, _ = keys
        client.cookies.set("privy-id-token", token(priv))
        r = client.get("/api/admin/overview")
        client.cookies.clear()
        assert r.status_code == 401

    def test_an_explicit_header_wins_over_a_stale_cookie(self, client, db, keys):
        """A fresh token beats whatever the browser happens to still hold."""
        priv, _ = keys
        client.cookies.set("privy-id-token", token(priv, email="someone@else.com"))
        r = client.get("/api/admin/whoami",
                       headers={**_session(db), "x-privy-id-token": token(priv)})
        client.cookies.clear()
        assert r.json()["is_admin"] is True
        assert r.json()["email"] == ADMIN_EMAIL

    def test_whoami_names_the_admin(self, client, db, keys):
        priv, _ = keys
        r = client.get("/api/admin/whoami",
                       headers={**_session(db), "x-privy-id-token": token(priv)})
        assert r.json() == {"is_admin": True, "configured": True,
                            "email": ADMIN_EMAIL, "address": ADDR.lower()}

    @pytest.mark.parametrize("path", [
        "/api/admin/overview", "/api/admin/users", "/api/admin/orders",
        "/api/admin/deposits", "/api/admin/grants", "/api/admin/audit",
    ])
    def test_every_read_route_is_gated(self, client, db, path):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=_session(db)).status_code == 403

    @pytest.mark.parametrize("path,body", [
        ("/api/admin/sessions/revoke", {"address": OTHER}),
        ("/api/admin/grants/revoke", {"address": OTHER}),
        ("/api/admin/deposits/retry", {"burn_tx": "0x" + "11" * 32}),
    ])
    def test_every_action_route_is_gated(self, client, db, path, body):
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers=_session(db)).status_code == 403

    def test_page_size_is_clamped(self, client, db, keys):
        priv, _ = keys
        r = client.get("/api/admin/users?limit=999999999",
                       headers={**_session(db), "x-privy-id-token": token(priv)})
        assert r.status_code == 200  # clamped, not a stalled event loop

    def test_bad_address_rejected_before_anything_happens(self, client, db, keys):
        priv, _ = keys
        r = client.post("/api/admin/sessions/revoke", json={"address": "nope"},
                        headers={**_session(db), "x-privy-id-token": token(priv)})
        assert r.status_code == 400


class TestActions:
    def test_revoking_sessions_ends_them(self, client, db, keys):
        priv, _ = keys
        victim_token, _ = auth.mint_session(db, OTHER.lower())
        assert auth.resolve_session(db, victim_token) == OTHER.lower()

        r = client.post("/api/admin/sessions/revoke", json={"address": OTHER},
                        headers={**_session(db), "x-privy-id-token": token(priv)})
        assert r.status_code == 200 and r.json()["sessions_revoked"] == 1
        assert auth.resolve_session(db, victim_token) is None

    def test_every_action_is_written_to_the_audit_log(self, client, db, keys):
        priv, _ = keys
        h = {**_session(db), "x-privy-id-token": token(priv)}
        client.post("/api/admin/sessions/revoke", json={"address": OTHER}, headers=h)
        client.post("/api/admin/grants/revoke", json={"address": OTHER}, headers=h)
        client.post("/api/admin/deposits/retry",
                    json={"burn_tx": "0x" + "11" * 32}, headers=h)

        entries = db.admin_audit_log()
        assert [e["action"] for e in entries] == [
            "deposits.retry", "grants.revoke", "sessions.revoke"]
        assert {e["actor_email"] for e in entries} == {ADMIN_EMAIL}
        assert {e["actor_address"] for e in entries} == {ADDR.lower()}

    def test_grant_revoke_says_it_is_only_the_local_half(self, client, db, keys):
        priv, _ = keys
        r = client.post("/api/admin/grants/revoke", json={"address": OTHER},
                        headers={**_session(db), "x-privy-id-token": token(priv)})
        assert "on-chain revoke" in r.json()["note"]

    def test_retrying_an_unknown_deposit_says_so(self, client, db, keys):
        priv, _ = keys
        r = client.post("/api/admin/deposits/retry",
                        json={"burn_tx": "0x" + "99" * 32},
                        headers={**_session(db), "x-privy-id-token": token(priv)})
        assert r.json()["requeued"] is False and r.json()["note"]

    def test_a_minted_deposit_is_never_re_queued(self, db):
        """Re-minting an already settled message is wasted gas and a confusing
        log loop, so the DB refuses regardless of what the console asks."""
        burn = "0x" + "22" * 32
        db.record_deposit(burn_tx=burn, address=OTHER.lower(), amount_usd=10.0)
        db.settle_deposit(burn, "0x" + "33" * 32)
        assert db.requeue_deposit(burn) is False

    def test_a_stuck_deposit_goes_back_to_the_sweeper(self, db):
        burn = "0x" + "44" * 32
        db.record_deposit(burn_tx=burn, address=OTHER.lower(), amount_usd=10.0)
        for _ in range(5):
            db.note_deposit_attempt(burn, "boom")
        db.fail_deposit(burn, "gave up")
        assert db.pending_deposits() == []

        assert db.requeue_deposit(burn) is True
        pending = db.pending_deposits()
        assert len(pending) == 1 and pending[0]["burn_tx"] == burn
        # Attempts reset too: leaving the counter at the cap means the next
        # failure abandons it again immediately.
        assert pending[0]["attempts"] == 0


# ------------------------------------------------------------------ the JWKS
#
# The reason these exist: this app's published key set holds TWO ES256 keys.
# Pinning one of them in .env is therefore a rotation away from locking the
# operator out, and "it worked when I set it up" is exactly how that defect
# hides. Rotation is the case worth testing.

def _jwk_of(priv, kid):
    """A public JWK for an EC private key, shaped like Privy's."""
    from jwt.algorithms import ECAlgorithm

    jwk = json.loads(ECAlgorithm.to_jwk(priv.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return jwk


class _FakeJwksSettings(_FakeSettings):
    """JWKS mode: no pinned verification key."""

    def __init__(self, **kw):
        super().__init__("", **kw)


@pytest.fixture
def jwks(monkeypatch):
    """Two keys, as Privy actually publishes, behind a counted fetch."""
    old = ec.generate_private_key(ec.SECP256R1())
    new = ec.generate_private_key(ec.SECP256R1())
    state = {"keys": {"kid-old": old, "kid-new": new}, "fetches": 0, "fail": False}

    def fake_fetch(app_id):
        state["fetches"] += 1
        if state["fail"]:
            raise privy_auth.PrivyError("network down")
        from jwt.algorithms import ECAlgorithm
        return {
            kid: ECAlgorithm.from_jwk(json.dumps(_jwk_of(k, kid)))
            for kid, k in state["keys"].items()
        }

    monkeypatch.setattr(privy_auth, "settings", _FakeJwksSettings())
    monkeypatch.setattr(privy_auth, "_fetch_jwks", fake_fetch)
    privy_auth.reset_jwks_cache()
    yield state
    privy_auth.reset_jwks_cache()


def kid_token(priv, kid, **kw):
    """A token whose header names which key signed it."""
    now = int(time.time())
    claims = {
        "sid": "s", "iss": "privy.io", "aud": APP_ID, "sub": "did:privy:abc",
        "iat": now, "exp": now + 600,
        "linked_accounts": json.dumps(
            [{"type": "google_oauth", "email": kw.get("email", ADMIN_EMAIL)}]),
    }
    return jwt.encode(claims, priv, algorithm="ES256", headers={"kid": kid})


class TestJwks:
    def test_either_published_key_verifies(self, jwks):
        """The whole point. A pinned PEM covers one of these two."""
        for kid, priv in jwks["keys"].items():
            assert privy_auth.admin_email(kid_token(priv, kid)) == ADMIN_EMAIL

    def test_key_set_is_cached_not_refetched_per_request(self, jwks):
        priv = jwks["keys"]["kid-old"]
        for _ in range(5):
            privy_auth.admin_email(kid_token(priv, "kid-old"))
        assert jwks["fetches"] == 1

    def test_rotation_to_an_unseen_kid_refetches_and_verifies(self, jwks):
        """A key Privy adds after we cached must not need a restart."""
        privy_auth.admin_email(kid_token(jwks["keys"]["kid-old"], "kid-old"))
        assert jwks["fetches"] == 1

        rotated = ec.generate_private_key(ec.SECP256R1())
        jwks["keys"]["kid-rotated"] = rotated
        privy_auth._jwks_attempt.clear()   # past the anti-storm floor

        assert privy_auth.admin_email(
            kid_token(rotated, "kid-rotated")) == ADMIN_EMAIL
        assert jwks["fetches"] == 2

    def test_unknown_kid_does_not_storm_the_endpoint(self, jwks):
        """Forged kids are free to generate; fetches must not be."""
        privy_auth.admin_email(kid_token(jwks["keys"]["kid-old"], "kid-old"))
        before = jwks["fetches"]
        stranger = ec.generate_private_key(ec.SECP256R1())
        for _ in range(20):
            with pytest.raises(privy_auth.PrivyError):
                privy_auth.admin_email(kid_token(stranger, "made-up-kid"))
        assert jwks["fetches"] == before

    def test_a_key_privy_does_not_publish_is_refused(self, jwks):
        """Naming a real kid does not make a foreign signature verify."""
        stranger = ec.generate_private_key(ec.SECP256R1())
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(kid_token(stranger, "kid-old"))

    def test_a_fetch_failure_falls_back_to_cached_keys(self, jwks):
        """A blip must not lock the operator out. Stale public keys admit
        nobody new — a token still has to carry a matching signature."""
        priv = jwks["keys"]["kid-old"]
        privy_auth.admin_email(kid_token(priv, "kid-old"))
        jwks["fail"] = True
        privy_auth._jwks_attempt.clear()
        # Force staleness so the next call attempts a fetch and fails.
        app, (_, cached) = next(iter(privy_auth._jwks_cache.items()))
        privy_auth._jwks_cache[app] = (0.0, cached)
        assert privy_auth.admin_email(kid_token(priv, "kid-old")) == ADMIN_EMAIL

    def test_with_no_cache_and_no_network_it_fails_closed(self, jwks):
        jwks["fail"] = True
        with pytest.raises(privy_auth.PrivyError):
            privy_auth.admin_email(kid_token(jwks["keys"]["kid-old"], "kid-old"))

    def test_a_pinned_key_overrides_the_jwks_entirely(self, monkeypatch, keys, jwks):
        """The offline escape hatch still works, and does not fetch."""
        priv, pem = keys
        monkeypatch.setattr(privy_auth, "settings", _FakeSettings(pem))
        assert privy_auth.admin_email(token(priv)) == ADMIN_EMAIL
        assert jwks["fetches"] == 0
