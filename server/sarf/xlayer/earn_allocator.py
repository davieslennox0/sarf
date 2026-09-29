"""Earn: one asset in, a blend of venues behind the scenes.

WHAT THE USER SEES
    A savings card. Their asset, a blended rate, their balance, what it has
    earned, and two buttons. No pools, no thresholds, no venue names. The
    allocation is Sarf's job, not theirs.

WHAT THAT OBLIGES US TO DO
    Hiding the machinery raises the bar on honesty about it, so three rules
    hold everywhere in this module:

    1. Only venues that have been verified on-chain and are currently passing
       their own gate are eligible. A pool the backtest says never paid for
       itself is not quietly included because it posts a big fee number.
    2. Every rate is measured, and what KIND of number it is travels with it.
       A liquidity fee is a yield. A gold price change is not. Blending the
       two into one "APY" without saying so would flatter the product, so the
       breakdown names each component's kind and the summary says what is in
       the blend.
    3. Nothing here is a forecast, and the copy says so in those words.

VENUES, AND WHY EACH IS ALLOWED
    - Zap LP legs. Eligible only at a threshold its own backtest cleared
      (see zap.pool_viability). A pool where no threshold beat holding is
      excluded outright.
    - Aave V3 on X Layer, USDT0 and USDG. Both confirmed in the pool's own
      getReservesList on 2026-09-29. The rate is read live from the reserve.
    - PAXGy, bought and held through the USDG/PAXGy V3 pool only. There is no
      mint or redeem: those are issuer-only and need KYC. PAXGy exposes NO
      on-chain accrual rate — no convertToAssets, no pricePerShare, nothing —
      so its return cannot be observed as a yield at all, only as a price
      change that mixes the gold price with whatever the issuer has accrued.
      It is carried as price exposure and labelled that way.

EVERYTHING IS MEASURED IN THE DEPOSIT ASSET
    The user deposits SPCXx and withdraws SPCXx, so that is the unit. A leg
    held in dollars is worth fewer SPCXx when SPCXx rises, and the balance
    shows it. That is not a rounding detail — it is most of why this balance
    can fall — and `RISK_FAQ` says so.

OFF BY DEFAULT
    settings.earn_allocator_enabled is false. With it off, `summary()` returns
    a disabled marker and nothing else in here runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import settings

log = logging.getLogger("sarf.earn")

# Verified on X Layer on 2026-09-29 against the contracts themselves.
USDT0 = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
USDG = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
PAXGY = "0x6c6494fd9962eb98b94ffa48f6679058f820700e"
# The one pool PAXGy may be traded through, and its fee tier.
PAXGY_POOL = "0x568e1aae314fc7e5e624dc47a505fc857d411528"
PAXGY_POOL_FEE = 500
# Uniswap V3 periphery on X Layer (uniswap's own deployment page, then checked
# on chain). Used for the PAXGy leg so the route cannot wander off that pool.
V3_ROUTER = "0x4f0c28f5926afda16bf2506d5d9e57ea190f9bca"

# What a kind of venue is allowed to be, at most, of one deposit. Caps rather
# than a chosen split: the allocator fills from the best measured rate down,
# and these stop any single kind of risk from becoming the whole position.
CAPS = {"lp": 0.50, "lending": 0.60, "gold": 0.20}
# Below this a leg is not worth its own transactions.
MIN_LEG_FRACTION = 0.05

# A price-exposure venue needs at least this much history before its return is
# allowed to mean anything. PAXGy launched on 2026-09-26 and had NINE daily
# closes when this was written; annualising a nine-day price move produced
# -77.8%, which is not a rate, it is a rounding error with a percent sign. A
# venue under this bar is reported as unmeasured and gets no allocation.
MIN_HISTORY_DAYS_FOR_RATE = 21


@dataclass(frozen=True)
class Venue:
    key: str
    kind: str                  # "lp" | "lending" | "gold"
    label: str                 # plain words, for the disclosure only
    rate_pct: float | None     # annualised, measured
    rate_kind: str             # "yield" | "price exposure"
    basis: str                 # how the rate was measured, in one sentence
    weight: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "label": self.label,
                "rate_pct": self.rate_pct, "rate_kind": self.rate_kind,
                "basis": self.basis, "weight_pct": round(self.weight * 100, 1),
                **({"detail": self.detail} if self.detail else {})}


# The FAQ. Required, not optional, and the wording is the product's — the only
# thing this module does with it is refuse to ship without it.
RISK_FAQ: list[dict[str, str]] = [
    {"q": "How does this earn?",
     "a": "Your deposit is spread across a few strategies: fees from providing "
          "liquidity to tokenized-stock pools, interest from lending "
          "stablecoins, and tokenized gold. Sarf picks the mix and moves it "
          "when the numbers change."},
    {"q": "Can my balance go down?",
     "a": "Yes. When prices move sharply, your SPCXx balance can fall. Sarf's "
          "engine watches your position and rebalances to limit this, but "
          "sudden one-day moves can't be avoided."},
    {"q": "What are the other risks?",
     "a": "Some of the tokens in these pools charge a tax on every transfer, "
          "which comes out of what you get back. The gold part is PAXGy, whose "
          "value can fall if one of the issuer's borrowers defaults; it can be "
          "bought and sold here but not minted or redeemed, which is "
          "issuer-only and needs identity checks."},
    {"q": "Where are my funds held?",
     "a": "In your own wallet. Sarf never takes custody, and every move is a "
          "transaction you approve."},
]


def _rate_or_zero(v: Venue) -> float:
    return v.rate_pct or 0.0


# No single venue may hold more than this, whatever its rate looks like. The
# per-KIND caps above bound a type of risk; this bounds a single point of
# failure inside it.
PER_VENUE_MAX = 0.30


def allocate(venues: list[Venue]) -> list[Venue]:
    """Best measured rate first, bounded by a cap per kind and per venue; the
    remainder stays in the deposit asset.

    Deliberately dull. A cleverer optimiser would need a risk model nobody
    here could defend, and the caps do the real work: no kind of risk and no
    single venue becomes the whole position, whatever its rate says this week.
    """
    eligible = sorted((v for v in venues if v.rate_pct is not None),
                      key=_rate_or_zero, reverse=True)
    remaining = 1.0
    per_kind: dict[str, float] = {}
    out: list[Venue] = []
    for v in eligible:
        room = min(CAPS.get(v.kind, 0.0) - per_kind.get(v.kind, 0.0),
                   PER_VENUE_MAX, remaining)
        if room < MIN_LEG_FRACTION:
            continue
        per_kind[v.kind] = per_kind.get(v.kind, 0.0) + room
        remaining -= room
        out.append(Venue(**{**v.__dict__, "weight": room}))
        if remaining < MIN_LEG_FRACTION:
            break
    if remaining > 1e-9:
        # Held as the deposit asset. Named as a leg so the weights always sum
        # to one and "we could not place all of it" is visible rather than
        # implied by arithmetic that does not add up.
        out.append(Venue(
            key="hold", kind="cash", label="held as your deposit asset",
            rate_pct=0.0, rate_kind="yield",
            basis="not placed in any venue: either the caps were reached or too "
                  "few venues passed their gate",
            weight=remaining))
    return out


def unavailable(venues: list[Venue]) -> list[dict[str, Any]]:
    """Venues that exist but are not holding anything, and why. The summary
    carries this so a thin allocation always comes with its reason."""
    return [v.as_dict() for v in venues if v.rate_pct is None]


def blended_rate(legs: list[Venue]) -> dict[str, Any]:
    """The one number on the card, plus what is inside it.

    Yield and price exposure are summed into the headline because that is what
    the position actually did, and then named separately so the headline can
    never be mistaken for interest.
    """
    total = sum(leg.weight for leg in legs) or 1.0
    blended = sum((leg.rate_pct or 0.0) * leg.weight for leg in legs) / total
    y = sum((leg.rate_pct or 0.0) * leg.weight for leg in legs
            if leg.rate_kind == "yield") / total
    p = blended - y
    return {
        "blended_apy_pct": round(blended, 2),
        "from_yield_pct": round(y, 2),
        "from_price_exposure_pct": round(p, 2),
        "label": "variable, based on the last 30 days, not a forecast",
        "composition_note": (
            "Most of this is yield: liquidity fees and lending interest, measured "
            "over the last 30 days. The rest is price exposure — the gold part earns "
            "nothing observable on-chain, so what it contributed is its price change, "
            "not interest. Neither half predicts the next 30 days."
            if p else
            "All of this is measured yield: liquidity fees and lending interest over "
            "the last 30 days. It is not a forecast."),
    }


async def _aave_supply_apy(rpc_mod, pool: str, reserve: str) -> float | None:
    """Live supply APY for one Aave reserve, from the reserve itself."""
    from .zap import RAY, _calldata
    try:
        raw = await rpc_mod._call("eth_call", [{"to": pool, "data": _calldata(
            "getReserveData(address)", ["address"], [reserve])}, "latest"])
        words = [int(raw[2 + i:2 + i + 64], 16) for i in range(0, len(raw) - 2, 64)]
        apr = words[2] / RAY                      # currentLiquidityRate, per year in ray
        return round(((1 + apr / 31536000) ** 31536000 - 1) * 100, 3)
    except Exception:
        log.warning("aave reserve %s unreadable", reserve, exc_info=True)
        return None


def _annualise(series_a: list[float], series_b: list[float]) -> tuple[float | None, int]:
    """Return of A measured in B, annualised, with the days it rests on.

    B is the deposit asset, so this answers the only question the user cares
    about: how many more (or fewer) SPCXx would this leg be worth.
    """
    n = min(len(series_a), len(series_b))
    if n < 2:
        return None, n
    days = n - 1
    start, end = series_a[0] / series_b[0], series_a[-1] / series_b[-1]
    if start <= 0:
        return None, days
    total = end / start - 1
    if days < MIN_HISTORY_DAYS_FOR_RATE:
        return None, days
    return round(((1 + total) ** (365 / days) - 1) * 100, 2), days


async def build_venues(engine, venues_cfg: dict[str, Any], rpc_mod) -> list[Venue]:
    """Every venue that is currently allowed to hold a deposit, with its
    measured rate. A venue that cannot be measured is returned with a null
    rate so the reason is visible, and `allocate` gives it nothing."""
    out: list[Venue] = []

    # --- zap LP legs, only where their own backtest passed ----------------
    for pool in engine.pools.values():
        try:
            v = await engine.pool_viability(pool)
        except Exception:
            # NOT silently skipped. A venue that cannot be measured is listed
            # with a null rate, which keeps it out of the allocation while
            # leaving the reason on the record. Dropping it instead would let
            # the product quietly become a lending-only account while the copy
            # still promised liquidity fees — which is exactly what happened
            # the first time this ran without a price source.
            log.warning("pool %s could not be judged", pool.key, exc_info=True)
            out.append(Venue(
                key=f"lp:{pool.key}", kind="lp",
                label="fees from a tokenized-stock liquidity pool",
                rate_pct=None, rate_kind="yield",
                basis="could not be measured just now: its price or volume was unreadable, "
                      "so it is left out of the allocation rather than guessed at",
                detail={"pool": pool.key, "unavailable": True}))
            continue
        protects = v.get("protects_from_bps")
        if v.get("basis_of_gate") == "backtest" and protects is None:
            # No threshold ever beat holding. Excluded, and said so.
            out.append(Venue(
                key=f"lp:{pool.key}", kind="lp",
                label="fees from a tokenized-stock liquidity pool",
                rate_pct=None, rate_kind="yield",
                basis="excluded: replaying the exit policy over this pair's own prices, no "
                      "threshold beat simply holding, so its fees did not cover its costs",
                detail={"pool": pool.key, "excluded_by_backtest": True}))
            continue
        if v.get("fee_apy_pct") is None:
            out.append(Venue(
                key=f"lp:{pool.key}", kind="lp",
                label="fees from a tokenized-stock liquidity pool",
                rate_pct=None, rate_kind="yield",
                basis="still being measured: not enough of this pool's own swap history yet",
                detail={"pool": pool.key, "measuring": True}))
            continue
        out.append(Venue(
            key=f"lp:{pool.key}", kind="lp",
            label="fees from a tokenized-stock liquidity pool",
            rate_pct=v["fee_apy_pct"], rate_kind="yield",
            basis=("trading fees measured over the last 7 days, entered at the exit "
                   f"threshold its own backtest cleared ({(protects or 0) / 100:g}%)"),
            detail={"pool": pool.key, "exit_threshold_bps": protects,
                    "confidence": v.get("confidence"),
                    "round_trip_cost_pct": v.get("round_trip_cost_pct")}))

    # --- Aave V3, both confirmed reserves ---------------------------------
    aave = venues_cfg.get("aave") or {}
    for sym, addr in (aave.get("reserves") or {}).items():
        apy = await _aave_supply_apy(rpc_mod, aave["pool"], addr)
        out.append(Venue(
            key=f"aave:{sym}", kind="lending", label="interest from lending stablecoins",
            rate_pct=apy, rate_kind="yield",
            basis="the supply rate Aave V3 is paying on this reserve right now",
            detail={"asset": sym, "reserve": addr}))

    # --- PAXGy, price exposure only ---------------------------------------
    pg, dep = venues_cfg.get("paxgy") or {}, venues_cfg.get("deposit_asset") or {}
    rate, days = _annualise(pg.get("usd") or [], dep.get("usd") or [])
    out.append(Venue(
        key="gold:PAXGy", kind="gold", label="tokenized gold",
        rate_pct=rate, rate_kind="price exposure",
        basis=(f"its price change against the deposit asset over {days} days, annualised. "
               "PAXGy publishes no on-chain accrual rate, so this is price movement, "
               "not interest"
               if rate is not None else
               f"only {days} days of price history, under the {MIN_HISTORY_DAYS_FOR_RATE} "
               "needed before a price change is worth annualising, so it is left unmeasured "
               "and unallocated"),
        detail={"pool": pg.get("pool"), "pool_fee": pg.get("pool_fee"),
                "days_of_history": days, "no_mint_or_redeem": pg.get("route_note")}))
    return out


def disabled_summary() -> dict[str, Any]:
    return {"enabled": False,
            "detail": "EARN_ALLOCATOR_ENABLED is off; no allocation is offered.",
            "risk_faq": RISK_FAQ}


def enabled() -> bool:
    return bool(settings.earn_allocator_enabled)
