"""Passkey (WebAuthn) layer: session binding + step-up for large orders.

Why this exists, precisely. Sarf is non-custodial: the user's wallet signature
is what authorizes moving funds, and no passkey can substitute for it. So the
passkey is NOT a second signer — it closes two different gaps:

  1. **Session binding.** A session token is a bearer credential that rides in
     an MCP connector. Anyone holding it can read the portfolio and generate
     orders (which still need a wallet signature to execute, but can be used
     to grief, to phish a lookalike order, or to enumerate holdings). Binding
     the session to a passkey means a stolen token alone is inert: the device
     that registered the passkey must be present.

  2. **Step-up on size.** Above a configurable USD threshold an order requires
     a fresh passkey assertion, so a compromised session cannot quietly push a
     large order in front of a user who is click-fatigued in their wallet.

Deliberately NOT per-action: the wallet already prompts on every trade, and a
second biometric on every small order trains users to approve reflexively —
which costs more security than it buys.

Verification is delegated to `py_webauthn`; nothing cryptographic is
hand-rolled here. Challenges are single-use, short-lived, and bound to both an
address and a purpose, so a challenge issued for registration can never be
replayed to satisfy a step-up.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import settings

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database

Purpose = Literal["register", "bind", "stepup"]

CHALLENGE_TTL_SECONDS = 300
# How long a step-up assertion stays good for. Long enough to review and sign
# one order in the wallet; short enough that it cannot authorize a later one.
STEPUP_VALIDITY_SECONDS = 180


class PasskeyError(RuntimeError):
    """Passkey flow failure. Message is safe to show the user."""


def _rp_id() -> str:
    """The WebAuthn Relying Party ID — the registrable domain, no scheme/port.

    A passkey is scoped to this value; changing it silently invalidates every
    registered credential, so it is derived from the public URL rather than
    being independently configurable.
    """
    explicit = os.environ.get("SARF_RP_ID", "").strip()
    if explicit:
        return explicit
    url = settings.public_url or "http://localhost:8760"
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return host.split(":", 1)[0]


def _expected_origins() -> list[str]:
    origins = [settings.public_url] if settings.public_url else []
    origins += ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8760"]
    return [o for o in origins if o]


def enabled() -> bool:
    return settings.passkey_required or settings.passkey_stepup_usd > 0


@dataclass(frozen=True)
class StepUpDecision:
    required: bool
    satisfied: bool
    reason: str

    @property
    def blocked(self) -> bool:
        return self.required and not self.satisfied


# --------------------------------------------------------------- challenges

def _issue_challenge(db: "Database", address: str, purpose: Purpose) -> bytes:
    challenge = secrets.token_bytes(32)
    db.put_passkey_challenge(
        challenge_id=challenge.hex(),
        address=address.lower(),
        purpose=purpose,
        expires_at=time.time() + CHALLENGE_TTL_SECONDS,
    )
    return challenge


def _consume_challenge(db: "Database", address: str, purpose: Purpose, challenge: bytes) -> None:
    row = db.consume_passkey_challenge(challenge.hex())
    if row is None:
        raise PasskeyError("passkey challenge is unknown, already used, or expired")
    if row["address"] != address.lower():
        raise PasskeyError("passkey challenge was issued for a different account")
    if row["purpose"] != purpose:
        # A registration challenge must never satisfy a step-up, and vice versa.
        raise PasskeyError("passkey challenge was issued for a different purpose")
    if row["expires_at"] < time.time():
        raise PasskeyError("passkey challenge expired; start again")


# ------------------------------------------------------------- registration

def registration_options(db: "Database", address: str) -> dict[str, Any]:
    addr = address.lower()
    challenge = _issue_challenge(db, addr, "register")
    existing = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
        for c in db.passkeys_for_address(addr)
    ]
    opts = generate_registration_options(
        rp_id=_rp_id(),
        rp_name="Sarf — X Layer RWA Assistant",
        user_id=addr.encode(),
        user_name=f"{addr[:6]}…{addr[-4:]}",
        user_display_name=f"Sarf wallet {addr[:6]}…{addr[-4:]}",
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=existing or None,
    )
    import json as _json

    return _json.loads(options_to_json(opts))


def verify_registration(db: "Database", address: str, credential: dict[str, Any]) -> dict[str, Any]:
    addr = address.lower()
    try:
        client_data = credential["response"]["clientDataJSON"]
    except (KeyError, TypeError):
        raise PasskeyError("malformed passkey credential")
    challenge = _challenge_from_client_data(client_data)
    _consume_challenge(db, addr, "register", challenge)
    try:
        v = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origins(),
        )
    except Exception as e:
        raise PasskeyError(f"passkey registration could not be verified: {type(e).__name__}")
    import base64

    cred_id = base64.urlsafe_b64encode(v.credential_id).decode().rstrip("=")
    db.put_passkey(
        credential_id=cred_id,
        address=addr,
        public_key=v.credential_public_key,
        sign_count=v.sign_count,
    )
    return {"credential_id": cred_id, "registered": True}


# ----------------------------------------------------------- authentication

def authentication_options(db: "Database", address: str, purpose: Purpose = "stepup") -> dict[str, Any]:
    addr = address.lower()
    creds = db.passkeys_for_address(addr)
    if not creds:
        raise PasskeyError("no passkey is registered for this account")
    challenge = _issue_challenge(db, addr, purpose)
    opts = generate_authentication_options(
        rp_id=_rp_id(),
        challenge=challenge,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
            for c in creds
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    import json as _json

    return _json.loads(options_to_json(opts))


def verify_authentication(
    db: "Database", address: str, credential: dict[str, Any], purpose: Purpose = "stepup"
) -> dict[str, Any]:
    addr = address.lower()
    try:
        client_data = credential["response"]["clientDataJSON"]
        cred_id = credential["id"]
    except (KeyError, TypeError):
        raise PasskeyError("malformed passkey assertion")
    challenge = _challenge_from_client_data(client_data)
    _consume_challenge(db, addr, purpose, challenge)

    stored = db.get_passkey(cred_id)
    if stored is None or stored["address"] != addr:
        raise PasskeyError("this passkey is not registered to this account")
    try:
        v = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=_rp_id(),
            expected_origin=_expected_origins(),
            credential_public_key=stored["public_key"],
            credential_current_sign_count=stored["sign_count"],
        )
    except Exception as e:
        raise PasskeyError(f"passkey assertion could not be verified: {type(e).__name__}")

    # A sign counter that fails to advance is the standard cloned-authenticator
    # signal. Many platform authenticators legitimately report 0 forever, so
    # only a *regression* from a previously non-zero counter is treated as fatal.
    if stored["sign_count"] > 0 and v.new_sign_count <= stored["sign_count"]:
        raise PasskeyError("passkey sign counter did not advance; possible cloned authenticator")
    db.touch_passkey(cred_id, sign_count=v.new_sign_count, verified_at=time.time())
    return {"verified": True, "credential_id": cred_id}


def _challenge_from_client_data(client_data_b64url: str) -> bytes:
    import json as _json

    try:
        data = _json.loads(base64url_to_bytes(client_data_b64url).decode())
        return base64url_to_bytes(data["challenge"])
    except Exception:
        raise PasskeyError("passkey response did not contain a readable challenge")


# ------------------------------------------------------------------ policy

def check_stepup(db: "Database", address: str, order_usd: float | None) -> StepUpDecision:
    """Decide whether this order needs a fresh passkey assertion.

    Fails CLOSED on an unpriceable order when a threshold is configured: if we
    cannot say an order is small, we must not treat it as small.
    """
    threshold = settings.passkey_stepup_usd
    if threshold <= 0:
        return StepUpDecision(False, True, "step-up disabled")
    if not db.passkeys_for_address(address.lower()):
        if settings.passkey_required:
            return StepUpDecision(True, False, "no passkey registered for this account")
        return StepUpDecision(False, True, "no passkey registered; step-up not enforced")
    if order_usd is None:
        return StepUpDecision(
            True, False,
            f"order could not be priced in USD, so the ${threshold:,.0f} step-up "
            "threshold cannot be ruled out",
        )
    if order_usd <= threshold:
        return StepUpDecision(False, True, f"order ~${order_usd:,.2f} is under the step-up threshold")

    last = db.last_passkey_verification(address.lower())
    fresh = last is not None and (time.time() - last) <= STEPUP_VALIDITY_SECONDS
    return StepUpDecision(
        True, fresh,
        f"order ~${order_usd:,.2f} exceeds the ${threshold:,.0f} step-up threshold"
        + ("; recent passkey verification accepted" if fresh else "; passkey verification required"),
    )
