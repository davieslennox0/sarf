"""X Layer RWA surface: validation, registry, passkey policy, order guards.

Offline by design — the DEX aggregator and X Layer RPC are faked, so the whole
security boundary is testable without a network or credentials.
"""

from __future__ import annotations

import time

import pytest

from sarf.config import Settings
from sarf.db import Database
from sarf.validation import ValidationError
from sarf.xlayer.evm import to_checksum_address, validate_evm_address, validate_tx_hash
from sarf.xlayer.registry import XStocksRegistry, registry

ADDR_A = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
ADDR_B = "0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a"


@pytest.fixture()
def db():
    return Database(":memory:")


@pytest.fixture(scope="module")
def reg():
    return registry()


# ------------------------------------------------------------------ EVM layer

def test_keccak_and_eip55_match_official_vectors():
    # If these drift, every checksummed address we accept or emit is wrong.
    for a in [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ]:
        assert to_checksum_address(a) == a


def test_lowercase_and_valid_checksum_accepted():
    assert validate_evm_address(ADDR_A) == ADDR_A
    assert validate_evm_address("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed") == \
        "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"


def test_bad_checksum_rejected():
    # A corrupted paste is the exact case EIP-55 exists to catch.
    with pytest.raises(ValidationError):
        validate_evm_address("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD")


@pytest.mark.parametrize("bad", [
    "0x123", "", "not-an-address", None, 12345,
    "0xd8da6bf26964af9d7eed9e03e53415d37aa9604",     # 39 nibbles
    "0xd8da6bf26964af9d7eed9e03e53415d37aa960455",   # 41 nibbles
])
def test_malformed_addresses_rejected(bad):
    with pytest.raises(ValidationError):
        validate_evm_address(bad)


def test_tx_hash_validation():
    assert validate_tx_hash("0x" + "ab" * 32) == "0x" + "ab" * 32
    with pytest.raises(ValidationError):
        validate_tx_hash("0x" + "ab" * 31)


# ------------------------------------------------------------------ registry

def test_registry_loads_and_every_address_is_valid(reg):
    assert len(reg.assets) >= 40
    for a in reg.assets:
        assert validate_evm_address(a.address) == a.address
        assert a.decimals == 18
        assert a.symbol.endswith("x")
    assert reg.quote.decimals == 6


def test_cex_prefix_ticker_is_refused_with_the_onchain_symbol(reg):
    # The trap: XAAPL is OKX's *centralized* ticker. Resolving it silently to
    # AAPLx would hide a venue difference from the user, so we refuse and name
    # the right symbol instead.
    with pytest.raises(ValidationError) as e:
        reg.resolve("XAAPL")
    msg = str(e.value)
    assert "AAPLx" in msg and "centralized" in msg


def test_unknown_symbol_lists_alternatives(reg):
    with pytest.raises(ValidationError) as e:
        reg.resolve("DOGE")
    assert "AAPLx" in str(e.value)


def test_symbol_resolution_is_case_insensitive(reg):
    assert reg.resolve("aaplx").symbol == "AAPLx"
    assert reg.resolve("  AAPLX ").symbol == "AAPLx"


def test_allowlist_narrows_universe(reg):
    assert reg.resolve("AAPLx", allowlist=frozenset({"AAPLX"})).symbol == "AAPLx"
    with pytest.raises(ValidationError):
        reg.resolve("TSLAx", allowlist=frozenset({"AAPLX"}))


def test_registry_rejects_a_non_xlayer_file(tmp_path):
    import json
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "chain_id": 1, "quote_asset": {"symbol": "USDT", "onchain_symbol": "USDT",
                                       "address": ADDR_A, "decimals": 6},
        "dex_token_approve_address": ADDR_A, "assets": [],
    }))
    with pytest.raises(RuntimeError):
        XStocksRegistry(p)


# ------------------------------------------------------------- passkey policy

def _register_fake_passkey(db, address, sign_count=0):
    db.put_passkey(credential_id=f"cred-{address}", address=address,
                   public_key=b"\x01\x02", sign_count=sign_count)


def test_stepup_not_required_below_threshold(db, monkeypatch):
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    d = passkey.check_stepup(db, ADDR_A, 10.0)
    assert not d.required and not d.blocked


def test_stepup_required_above_threshold_and_blocks_without_verification(db, monkeypatch):
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    d = passkey.check_stepup(db, ADDR_A, 50_000.0)
    assert d.required and d.blocked


def test_recent_passkey_verification_satisfies_stepup(db, monkeypatch):
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    db.touch_passkey(f"cred-{ADDR_A}", sign_count=1, verified_at=time.time())
    d = passkey.check_stepup(db, ADDR_A, 50_000.0)
    assert d.required and d.satisfied and not d.blocked


def test_stale_passkey_verification_does_not_satisfy_stepup(db, monkeypatch):
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    old = time.time() - (passkey.STEPUP_VALIDITY_SECONDS + 60)
    db.touch_passkey(f"cred-{ADDR_A}", sign_count=1, verified_at=old)
    assert passkey.check_stepup(db, ADDR_A, 50_000.0).blocked


def test_unpriceable_order_fails_closed(db, monkeypatch):
    # If we cannot price it, we cannot claim it is under the threshold.
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    d = passkey.check_stepup(db, ADDR_A, None)
    assert d.required and d.blocked


def test_challenges_are_single_use_and_purpose_bound(db):
    db.put_passkey_challenge(challenge_id="c1", address=ADDR_A,
                             purpose="register", expires_at=time.time() + 60)
    first = db.consume_passkey_challenge("c1")
    assert first is not None and first["purpose"] == "register"
    assert db.consume_passkey_challenge("c1") is None  # replay refused


# --------------------------------------------------------------- order audit

def test_order_lifecycle_and_ownership(db):
    oid = db.create_order(address=ADDR_A, side="buy", symbol="AAPLx",
                          amount_in=100_000_000, quoted_out=319_500_000_000_000_000,
                          est_usd=100.0, tx={"to": ADDR_B, "data": "0xdead"},
                          ttl_seconds=300)
    o = db.get_order(oid)
    assert o["address"] == ADDR_A and o["status"] == "proposed" and not o["expired"]
    db.mark_order(oid, "submitted", tx_hash="0x" + "cd" * 32)
    assert db.get_order(oid)["tx_hash"] == "0x" + "cd" * 32
    assert db.orders_for_address(ADDR_A)[0]["order_id"] == oid
    assert db.orders_for_address(ADDR_B) == []


def test_expired_order_reports_expired(db):
    oid = db.create_order(address=ADDR_A, side="sell", symbol="TSLAx", amount_in=1,
                          quoted_out=1, est_usd=1.0, tx={}, ttl_seconds=-1)
    assert db.get_order(oid)["expired"] is True


# ------------------------------------------------------------ config policy

def test_order_cap_is_tighter_than_the_old_lending_cap():
    # xStocks pools are ~$200k-750k deep; a lending-sized cap would let a
    # single order move the pool badly against the user.
    s = Settings()
    assert 0 < s.max_order_usd <= 100_000
    assert s.max_price_impact_pct > 0


# ----------------------------------------------------------- platform fee

def test_flat_fee_becomes_the_right_percentage(monkeypatch):
    import sarf.providers.xlayer_rwa as m
    monkeypatch.setenv("PLATFORM_FEE_ADDRESS", ADDR_A)
    monkeypatch.setattr(m, "settings", Settings())
    for order_usd in (20.0, 100.0, 1000.0, 25_000.0):
        f = m.XLayerRwaProvider.fee_plan(order_usd)
        assert f["charge"] and abs(f["usd"] - 0.10) < 1e-6, (order_usd, f)


def test_fee_is_zero_without_a_recipient_address(monkeypatch):
    # An unset address must mean NO fee, never a fee sent somewhere unowned.
    import sarf.providers.xlayer_rwa as m
    monkeypatch.setenv("PLATFORM_FEE_ADDRESS", "")
    monkeypatch.setattr(m, "settings", Settings())
    assert m.XLayerRwaProvider.fee_plan(100.0)["charge"] is False


def test_fee_not_charged_when_order_cannot_be_priced(monkeypatch):
    import sarf.providers.xlayer_rwa as m
    monkeypatch.setenv("PLATFORM_FEE_ADDRESS", ADDR_A)
    monkeypatch.setattr(m, "settings", Settings())
    assert m.XLayerRwaProvider.fee_plan(None)["charge"] is False


def test_fee_percentage_is_capped(monkeypatch):
    # A tiny order would otherwise imply an absurd percentage.
    import sarf.providers.xlayer_rwa as m
    monkeypatch.setenv("PLATFORM_FEE_ADDRESS", ADDR_A)
    monkeypatch.setenv("MAX_FEE_PERCENT", "1.0")
    monkeypatch.setattr(m, "settings", Settings())
    f = m.XLayerRwaProvider.fee_plan(1.0)
    assert f["capped"] and f["percent"] <= 1.0 and f["usd"] < 0.10
