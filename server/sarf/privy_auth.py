"""Privy identity tokens — the one place a browser login becomes a server fact.

Everywhere else in Sarf, identity is an EVM address proven by a signature over
a nonce this server issued (auth.py). That model has no room for an email, and
deliberately so: `privy.jsx` says it plainly — Privy's login state is a claim
the browser relays, not a signature over our nonce — which is why nothing that
moves money has ever consulted it.

The admin console is the exception, and it is worth being precise about why it
is allowed to be one. Admin here READS operational aggregates and revokes
things; it cannot sign, cannot move funds, cannot mint a session for someone
else and cannot raise anybody's caps. So the question is not "may Privy
authorise a transfer" (never) but "may Privy answer 'which human is this'" for
a console whose worst case is an operator seeing counts they should not. For
that, a Privy identity token is a reasonable answer, because it is not a claim
the browser makes — it is a JWT that Privy signed with a key only Privy holds,
and this module verifies that signature before believing one word of it.

What is checked, all of it by PyJWT rather than by hand:

  * ES256 ONLY, pinned in `algorithms`. The classic JWT break is trusting the
    token's own `alg` header ("alg": "none", or an RSA key reinterpreted as an
    HMAC secret); passing an explicit allow-list is what forecloses it.
  * `iss == "privy.io"`, `aud == PRIVY_APP_ID`. Without the audience check, an
    identity token minted for somebody ELSE'S Privy app — which they control,
    and can put any email into — would verify here, since Privy signs those
    with the same key.
  * `exp` (and `iat`/`sub`/`aud`/`iss` required to be present at all).
  * The signing key is one Privy PUBLISHES for this app, fetched from its
    JWKS — see the key-resolution section below for why a key pasted into
    `.env` is the fragile option rather than the simple one.

And then the part that actually gates: the email is read only from a
`google_oauth` linked account. Privy obtained that address through Google's
OAuth flow, so it is an address Google confirmed the user controls. A
self-asserted `email` login account is NOT accepted, because on an app where
email login is enabled that would be a different and weaker claim.

Fail-closed throughout: an unset allow-list, an unconfigured app id, or a key
set that cannot be reached and was never cached all mean no admin, never open
admin. See admin.py for the gate that consumes this.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from .config import settings

log = logging.getLogger("sarf.privy")

# Privy's identity tokens are issued by this fixed issuer for every app; the
# per-app part is the audience.
PRIVY_ISSUER = "privy.io"

# --- key resolution ----------------------------------------------------------
#
# Keys come from Privy's per-app JWKS, not from a value pasted into .env, and
# the reason is a fact about the endpoint rather than a preference: this app's
# key set contains TWO ES256 keys. A single pinned public key therefore covers
# whichever one the dashboard happened to show, and the first token signed with
# the other verifies against nothing — the console 403s the one person it
# exists to admit, and the only cure is a dashboard visit and a restart. A key
# set with more than one key in it is a rotation waiting to happen.
#
# The URL is DERIVED from the configured app id and nothing else. No part of it
# comes from a token, a header or a request, so a forged `kid` can steer which
# key is looked up but never where keys are fetched from — there is no
# attacker-reachable URL here to point somewhere else.
JWKS_HOST = "https://auth.privy.io"
JWKS_TTL = 3600.0          # re-fetch an hour old key set on next use
JWKS_MIN_REFETCH = 60.0    # floor between fetches, so unknown kids cannot storm
JWKS_TIMEOUT = 6.0
JWKS_MAX_BYTES = 64 * 1024

# Privy app ids are opaque alphanumerics. Validated before being interpolated
# into a URL: a path separator in an app id would otherwise choose the endpoint.
_APP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_jwks_lock = threading.Lock()
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}   # app -> (at, {kid: key})
_jwks_attempt: dict[str, float] = {}                        # app -> last fetch try

# Linked-account types whose `email` we are willing to treat as verified.
# google_oauth only: Privy got it from Google's OAuth response, which means
# Google vouched for it. Deliberately not "email" (self-asserted, and only as
# strong as whatever Privy did to confirm it) and not "phone"/"wallet".
VERIFIED_EMAIL_TYPES = ("google_oauth",)


class PrivyError(Exception):
    """The identity token was absent, malformed, expired, or not ours.

    One exception type for every failure mode on purpose: the caller turns it
    into a single 403 with a generic message, because telling an unauthorised
    caller *which* check they failed is a free oracle for tuning the next try.
    The specific reason is logged server-side, where it is a debugging aid
    rather than a hint.
    """


def _pem(raw: str) -> bytes:
    """Normalise whatever shape the verification key arrived in into PEM.

    Privy's dashboard shows the key as a PEM block, but by the time it has
    been through a copy button, a `.env` file and possibly a secrets manager
    it can turn up three ways: proper PEM with real newlines, PEM with the
    newlines escaped as a literal backslash-n (what happens when a multi-line
    value is pasted into a single-line env var), or the bare base64 body with
    the armour stripped. All three mean the same key, and a server that
    refuses two of them is a server that looks broken for a formatting reason.
    """
    key = raw.strip().replace("\\n", "\n")
    if "BEGIN PUBLIC KEY" in key:
        return key.encode()
    body = "".join(key.split())
    wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN PUBLIC KEY-----\n{wrapped}\n-----END PUBLIC KEY-----".encode()


def jwks_url(app_id: str) -> str:
    """The key-set URL for an app id. Raises on an id that is not id-shaped."""
    if not _APP_ID_RE.match(app_id or ""):
        raise PrivyError("PRIVY_APP_ID is not a valid Privy app id")
    return f"{JWKS_HOST}/api/v1/apps/{app_id}/jwks.json"


def _fetch_jwks(app_id: str) -> dict[str, Any]:
    """Pull the key set. -> {kid: public key}. Raises PrivyError on any doubt.

    Bounded on both axes — a timeout and a response-size cap — because this
    runs inside a request and a hung or enormous response would otherwise be
    an admin-console outage rather than a refused login.
    """
    import httpx

    url = jwks_url(app_id)
    try:
        with httpx.Client(timeout=JWKS_TIMEOUT, follow_redirects=False) as c:
            resp = c.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
        if len(resp.content) > JWKS_MAX_BYTES:
            raise PrivyError("jwks response too large")
        doc = resp.json()
    except PrivyError:
        raise
    except Exception as e:
        raise PrivyError(f"could not fetch jwks: {type(e).__name__}: {e}") from e

    keys: dict[str, Any] = {}
    for jwk in doc.get("keys", []) if isinstance(doc, dict) else []:
        # ES256 signing keys only. Anything else in the set is ignored rather
        # than imported: the algorithm is pinned at verification time too, and
        # agreeing with it here keeps one answer to "what may sign a token".
        if not isinstance(jwk, dict) or jwk.get("kty") != "EC":
            continue
        if jwk.get("alg") not in (None, "ES256") or jwk.get("crv") != "P-256":
            continue
        if jwk.get("use") not in (None, "sig"):
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        try:
            from jwt.algorithms import ECAlgorithm

            keys[kid] = ECAlgorithm.from_jwk(json.dumps(jwk))
        except Exception:
            log.warning("privy jwks: unusable key %s", kid)
    if not keys:
        raise PrivyError("jwks contained no usable ES256 keys")
    return keys


def _keys_for_app(app_id: str, *, want_kid: str | None = None) -> dict[str, Any]:
    """Cached key set, refreshed when stale or when a `kid` is unknown.

    Refreshing on an unknown kid is what makes a rotation invisible here: the
    first token signed with a new key misses the cache, triggers one fetch and
    then verifies. JWKS_MIN_REFETCH is the floor on that path — without it, a
    stream of tokens carrying invented kids would turn every request into an
    outbound fetch.
    """
    now = time.time()
    with _jwks_lock:
        cached = _jwks_cache.get(app_id)
        fresh = cached and (now - cached[0]) < JWKS_TTL
        known = fresh and (want_kid is None or want_kid in cached[1])
        if known:
            return cached[1]
        if (now - _jwks_attempt.get(app_id, 0.0)) < JWKS_MIN_REFETCH:
            # Too soon to try again. A usable cached set still beats failing.
            if cached:
                return cached[1]
            raise PrivyError("jwks unavailable (waiting before retry)")
        _jwks_attempt[app_id] = now

    try:
        keys = _fetch_jwks(app_id)
    except PrivyError:
        # Serve a stale set rather than locking the operator out over a blip.
        # Staleness cannot admit anyone: these are public keys, and a token
        # still has to carry a signature one of them made.
        with _jwks_lock:
            cached = _jwks_cache.get(app_id)
        if cached:
            log.warning("privy jwks fetch failed; using cached keys")
            return cached[1]
        raise

    with _jwks_lock:
        _jwks_cache[app_id] = (time.time(), keys)
    return keys


def reset_jwks_cache() -> None:
    """Drop cached key sets. For tests and for a deliberate re-read."""
    with _jwks_lock:
        _jwks_cache.clear()
        _jwks_attempt.clear()


def configured() -> tuple[bool, str | None]:
    """Is the Privy gate usable? -> (ok, what is missing).

    Split out from verification so the console can SAY what is unset instead
    of returning a bare "not an admin" to the one person who is. A half-
    configured gate is a deployment mistake, and it should look like one.

    The verification key is NOT required: keys normally come from Privy's
    JWKS. It is an optional pin for a deployment that cannot make outbound
    requests — see _signing_key.
    """
    if not settings.privy_app_id:
        return False, "PRIVY_APP_ID is not set"
    if not _APP_ID_RE.match(settings.privy_app_id):
        return False, "PRIVY_APP_ID is not a valid Privy app id"
    if not settings.admin_emails:
        return False, "SARF_ADMIN_EMAILS is not set"
    return True, None


def _signing_key(kid: str | None):
    """The key a token claims to be signed by.

    PRIVY_VERIFICATION_KEY, when set, wins and is used alone. It exists for a
    deployment with no outbound network, and it is the fragile option on
    purpose-built display: it pins ONE key out of a set that currently holds
    two, so a rotation ends admin access until someone re-pastes it. Unset —
    the recommended state — keys come from the JWKS and rotation is invisible.
    """
    if settings.privy_verification_key:
        return _pem(settings.privy_verification_key)
    keys = _keys_for_app(settings.privy_app_id, want_kid=kid)
    if kid is None:
        # No kid in the header and more than one candidate: refuse rather than
        # guess. Guessing would mean trying keys until one matched, which is a
        # slower way of saying "any key in the set will do".
        if len(keys) == 1:
            return next(iter(keys.values()))
        raise PrivyError("token has no kid and the key set has several keys")
    key = keys.get(kid)
    if key is None:
        raise PrivyError(f"no Privy key matches kid {kid!r}")
    return key


def verify(token: str | None) -> dict[str, Any]:
    """Verify a Privy identity token. -> its claims. Raises PrivyError.

    Signature, issuer, audience and expiry are all enforced by PyJWT with the
    algorithm pinned; nothing in the token decides how the token is checked.
    The header's `kid` selects WHICH published key is tried — it cannot
    introduce a key, only name one Privy already publishes for this app.
    """
    ok, why = configured()
    if not ok:
        raise PrivyError(f"privy gate not configured: {why}")
    if not token or not token.strip():
        raise PrivyError("no identity token presented")

    import jwt  # imported lazily: nothing else in the server needs JWT

    token = token.strip()
    try:
        header = jwt.get_unverified_header(token)
    except Exception as e:
        raise PrivyError(f"unreadable token header: {type(e).__name__}") from e
    # Checked here as well as in `algorithms` below. Redundant, and worth it:
    # this is the check whose absence is the classic JWT break, and a reader
    # should not have to know that PyJWT enforces it to see that it happens.
    if header.get("alg") != "ES256":
        raise PrivyError(f"unexpected token algorithm {header.get('alg')!r}")

    kid = header.get("kid")
    key = _signing_key(kid if isinstance(kid, str) else None)

    try:
        return jwt.decode(
            token,
            key=key,
            algorithms=["ES256"],
            issuer=PRIVY_ISSUER,
            audience=settings.privy_app_id,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except Exception as e:  # PyJWT raises a family; they all mean "no"
        raise PrivyError(f"{type(e).__name__}: {e}") from e


def _linked_accounts(claims: dict[str, Any]) -> list[dict[str, Any]]:
    """The token's linked accounts, whichever way Privy encoded them.

    Privy ships this claim as a JSON *string* rather than a nested array, to
    keep the token small. Handled as either, because "it is a string today"
    is not a load-bearing fact to build a permission check on.
    """
    raw = claims.get("linked_accounts")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [a for a in raw if isinstance(a, dict)] if isinstance(raw, list) else []


def verified_emails(claims: dict[str, Any]) -> set[str]:
    """Every Google-verified address on the token, lowercased.

    A set, not a single value: Privy allows more than one linked account, and
    picking "the first one" would make which address you are treated as depend
    on Privy's array order.
    """
    out: set[str] = set()
    for acct in _linked_accounts(claims):
        if acct.get("type") not in VERIFIED_EMAIL_TYPES:
            continue
        email = acct.get("email")
        if isinstance(email, str) and email.strip():
            out.add(email.strip().lower())
    return out


def admin_email(token: str | None) -> str:
    """The allow-listed address this token proves, or raise.

    Matching is exact after lowercasing — no Gmail dot/plus folding. Treating
    `holly.hush@` and `hollyhush@` as one identity is a convenience that would
    also mean the allow-list covers addresses nobody wrote down, and an
    admin gate is the wrong place to be clever about equivalence.
    """
    claims = verify(token)
    emails = verified_emails(claims)
    match = emails & set(settings.admin_emails)
    if not match:
        # Logged, not returned: the caller gets one undifferentiated refusal.
        # The account *types* go in the log but never the addresses, so an
        # operator can tell "no Google account linked" from "linked, but a
        # different address" without the log becoming a list of user emails.
        types = sorted({str(a.get("type")) for a in _linked_accounts(claims)})
        log.warning("privy identity token for %s carries no admin email "
                    "(linked account types: %s; %d google-verified address(es))",
                    claims.get("sub", "?"), ",".join(types) or "none", len(emails))
        raise PrivyError("no allow-listed email on this identity token")
    return sorted(match)[0]
