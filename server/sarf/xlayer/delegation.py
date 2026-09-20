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

import asyncio
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
CONTRACT_MAX_GRANT_SECONDS = 30 * 24 * 3600

# What this deployment will actually issue, which is far shorter than what the
# contract permits. A session key is a key that trades without asking, so its
# lifetime is bounded by policy well under the contract's 30 days.
#
# It used to be a flat hour, on the reasoning that the key should die with the
# passkey assertion that bought it. That reasoning was half right: the passkey
# proves who is asking, and it should be required to START a grant — but making
# every user re-prove themselves hourly to keep a $50-per-trade cap alive is a
# tax on the honest case, and the caps, not the clock, are what bound the
# damage. So the user picks, up to a week, and the on-chain limits they signed
# are unchanged whichever they pick.
#
# NOTE FOR ANYONE RAISING THIS: nothing about the deployed contract changes.
# `expiry` is a parameter of the grant the user signs, and SarfSessionKey
# already accepts anything up to MAX_GRANT = 30 days. No redeploy, no migration
# — this constant only decides what Sarf is willing to ask for.
MAX_GRANT_SECONDS = min(
    int(os.environ.get("SARF_MAX_GRANT_SECONDS", "").strip() or str(7 * 24 * 3600)),
    CONTRACT_MAX_GRANT_SECONDS,
)

# The lifetimes offered in the UI. A short list beats a free-form number: these
# are the four spans people actually mean ("today", "the weekend", "a few
# days", "the week"), and every one is inside the ceiling above. Arbitrary
# values in between are still accepted — this is what is OFFERED, not what is
# permitted.
#
# These are long for a key that trades without asking, and that is a deliberate
# trade the account owner makes rather than one Sarf makes for them. The point
# of a session key is that the assistant works while nobody is at the website;
# a lifetime measured in hours meant coming back to re-authorise all day, which
# pushed people toward larger caps to make each authorisation "worth it" — the
# expensive knob, tightened, to relieve pressure on the cheap one. What bounds
# the loss is the per-trade and per-day cap enforced by the contract, and those
# do not stretch with the clock: a week-long grant at $50 a trade is still $50
# a trade. Revocation stays one click, takes effect on chain, and does not need
# Sarf's cooperation.
GRANT_CHOICES_SECONDS = tuple(
    s for s in (24 * 3600, 48 * 3600, 96 * 3600, 7 * 24 * 3600)
    if s <= MAX_GRANT_SECONDS
)


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
    and reads as breakage, and MAX_GRANT_SECONDS is the ceiling this deployment
    will issue. The contract would accept far more; this is the narrower of the
    two, and the narrower of two limits is the one that matters.
    """
    if not isinstance(days, (int, float)) or days != days:
        raise ValidationError("days must be a number")
    seconds = int(float(days) * 24 * 3600)
    if seconds < 3600:
        raise ValidationError("the shortest grant is 1 hour")
    if seconds > MAX_GRANT_SECONDS:
        raise ValidationError(
            f"the longest grant is {_humanise(MAX_GRANT_SECONDS)} — this ceiling "
            "is Sarf's policy, not a contract limit, and it exists because a key "
            "that trades without asking should have a short life"
        )
    return int(time.time()) + seconds


def _humanise(seconds: int) -> str:
    if seconds % 86400 == 0:
        d = seconds // 86400
        return f"{d} day" + ("s" if d != 1 else "")
    h = seconds / 3600
    return f"{h:g} hour" + ("s" if h != 1 else "")


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


# The relayer has ONE nonce, and three code paths spend it: an in-chat swap,
# a 7702 authorization, and a receipt anchor. Each reads the account's current
# count and then signs with it, so two that overlap read the same number and
# the second transaction is rejected as a duplicate. With a single tester that
# never happens; with real traffic it is only a matter of two people trading
# in the same second. Reading the nonce, signing and broadcasting therefore
# happen as one critical section rather than three racing ones.
NONCE_LOCK = asyncio.Lock()


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

    async with NONCE_LOCK:
        nonce = await rpc.transaction_count(acct.address)
        gas_price = await rpc.gas_price()
        tx = {
            "to": to, "data": data, "value": 0, "gas": gas_limit,
            # X Layer runs at ~0.02 gwei; a 2x ceiling still costs a fraction
            # of a cent and keeps a submission from stalling in a fee spike.
            "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": gas_price,
            "nonce": nonce, "chainId": CHAIN_ID, "type": 2,
        }
        raw = acct.sign_transaction(tx).raw_transaction
        return await rpc.send_raw_transaction("0x" + raw.hex())


# ------------------------------------------------- gas for the self-call
#
# WHY A DRIP IS NEEDED ON X LAYER TOO
#     Installing the delegate and authorising the grant cannot be one relayed
#     transaction: SarfSessionKey.authorize() is self-only (`msg.sender !=
#     address(this) -> NotSelf`), so the relayer can carry the 7702
#     authorization but not the call that has to follow it. That second half is
#     an ordinary self-call sent by the user's OWN wallet, and a Privy embedded
#     wallet that has only ever received tokens holds no OKB — so it fails on
#     "insufficient funds for transfer" before it reaches the contract at all.
#     The delegate ends up installed and the grant never recorded, which reads
#     to the user as the whole setup having failed.
#
#     The fix rides along with the relay the server is already paying for. That
#     transaction targets the user's account anyway, so giving it a `value`
#     installs the delegate and funds the follow-up in a single send.
#
# THE BOUNDS
#     The same fence as the Base drip in deposit.py, for the same reason: only
#     to the session's own verified address, only when that address is
#     genuinely short, only up to the shortfall, never more than
#     GRANT_DRIP_MAX_WEI at once, and no more than GRANT_DRIP_DAILY_MAX_WEI per
#     address per day. The caller is already behind a fresh passkey assertion
#     and a prepared grant row — a narrower gate than the Base drip's "hold
#     enough USDC to be making a real deposit".

# Measured against the live delegate on X Layer: grant() with a 40-token
# allowlist estimates at ~1.1M gas. Rounded up, because under-funding is the
# one failure this exists to prevent and the difference is a fraction of a cent.
GAS_GRANT = 1_300_000
# Multiplier on the current gas price, so a top-up sent now still covers a call
# the user signs a minute later into a busier block.
GRANT_GAS_PRICE_HEADROOM = 3.0

GRANT_DRIP_MAX_WEI = int(os.environ.get("XLAYER_GAS_DRIP_MAX_WEI", str(10 ** 14)))
GRANT_DRIP_DAILY_MAX_WEI = int(
    os.environ.get("XLAYER_GAS_DRIP_DAILY_WEI", str(3 * 10 ** 14))
)


async def grant_gas_shortfall(owner: str) -> dict[str, int]:
    """-> {balance, required, short}. All wei on X Layer, all read live."""
    have = await rpc.native_balance(owner)
    price = await rpc.gas_price()
    need = int(price * GRANT_GAS_PRICE_HEADROOM * GAS_GRANT)
    return {"balance": have, "required": need, "short": max(0, need - have)}


async def affordable_drip(wei: int) -> int:
    """Clamp a top-up to what the relayer can send and still pay its own gas.

    A thin gas tank degrades to "the delegate installed, the top-up was
    skipped" rather than to "nothing worked". The install is what the user is
    waiting on; the drip only saves them from having to fund the wallet
    themselves.
    """
    if wei <= 0:
        return 0
    addr = relayer_address()
    if not addr:
        return 0
    have = await rpc.native_balance(addr)
    price = await rpc.gas_price()
    # Reserve this transaction's own worst case before giving anything away.
    reserve = 900_000 * price * 2
    return max(0, min(wei, have - reserve))


async def await_relay_mined(tx_hash: str, *, timeout: float = 25.0) -> bool:
    """Block until a relayed transaction is in a block. -> whether it landed.

    The wait is not optional, for the same reason deposit.await_mined exists: a
    broadcast hash has not moved anything yet, and the browser prices its
    self-call against the balance it can SEE. Returning the instant the relay
    was submitted would hand the user back the very "insufficient funds" this
    drip removes — and would race the delegate install besides, since the
    self-call is only valid once the account has the delegate's code.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = await rpc.tx_status(tx_hash)
        except Exception:  # pragma: no cover - transient RPC failure
            status = None
        if status is not None and status.mined:
            # A reverted install would be odd, but reporting it as landed
            # would be a lie the next step cannot recover from.
            return status.success is not False
        await asyncio.sleep(1.0)
    return False


async def relay_authorization(
    *, authorization: dict[str, Any], to: str, data: str, gas_limit: int = 900_000,
    value: int = 0,
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

    `value` is the gas drip described above — OKB handed to the account so it
    can send the self-call that authorize() requires. The ceiling is enforced
    here and not only by the caller that computes the amount, because free
    money attracts scripts and this is the function that actually spends.
    """
    key = settings.relayer_private_key
    if not key:
        raise DelegationError(
            "no relayer configured — set SARF_RELAYER_PRIVATE_KEY to a gas-only "
            "wallet before enabling in-chat execution"
        )
    value = int(value)
    if value < 0:
        raise DelegationError("a relayed gas top-up cannot be negative")
    if value > GRANT_DRIP_MAX_WEI:
        raise DelegationError("gas top-up above the per-transfer ceiling")
    acct = Account.from_key(key)
    validate_evm_address(to)

    auth = _normalise_authorization(authorization)
    # eth_account validates addresses as EIP-55 checksummed and rejects the
    # lowercase form outright ("Transaction had invalid fields: {'to': ...}").
    # validate_evm_address normalises to lowercase — right for storage and
    # comparison, wrong for handing to the signer — so checksum on the way in.
    to = to_checksum_address(to)
    async with NONCE_LOCK:
        nonce = await rpc.transaction_count(acct.address)
        gas_price = await rpc.gas_price()
        tx = {
            "to": to, "data": data, "value": value, "gas": gas_limit,
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
    *, session_key: str, expiry: int, router: str, spender: str, stable: str,
    per_trade_cap: int, daily_cap: int, tokens: list[str],
) -> str:
    """Calldata the USER signs with their own wallet to authorise a grant.

    Built here so the site and the assistant cannot disagree about what is
    being authorised, but it is worthless without the user's signature — this
    is the one step Sarf structurally cannot do on their behalf.

    `router` and `spender` are two different contracts and both are pinned
    here. The router is what the swap calls; the spender is what the sell-side
    allowance is granted to, which for OKX is its TokenApprove rather than the
    router itself. Conflating them is not a mis-configuration that degrades
    gracefully — it reverts every trade on-chain, after the gas is spent.
    """
    selector = keccak(text=(
        "authorize(address,uint64,address,address,address,uint128,uint128,address[])"
    ))[:4]
    args = abi_encode(
        ["address", "uint64", "address", "address", "address",
         "uint128", "uint128", "address[]"],
        [session_key, expiry, router, spender, stable,
         per_trade_cap, daily_cap, tokens],
    )
    return "0x" + (selector + args).hex()


def revoke_calldata() -> str:
    return "0x" + keccak(text="revoke()")[:4].hex()


def describe(payload: dict[str, Any]) -> str:
    """One-line human summary of a grant, for the consent screen and the card."""
    return json.dumps(payload, sort_keys=True)
