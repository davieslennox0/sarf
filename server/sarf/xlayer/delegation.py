"""Session-key delegation: issue, sign, relay, expire.

WHAT SARF HOLDS, AND WHAT IT IS WORTH
    A session key. Not the user's wallet key — that never leaves their wallet
    — and not their funds. The session key has no authority of its own: every
    power it has is written into the grant the user signed on-chain, and the
    contract (contracts/src/SarfSessionKey.sol) is what enforces it. Holding
    this key lets Sarf submit swaps of allowed tokens, at prices bounded by
    minBuyAmount, under per-trade and per-day caps, until the grant expires.
    It cannot move funds anywhere, touch OKB, call anything but the granted
    router, or raise its own limits, and the user can revoke it without
    Sarf's cooperation.

    That is the whole security argument, and it lives in Solidity rather than
    here. This module is deliberately not a place where limits are enforced:
    a check in Python is a check an attacker who reaches this process can
    skip. The caps below are copied into responses so the user can see them;
    the ones that bind are on-chain.

AT REST
    Session private keys are encrypted with a key derived from
    SARF_SESSION_SECRET, so a stolen database file is not a set of usable
    keys. Rotation is automatic: `due_for_rotation` retires a key after
    `rotate_after_seconds` (24h by default) even when the grant runs longer,
    so the window in which any single key is worth stealing stays short. A
    rotated key is replaced by re-signing the grant, which needs the user's
    wallet again — the rotation cannot quietly extend the user's exposure.

GAS
    executeSwap is callable by anyone; the session signature is the authority,
    not the sender. So a relayer submits it and pays the OKB. The relayer is a
    dedicated gas-only wallet and is deliberately NOT any wallet that holds
    funds — see the note in README on why the payout wallet is not reused.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak

from ..config import settings
from ..validation import ValidationError
from . import rpc
from .evm import to_checksum_address, validate_evm_address
from .registry import CHAIN_ID

# Must match SarfSessionKey.SWAP_TYPEHASH exactly. Verified against
# `cast abi-encode` — a drift here produces signatures the contract rejects,
# which fails closed but silently wastes the user's time, so it is pinned.
SWAP_TYPEHASH = keccak(
    text="SarfSwap(address account,uint256 chainId,address sellToken,address buyToken,"
         "uint256 sellAmount,uint256 minBuyAmount,address target,bytes32 dataHash,"
         "uint256 nonce,uint256 deadline)"
)

# Mirrors the contract's ceiling. Kept in sync deliberately rather than read
# from chain on every call: if they ever disagree the contract wins, and a
# grant this module refuses to request is a grant that cannot exist.
MAX_GRANT_SECONDS = 30 * 24 * 3600


class DelegationError(RuntimeError):
    """Something about the grant is wrong. Always safe to show a user."""


@dataclass(frozen=True)
class Grant:
    address: str
    session_address: str
    delegate: str
    router: str
    stable: str
    expiry: int
    per_trade_cap: int      # stable min-units (USDT: 6dp)
    daily_cap: int
    created_at: float
    rotated_at: float
    revoked_at: float | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None and time.time() < self.expiry

    def view(self, stable_decimals: int = 6) -> dict[str, Any]:
        """Public shape. Caps are shown in dollars because that is the unit
        the user chose them in; the on-chain values are the authority."""
        def usd(x: int) -> float:
            return round(x / (10 ** stable_decimals), 2)
        return {
            "active": self.active,
            "session_key": self.session_address,
            "delegate_contract": self.delegate,
            "expires_at": self.expiry,
            "expires_in_seconds": max(0, int(self.expiry - time.time())),
            "per_trade_cap_usd": usd(self.per_trade_cap),
            "daily_cap_usd": usd(self.daily_cap),
            "revoked": self.revoked_at is not None,
            "chain_id": CHAIN_ID,
            "note": (
                "Sarf holds a session key scoped by this grant. It can trade the "
                "allowed tokens within these caps until it expires, and can never "
                "move funds, spend gas, or raise its own limits. Revoke any time "
                "from the Security page — it needs nothing from Sarf."
            ),
        }


# --------------------------------------------------------------- key storage

def _cipher_key() -> bytes:
    """Derive the at-rest key from the session secret.

    Separate HKDF info string from anything else that secret is used for, so
    a key that encrypts session tokens is not the same key that encrypts
    signing material.
    """
    secret = settings.session_secret
    if not secret:
        raise DelegationError("SARF_SESSION_SECRET is not set; refusing to store a session key")
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"sarf.delegation.v1",
        info=b"session-key-at-rest",
    ).derive(secret.encode())


def _seal(private_key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_cipher_key()).encrypt(nonce, private_key, b"sarf-session-key")
    return base64.b64encode(nonce + ct).decode()


def _open(sealed: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(sealed)
    return AESGCM(_cipher_key()).decrypt(raw[:12], raw[12:], b"sarf-session-key")


def new_session_key() -> tuple[str, str]:
    """-> (address, sealed_private_key). The plaintext key exists only inside
    this function and inside `_open` at signing time."""
    acct = Account.create()
    return acct.address, _seal(acct.key)


# ------------------------------------------------------------------ lifecycle

def requested_expiry(days: float) -> int:
    """Validate the lifetime the user picked and turn it into a timestamp.

    Bounded at both ends: under an hour is a grant that expires mid-conversation
    and reads as breakage, and the contract refuses anything over 30 days
    regardless of what is asked here.
    """
    if not isinstance(days, (int, float)) or days != days:
        raise ValidationError("days must be a number")
    seconds = int(float(days) * 24 * 3600)
    if seconds < 3600:
        raise ValidationError("the shortest grant is 1 hour")
    if seconds > MAX_GRANT_SECONDS:
        raise ValidationError(
            f"the longest grant is {MAX_GRANT_SECONDS // 86400} days — the contract "
            "will not accept more, so a longer one cannot be created"
        )
    return int(time.time()) + seconds


def due_for_rotation(grant: Grant) -> bool:
    """A live grant whose key has outlived its rotation window.

    Rotation shortens the life of the *key*, not the grant. Re-keying needs
    the user's wallet signature again, so it can never silently extend what
    they agreed to — it only shrinks how long any one key is worth stealing.
    """
    return grant.active and (time.time() - grant.rotated_at) >= settings.session_key_rotate_seconds


# ------------------------------------------------------------------- signing

def swap_digest(
    *, account: str, sell_token: str, buy_token: str, sell_amount: int,
    min_buy_amount: int, target: str, data: str, nonce: int, deadline: int,
) -> bytes:
    """The exact 32 bytes SarfSessionKey.executeSwap recovers against."""
    inner = keccak(abi_encode(
        ["bytes32", "address", "uint256", "address", "address",
         "uint256", "uint256", "address", "bytes32", "uint256", "uint256"],
        [SWAP_TYPEHASH, account, CHAIN_ID, sell_token, buy_token,
         sell_amount, min_buy_amount, target,
         keccak(bytes.fromhex(data[2:] if data.startswith("0x") else data)),
         nonce, deadline],
    ))
    return inner


def sign_swap(sealed_key: str, **kw: Any) -> tuple[str, int]:
    """-> (signature, nonce). A fresh random nonce each time; the contract
    records spent ones, so collisions fail closed rather than replaying."""
    nonce = kw.pop("nonce", None) or secrets.randbits(128)
    inner = swap_digest(nonce=nonce, **kw)
    signed = Account.from_key(_open(sealed_key)).sign_message(encode_defunct(inner))
    sig = signed.signature.hex()
    return (sig if sig.startswith("0x") else "0x" + sig), nonce


def encode_execute_swap(
    *, sell_token: str, buy_token: str, sell_amount: int, min_buy_amount: int,
    target: str, data: str, nonce: int, deadline: int, signature: str,
) -> str:
    """Calldata for executeSwap, to be sent *to the user's own address* —
    which under EIP-7702 is running the delegate's code."""
    selector = keccak(text=(
        "executeSwap(address,address,uint256,uint256,address,bytes,uint256,uint256,bytes)"
    ))[:4]
    args = abi_encode(
        ["address", "address", "uint256", "uint256", "address", "bytes",
         "uint256", "uint256", "bytes"],
        [sell_token, buy_token, sell_amount, min_buy_amount, target,
         bytes.fromhex(data[2:] if data.startswith("0x") else data),
         nonce, deadline,
         bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)],
    )
    return "0x" + (selector + args).hex()


# -------------------------------------------------------------------- relay

def relayer_address() -> str | None:
    key = settings.relayer_private_key
    if not key:
        return None
    return Account.from_key(key).address


async def relay(*, to: str, data: str, gas_limit: int = 900_000) -> str:
    """Submit a signed executeSwap and return its X Layer tx hash.

    The relayer pays gas and gains nothing: the session signature inside
    `data` is what authorises the swap, so a compromised relayer can submit
    swaps that were already authorised, and nothing else.
    """
    key = settings.relayer_private_key
    if not key:
        raise DelegationError(
            "no relayer configured — set SARF_RELAYER_PRIVATE_KEY to a gas-only "
            "wallet before enabling in-chat execution"
        )
    acct = Account.from_key(key)
    # Checksummed, not just validated. eth_account rejects the lowercase form
    # outright ("Transaction had invalid fields: {'to': ...}") and
    # validate_evm_address normalises TO lowercase — correct for storage and
    # comparison, wrong at the signing boundary. The same fix was applied to
    # relay_authorization and missed here, which is the path execute_order
    # uses, so every in-chat trade failed on its own `to` field.
    to = to_checksum_address(validate_evm_address(to))

    nonce = await rpc.transaction_count(acct.address)
    gas_price = await rpc.gas_price()
    tx = {
        "to": to, "data": data, "value": 0, "gas": gas_limit,
        # X Layer runs at ~0.02 gwei; a 2x ceiling still costs a fraction of a
        # cent and keeps a submission from stalling in a fee spike.
        "maxFeePerGas": gas_price * 2,
        "maxPriorityFeePerGas": gas_price,
        "nonce": nonce, "chainId": CHAIN_ID, "type": 2,
    }
    raw = acct.sign_transaction(tx).raw_transaction
    return await rpc.send_raw_transaction("0x" + raw.hex())


async def relay_authorization(
    *, authorization: dict[str, Any], to: str, data: str, gas_limit: int = 900_000,
) -> str:
    """Broadcast the user's signed EIP-7702 authorization and return the hash.

    Why this exists: Privy's embedded wallet SIGNS a 7702 authorization without
    complaint but will not BROADCAST the type-4 transaction that carries it —
    it fails with an opaque "An unexpected error occurred". X Layer is not the
    obstacle; its RPC parses type-4 fine (a truncated one errors on RLP
    decoding, not on an unknown type). So the signature is obtainable and the
    chain is willing; only the browser's send path is missing, and that is the
    one part a relayer can stand in for.

    This changes nothing about custody. The authorization is signed by the
    user's own key and names the delegate contract; the relayer only pays gas
    and presses send. A compromised relayer could submit an authorization the
    user already signed, and nothing else — it cannot forge one, alter the
    delegate, or raise the caps, all of which live inside the signed payload.

    The signing wallet must therefore NOT be the authority: the authorization is
    built with the relayer as `executor`, so its own nonce advances rather than
    the user's.
    """
    key = settings.relayer_private_key
    if not key:
        raise DelegationError(
            "no relayer configured — set SARF_RELAYER_PRIVATE_KEY to a gas-only "
            "wallet before enabling in-chat execution"
        )
    acct = Account.from_key(key)
    validate_evm_address(to)

    auth = _normalise_authorization(authorization)
    nonce = await rpc.transaction_count(acct.address)
    # eth_account validates addresses as EIP-55 checksummed and rejects the
    # lowercase form outright ("Transaction had invalid fields: {'to': ...}").
    # validate_evm_address normalises to lowercase — right for storage and
    # comparison, wrong for handing to the signer — so checksum on the way in.
    to = to_checksum_address(to)
    gas_price = await rpc.gas_price()
    tx = {
        "to": to, "data": data, "value": 0, "gas": gas_limit,
        "maxFeePerGas": gas_price * 2,
        "maxPriorityFeePerGas": gas_price,
        "nonce": nonce, "chainId": CHAIN_ID, "type": 4,
        "authorizationList": [auth],
    }
    signed = acct.sign_transaction(tx)
    return await rpc.send_raw_transaction("0x" + signed.raw_transaction.hex())


def _normalise_authorization(a: dict[str, Any]) -> dict[str, Any]:
    """Accept what the browser actually sends and reject what it must not.

    Wallet SDKs disagree on shape and casing (chainId/chain_id, address/
    contractAddress, hex strings vs ints), so the fields are read tolerantly
    and then validated strictly — a malformed authorization that reached
    eth_account would produce an unhelpful encoding error rather than a
    statement about what was wrong.
    """
    if not isinstance(a, dict):
        raise DelegationError("authorization must be an object")

    def pick(*names: str) -> Any:
        for n in names:
            if a.get(n) is not None:
                return a[n]
        return None

    def as_int(v: Any, what: str) -> int:
        if isinstance(v, bool) or v is None:
            raise DelegationError(f"authorization {what} is missing")
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v, 16) if v.startswith("0x") else int(v)
            except ValueError:
                raise DelegationError(f"authorization {what} is not a number")
        raise DelegationError(f"authorization {what} is not a number")

    delegate = validate_evm_address(pick("address", "contractAddress"))
    # The delegate is the whole point of the authorization: accepting one that
    # names some other contract would install an arbitrary implementation on the
    # user's account, which is the single worst thing this endpoint could do.
    if not settings.delegate_address or delegate.lower() != settings.delegate_address.lower():
        raise DelegationError(
            "authorization names a different delegate than this server's — refusing to relay"
        )
    chain = as_int(pick("chainId", "chain_id"), "chainId")
    if chain != CHAIN_ID:
        raise DelegationError(f"authorization is for chain {chain}, not X Layer ({CHAIN_ID})")

    return {
        "chainId": chain,
        # Checksummed for the same reason as `to` above: eth_account rejects
        # the lowercase form when it encodes the authorization list.
        "address": to_checksum_address(delegate),
        "nonce": as_int(pick("nonce"), "nonce"),
        "yParity": as_int(pick("yParity", "v"), "yParity") & 1,
        "r": as_int(pick("r"), "r"),
        "s": as_int(pick("s"), "s"),
    }


async def relayer_status() -> dict[str, Any]:
    """Gas-tank health. Surfaced so a drained relayer is visible before it
    starts failing user trades rather than after."""
    addr = relayer_address()
    if not addr:
        return {"configured": False, "note": "in-chat execution is unavailable"}
    bal = await rpc.native_balance(addr)
    okb = bal / 1e18
    gas_price = await rpc.gas_price()
    per_swap = 300_000 * gas_price / 1e18
    return {
        "configured": True,
        "address": addr,
        "okb_balance": round(okb, 6),
        "estimated_swaps_remaining": int(okb / per_swap) if per_swap else None,
        "low": okb < settings.relayer_min_okb,
    }


def grant_calldata(
    *, session_key: str, expiry: int, router: str, stable: str,
    per_trade_cap: int, daily_cap: int, tokens: list[str],
) -> str:
    """Calldata the USER signs with their own wallet to authorise a grant.

    Built here so the site and the assistant cannot disagree about what is
    being authorised, but it is worthless without the user's signature — this
    is the one step Sarf structurally cannot do on their behalf.
    """
    selector = keccak(text=(
        "authorize(address,uint64,address,address,uint128,uint128,address[])"
    ))[:4]
    args = abi_encode(
        ["address", "uint64", "address", "address", "uint128", "uint128", "address[]"],
        [session_key, expiry, router, stable, per_trade_cap, daily_cap, tokens],
    )
    return "0x" + (selector + args).hex()


def revoke_calldata() -> str:
    return "0x" + keccak(text="revoke()")[:4].hex()


def describe(payload: dict[str, Any]) -> str:
    """One-line human summary of a grant, for the consent screen and the card."""
    return json.dumps(payload, sort_keys=True)
