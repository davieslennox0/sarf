"""PAXGy is in the registry but is not an xStock, and the two rules that make
that safe have to hold everywhere: it trades only against USDG, and it carries
its own disclosure instead of the synthetic-equity one."""

import pytest

from sarf.providers.xlayer_rwa import SYNTHETIC_DISCLOSURE, _check_pair, _resolve_any
from sarf.validation import ValidationError
from sarf.xlayer.registry import USDC, USDG, registry


@pytest.fixture()
def reg():
    return registry()


def test_paxgy_is_registered_and_not_an_xstock(reg):
    a = reg.get("PAXGy")
    assert a is not None
    assert a.address == "0x6c6494fd9962eb98b94ffa48f6679058f820700e"
    assert a.decimals == 18
    assert a.kind == "gold" and a.is_xstock is False
    assert a.quote_symbol == "USDG"


def test_the_disclosure_says_the_three_things_it_must(reg):
    d = reg.get("PAXGy").disclosure.lower()
    assert "issuer-only" in d and "kyc" in d, "mint/redeem is issuer-only with KYC"
    assert "cannot mint" in d or "cannot mint or redeem" in d, "no mint path"
    assert "fall" in d and "default" in d, "the rate can fall on borrower default"
    assert d != SYNTHETIC_DISCLOSURE.lower()


def test_every_xstock_still_has_no_override(reg):
    for a in reg.assets:
        if a.symbol == "PAXGy":
            continue
        assert a.is_xstock and not a.disclosure and not a.quote_symbol


def test_paxgy_trades_against_usdg_only(reg):
    paxgy = reg.get("PAXGy")
    _check_pair(paxgy, USDG)  # the one allowed pair
    for other in (reg.quote, USDC, reg.get("NVDAx")):
        with pytest.raises(ValidationError) as e:
            _check_pair(paxgy, other)
        assert "USDG" in str(e.value)
        with pytest.raises(ValidationError):
            _check_pair(other, paxgy)  # and in either direction


def test_xstock_pairs_are_untouched(reg):
    _check_pair(reg.get("NVDAx"), reg.quote)
    _check_pair(reg.get("NVDAx"), reg.get("AAPLx"))
    _check_pair(reg.quote, USDC)


def test_usdg_is_nameable_but_is_not_the_quote_asset(reg):
    assert _resolve_any(reg, "USDG") is USDG
    assert _resolve_any(reg, "usdg") is USDG
    assert reg.quote.symbol == "USDT", "caps and pricing stay in USDT"
    assert USDG.decimals == 6
