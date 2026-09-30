"""Earn's hidden allocator. Because the user never sees the venues, these
tests are mostly about what the product is NOT allowed to do quietly: hide a
venue it could not measure, blend price movement into a rate without saying
so, breach a cap, or ship without the risk FAQ."""

import dataclasses

import pytest

from sarf.config import settings
from sarf.xlayer import earn_allocator as EA


def V(key, kind, rate, rate_kind="yield"):
    return EA.Venue(key=key, kind=kind, label=kind, rate_pct=rate,
                    rate_kind=rate_kind, basis="test")


# ----------------------------------------------------------------- the flag

def test_off_by_default():
    assert settings.earn_allocator_enabled is False
    d = EA.disabled_summary()
    assert d["enabled"] is False
    assert d["risk_faq"], "the FAQ ships even when the product does not"


# ------------------------------------------------------------- the caps

def test_no_kind_and_no_venue_exceeds_its_cap():
    legs = EA.allocate([
        V("lp:a", "lp", 500.0), V("lp:b", "lp", 400.0), V("lp:c", "lp", 300.0),
        V("aave:x", "lending", 5.0), V("gold:g", "gold", 30.0, "price exposure"),
    ])
    by_kind = {}
    for leg in legs:
        by_kind[leg.kind] = by_kind.get(leg.kind, 0) + leg.weight
        if leg.kind != "cash":
            assert leg.weight <= EA.PER_VENUE_MAX + 1e-9, f"{leg.key} over the venue cap"
    for kind, w in by_kind.items():
        if kind != "cash":
            assert w <= EA.CAPS[kind] + 1e-9, f"{kind} over its cap"


def test_weights_always_sum_to_one():
    for venues in ([], [V("aave:x", "lending", 3.0)],
                   [V("lp:a", "lp", 99.0), V("aave:x", "lending", 3.0)]):
        legs = EA.allocate(venues)
        assert abs(sum(leg.weight for leg in legs) - 1.0) < 1e-9


def test_what_cannot_be_placed_is_named_not_dropped():
    """An allocation that does not fill up says so with a leg, rather than
    leaving the reader to notice the weights do not add up."""
    legs = EA.allocate([V("aave:x", "lending", 3.0)])
    hold = [leg for leg in legs if leg.kind == "cash"]
    assert hold and hold[0].weight == pytest.approx(0.70)
    assert "not placed" in hold[0].basis


def test_an_unmeasured_venue_gets_nothing():
    legs = EA.allocate([V("lp:a", "lp", None), V("aave:x", "lending", 3.0)])
    assert all(leg.key != "lp:a" for leg in legs)


def test_higher_measured_rate_is_filled_first():
    legs = EA.allocate([V("aave:x", "lending", 3.0), V("lp:a", "lp", 99.0)])
    assert legs[0].key == "lp:a"


# --------------------------------------------------------- the headline rate

def test_price_exposure_is_separated_from_yield():
    legs = EA.allocate([V("aave:x", "lending", 10.0), V("gold:g", "gold", 50.0, "price exposure")])
    b = EA.blended_rate(legs)
    assert b["from_yield_pct"] > 0 and b["from_price_exposure_pct"] > 0
    assert b["blended_apy_pct"] == pytest.approx(
        b["from_yield_pct"] + b["from_price_exposure_pct"], abs=0.02)
    assert "not interest" in b["composition_note"]
    assert b["label"] == "variable, based on the last 30 days, not a forecast"


def test_a_pure_yield_blend_does_not_claim_price_exposure():
    legs = EA.allocate([V("aave:x", "lending", 4.0), V("lp:a", "lp", 60.0)])
    b = EA.blended_rate(legs)
    assert b["from_price_exposure_pct"] == 0
    assert "All of this is measured yield" in b["composition_note"]


# ------------------------------------------------------- history thresholds

def test_a_short_price_history_is_refused_rather_than_annualised():
    """Nine days of a new token annualised to -77.8% the first time this ran.
    A number like that must never reach a savings card."""
    rate, days = EA._annualise([100.0] * 10, [100.0] * 10)
    assert rate is None and days == 9
    rate, days = EA._annualise([100.0] * 30, [100.0] * 30)
    assert rate == 0.0 and days == 29


def test_the_rate_is_measured_against_the_deposit_asset():
    """A leg that doubled in dollars while the deposit asset doubled too has
    earned the user nothing, and must read as nothing."""
    flat_in_spcxx = EA._annualise([100.0 * 1.02 ** i for i in range(30)],
                                  [50.0 * 1.02 ** i for i in range(30)])
    assert flat_in_spcxx[0] == pytest.approx(0.0, abs=0.01)


# ------------------------------------------------------------------- the FAQ

def test_the_faq_says_the_balance_can_fall_in_the_words_required():
    a = next(x["a"] for x in EA.RISK_FAQ if x["q"] == "Can my balance go down?")
    assert a == ("Yes. When prices move sharply, your SPCXx balance can fall. Sarf's "
                 "engine watches your position and rebalances to limit this, but "
                 "sudden one-day moves can't be avoided.")
    assert "impermanent loss" not in " ".join(x["a"] for x in EA.RISK_FAQ).lower()


def test_the_faq_keeps_the_token_tax_and_paxgy_lines():
    joined = " ".join(x["a"] for x in EA.RISK_FAQ)
    assert "tax on every transfer" in joined
    assert "PAXGy" in joined and "defaults" in joined
    assert "not minted or redeemed" in joined or "not minted" in joined
    assert "your own wallet" in joined


# --------------------------------------------------- the endpoint the card reads

def test_summary_endpoint_is_silent_and_harmless_while_the_flag_is_off():
    """With the flag off it must not read a venue, not price anything, and
    still hand back the FAQ — the card can render its disclosure before the
    product is ever switched on."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sarf.db import Database
    from sarf.xlayer.registry import registry
    from sarf.xlayer.zap import ZapEngine
    from sarf.xlayer.zap_api import build_zap_api

    engine = ZapEngine(Database(":memory:"), None, registry())
    # Any venue read would explode on this: proof by detonation.
    engine.pool_viability = lambda *a, **k: (_ for _ in ()).throw(AssertionError("read a venue"))
    app = FastAPI()
    app.include_router(build_zap_api(Database(":memory:"), engine))
    r = TestClient(app).get("/api/zap/earn/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["risk_faq"][1]["q"] == "Can my balance go down?"
    assert "blended_apy_pct" not in body


# ------------------------------------------------------- per-asset vaults

def test_paxgy_vault_never_fabricates_a_yield():
    """PAXGy has no on-chain accrual and no LP execution behind it. Its card
    must say that plainly rather than reporting its own price swing as APY."""
    import asyncio

    v = asyncio.run(EA._paxgy_vault({"paxgy": {"usd": [1.0] * 9},
                                     "deposit_asset": {"usd": [1.0] * 9}}))
    assert v.apy_pct is None
    assert v.apy_status == "not_yet_earning"
    assert v.flagship is True
    assert v.withdraw_label == "Instant" and v.withdraw_cost_pct is None


def test_a_vault_with_no_eligible_pool_reads_as_measuring_not_zero():
    import asyncio

    from types import SimpleNamespace

    engine = SimpleNamespace(pools={}, )
    v = asyncio.run(EA.asset_vault(engine, "NVDAx", {"aave": {}}, None))
    assert v.apy_status == "measuring"
    assert v.apy_pct is None


def test_asset_vault_symbols_match_the_spec():
    assert set(EA.ASSET_POOLS) | {"PAXGy"} == {"SPCXx", "NVDAx", "PAXGy"}
    for sym in ("SPCXx", "NVDAx", "PAXGy"):
        assert sym in EA.ASSET_META


def test_earn_vaults_endpoint_lists_every_asset_ungated():
    """Reading the vault list must work even with the flag off — only
    deposits are gated, not the numbers."""
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sarf.db import Database
    from sarf.xlayer.okx_dex import OkxDexClient
    from sarf.xlayer.registry import registry
    from sarf.xlayer.zap import ZapEngine
    from sarf.xlayer.zap_api import build_zap_api

    engine = ZapEngine(Database(":memory:"), OkxDexClient(), registry())
    app = FastAPI()
    app.include_router(build_zap_api(Database(":memory:"), engine))
    r = TestClient(app).get("/api/zap/earn/vaults")
    assert r.status_code == 200
    body = r.json()
    symbols = {v["symbol"] for v in body["vaults"]}
    assert symbols == {"SPCXx", "NVDAx", "PAXGy"}
    assert body["deposits_open"] is False, "flag is off; deposits must read as closed"
    assert next(v for v in body["vaults"] if v["symbol"] == "PAXGy")["flagship"] is True
    for v in body["vaults"]:
        assert "tvl" not in json.dumps(v).lower(), "no TVL anywhere in a vault row"


def test_single_vault_endpoint_rejects_unknown_symbols():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sarf.db import Database
    from sarf.xlayer.okx_dex import OkxDexClient
    from sarf.xlayer.registry import registry
    from sarf.xlayer.zap import ZapEngine
    from sarf.xlayer.zap_api import build_zap_api

    engine = ZapEngine(Database(":memory:"), OkxDexClient(), registry())
    app = FastAPI()
    app.include_router(build_zap_api(Database(":memory:"), engine))
    c = TestClient(app)
    assert c.get("/api/zap/earn/vaults/AAPLx").status_code == 404
    ok = c.get("/api/zap/earn/vaults/spcxx")
    assert ok.status_code == 200 and ok.json()["symbol"] == "SPCXx"
