"""Unit tests for the validation layer — the actual security boundary.

These run fully offline: validation.py is pure functions over data. The
scenarios mirror what a confused or adversarial LLM layer could actually
send as tool arguments.
"""

import pytest

from sarf.validation import (
    AssetInfo,
    OwnershipError,
    ValidationError,
    check_cap_ownership,
    validate_address,
    validate_amount,
    validate_asset,
    validate_base64,
    validate_market,
    validate_multiplier,
    validate_usd_cap,
)
from sarf.config import LEVERAGE_ABSOLUTE_MAX

ADDR = "0x" + "ab" * 32
OTHER = "0x" + "cd" * 32


# ---------------------------------------------------------------- addresses

class TestAddress:
    def test_valid_address_normalized(self):
        assert validate_address(ADDR.upper().replace("0X", "0x")) == ADDR
        assert validate_address(f"  {ADDR}  ") == ADDR

    @pytest.mark.parametrize("bad", [
        "0xdeadbeef",                     # too short
        ADDR + "ff",                      # too long
        ADDR[2:],                         # missing 0x
        "0x" + "zz" * 32,                 # non-hex
        "sui1qqqq",                       # bech32-ish
        1234, None, ["0x" + "ab" * 32],   # wrong types
        "0x" + "ab" * 31 + "aа",     # unicode homoglyph smuggling
    ])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValidationError):
            validate_address(bad)


# ------------------------------------------------------------ asset whitelist

REGISTRY = {
    ("MainMarket", "USDC"): AssetInfo("USDC", "0x" + "11" * 32 + "::usdc::USDC", 6, "MainMarket"),
    ("MainMarket", "SUI"): AssetInfo("SUI", "0x" + "02".zfill(64) + "::sui::SUI", 9, "MainMarket"),
    ("AltCoinMarket", "DEEP"): AssetInfo("DEEP", "0x" + "22" * 32 + "::deep::DEEP", 6, "AltCoinMarket"),
}


class TestAssetWhitelist:
    def test_known_asset_resolves(self):
        info = validate_asset("usdc", "MainMarket", REGISTRY)
        assert info.coin_type.endswith("::usdc::USDC")
        assert info.decimals == 6

    def test_symbol_case_and_whitespace_normalized(self):
        assert validate_asset(" Sui ", "MainMarket", REGISTRY).symbol == "SUI"

    def test_unknown_asset_rejected(self):
        with pytest.raises(ValidationError, match="not available"):
            validate_asset("SCAM", "MainMarket", REGISTRY)

    def test_asset_from_other_market_rejected(self):
        # DEEP exists — but not in MainMarket. Cross-market confusion must fail.
        with pytest.raises(ValidationError, match="not available"):
            validate_asset("DEEP", "MainMarket", REGISTRY)

    def test_raw_coin_type_rejected(self):
        # The tool layer must never accept free-text Move types.
        with pytest.raises(ValidationError):
            validate_asset("0x2::sui::SUI", "MainMarket", REGISTRY)

    def test_allowlist_narrowing(self):
        with pytest.raises(ValidationError, match="not enabled"):
            validate_asset("SUI", "MainMarket", REGISTRY, allowlist=frozenset({"USDC"}))
        assert validate_asset("USDC", "MainMarket", REGISTRY, allowlist=frozenset({"USDC"})).symbol == "USDC"

    def test_non_string_rejected(self):
        for bad in (None, 42, {"symbol": "USDC"}):
            with pytest.raises(ValidationError):
                validate_asset(bad, "MainMarket", REGISTRY)

    def test_market_names(self):
        assert validate_market("MainMarket", {"MainMarket"}) == "MainMarket"
        with pytest.raises(ValidationError):
            validate_market("MoonMarket", {"MainMarket"})


# ------------------------------------------------------------- amount bounds

class TestAmountBounds:
    def test_integer_and_decimal_amounts(self):
        assert validate_amount("1", 9) == 10**9
        assert validate_amount("0.5", 9) == 5 * 10**8
        assert validate_amount("12.345678", 6) == 12_345_678

    def test_smallest_unit(self):
        assert validate_amount("0.000000001", 9) == 1

    @pytest.mark.parametrize("bad", [
        "1e18",          # exponent
        "-5", "+5",      # signs
        "0", "0.0",      # zero
        "0.0000000001",  # below one min unit (9 decimals) — no silent rounding
        "1_000",         # separators
        "١٢٣",           # non-ASCII digits
        "NaN", "Infinity", "0x10",
        "1.2.3", "", " ", ".5", "5.",
        "999999999999999999999",  # > 20 digits (and > u64 after scaling)
    ])
    def test_rejects_malformed_amounts(self, bad):
        with pytest.raises(ValidationError):
            validate_amount(bad, 9)

    @pytest.mark.parametrize("bad", [1.5, 100, True, None, [1], {"amt": 1}])
    def test_rejects_non_string_types(self, bad):
        # JSON numbers lose precision and admit 1e18 shapes; strings only.
        with pytest.raises(ValidationError):
            validate_amount(bad, 9)

    def test_u64_overflow_rejected(self):
        with pytest.raises(ValidationError, match="maximum representable"):
            validate_amount("18446744073709.551616", 6)  # u64::MAX + 1 in min units
        assert validate_amount("18446744073709.551615", 6) == 2**64 - 1

    def test_excess_precision_rejected_not_rounded(self):
        with pytest.raises(ValidationError, match="decimal places"):
            validate_amount("1.1234567", 6)


# -------------------------------------------------------------- leverage cap

class TestLeverageCap:
    def test_accepts_sane_multiplier(self):
        assert validate_multiplier(2.5, 3.0) == 2.5

    def test_rejects_above_server_max(self):
        with pytest.raises(ValidationError, match="maximum"):
            validate_multiplier(3.01, 3.0)

    def test_absolute_cap_beats_loose_config(self):
        # Even if config/env is misconfigured to 50x, the absolute cap holds.
        with pytest.raises(ValidationError, match="maximum"):
            validate_multiplier(LEVERAGE_ABSOLUTE_MAX + 0.1, server_max=50.0)
        assert validate_multiplier(LEVERAGE_ABSOLUTE_MAX, server_max=50.0) == LEVERAGE_ABSOLUTE_MAX

    @pytest.mark.parametrize("bad", [1.0, 0.5, 0, -3, float("nan"), float("inf"), "3", True, None])
    def test_rejects_degenerate_values(self, bad):
        with pytest.raises(ValidationError):
            validate_multiplier(bad, 3.0)


# ------------------------------------------------------------------- USD cap

class TestUsdCap:
    def test_within_cap_passes(self):
        validate_usd_cap(100.0, 1000.0, what="x")

    def test_over_cap_rejected(self):
        with pytest.raises(ValidationError, match="cap"):
            validate_usd_cap(1001.0, 1000.0, what="x")

    def test_unpriceable_fails_closed(self):
        # No oracle price != no cap. Degraded feeds must not lift limits.
        with pytest.raises(ValidationError):
            validate_usd_cap(None, 1000.0, what="x")

    def test_cap_disabled_allows_unpriced(self):
        validate_usd_cap(None, 0, what="x")


# ------------------------------------------------------- obligation ownership

class TestCapOwnership:
    def test_owner_match_passes(self):
        check_cap_ownership(ADDR, "0x" + "ee" * 32, ADDR)

    def test_owner_mismatch_rejected(self):
        with pytest.raises(OwnershipError, match="not owned by"):
            check_cap_ownership(ADDR, "0x" + "ee" * 32, OTHER)

    def test_shared_or_missing_object_rejected(self):
        # Shared/immutable/nonexistent objects have no AddressOwner.
        with pytest.raises(OwnershipError, match="not an address-owned"):
            check_cap_ownership(ADDR, "0x" + "ee" * 32, None)

    def test_case_variant_owner_still_matches(self):
        check_cap_ownership(ADDR, "0x" + "ee" * 32, ADDR.upper().replace("0X", "0x"))


# ------------------------------------------------------------------- base64

class TestBase64:
    def test_valid(self):
        assert validate_base64("AAAA", what="x") == "AAAA"

    @pytest.mark.parametrize("bad", ["", "not base64!!!", None, 5, "A" * (300 * 1024)])
    def test_rejects(self, bad):
        with pytest.raises(ValidationError):
            validate_base64(bad, what="x")
