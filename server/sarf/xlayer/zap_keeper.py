"""The EIP-7702 keeper path for the zap: running an exit or a re-entry without
the user signing each step.

HOW IT RELATES TO THE SIGNING FLOW
    Nothing here replaces zap.py's flow. The engine still builds every step
    the same way; this module only changes who sends it. When the user's
    account is delegated to SarfDelegate with a live scope naming our keeper,
    the keeper wraps the step in `execute(...)` and pays the gas. When it is
    not — no delegation, no scope, an expired one, a step whose bounds are not
    known, or the feature switched off — the step goes back to the queue and
    the user signs it, exactly as before. Falling back is the normal case, not
    an error path.

WHAT THE KEEPER CAN AND CANNOT DO
    It can call `execute` on an account that named it. That is all. The
    delegate requires every output to land in the user's own account, allows
    only targets the user whitelisted, and counts every spend against the
    user's own per-token caps. A stolen keeper key cannot move funds out; it
    can waste fees up to the daily caps until the user revokes. See
    contracts/src/SarfDelegate.sol.

    The keeper key is an ordinary EOA key held by this server. It holds no
    user funds and needs only gas.

OFF BY DEFAULT
    settings.zap_7702_enabled is false, and the contract is unaudited. With it
    off, nothing in this module runs and `scope_for` is never called.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address

from ..config import settings
from . import rpc
from .evm import validate_evm_address

log = logging.getLogger("sarf.zap.keeper")

# SarfDelegate selectors and the 7702 delegation indicator.
_SCOPE_SEL = "0x6e62d0a8"       # scope()
_EXECUTE_SEL = "0xb31f1a4b"     # execute(address,bytes,(address,uint256,address)[],(address,uint256)[])
DELEGATION_PREFIX = "0xef0100"


def enabled() -> bool:
    return bool(settings.zap_7702_enabled and settings.zap_delegate_address
                and settings.zap_keeper_private_key)


def keeper_address() -> str | None:
    if not settings.zap_keeper_private_key:
        return None
    return Account.from_key(settings.zap_keeper_private_key).address


async def delegated_to(address: str) -> str | None:
    """The implementation this EOA delegates to, or None if it is a plain EOA.

    Under EIP-7702 the account's code is exactly 23 bytes: 0xef0100 followed
    by the implementation address.
    """
    code = await rpc._call("eth_getCode", [address.lower(), "latest"])
    if not code or not code.startswith(DELEGATION_PREFIX) or len(code) != 2 + 46:
        return None
    return "0x" + code[8:48]


async def scope_for(address: str) -> dict[str, Any] | None:
    """This account's live scope for OUR keeper, or None.

    None is the answer for every "no" — not delegated, delegated to something
    that is not our contract, no scope, someone else's keeper, expired — and
    every one of them means the same thing to the caller: the user signs.
    """
    if not enabled():
        return None
    ours = settings.zap_delegate_address.lower()
    impl = await delegated_to(address)
    if impl != ours:
        return None
    try:
        raw = await rpc._call("eth_call", [{"to": address, "data": _SCOPE_SEL}, "latest"])
    except Exception as e:
        log.warning("scope read failed for %s: %s", address, e)
        return None
    if not raw or len(raw) < 2 + 128:
        return None
    keeper = "0x" + raw[2 + 24:2 + 64]
    expiry = int(raw[2 + 64:2 + 128], 16)
    mine = keeper_address()
    if not mine or keeper.lower() != mine.lower():
        return None
    if expiry <= time.time():
        return None
    return {"keeper": keeper, "expiry": expiry, "delegate": impl}


def _encode_execute(target: str, data: str, spend: tuple[str, int, str] | None,
                    want: tuple[str, int] | None) -> str:
    """ABI-encode execute(address,bytes,Leg[],Want[]).

    Hand-encoded rather than pulled through a contract object: the server has
    no web3 contract layer, and this is one static signature.
    """
    body = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    legs = [] if spend is None else [spend]
    wants = [] if want is None else [want]

    head_words = 4  # target, data offset, legs offset, wants offset
    parts: list[bytes] = []

    def word(v: int) -> bytes:
        return v.to_bytes(32, "big")

    def addr(a: str) -> bytes:
        return bytes.fromhex(a[2:].rjust(40, "0")).rjust(32, b"\x00")

    # bytes: offset then length + padded content
    data_blob = word(len(body)) + body + b"\x00" * ((32 - len(body) % 32) % 32)
    # Leg[] is a dynamic array of a static 3-word tuple; Want[] of a 2-word one.
    legs_blob = word(len(legs)) + b"".join(
        addr(t) + word(amount) + addr(sp) for t, amount, sp in legs)
    wants_blob = word(len(wants)) + b"".join(addr(t) + word(m) for t, m in wants)

    off_data = head_words * 32
    off_legs = off_data + len(data_blob)
    off_wants = off_legs + len(legs_blob)
    head = addr(target) + word(off_data) + word(off_legs) + word(off_wants)
    parts = [head, data_blob, legs_blob, wants_blob]
    return _EXECUTE_SEL + b"".join(parts).hex()


async def run_step(account: str, step: dict[str, Any]) -> dict[str, Any]:
    """Send one built step through the account's delegate, as the keeper.

    `step` is a zap engine step dict: {to, data, spend, want}. Returns
    {"status": "sent", "tx_hash": ...} or {"status": "fallback", "reason": ...}
    — never raises, because a keeper that cannot run a step must leave the
    position exactly where the signing flow expects to find it.
    """
    if not enabled():
        return {"status": "fallback", "reason": "7702 keeper disabled"}
    if not step.get("spend") and not step.get("want"):
        # Nothing to bound: the delegate would be asked to run an unconstrained
        # call. It would refuse an unknown target anyway, but a step whose
        # bounds we do not know is one the user should be signing.
        return {"status": "fallback", "reason": f"step {step.get('kind')} has no declared bounds"}
    scope = await scope_for(account)
    if not scope:
        return {"status": "fallback", "reason": "no live scope for this account"}

    acct = Account.from_key(settings.zap_keeper_private_key)
    data = _encode_execute(validate_evm_address(step["to"]), step["data"],
                           step.get("spend"), step.get("want"))
    try:
        nonce = int(await rpc._call("eth_getTransactionCount", [acct.address, "pending"]), 16)
        gas_price = int(await rpc._call("eth_gasPrice", []), 16)
        try:
            gas = int(await rpc._call("eth_estimateGas", [
                {"from": acct.address, "to": account, "data": data}]), 16)
            gas = int(gas * 1.6)
        except Exception as e:
            # The delegate reverts on anything it will not allow, so a failed
            # estimate is usually the contract saying no — which is a correct
            # refusal, not an outage. Either way the user signs instead.
            return {"status": "fallback", "reason": f"the delegate refused this step: {e}"}
        signed = acct.sign_transaction({
            # Checksummed: eth-account rejects a lowercase `to` outright, and
            # validate_evm_address deliberately hands back the lowercase form
            # the rest of the server compares against.
            "chainId": 196, "to": to_checksum_address(validate_evm_address(account)), "data": data,
            "value": 0, "nonce": nonce, "gas": gas, "gasPrice": gas_price,
        })
        tx_hash = await rpc._call("eth_sendRawTransaction",
                                  ["0x" + signed.raw_transaction.hex().removeprefix("0x")])
    except Exception as e:
        log.warning("keeper send failed for %s: %s", account, e)
        return {"status": "fallback", "reason": f"keeper could not send: {e}"}
    log.info("keeper ran %s for %s: %s", step.get("kind"), account, tx_hash)
    return {"status": "sent", "tx_hash": tx_hash}
