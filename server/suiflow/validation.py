"""The security boundary.

Every tool argument passes through here before anything is built or stored.
Treat all inputs as coming from an untrusted client: the LLM layer can be
prompted into sending malformed, adversarial, or simply wrong values, and the
system prompt on the assistant side is NOT a security control.

Pure functions only (no I/O) so the whole boundary is unit-testable offline;
the one on-chain check (obligation cap ownership) is expressed as a pure
check over data the caller fetched (`check_cap_ownership`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .config import LEVERAGE_ABSOLUTE_MAX

# Sui addresses and object IDs: 0x + exactly 64 lowercase hex chars after
# normalization. Anything else (short forms, ENS-ish strings, whitespace
# smuggling) is rejected outright.
_HEX32_RE = re.compile(r"^0x[0-9a-f]{64}$")

# Amounts: plain decimal string, no sign, no exponent, no whitespace, no
# locale separators. "1", "0.5", "12.345678" — nothing else. This closes the
# usual float/JSON coercion holes ("1e18", "Infinity", "-1", "0x10", "1_000").
_AMOUNT_RE = re.compile(r"^(?P<int>[0-9]{1,20})(\.(?P<frac>[0-9]{1,20}))?$")

U64_MAX = 2**64 - 1


class ValidationError(ValueError):
    """Input rejected by the validation layer. Message is safe to show."""


class OwnershipError(ValidationError):
    """Obligation cap exists but is not owned by the claimed user."""


@dataclass(frozen=True)
class AssetInfo:
    symbol: str
    coin_type: str
    decimals: int
    market: str


def validate_address(value: object, *, what: str = "user_address") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string")
    v = value.strip().lower()
    if not _HEX32_RE.match(v):
        raise ValidationError(f"{what} must be a 0x-prefixed 32-byte Sui address")
    return v


def validate_object_id(value: object, *, what: str = "object_id") -> str:
    # Same shape as an address; kept separate for clearer error messages.
    return validate_address(value, what=what)


def validate_market(value: object, known_markets: set[str]) -> str:
    if not isinstance(value, str):
        raise ValidationError("market must be a string")
    v = value.strip()
    if v not in known_markets:
        raise ValidationError(f"unknown market {v!r}; expected one of {sorted(known_markets)}")
    return v


def validate_asset(
    symbol: object,
    market: str,
    registry: dict[tuple[str, str], AssetInfo],
    allowlist: frozenset[str] = frozenset(),
) -> AssetInfo:
    """Resolve a (market, symbol) pair against the server-side registry.

    The registry is built from the protocol's own market config (via the
    sidecar); `allowlist` optionally narrows it further. Free-text coin types
    are never accepted from the tool layer — symbols only, resolved here.
    """
    if not isinstance(symbol, str):
        raise ValidationError("asset must be a string symbol like 'USDC'")
    sym = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{2,20}", sym):
        raise ValidationError(f"asset symbol {symbol!r} is not a valid symbol")
    if allowlist and sym not in allowlist:
        raise ValidationError(f"asset {sym} is not enabled on this server")
    info = registry.get((market, sym))
    if info is None:
        available = sorted(s for (m, s) in registry if m == market)
        raise ValidationError(f"asset {sym} is not available in {market}; available: {available}")
    return info


def validate_amount(
    value: object,
    decimals: int,
    *,
    what: str = "amount",
) -> int:
    """Parse a human-units decimal string into integer min-units.

    Strict by construction: string input only (JSON numbers lose precision
    and accept 1e18/NaN shapes), regex-limited charset, positive, within u64,
    and no sub-minimal-unit dust (0.0000001 SUI with 9 decimals is fine;
    0.0000000001 is not representable and is rejected rather than rounded —
    silent rounding is how a "repay everything" turns into "repay almost
    everything, keep paying interest").
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValidationError(f"{what} must be a decimal string like \"12.5\"")
    v = value.strip()
    m = _AMOUNT_RE.match(v)
    if not m:
        raise ValidationError(
            f"{what} {value!r} is not a plain decimal amount (no signs, exponents, or separators)"
        )
    frac = m.group("frac") or ""
    if len(frac) > decimals:
        raise ValidationError(
            f"{what} has {len(frac)} decimal places; asset supports at most {decimals}"
        )
    try:
        scaled = Decimal(v).scaleb(decimals)
    except InvalidOperation as e:  # pragma: no cover - regex prevents this
        raise ValidationError(f"{what} could not be parsed") from e
    min_units = int(scaled)
    if min_units <= 0:
        raise ValidationError(f"{what} must be greater than zero")
    if min_units > U64_MAX:
        raise ValidationError(f"{what} exceeds the chain's maximum representable value")
    return min_units


def validate_multiplier(value: object, server_max: float) -> float:
    """Leverage multiplier: bounded (1, min(server_max, ABSOLUTE_MAX)].

    The absolute cap is applied here *again* on purpose — even if config or a
    future caller passes a looser server_max, this function will not return
    more than LEVERAGE_ABSOLUTE_MAX. Rationale for the numbers: config.py.
    """
    if isinstance(value, bool):
        raise ValidationError("target_multiplier must be a number")
    if not isinstance(value, (int, float)):
        raise ValidationError("target_multiplier must be a number")
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValidationError("target_multiplier must be finite")
    if v <= 1.0:
        raise ValidationError("target_multiplier must be greater than 1.0")
    hard_max = min(server_max, LEVERAGE_ABSOLUTE_MAX)
    if v > hard_max:
        raise ValidationError(
            f"target_multiplier {v} exceeds this server's maximum of {hard_max}"
        )
    return v


def validate_usd_cap(est_usd: float | None, max_usd: float, *, what: str) -> None:
    """Enforce the per-proposal USD ceiling.

    Fail-closed: if the oracle price was unavailable (est_usd is None) and a
    cap is configured, the proposal is rejected — an attacker who can degrade
    the price feed must not thereby lift the cap.
    """
    if max_usd <= 0:
        return
    if est_usd is None:
        raise ValidationError(
            f"{what}: could not price the amount in USD to enforce the "
            f"${max_usd:,.0f} per-proposal cap; try again shortly"
        )
    if est_usd > max_usd:
        raise ValidationError(
            f"{what}: ~${est_usd:,.0f} exceeds this server's ${max_usd:,.0f} per-proposal cap"
        )


def check_cap_ownership(
    user_address: str,
    cap_id: str,
    onchain_owner: str | None,
) -> None:
    """The obligation ownership check.

    `onchain_owner` is the live owner field of the ObligationOwnerCap object
    (fetched by the caller). The SQLite cache is a convenience index only —
    ownership is always decided by chain state, so a cap that was transferred
    away stops validating immediately.
    """
    if onchain_owner is None:
        raise OwnershipError(
            f"obligation cap {cap_id} is not an address-owned object (wrong ID?)"
        )
    if validate_address(onchain_owner, what="cap owner") != user_address:
        raise OwnershipError(
            f"obligation cap {cap_id} is not owned by {user_address}"
        )


def validate_base64(value: object, *, what: str, max_bytes: int = 256 * 1024) -> str:
    import base64
    import binascii

    if not isinstance(value, str) or not value:
        raise ValidationError(f"{what} must be a non-empty base64 string")
    v = value.strip()
    if len(v) > max_bytes:
        raise ValidationError(f"{what} is too large")
    try:
        base64.b64decode(v, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValidationError(f"{what} is not valid base64") from e
    return v
