"""Fiat deposits: dollars from a card to USDC on X Layer, by burn and mint.

THE ROUTE
    1. Fiat -> USDC on Base, by MoonPay through Privy's funding flow. It runs
       its own KYC and pays out to the user's OWN address. Sarf never touches
       the money, never sees a card detail, and is not a party to the payment.
       It cannot be: taking one is money transmission, and the entire design of
       this product is that Sarf holds nothing.

       Card only. An ACH/wire route through Bridge was built and withdrawn —
       Bridge is not enabled on the Privy app, so it produced a payment flow
       with no way to pay. If it is ever enabled, the frontend's ONRAMP_NAME is
       what the user-facing copy reads from.
    2. USDC on Base -> USDC on X Layer, through Circle's CCTP. This module.
    3. USDC -> xStocks, on the trading path that already exists.

WHY CCTP AND NOT A BRIDGE
    The first version of this routed through OKX's cross-chain aggregator into
    USDT, because that was what could be verified working. CCTP is better on
    every axis that matters here, and the numbers are measured, not claimed:

        OKX bridge   $0.43 per $100   ~129s   wrapped-ish USDT
        CCTP fast    $0.013 per $100  seconds native USDC
        CCTP standard  free           ~13-19m native USDC

    CCTP does not move a token across a bridge holding a pot of collateral. It
    BURNS the USDC on Base and MINTS the same amount on X Layer against
    Circle's own attestation — 1:1, no slippage, no bridge liquidity to be
    drained, and what arrives is real Circle USDC rather than someone's
    wrapper. The failure mode of a bridge hack does not exist on this path.

VERIFIED ON CHAIN, NOT ASSUMED (2026-08-16, X Layer RPC)
    - MessageTransmitterV2.localDomain() = 37, so X Layer is a CCTP domain.
    - The V2 contracts are live at the addresses CCTP V2 uses on every EVM
      chain; behind their proxies the implementations carry depositForBurn
      (0x8e0250ee, the 7-argument V2 form) and receiveMessage (0x57ecfd28).
    - USDC at 0xb6ceceab…3061 answers currency() = "USD" with 6 decimals and a
      masterMinter — Circle's own FiatToken, not a wrapper wearing the ticker.
    - Circle's attestation service quotes the 6 -> 37 corridor, so the route
      is supported by the service that has to sign for it, not just by the
      contracts being present.

WHO PAYS AND WHO SIGNS
    The user signs ONE transaction, the burn on Base. The mint on X Layer is
    permissionless — anyone may submit the attested message — so Sarf's
    relayer submits it and pays the gas, and the deposit completes without a
    second prompt. That delegation is safe by construction rather than by
    trust: the recipient is written into the burn message the user signed, so
    the relayer can deliver the funds and can do nothing else with them.

    The burn's own gas is the exception, and it is why send_gas() exists below:
    depositForBurn must be sent by the token holder, so the user is the sender
    and pays Base gas in ETH — which an on-ramp does not deliver. The relayer
    tops that up in the user's own account first. See the block above send_gas
    for the reasoning and the bounds.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak

from ..validation import ValidationError
from .evm import to_checksum_address, validate_evm_address
from .registry import CHAIN_ID

# CCTP V2 uses the same addresses on every supported EVM chain. Both verified
# present on X Layer; Base's are the same by construction.
TOKEN_MESSENGER_V2 = "0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d"
MESSAGE_TRANSMITTER_V2 = "0x81D40F21F12A8F0E3252Bccb954D722d4c464B64"

# Circle's domain ids — NOT chain ids, and the two must never be confused: a
# burn quoting the wrong destination domain mints somewhere else.
DOMAIN_BASE = 6
DOMAIN_XLAYER = 37

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_XLAYER = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
USDC_DECIMALS = 6

BASE_CHAIN_ID = 8453

IRIS = os.environ.get("CIRCLE_IRIS_URL", "https://iris-api.circle.com").rstrip("/")

# Fast transfer buys seconds instead of Base finality (~13-19 minutes) for
# about 1.3 basis points. On a deposit somebody is watching, that is worth it;
# the standard path stays available and free.
FAST_FINALITY_THRESHOLD = 1000
STANDARD_FINALITY_THRESHOLD = 2000

# Bounds on one deposit. The floor is arithmetic rather than policy: below it
# the Base gas to burn is a large fraction of the transfer.
MIN_DEPOSIT_USD = float(os.environ.get("MIN_DEPOSIT_USD", "5"))
MAX_DEPOSIT_USD = float(os.environ.get("MAX_DEPOSIT_USD", "25000"))


class DepositError(RuntimeError):
    """Deposit routing failure. Message is safe to show a user."""


@dataclass(frozen=True)
class Leg:
    """One end of the corridor, for display and for building calldata."""

    chain_id: int
    chain_name: str
    domain: int
    usdc: str
    caip2: str          # what Privy's funding flow wants for a destination


SOURCE = Leg(BASE_CHAIN_ID, "Base", DOMAIN_BASE, USDC_BASE, f"eip155:{BASE_CHAIN_ID}")
DESTINATION = Leg(CHAIN_ID, "X Layer", DOMAIN_XLAYER, USDC_XLAYER, f"eip155:{CHAIN_ID}")


# ------------------------------------------------------------------ amounts

def units(amount_usd: object) -> int:
    """Dollars -> USDC minimal units, with the deposit bounds applied.

    Bounds live here, not at each edge: the REST endpoint and the MCP tool
    must obey the same ones, and a floor enforced in one path is a floor that
    does not exist.
    """
    if isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float, str)):
        raise ValidationError("amount must be a number of US dollars")
    try:
        v = float(amount_usd)
    except (TypeError, ValueError):
        raise ValidationError("amount must be a number of US dollars")
    if v != v or v in (float("inf"), float("-inf")):
        raise ValidationError("amount must be finite")
    if v < MIN_DEPOSIT_USD:
        raise ValidationError(
            f"the smallest deposit is ${MIN_DEPOSIT_USD:,.2f} — below that the "
            "gas to send it is a large fraction of the deposit"
        )
    if v > MAX_DEPOSIT_USD:
        raise ValidationError(f"the largest single deposit is ${MAX_DEPOSIT_USD:,.0f}")
    return int(round(v * 10 ** USDC_DECIMALS))


# ------------------------------------------------------- Circle's attestation

def _iris(path: str, *, timeout: float = 15.0) -> Any:
    # A User-Agent is required, not polite: Circle's edge answers 403 to
    # urllib's default one, which surfaced as "the corridor is unsupported"
    # when the corridor was fine and the header was missing.
    req = urllib.request.Request(
        f"{IRIS}{path}",
        headers={"User-Agent": "sarf/1.0 (+https://sarf.managerx.xyz)",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # network, 4xx, malformed — all the same to a caller
        raise DepositError(f"Circle's attestation service did not answer: {type(e).__name__}")


def fee_bps(fast: bool = True) -> float:
    """Circle's fee for this corridor, in basis points. Quoted, never guessed.

    A hard-coded fee is a fee that is wrong the day Circle changes it, and the
    consequence of quoting one that is too low is a burn that never mints —
    maxFee below the actual fee is rejected on the destination side.
    """
    rows = _iris(f"/v2/burn/USDC/fees/{SOURCE.domain}/{DESTINATION.domain}")
    want = FAST_FINALITY_THRESHOLD if fast else STANDARD_FINALITY_THRESHOLD
    for row in rows if isinstance(rows, list) else []:
        if int(row.get("finalityThreshold", 0)) == want:
            return float(row.get("minimumFee", 0))
    raise DepositError(
        "Circle does not currently quote this transfer speed for the "
        "Base to X Layer corridor"
    )


def quote(amount_usd: object, *, fast: bool = True) -> dict[str, Any]:
    """What a deposit of this size delivers. Read-only."""
    amount = units(amount_usd)
    bps = fee_bps(fast)
    # Round the fee UP into minimal units. Rounding down produces a maxFee a
    # hair under Circle's minimum, which does not fail loudly — it leaves a
    # burn sitting unattested.
    fee = math.ceil(amount * bps / 10_000)
    return {
        "amount_usd": round(amount / 10 ** USDC_DECIMALS, 6),
        "receives_usdc": round((amount - fee) / 10 ** USDC_DECIMALS, 6),
        "fee_usd": round(fee / 10 ** USDC_DECIMALS, 6),
        "fee_bps": bps,
        "speed": "fast" if fast else "standard",
        "estimated_seconds": 20 if fast else 19 * 60,
        "mechanism": "Circle CCTP — burned on Base, minted on X Layer, 1:1",
        # Address and CAIP-2 as well as the names, because the on-ramp needs
        # them: Privy's unified funding flow is told the destination by token
        # ADDRESS, and the deposit page had been carrying its own copy of both.
        # Two copies of a token address is one of them being wrong eventually,
        # and the wrong one here sends real dollars to the wrong contract.
        "from": {"chain": SOURCE.chain_name, "chain_id": SOURCE.chain_id, "token": "USDC",
                 "token_address": SOURCE.usdc, "caip2": SOURCE.caip2},
        "to": {"chain": DESTINATION.chain_name, "chain_id": DESTINATION.chain_id,
               "token": "USDC", "token_address": DESTINATION.usdc,
               "caip2": DESTINATION.caip2},
        "gas_note": (
            "You pay gas on Base to send it. The mint on X Layer is submitted "
            "and paid for by Sarf's relayer, so there is no second prompt."
        ),
    }


# ------------------------------------------------------------------ calldata

def _bytes32(address: str) -> bytes:
    """An address as CCTP wants it: left-padded into 32 bytes."""
    return bytes(12) + bytes.fromhex(validate_evm_address(address)[2:])


def approve_calldata(spender: str, amount: int) -> str:
    return ("0x" + "095ea7b3"
            + validate_evm_address(spender)[2:].rjust(64, "0")
            + f"{amount:064x}")


def burn_calldata(*, amount: int, recipient: str, max_fee: int, fast: bool = True) -> str:
    """depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32).

    `destinationCaller` is left as zero on purpose: it would restrict who may
    submit the mint, and the whole point of the relayer finishing the job is
    that anybody can. The recipient is NOT open in the same way — it is bound
    here, to the user's own address, which is what makes relaying the mint
    safe to delegate.
    """
    selector = keccak(text=(
        "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"
    ))[:4]
    args = abi_encode(
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        [amount, DESTINATION.domain, _bytes32(recipient),
         to_checksum_address(SOURCE.usdc), bytes(32), max_fee,
         FAST_FINALITY_THRESHOLD if fast else STANDARD_FINALITY_THRESHOLD],
    )
    return "0x" + (selector + args).hex()


def prepare(amount_usd: object, wallet: str, *, fast: bool = True) -> dict[str, Any]:
    """The unsigned transactions the user signs on Base: approve, then burn.

    `wallet` is the session's verified address and is used for both ends — it
    is the sender on Base and, written into the message, the recipient on
    X Layer. It is never taken from a caller.
    """
    addr = validate_evm_address(wallet, what="wallet")
    amount = units(amount_usd)
    q = quote(amount_usd, fast=fast)
    max_fee = int(round(q["fee_usd"] * 10 ** USDC_DECIMALS))
    return {
        **q,
        "wallet": addr,
        "chain_id": SOURCE.chain_id,
        "approval": {
            "to": SOURCE.usdc,
            "data": approve_calldata(TOKEN_MESSENGER_V2, amount),
            "value": "0",
            "chainId": SOURCE.chain_id,
            "spender": TOKEN_MESSENGER_V2,
            "amount": str(amount),
            "note": "Only needed when the existing allowance is short.",
        },
        "transaction": {
            "to": TOKEN_MESSENGER_V2,
            "data": burn_calldata(amount=amount, recipient=addr,
                                  max_fee=max_fee, fast=fast),
            "value": "0",
            "chainId": SOURCE.chain_id,
        },
        "next_step": (
            "Sign the burn on Base. Sarf watches for Circle's attestation and "
            "submits the mint on X Layer — the USDC arrives at the same address."
        ),
    }


# --------------------------------------------------------------- completion

def attestation(burn_tx_hash: str) -> dict[str, Any]:
    """Circle's signed message for a burn, once it exists.

    `pending` is a normal state, not a failure: fast transfers take seconds
    and standard ones wait for Base finality.
    """
    body = _iris(f"/v2/messages/{SOURCE.domain}?transactionHash={burn_tx_hash}")
    messages = (body or {}).get("messages") or []
    if not messages:
        return {"ready": False, "state": "unknown",
                "detail": "Circle has not seen this transaction yet"}
    m = messages[0]
    status = str(m.get("status") or "").lower()
    if status != "complete" or not m.get("attestation") or m["attestation"] == "PENDING":
        return {"ready": False, "state": status or "pending",
                "detail": "waiting for Circle to attest the burn"}
    return {
        "ready": True,
        "state": "complete",
        "message": m["message"],
        "attestation": m["attestation"],
        "event_nonce": m.get("eventNonce"),
    }


BASE_RPC = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")


def _base_call(method: str, params: list[Any], *, what: str) -> Any:
    """One JSON-RPC call to Base. Blocking; call it off the event loop."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        BASE_RPC, json.dumps(body).encode(),
        {"content-type": "application/json", "User-Agent": "sarf/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.load(r)
    except Exception as e:
        raise DepositError(f"could not {what}: {type(e).__name__}")
    if isinstance(payload, dict) and payload.get("error"):
        raise DepositError(
            f"could not {what}: {payload['error'].get('message', 'RPC error')}")
    return (payload or {}).get("result")


def _base_uint(method: str, params: list[Any], *, what: str) -> int:
    res = _base_call(method, params, what=what)
    return int(res, 16) if res and res != "0x" else 0


def allowance(owner: str) -> int:
    """USDC allowance granted to the TokenMessenger on Base, in minimal units.

    Read so the site only asks for an approval when there is not one. An
    unconditional approve is a second wallet prompt on every deposit after the
    first, which reads as the app not remembering what you already did.
    """
    data = ("0xdd62ed3e"
            + validate_evm_address(owner)[2:].rjust(64, "0")
            + validate_evm_address(TOKEN_MESSENGER_V2)[2:].rjust(64, "0"))
    return _base_uint(
        "eth_call", [{"to": SOURCE.usdc, "data": data}, "latest"],
        what="read the Base allowance")


def usdc_balance(owner: str) -> int:
    """USDC held on Base, in minimal units."""
    data = "0x70a08231" + validate_evm_address(owner)[2:].rjust(64, "0")
    return _base_uint(
        "eth_call", [{"to": SOURCE.usdc, "data": data}, "latest"],
        what="read the Base USDC balance")


def eth_balance(owner: str) -> int:
    """Native ETH on Base, in wei. This is what pays for the burn."""
    return _base_uint(
        "eth_getBalance", [validate_evm_address(owner), "latest"],
        what="read the Base gas balance")


# ---------------------------------------------------------------- gas on Base
#
# THE PROBLEM THIS SOLVES
#     Everything about the deposit route is designed so the user never has to
#     hold a second asset — and then the burn on Base needs ETH to pay for
#     itself. An on-ramp delivers USDC and nothing else, so a wallet that has
#     just been funded through the bank flow holds exactly the thing it is
#     trying to move and none of the thing that moves it. The wallet answers
#     "insufficient funds for gas", which reads as the deposit being broken;
#     the deposit is fine, the account is simply a cent short of sending it.
#
#     There is no way around it on this leg. depositForBurn has to be sent by
#     the token holder, so the user is the sender, so the user pays Base gas.
#
# THE FIX
#     Sarf's relayer — the same gas-only wallet that already pays for the mint
#     on X Layer — sends the shortfall to the user's own address on Base before
#     the burn. A cent of ETH, unconditionally theirs, spent by them on their
#     own transaction. It changes no custody: the relayer gives value away and
#     gains nothing, which is the opposite direction from every risk in this
#     product.
#
# THE BOUNDS
#     Free money attracts scripts, so it is fenced: only to the session's own
#     verified address, only when that address is already holding enough USDC
#     on Base to be making a real deposit, only up to the shortfall, never more
#     than DRIP_MAX_WEI at once, and no more than DRIP_DAILY_MAX_WEI per
#     address per day. The worst case is somebody farming fractions of a cent
#     while holding the minimum deposit in USDC, which costs them more in their
#     own attention than it costs us.

# Measured shapes, with room: ERC-20 approve lands around 55k and V2's
# depositForBurn around 160k. Deliberately generous — under-funding is the one
# failure this exists to prevent, and the difference is a fraction of a cent.
GAS_APPROVE = 70_000
GAS_BURN = 220_000
# Multiplier on the current gas price, so a top-up sent now still covers a
# transaction the user sends a minute later into a busier block.
GAS_PRICE_HEADROOM = 3.0

DRIP_MAX_WEI = int(os.environ.get("BASE_GAS_DRIP_MAX_WEI", str(10 ** 14)))        # 0.0001 ETH
DRIP_DAILY_MAX_WEI = int(os.environ.get("BASE_GAS_DRIP_DAILY_WEI", str(3 * 10 ** 14)))


def gas_price() -> int:
    return _base_uint("eth_gasPrice", [], what="read the Base gas price")


def gas_required(*, needs_approval: bool) -> int:
    """Wei the user needs on Base to send this deposit themselves."""
    units_ = GAS_BURN + (GAS_APPROVE if needs_approval else 0)
    return int(gas_price() * GAS_PRICE_HEADROOM * units_)


def gas_shortfall(owner: str, *, needs_approval: bool) -> dict[str, int]:
    """-> {balance, required, short}. All wei, all read live."""
    have = eth_balance(owner)
    need = gas_required(needs_approval=needs_approval)
    return {"balance": have, "required": need, "short": max(0, need - have)}


def send_gas(to: str, wei: int) -> str:
    """Relayer -> user, plain ETH on Base. Returns the tx hash.

    Kept here rather than in delegation.py because that module is pinned to
    X Layer at every layer — its RPC, its chain id, its gas assumptions. A
    Base transfer that borrowed those would be signed for the wrong chain.
    """
    from eth_account import Account  # local: only this path signs on Base

    from ..config import settings

    key = settings.relayer_private_key
    if not key:
        raise DepositError(
            "no relayer configured, so Sarf cannot cover the gas for this burn"
        )
    if wei <= 0:
        raise DepositError("nothing to send")
    if wei > DRIP_MAX_WEI:
        raise DepositError("gas top-up above the per-transfer ceiling")

    acct = Account.from_key(key)
    have = eth_balance(acct.address)
    gp = gas_price()
    # 21000 for the transfer itself, on top of what is being given away.
    if have < wei + 21_000 * gp * 2:
        raise DepositError(
            "Sarf's relayer has no ETH on Base to cover this burn's gas. Send a "
            "little ETH to your own address on Base and press Move again — the "
            "deposit itself is unaffected."
        )
    nonce = _base_uint(
        "eth_getTransactionCount", [acct.address, "pending"],
        what="read the relayer's Base nonce")
    tx = {
        "to": to_checksum_address(validate_evm_address(to)),
        "value": int(wei),
        "gas": 21_000,
        "maxFeePerGas": gp * 3,
        "maxPriorityFeePerGas": max(1, gp // 2),
        "nonce": nonce,
        "chainId": BASE_CHAIN_ID,
        "type": 2,
    }
    raw = acct.sign_transaction(tx).raw_transaction
    res = _base_call("eth_sendRawTransaction", ["0x" + raw.hex()],
                     what="submit the gas top-up on Base")
    if not isinstance(res, str) or not res.startswith("0x"):
        raise DepositError("Base did not return a transaction hash for the gas top-up")
    return res


def await_mined(tx_hash: str, *, timeout: float = 25.0) -> bool:
    """Block until a Base transaction is in a block. -> whether it landed.

    The top-up is only useful once it has actually arrived: a broadcast hash
    does not spend, and the wallet estimates gas for the burn against the
    balance it can see. Returning the instant the transfer was submitted would
    hand the user the same "insufficient funds" error this exists to remove,
    one second later. Base blocks are two seconds, so this is a short wait.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            r = _base_call("eth_getTransactionReceipt", [tx_hash],
                           what="check the gas top-up")
        except DepositError:
            r = None
        if isinstance(r, dict) and r.get("blockNumber"):
            # status 0x1 is success; a reverted plain transfer would be odd,
            # but reporting it as landed would be a lie.
            return str(r.get("status", "0x1")).lower() in ("0x1", "1")
        _time.sleep(1.5)
    return False


# CCTP V2 message layout, from Circle's spec. The header is fixed-width and
# the burn body follows it, so the recipient sits at a known offset.
#
#   header  version u32 | sourceDomain u32 | destinationDomain u32 | nonce b32
#           | sender b32 | recipient b32 | destinationCaller b32
#           | minFinalityThreshold u32 | finalityThresholdExecuted u32   = 148
#   body    version u32 | burnToken b32 | mintRecipient b32 | ...
#
_HEADER_BYTES = 148
_MINT_RECIPIENT_OFFSET = _HEADER_BYTES + 4 + 32     # 184
_MIN_MESSAGE_BYTES = _MINT_RECIPIENT_OFFSET + 32


def mint_recipient(message: str) -> str:
    """Who a burn pays out to, read from the message itself.

    Needed because /deposit/complete accepts a transaction hash, and a hash is
    public: without this, anyone with a session could hand us a stranger's
    burn from a block explorer and have our relayer pay the gas to finish it.
    Harmless to the stranger — the mint can only go where they said — but it
    is our gas, in unbounded volume. Binding the message to the session's own
    address closes it.
    """
    raw = bytes.fromhex(message[2:] if message.startswith("0x") else message)
    if len(raw) < _MIN_MESSAGE_BYTES:
        raise DepositError("this does not look like a CCTP burn message")
    word = raw[_MINT_RECIPIENT_OFFSET:_MINT_RECIPIENT_OFFSET + 32]
    if word[:12] != bytes(12):
        # A bytes32 recipient with a non-zero prefix is not an EVM address.
        raise DepositError("the burn pays out to a non-EVM recipient")
    return "0x" + word[12:].hex()


def receive_calldata(message: str, att: str) -> str:
    """receiveMessage(bytes,bytes) on X Layer — this is the mint."""
    selector = keccak(text="receiveMessage(bytes,bytes)")[:4]
    args = abi_encode(
        ["bytes", "bytes"],
        [bytes.fromhex(message[2:] if message.startswith("0x") else message),
         bytes.fromhex(att[2:] if att.startswith("0x") else att)],
    )
    return "0x" + (selector + args).hex()


class NotYours(DepositError):
    """The burn pays out to someone else, so it is not this account's to finish."""


async def settle(burn_tx: str, *, expect_address: str | None = None) -> dict[str, Any]:
    """Attest and mint, or say why not yet. -> {"settled": bool, ...}

    One implementation for two callers: the browser, which polls this while
    someone watches, and the server's own sweeper, which finishes deposits for
    tabs that were closed. They must not be able to disagree about what
    completing a deposit means, so they run the same code.

    Idempotent where it counts — CCTP records spent nonces, so re-submitting a
    message that already minted reverts on chain instead of minting twice.
    """
    if expect_address is not None:
        expect_address = validate_evm_address(expect_address)
    # urllib and the relay are blocking; this runs on the event loop.
    att = await asyncio.to_thread(attestation, burn_tx)
    if not att.get("ready"):
        return {"settled": False, "state": att.get("state"),
                "detail": att.get("detail")}
    # A transaction hash is public. Only finish a burn that pays out to the
    # address we expect — otherwise a caller could feed us strangers' burns off
    # a block explorer and spend our relayer's gas completing them.
    payee = mint_recipient(att["message"])
    if expect_address and payee.lower() != expect_address.lower():
        raise NotYours(
            "that deposit pays out to a different address, so it is not this "
            "account's to complete"
        )
    from . import delegation  # local: delegation imports the registry this feeds

    mint_tx = await delegation.relay(
        to=MESSAGE_TRANSMITTER_V2,
        data=receive_calldata(att["message"], att["attestation"]),
        gas_limit=400_000,
    )
    return {"settled": True, "burn_tx": burn_tx, "mint_tx": mint_tx,
            "recipient": payee}
