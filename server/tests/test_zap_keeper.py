"""The 7702 keeper path: off by default, refuses to act without a live scope,
and falls back to the signing flow for every "no" rather than failing the
position. No test here touches the network or the live server."""

import asyncio
import dataclasses

import pytest

from sarf.config import settings
from sarf.xlayer import zap_keeper as zk

ACCOUNT = "0xf0139a53d77246ee6cc506753784034a3da74039"
DELEGATE = "0x000000000000000000000000000000000000d00d"
# Anvil's first account; a throwaway key that exists in every Foundry doc.
KEEPER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
KEEPER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _on(**over):
    base = dict(zap_7702_enabled=True, zap_delegate_address=DELEGATE,
                zap_keeper_private_key=KEEPER_KEY)
    base.update(over)
    return dataclasses.replace(settings, **base)


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setattr(zk, "settings", _on())
    return monkeypatch


def _rpc(monkeypatch, table):
    """Stub rpc._call with a {method: value-or-callable} table."""
    calls = []

    async def fake(method, params=None):
        calls.append((method, params))
        v = table.get(method)
        if callable(v):
            return v(params)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(zk.rpc, "_call", fake)
    return calls


def _code(impl: str) -> str:
    return "0xef0100" + impl[2:]


def _scope_ret(keeper: str, expiry: int) -> str:
    return "0x" + keeper[2:].rjust(64, "0") + f"{expiry:064x}"


# ------------------------------------------------------------------- the flag

def test_disabled_by_default_and_touches_nothing(monkeypatch):
    monkeypatch.setattr(zk, "settings", dataclasses.replace(settings, zap_7702_enabled=False))
    calls = _rpc(monkeypatch, {})
    assert zk.enabled() is False
    assert asyncio.run(zk.scope_for(ACCOUNT)) is None
    out = asyncio.run(zk.run_step(ACCOUNT, {"to": "0x1", "data": "0x", "spend": ("0x2", 1, "0x3")}))
    assert out["status"] == "fallback"
    assert calls == [], "nothing was read or sent"


def test_enabled_needs_both_a_delegate_and_a_key(monkeypatch):
    monkeypatch.setattr(zk, "settings", _on(zap_delegate_address=""))
    assert zk.enabled() is False
    monkeypatch.setattr(zk, "settings", _on(zap_keeper_private_key=""))
    assert zk.enabled() is False
    assert zk.keeper_address() is None


# ------------------------------------------------------------------ the scope

def test_scope_is_read_only_for_an_account_delegated_to_our_contract(cfg, monkeypatch):
    import time
    future = int(time.time()) + 3600

    # A plain EOA.
    _rpc(monkeypatch, {"eth_getCode": "0x"})
    assert asyncio.run(zk.scope_for(ACCOUNT)) is None

    # Delegated to somebody else's contract.
    _rpc(monkeypatch, {"eth_getCode": _code("0x00000000000000000000000000000000deadbeef")})
    assert asyncio.run(zk.scope_for(ACCOUNT)) is None

    # Ours, live, naming our keeper.
    _rpc(monkeypatch, {"eth_getCode": _code(DELEGATE),
                       "eth_call": _scope_ret(KEEPER, future)})
    got = asyncio.run(zk.scope_for(ACCOUNT))
    assert got["keeper"].lower() == KEEPER.lower()
    assert got["expiry"] == future


def test_scope_for_another_keeper_or_an_expired_one_is_no_scope(cfg, monkeypatch):
    import time
    _rpc(monkeypatch, {"eth_getCode": _code(DELEGATE),
                       "eth_call": _scope_ret("0x00000000000000000000000000000000000000aa",
                                              int(time.time()) + 3600)})
    assert asyncio.run(zk.scope_for(ACCOUNT)) is None, "someone else's keeper"

    _rpc(monkeypatch, {"eth_getCode": _code(DELEGATE),
                       "eth_call": _scope_ret(KEEPER, int(time.time()) - 1)})
    assert asyncio.run(zk.scope_for(ACCOUNT)) is None, "expired"


def test_an_unreadable_chain_is_no_scope_not_an_error(cfg, monkeypatch):
    _rpc(monkeypatch, {"eth_getCode": _code(DELEGATE), "eth_call": RuntimeError("node down")})
    assert asyncio.run(zk.scope_for(ACCOUNT)) is None


# ----------------------------------------------------------------- run_step

def test_a_step_without_bounds_is_never_run(cfg, monkeypatch):
    calls = _rpc(monkeypatch, {})
    out = asyncio.run(zk.run_step(ACCOUNT, {"kind": "add_liquidity", "to": "0x1", "data": "0x00"}))
    assert out["status"] == "fallback"
    assert "bounds" in out["reason"]
    assert calls == [], "the chain was not even asked"


def test_no_scope_means_fallback_and_no_transaction(cfg, monkeypatch):
    calls = _rpc(monkeypatch, {"eth_getCode": "0x"})
    out = asyncio.run(zk.run_step(ACCOUNT, {
        "kind": "remove_liquidity", "to": "0x" + "11" * 20, "data": "0xabcdef",
        "spend": ("0x" + "22" * 20, 5, "0x" + "11" * 20), "want": ("0x" + "33" * 20, 1)}))
    assert out["status"] == "fallback"
    assert not any(m == "eth_sendRawTransaction" for m, _ in calls)


def test_a_refused_estimate_falls_back_rather_than_sending(cfg, monkeypatch):
    import time
    calls = _rpc(monkeypatch, {
        "eth_getCode": _code(DELEGATE),
        "eth_call": _scope_ret(KEEPER, int(time.time()) + 3600),
        "eth_getTransactionCount": "0x1", "eth_gasPrice": "0x4c4b40",
        "eth_estimateGas": RuntimeError("execution reverted: TargetNotAllowed()"),
    })
    out = asyncio.run(zk.run_step(ACCOUNT, {
        "kind": "remove_liquidity", "to": "0x" + "11" * 20, "data": "0xabcdef",
        "spend": ("0x" + "22" * 20, 5, "0x" + "11" * 20), "want": ("0x" + "33" * 20, 1)}))
    assert out["status"] == "fallback"
    assert "refused" in out["reason"]
    assert not any(m == "eth_sendRawTransaction" for m, _ in calls)


def test_a_sent_step_goes_to_the_account_not_the_target(cfg, monkeypatch):
    import time
    sent = {}
    calls = _rpc(monkeypatch, {
        "eth_getCode": _code(DELEGATE),
        "eth_call": _scope_ret(KEEPER, int(time.time()) + 3600),
        "eth_getTransactionCount": "0x1", "eth_gasPrice": "0x4c4b40",
        "eth_estimateGas": "0x30000",
        "eth_sendRawTransaction": lambda p: sent.setdefault("raw", p[0]) and "0x" + "ab" * 32,
    })
    out = asyncio.run(zk.run_step(ACCOUNT, {
        "kind": "remove_liquidity", "to": "0x" + "11" * 20, "data": "0xabcdef",
        "spend": ("0x" + "22" * 20, 5, "0x" + "11" * 20), "want": ("0x" + "33" * 20, 1)}))
    assert out["status"] == "sent"

    est = dict(calls)["eth_estimateGas"][0]
    assert est["to"].lower() == ACCOUNT, "the keeper calls the user's own account"
    assert est["from"].lower() == KEEPER.lower()
    assert est["data"].startswith(zk._EXECUTE_SEL)


# ------------------------------------------------------------ abi encoding

def test_execute_encoding_round_trips():
    """Decoded with eth_abi, against the signature the contract compiled to."""
    from eth_abi import decode

    target = "0x" + "11" * 20
    token = "0x" + "22" * 20
    spender = "0x" + "33" * 20
    out_token = "0x" + "44" * 20
    data = "0x" + "de" * 100  # not a multiple of 32, so padding matters

    blob = zk._encode_execute(target, data, (token, 12345, spender), (out_token, 678))
    assert blob.startswith(zk._EXECUTE_SEL)
    t, d, legs, wants = decode(
        ["address", "bytes", "(address,uint256,address)[]", "(address,uint256)[]"],
        bytes.fromhex(blob[10:]))
    assert t.lower() == target
    assert d.hex() == data[2:]
    assert legs == ((token, 12345, spender),) or [tuple(x) for x in legs] == [(token, 12345, spender)]
    assert [tuple(x) for x in wants] == [(out_token, 678)]


def test_execute_encoding_handles_no_legs():
    from eth_abi import decode
    blob = zk._encode_execute("0x" + "11" * 20, "0x", None, None)
    t, d, legs, wants = decode(
        ["address", "bytes", "(address,uint256,address)[]", "(address,uint256)[]"],
        bytes.fromhex(blob[10:]))
    assert d == b"" and list(legs) == [] and list(wants) == []
