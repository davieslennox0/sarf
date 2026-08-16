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


def test_small_transactions_are_gated_too(db, monkeypatch):
    """The passkey gates every transaction, not just large ones.

    This asserted the opposite until 2026-08-10, when the passkey stopped being
    a step-up on big orders and became the per-session gate on all of them. A
    $10 trade moves real money and is exactly the size an attacker with a stolen
    session token would start with.
    """
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    d = passkey.check_stepup(db, ADDR_A, 10.0)
    assert d.required and d.blocked


def test_one_assertion_covers_the_session(db, monkeypatch):
    """A single verification covers later trades until the window expires.

    This is what makes an in-chat Approve possible without a wallet round trip
    per trade, and it is the security trade the session caps exist to bound.
    """
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    db.touch_passkey(f"cred-{ADDR_A}", sign_count=1, verified_at=time.time())
    assert not passkey.check_stepup(db, ADDR_A, 10.0).blocked
    assert not passkey.check_stepup(db, ADDR_A, 250.0).blocked


def test_no_registered_passkey_blocks_when_required(db, monkeypatch):
    """Safe only because registration happens at sign-in — see config.py."""
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    d = passkey.check_stepup(db, ADDR_A, 10.0)
    assert d.required and d.blocked
    assert "register" in d.reason


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
    old = time.time() - (passkey.stepup_validity_seconds() + 60)
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
        assert f["charge"] and abs(f["usd"] - 0.01) < 1e-6, (order_usd, f)


def test_fee_is_zero_without_a_recipient_address(monkeypatch):
    from sarf.xlayer import delegation
    monkeypatch.setattr(delegation, "relayer_address", lambda: None)
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
    # $0.01 on a $0.50 order is 2%, over the 1% ceiling, so the RATE is cut
    # rather than the order refused.
    f = m.XLayerRwaProvider.fee_plan(0.5)
    assert f["capped"] and f["percent"] <= 1.0 and f["usd"] < 0.01


def test_signer_page_gets_fee_and_risk_notes(db):
    # Regression: the signer is the LAST review surface before funds move.
    # It renders these fields, so they must survive the round trip through
    # the database -- otherwise it silently shows blanks where the fee and
    # the risk notes should be.
    oid = db.create_order(
        address=ADDR_A, side="buy", symbol="AAPLx", amount_in=100_000_000,
        quoted_out=319_500_000_000_000_000, est_usd=100.0,
        tx={"to": ADDR_B, "data": "0xdead"}, ttl_seconds=300,
        display={
            "name": "Apple xStock",
            "human_summary": "BUY 0.319 AAPLx for 100 USDT",
            "risk_notes": ["synthetic exposure", "platform fee $0.10"],
            "platform_fee": {"charged": True, "usd": 0.10, "denominated_in": "USDT"},
            "spending": "100 USDT",
            "receiving_estimated": "0.3195 AAPLx",
            "minimum_received": "0.3163 AAPLx",
        },
    )
    o = db.get_order(oid)
    assert o["platform_fee"]["charged"] is True
    assert o["platform_fee"]["usd"] == 0.10
    assert len(o["risk_notes"]) == 2
    assert o["spending"] == "100 USDT" and o["minimum_received"] == "0.3163 AAPLx"
    assert o["human_summary"].startswith("BUY")


def test_display_never_overrides_authoritative_columns(db):
    # A display blob is cosmetic; it must not be able to rewrite the address,
    # amount or status the rest of the system trusts.
    oid = db.create_order(
        address=ADDR_A, side="buy", symbol="AAPLx", amount_in=5, quoted_out=6,
        est_usd=100.0, tx={}, ttl_seconds=300,
        display={"address": ADDR_B, "status": "confirmed", "est_usd": 999999.0},
    )
    o = db.get_order(oid)
    assert o["address"] == ADDR_A
    assert o["status"] == "proposed"
    assert o["est_usd"] == 100.0


# --- reporting honesty -------------------------------------------------------

def test_cards_never_contradict_can_execute():
    """The image and the text card must agree with the payload, and each other.

    A place_order response once advertised an order as auto-executable while
    the card rendered under it read "Sarf cannot execute this" — two halves of
    one response disagreeing about a fund-moving action. The text card was
    fixed; the PNG kept the hard-coded line for another day.
    """
    import base64

    from sarf.xlayer.card import render_order_card, render_order_card_text

    o = {"side": "buy", "symbol": "PLTRx", "name": "Palantir xStock",
         "spending": "1 USDT", "receiving_estimated": "0.005 PLTRx",
         "estimated_usd": 1.0, "risk_notes": ["note"]}

    o["can_execute"] = True
    assert "cannot execute" not in render_order_card_text(o)
    executable_png = base64.b64decode(render_order_card(o))

    o["can_execute"] = False
    assert "cannot execute" in render_order_card_text(o)
    # The PNG has no text to assert on, so compare renders: identical bytes
    # would mean the footer ignored the flag entirely.
    assert base64.b64decode(render_order_card(o)) != executable_png


# --- router identity ---------------------------------------------------------
# SarfSessionKey enforces `target == g.router`. The approval spender and the
# swap router are DIFFERENT contracts on X Layer, and a grant that names the
# former reverts every trade with TargetNotAllowed while looking healthy by
# every other measure. That shipped: two trades burned 69,750 gas each on
# 2026-08-11 before the cause was found.

def test_router_and_approve_spender_are_not_the_same_contract(reg):
    assert reg.dex_router_address.lower() != reg.dex_approve_address.lower(), (
        "the swap router and the approval spender have collapsed into one "
        "address — grants built from this registry will revert on every trade"
    )


def test_registry_pins_the_swap_router_explicitly(reg):
    """Falling back to the approve address is the exact bug; require the key.

    The fallback stays in registry.py so an older registry file still loads,
    but the SHIPPED registry must name the router outright rather than inherit
    the spender by omission.
    """
    import json
    from pathlib import Path
    from sarf.xlayer import registry as regmod

    raw = json.loads(Path(regmod._REGISTRY_PATH).read_text())
    assert raw.get("dex_router_address"), "registry must state dex_router_address"
    assert raw["dex_router_address"].lower() != raw["dex_token_approve_address"].lower()


# The second half of the same mistake. Naming the approve address as the router
# reverts with TargetNotAllowed; naming the router as the SPENDER reverts with
# SwapFailed, because OKX's router never calls transferFrom itself — it reaches
# the tokens through its TokenApprove, so an allowance held by the router is one
# nobody ever spends. That shipped too: 223,459 gas a time, four trades, all
# reverted. The grant has to carry both addresses, and they have to be the right
# way round.

def test_grant_calldata_approves_the_spender_not_the_router(reg):
    from eth_abi import decode as abi_decode

    from sarf.xlayer import delegation

    data = delegation.grant_calldata(
        session_key=ADDR_A, expiry=int(time.time()) + 3600,
        router=reg.dex_router_address, spender=reg.dex_approve_address,
        stable=reg.quote.address, per_trade_cap=1, daily_cap=2, tokens=[],
    )
    router, spender = abi_decode(
        ["address", "uint64", "address", "address", "address",
         "uint128", "uint128", "address[]"],
        bytes.fromhex(data[10:]),
    )[2:4]
    assert router.lower() == reg.dex_router_address.lower()
    assert spender.lower() == reg.dex_approve_address.lower()
    assert router.lower() != spender.lower()


def test_grant_signature_carries_the_spender_argument():
    """The selector must match the deployed contract's 8-argument authorize.

    A 7-argument grant is not a weaker grant — it is calldata the new delegate
    has no function for, so it reverts at the point of authorising.
    """
    from eth_utils import keccak

    from sarf.xlayer import delegation

    expected = "0x" + keccak(text=(
        "authorize(address,uint64,address,address,address,uint128,uint128,address[])"
    ))[:4].hex()
    data = delegation.grant_calldata(
        session_key=ADDR_A, expiry=int(time.time()) + 3600, router=ADDR_A,
        spender=ADDR_B, stable=ADDR_A, per_trade_cap=1, daily_cap=2, tokens=[],
    )
    assert data.startswith(expected)


# --- widget logos ------------------------------------------------------------
# The host iframe's content policy blocks external image hosts, so a logo named
# by URL never loads and every asset falls back to its monogram — a bought
# PLTRx position rendered as a "P" in a coloured box. Logos are inlined into
# the page instead. If the placeholder ever disappears the substitution becomes
# a silent no-op and the monogram comes back, so it is asserted here.

def test_every_widget_has_a_logo_injection_point():
    from sarf.xlayer.widget import WIDGETS

    for uri, (_name, html, _desc) in WIDGETS.items():
        assert "/*__SARF_LOGOS__*/" in html, f"{uri} lost its logo injection point"


def test_widgets_prefer_the_inlined_logo_over_a_remote_url():
    """A payload URL must never win over the baked-in copy."""
    from sarf.xlayer.widget import ORDER_CARD_URI, WIDGETS

    for uri, (_name, html, _desc) in WIDGETS.items():
        if "logoFor(" not in html:
            continue
        assert "window.SARF_LOGOS" in html, f"{uri} does not read the inlined map"
    order = WIDGETS[ORDER_CARD_URI][1]
    assert "im.src=src0" in order, "the order card bypassed logoFor()"
    assert "im.src=d.logo_url" not in order


def test_a_fetched_logo_survives_the_source_going_down(tmp_path, monkeypatch):
    """OKX's CDN is the only source that carries X Layer xStock icons.

    DexScreener does not index these tokens, Trustwallet has no X Layer, and
    xStocks' own icons are Webflow assets with per-asset hashes rather than
    anything addressable by symbol. So the answer to "what if OKX is down" is
    not a second source — it is not needing the first one twice.
    """
    from PIL import Image

    from sarf.xlayer import card as cardmod

    monkeypatch.setattr(cardmod, "_LOGO_DIR", tmp_path)
    cardmod._DATA_URI_CACHE.clear()
    url = "https://static.oklink.com/whatever/logo.png"

    monkeypatch.setattr(
        cardmod, "_logo", lambda u, s: Image.new("RGBA", (s, s), (1, 2, 3, 255)))
    live = cardmod.logo_data_uri(url)
    assert live.startswith("data:image/png;base64,")
    assert list(tmp_path.glob("*.png")), "the icon was never persisted"

    # Source unreachable, and nothing warm in memory.
    cardmod._DATA_URI_CACHE.clear()
    monkeypatch.setattr(cardmod, "_logo", lambda u, s: None)
    assert cardmod.logo_data_uri(url) == live, "an icon we already hold was lost"


def test_an_icon_we_never_had_fails_to_the_monogram_not_to_a_broken_image():
    from sarf.xlayer import card as cardmod

    cardmod._DATA_URI_CACHE.pop("https://example.invalid/x.png@48", None)
    assert cardmod.logo_data_uri("") == ""


# --- grant lifetime ----------------------------------------------------------
# A session key trades without asking. Its lifetime is therefore capped at the
# passkey assertion that bought it rather than at the contract's 30-day ceiling.

def test_grant_lifetime_is_capped_at_one_hour():
    from sarf.xlayer import delegation

    assert delegation.MAX_GRANT_SECONDS == 3600
    assert delegation.MAX_GRANT_SECONDS <= delegation.CONTRACT_MAX_GRANT_SECONDS

    now = int(time.time())
    assert delegation.requested_expiry(1 / 24) - now == pytest.approx(3600, abs=2)

    for too_long in (1, 7, 30):
        with pytest.raises(ValidationError):
            delegation.requested_expiry(too_long)


# --- approval mode -----------------------------------------------------------
# The mode decides whether a trade can settle in chat on a session assertion
# alone. It is read from the grant row, never from a request argument, so these
# assert the storage layer defaults the way the gate expects.

def test_every_grant_is_autonomous(db):
    """Always Ask was removed from the platform on 2026-08-11.

    It promised a passkey on every trade and could not deliver one where trades
    happen: WebAuthn needs a top-level browsing context and an MCP widget is a
    sandboxed iframe, so it degraded to a link out every time. The passkey now
    gates the session; the contract caps bound what the key can do after that.
    """
    db.put_grant(address=ADDR_A, session_address=ADDR_A, sealed_key="x",
                 delegate=ADDR_A, router=ADDR_A, stable=ADDR_A,
                 expiry=int(time.time()) + 3600, per_trade_cap=500, daily_cap=2000)
    assert db.get_grant(ADDR_A)["approval_mode"] == "autonomous"


def test_grant_records_autonomous_mode_and_limit(db):
    db.put_grant(address=ADDR_A, session_address=ADDR_A, sealed_key="x",
                 delegate=ADDR_A, router=ADDR_A, stable=ADDR_A,
                 expiry=int(time.time()) + 3600, per_trade_cap=500, daily_cap=2000,
                 approval_mode="autonomous", autonomous_limit=250)
    row = db.get_grant(ADDR_A)
    assert row["approval_mode"] == "autonomous"
    assert row["autonomous_limit"] == 250


def test_mode_argument_cannot_reintroduce_always_ask(db):
    """Nothing a caller passes can put a grant back into the removed mode."""
    db.put_grant(address=ADDR_A, session_address=ADDR_A, sealed_key="x",
                 delegate=ADDR_A, router=ADDR_A, stable=ADDR_A,
                 expiry=int(time.time()) + 3600, per_trade_cap=500, daily_cap=2000,
                 approval_mode="always_ask", autonomous_limit=999)
    assert db.get_grant(ADDR_A)["approval_mode"] == "autonomous"


def test_consuming_a_verification_forces_the_next_one(db, monkeypatch):
    """Always Ask means every trade, not the first trade and then an hour free.

    Modelled on PayBox, where an approval is valid for exactly one transaction
    so a captured authorization cannot be replayed. Before this, one assertion
    covered the whole session window and trades 2..n went through unprompted —
    weaker than the mode's own name promises.
    """
    from sarf import passkey
    monkeypatch.setattr(passkey, "settings", Settings())
    _register_fake_passkey(db, ADDR_A)
    db.touch_passkey(f"cred-{ADDR_A}", sign_count=1, verified_at=time.time())
    assert not passkey.check_stepup(db, ADDR_A, 10.0).blocked

    db.consume_passkey_verification(ADDR_A)
    assert db.last_passkey_verification(ADDR_A) is None
    assert passkey.check_stepup(db, ADDR_A, 10.0).blocked


# --- grant retirement --------------------------------------------------------
# The grant lasts an hour. What happens at the end of that hour is not just a
# matter of a flag flipping: Sarf holds a signing key for it, and every surface
# that showed the grant read the row rather than the clock.

def test_an_expired_grant_is_retired_and_its_key_destroyed(db):
    db.put_grant(address=ADDR_A, session_address=ADDR_B, sealed_key="sealed",
                 delegate=ADDR_A, router=ADDR_A, stable=ADDR_A,
                 expiry=int(time.time()) - 1, per_trade_cap=500, daily_cap=2000)

    assert db.expire_grants() == 1
    row = db.get_grant(ADDR_A)
    assert row["revoked_at"] is not None, "an expired grant must not read as live"
    assert row["sealed_key"] == "", "a key that can authorise nothing must not be kept"
    # Idempotent: the sweeper runs every minute and on every read.
    assert db.expire_grants() == 0


def test_a_live_grant_survives_the_sweep(db):
    db.put_grant(address=ADDR_A, session_address=ADDR_B, sealed_key="sealed",
                 delegate=ADDR_A, router=ADDR_A, stable=ADDR_A,
                 expiry=int(time.time()) + 3600, per_trade_cap=500, daily_cap=2000)
    assert db.expire_grants() == 0
    row = db.get_grant(ADDR_A)
    assert row["revoked_at"] is None
    assert row["sealed_key"] == "sealed"


def test_revoking_destroys_the_key_material(db):
    db.put_grant(address=ADDR_A, session_address=ADDR_B, sealed_key="sealed",
                 delegate=ADDR_A, router=ADDR_A, stable=ADDR_A,
                 expiry=int(time.time()) + 3600, per_trade_cap=500, daily_cap=2000)
    assert db.revoke_grant(ADDR_A) is True
    row = db.get_grant(ADDR_A)
    assert row["revoked_at"] is not None
    assert row["sealed_key"] == ""
    # A second revoke changes nothing — the row is already dead.
    assert db.revoke_grant(ADDR_A) is False


def test_an_expired_grant_is_not_active(db):
    """The dataclass is what every execution path consults."""
    from sarf.xlayer.delegation import Grant

    g = Grant(address=ADDR_A, session_address=ADDR_B, delegate=ADDR_A,
              router=ADDR_A, stable=ADDR_A, expiry=int(time.time()) - 1,
              per_trade_cap=500, daily_cap=2000, created_at=0.0, rotated_at=0.0)
    assert not g.active
    assert g.view()["expires_in_seconds"] == 0


# --- who pays the gas --------------------------------------------------------

def test_the_card_only_claims_sponsored_gas_when_sarf_pays():
    """Both cards default a missing `gas_sponsored` to "sponsored by Sarf", and
    the field was never set on an order — so a trade the user was about to sign
    and pay OKB for was captioned as free. Sponsorship is exactly as narrow as
    can_execute: it is the relayer submitting the swap, nothing else."""
    from sarf.xlayer.card import render_order_card_text

    o = {"side": "buy", "symbol": "AAPLx", "spending": "10 USDT",
         "receiving_estimated": "0.03 AAPLx", "estimated_usd": 10.0}

    assert "paid from your OKB" in render_order_card_text({**o, "gas_sponsored": False})
    assert "sponsored by Sarf" in render_order_card_text({**o, "gas_sponsored": True})


# --- balances: a failed read is not a zero balance ---------------------------

def test_a_failed_balance_read_is_reported_not_silently_zeroed():
    """`erc20_balances` used to return only the successes, so a token whose
    read failed looked exactly like a token the wallet does not hold — during
    an RPC wobble a position simply vanished from the portfolio. The failures
    now come back by address so the caller can say which ones it could not
    read."""
    import asyncio

    from sarf.xlayer import rpc

    async def fake_call(method, params, **kw):
        to = params[0]["to"] if method == "eth_call" else ""
        if to == ADDR_B:
            raise rpc.RpcError("node said no")
        return hex(42)

    original = rpc._call
    rpc._call = fake_call
    try:
        got, unread = asyncio.run(rpc.erc20_balances([ADDR_A, ADDR_B], ADDR_A))
    finally:
        rpc._call = original

    assert got == {ADDR_A: 42}
    assert unread == [ADDR_B]


def test_every_balance_failing_is_an_error_not_an_empty_wallet():
    import asyncio

    from sarf.xlayer import rpc

    async def fake_call(method, params, **kw):
        raise rpc.RpcError("node down")

    original = rpc._call
    rpc._call = fake_call
    try:
        with pytest.raises(rpc.RpcError):
            asyncio.run(rpc.erc20_balances([ADDR_A, ADDR_B], ADDR_A))
    finally:
        rpc._call = original


# --- what is connected -------------------------------------------------------
# "A session is live" is a poor answer to "what is connected to my wallet".
# The client's OAuth registration name is carried onto the session so the
# account page can name it — and it is a LABEL, never an authorisation input.

def test_active_sessions_name_their_client_and_drop_dead_ones(db):
    from sarf import auth

    auth.mint_session(db, ADDR_A, client_name="Claude", client_id="cid-claude")
    auth.mint_session(db, ADDR_A, client_name="Sarf website")
    auth.mint_session(db, ADDR_A)                      # legacy ?key=, unnamed
    auth.mint_session(db, ADDR_B, client_name="Claude")  # another wallet

    live = db.active_sessions(ADDR_A)
    assert len(live) == 3, "another wallet's sessions must not appear"
    assert {s["client_name"] for s in live} == {"Claude", "Sarf website", None}
    # The token id is a credential half and must never come back out.
    assert all("token" not in s for s in live)

    db.revoke_sessions_for_address(ADDR_A, reason="user_logout")
    assert db.active_sessions(ADDR_A) == [], "ending the session disconnects everything"


# --- where the fee goes ------------------------------------------------------

def test_fee_defaults_to_the_relayer_when_no_recipient_is_configured(monkeypatch):
    """The wallet that pays gas is the wallet the fee funds.

    Fails to ZERO fee if there is no relayer either — never to an address
    nobody controls.
    """
    from sarf.providers import xlayer_rwa
    from sarf.xlayer import delegation

    monkeypatch.setenv("PLATFORM_FEE_USD", "0.01")
    monkeypatch.setenv("PLATFORM_FEE_ADDRESS", "")
    monkeypatch.setattr(xlayer_rwa, "settings", Settings())
    monkeypatch.setattr(delegation, "relayer_address", lambda: ADDR_B)

    plan = xlayer_rwa.XLayerRwaProvider.fee_plan(100.0)
    assert plan["charge"] is True
    assert plan["recipient"] == ADDR_B
    assert plan["usd"] == 0.01

    monkeypatch.setattr(delegation, "relayer_address", lambda: None)
    assert xlayer_rwa.XLayerRwaProvider.fee_plan(100.0)["charge"] is False


def test_an_explicit_recipient_still_wins(monkeypatch):
    from sarf.providers import xlayer_rwa
    from sarf.xlayer import delegation

    monkeypatch.setenv("PLATFORM_FEE_USD", "0.01")
    monkeypatch.setenv("PLATFORM_FEE_ADDRESS", ADDR_A)
    monkeypatch.setattr(xlayer_rwa, "settings", Settings())
    monkeypatch.setattr(delegation, "relayer_address", lambda: ADDR_B)
    assert xlayer_rwa.XLayerRwaProvider.fee_plan(100.0)["recipient"] == ADDR_A


# --- deposits: dollars in, by burn and mint ----------------------------------
# The route is CCTP, not a bridge: USDC is burned on Base and minted on
# X Layer 1:1. The parts worth pinning in a test are the ones that decide
# where money lands.

def test_the_burn_names_x_layer_and_the_users_own_address():
    from eth_abi import decode

    from sarf.xlayer import deposit as d

    data = d.burn_calldata(amount=100_000000, recipient=ADDR_A, max_fee=13_000)
    assert data.startswith("0x8e0250ee"), "must be the V2 depositForBurn selector"
    a = decode(["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
               bytes.fromhex(data[10:]))
    assert a[0] == 100_000000
    assert a[1] == 37, "destination domain must be X Layer, not a chain id"
    assert "0x" + a[2].hex()[24:] == ADDR_A, "the recipient is the user, always"
    assert a[3].lower() == d.USDC_BASE, "burning anything but Base USDC is a bug"
    assert a[4] == bytes(32), "destinationCaller stays open so the relayer can mint"
    assert a[6] == d.FAST_FINALITY_THRESHOLD


def test_deposit_bounds_are_enforced_in_one_place():
    from sarf.xlayer import deposit as d

    with pytest.raises(ValidationError):
        d.units(d.MIN_DEPOSIT_USD - 0.01)
    with pytest.raises(ValidationError):
        d.units(d.MAX_DEPOSIT_USD + 1)
    with pytest.raises(ValidationError):
        d.units("not a number")
    assert d.units(100) == 100_000000


def test_the_domain_and_the_chain_id_are_not_the_same_number():
    """A burn that quotes a chain id where a domain belongs mints somewhere
    else entirely. They are different numbering schemes and this is the
    cheapest possible guard against conflating them."""
    from sarf.xlayer import deposit as d

    assert d.DESTINATION.domain == 37
    assert d.DESTINATION.chain_id == 196
    assert d.SOURCE.domain == 6
    assert d.SOURCE.chain_id == 8453
