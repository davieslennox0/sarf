"""Fiat deposits: dollars in a bank account to USDC on X Layer, by burn and mint.

THE ROUTE
    1. Fiat -> USDC on Base. A licensed provider does this (Stripe, Coinbase
       Onramp or MoonPay, surfaced by Privy's funding flow), with its own KYC,
       paying out to the user's OWN address. Sarf never touches the money,
       never sees a bank detail, and is not a party to the transfer. It cannot
       be: taking a bank deposit is money transmission, and the entire design
       of this product is that Sarf holds nothing.
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
"""

from __future__ import annotations

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
        "from": {"chain": SOURCE.chain_name, "chain_id": SOURCE.chain_id, "token": "USDC"},
        "to": {"chain": DESTINATION.chain_name, "chain_id": DESTINATION.chain_id, "token": "USDC"},
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


def allowance(owner: str) -> int:
    """USDC allowance granted to the TokenMessenger on Base, in minimal units.

    Read so the site only asks for an approval when there is not one. An
    unconditional approve is a second wallet prompt on every deposit after the
    first, which reads as the app not remembering what you already did.
    """
    data = ("0xdd62ed3e"
            + validate_evm_address(owner)[2:].rjust(64, "0")
            + validate_evm_address(TOKEN_MESSENGER_V2)[2:].rjust(64, "0"))
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": SOURCE.usdc, "data": data}, "latest"]}
    req = urllib.request.Request(
        BASE_RPC, json.dumps(body).encode(),
        {"content-type": "application/json", "User-Agent": "sarf/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.load(r).get("result")
    except Exception as e:
        raise DepositError(f"could not read the Base allowance: {type(e).__name__}")
    return int(res, 16) if res and res != "0x" else 0


def receive_calldata(message: str, att: str) -> str:
    """receiveMessage(bytes,bytes) on X Layer — this is the mint."""
    selector = keccak(text="receiveMessage(bytes,bytes)")[:4]
    args = abi_encode(
        ["bytes", "bytes"],
        [bytes.fromhex(message[2:] if message.startswith("0x") else message),
         bytes.fromhex(att[2:] if att.startswith("0x") else att)],
    )
    return "0x" + (selector + args).hex()
