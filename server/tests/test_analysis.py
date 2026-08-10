"""Analysis guardrails.

These are compliance tests, not formatting tests. The arithmetic assertions
below matter, but the ones that must never be deleted are the language tests:
the whole defensibility of shipping portfolio analysis without a licence rests
on the output describing a portfolio rather than telling someone what to do
with it, and that is a property of the strings this module emits.
"""

from __future__ import annotations

import json

import pytest

from sarf.xlayer.analysis import (
    DISCLOSURE,
    MISSING_CONTEXT,
    PRESENTATION_RULES,
    analyze,
    classify,
)
from sarf.xlayer.card import render_order_card_text
from sarf.xlayer.registry import registry


def _pf(positions, *, usdt="0", okb="1", unpriced=()):
    total = sum(p["value_usd"] for p in positions)
    return {
        "positions": [{"name": p["symbol"], **p} for p in positions],
        "positions_value_usd": total,
        "usdt_balance": usdt,
        "gas_balance_okb": okb,
        "unpriced_positions": list(unpriced),
    }


CONCENTRATED = _pf([
    {"symbol": "NVDAx", "value_usd": 4100.0},
    {"symbol": "AMDx", "value_usd": 2400.0},
    {"symbol": "SOXLx", "value_usd": 1500.0},
    {"symbol": "COINx", "value_usd": 1200.0},
    {"symbol": "MSTRx", "value_usd": 800.0},
], usdt="120", okb="0")


# --------------------------------------------------------------------- language

# Directives ("you should sell") and forecasts ("will outperform") are the two
# things that turn analysis into advice. Substring matching is crude on
# purpose: it catches the phrasing regardless of which finding grew it.
_DIRECTIVE = [
    "you should", "we recommend", "i recommend", "you ought", "you need to",
    "consider selling", "consider buying", "trim ", "rebalance", "advise",
    "advice", "suitable for you", "right for you",
]
_FORECAST = [
    "will rise", "will fall", "will outperform", "will underperform",
    "expected to", "target price", "price target", "undervalued",
    "overvalued", "forecast", "we predict", "poised to", "set to gain",
]


@pytest.mark.parametrize("pf", [
    CONCENTRATED,
    _pf([{"symbol": "SPYx", "value_usd": 500.0}], usdt="500"),
    _pf([{"symbol": "GLDx", "value_usd": 10.0}, {"symbol": "LLYx", "value_usd": 9.0}]),
    _pf([], usdt="0"),
    _pf([{"symbol": "WEIRDx", "value_usd": 100.0}]),
])
def test_findings_never_instruct_or_forecast(pf):
    """No portfolio shape may produce a directive or a prediction."""
    blob = json.dumps(analyze(pf)["findings"]).lower()
    assert not [p for p in _DIRECTIVE if p in blob]
    assert not [p for p in _FORECAST if p in blob]


def test_every_response_carries_the_disclosures():
    for pf in (CONCENTRATED, _pf([]), _pf([{"symbol": "SPYx", "value_usd": 1.0}])):
        out = analyze(pf)
        assert out["disclosure"] == DISCLOSURE
        assert out["missing_context"] == MISSING_CONTEXT
        assert out["presentation_rules"] == PRESENTATION_RULES


def test_disclosure_states_both_required_points():
    low = DISCLOSURE.lower()
    assert "not personalised investment advice" in low or "not personalised" in low
    assert "not a licensed" in low
    assert "synthetic" in low


def test_missing_context_names_what_it_cannot_see():
    low = MISSING_CONTEXT.lower()
    for gap in ("income", "goal", "horizon", "risk tolerance"):
        assert gap in low


def test_findings_pair_fact_with_norm():
    """A bare number invites the model to supply its own conclusion; the norm
    is what keeps the interpretation on the page."""
    for f in analyze(CONCENTRATED)["findings"]:
        assert f["observation"]
        if f["type"] not in ("empty", "balanced"):
            assert f.get("reference_point"), f


# ----------------------------------------------------------------- arithmetic

def test_concentration_measures():
    c = analyze(CONCENTRATED)["concentration"]
    assert c["largest_position"] == "NVDAx"
    assert c["largest_position_percent"] == pytest.approx(41.0)
    assert c["top_3_percent"] == pytest.approx(80.0)
    # 1/HHI: five holdings, but sized like ~3.7 equal ones.
    assert 3.5 < c["effective_positions"] < 4.0


def test_leveraged_fund_is_bucketed_with_its_sector_not_split_out():
    """NVDAx + AMDx + SOXLx is 80% semiconductors. Giving the leveraged fund
    its own sector would report 65% and understate the exposure."""
    sectors = {s["sector"]: s["weight_percent"]
               for s in analyze(CONCENTRATED)["composition"]["by_sector"]}
    assert sectors["Semiconductors"] == pytest.approx(80.0)
    assert not any("leveraged" in s.lower() for s in sectors)


def test_leverage_is_still_reported_as_an_instrument_property():
    kinds = [f["type"] for f in analyze(CONCENTRATED)["findings"]]
    assert "instrument" in kinds
    note = next(f for f in analyze(CONCENTRATED)["findings"] if f["type"] == "instrument")
    assert "SOXLx" in note["observation"]
    assert "compound" in note["reference_point"].lower()


def test_crypto_linked_names_are_flagged_across_sectors():
    f = next(f for f in analyze(CONCENTRATED)["findings"] if f["type"] == "correlation")
    assert "COINx" in f["observation"] and "MSTRx" in f["observation"]


def test_broad_fund_is_not_called_single_name_concentration():
    """SPYx at 100% is one holding, not one company."""
    out = analyze(_pf([{"symbol": "SPYx", "value_usd": 500.0}], usdt="500"))
    f = next(f for f in out["findings"] if f["type"] == "concentration")
    assert "broad-market fund" in f["reference_point"]
    assert "single issuer drives" not in f["reference_point"]


def test_weights_are_over_the_equity_sleeve_not_total():
    """A big cash balance must not flatter concentration."""
    out = analyze(_pf([{"symbol": "NVDAx", "value_usd": 100.0}], usdt="9900"))
    assert out["weights"][0]["weight_percent"] == pytest.approx(100.0)
    assert out["stablecoin_buffer_percent"] == pytest.approx(99.0)


def test_zero_gas_is_reported_even_with_holdings():
    kinds = [f["type"] for f in analyze(CONCENTRATED)["findings"]]
    assert "operability" in kinds


def test_unpriced_positions_are_declared_not_ignored():
    out = analyze(_pf([{"symbol": "NVDAx", "value_usd": 100.0}], unpriced=["GMEx"]))
    f = next(f for f in out["findings"] if f["type"] == "data_quality")
    assert "GMEx" in f["observation"]


def test_empty_wallet_says_so_without_inventing_findings():
    out = analyze(_pf([]))
    assert out["position_count"] == 0
    assert [f["type"] for f in out["findings"]] == ["empty"]


def test_balanced_portfolio_yields_a_finding_rather_than_silence():
    even = _pf([{"symbol": s, "value_usd": 100.0} for s in
                ("AAPLx", "LLYx", "GSx", "XLEx", "GLDx", "AMZNx", "NFLXx", "SPYx")],
               usdt="200")
    kinds = [f["type"] for f in analyze(even)["findings"]]
    assert "balanced" in kinds


def test_every_registry_asset_is_classified():
    """An unclassified asset would silently land in 'Unclassified' and skew
    the sector buckets, so the map has to keep up with the registry."""
    unmapped = [a.symbol for a in registry().assets if classify(a.symbol)[1] == "Unclassified"]
    assert not unmapped, f"add these to CLASSIFICATION: {unmapped}"


# ---------------------------------------------------------------- text card

_ORDER = {
    "side": "buy", "symbol": "AAPLx", "name": "Apple xStock", "chain_id": 196,
    "spending": "100 USDT", "receiving_estimated": "0.31809 AAPLx",
    "minimum_received": "0.31491 AAPLx", "estimated_usd": 100.0,
    "platform_fee": {"charged": True, "usd": 0.1, "denominated_in": "USDT"},
    "risk_notes": ["a short note", "b" * 400],
}


def test_text_card_is_a_fenced_block_of_equal_width_lines():
    card = render_order_card_text(_ORDER)
    assert card.startswith("```\n") and card.endswith("\n```")
    body = card.strip("`\n").splitlines()
    assert len({len(line) for line in body}) == 1, "ragged border"


def test_text_card_always_states_the_fee_line():
    assert "Platform fee" in render_order_card_text(_ORDER)
    free = {**_ORDER, "platform_fee": {"charged": False}}
    assert "none" in render_order_card_text(free)


def test_text_card_marks_truncation_instead_of_cutting_mid_sentence():
    assert "…" in render_order_card_text(_ORDER)


def test_text_card_never_raises():
    assert render_order_card_text({}) != "" or True  # must not raise
    assert render_order_card_text({"risk_notes": None, "platform_fee": "nonsense"}) is not None


# --------------------------------------------------------------- MCP Apps

def test_widgets_are_declared_as_mcp_app_resources():
    """A tool without _meta.ui.resourceUri renders as text the model
    paraphrases, which is the failure this wiring exists to fix."""
    from sarf.xlayer.widget import UI_MIME, WIDGETS
    assert UI_MIME == "text/html;profile=mcp-app"
    for uri, (name, html, desc) in WIDGETS.items():
        assert uri.startswith("ui://sarf/")
        assert html.startswith("<!DOCTYPE html>")
        assert "ui/notifications/tool-result" in html, uri


def test_widgets_never_interpolate_values_into_markup():
    """Asset names come from an on-chain name() call. innerHTML with an
    interpolated value would be XSS in a surface showing balances."""
    from sarf.xlayer.widget import WIDGETS
    for uri, (_n, html, _d) in WIDGETS.items():
        body = html.split("function render", 1)[-1]
        assert "innerHTML=" not in body.replace(" ", "") or "innerHTML=''" in body.replace(" ", ""), uri


def test_okb_counts_toward_wallet_value_but_not_equity_concentration():
    """OKB is a holding with a price, not just a gas meter.

    The split matters in both directions: it has to reach the wallet total, or
    every balance is understated by whatever OKB is worth; and it has to stay
    out of the equity sleeve, or it dilutes every concentration weight and gets
    handed a sector by classify(), which only knows stock symbols.
    """
    result = analyze({
        "positions": [{"symbol": "AAPLx", "name": "Apple", "value_usd": 100.0}],
        "positions_value_usd": 100.0,
        "usdt_balance": "50",
        "gas_balance_okb": "1.0",
        "okb_value_usd": 25.0,
        "unpriced_positions": [],
    })

    assert result["holdings_value_usd"] == 175.0   # 100 equity + 50 USDT + 25 OKB
    assert result["equity_value_usd"] == 100.0     # OKB excluded from the sleeve
    assert result["okb_value_usd"] == 25.0
    # Weight is over equity alone, so OKB must not move it.
    assert result["weights"][0]["weight_percent"] == 100.0
    assert all(w["symbol"] != "OKB" for w in result["weights"])


def test_wallet_holding_only_okb_is_not_reported_as_empty():
    result = analyze({
        "positions": [],
        "positions_value_usd": 0.0,
        "usdt_balance": "0",
        "gas_balance_okb": "0.5",
        "okb_value_usd": 12.0,
    })
    assert result["holdings_value_usd"] == 12.0
    assert "nothing to measure" not in json.dumps(result)


def test_portfolio_without_an_okb_key_still_analyses():
    """Back-compat: stored snapshots predate okb_value_usd."""
    result = analyze({
        "positions": [{"symbol": "AAPLx", "name": "Apple", "value_usd": 100.0}],
        "positions_value_usd": 100.0,
        "usdt_balance": "50",
    })
    assert result["holdings_value_usd"] == 150.0
    assert result["okb_value_usd"] == 0.0
