"""Replay the zap's own exit/re-entry policy over real price history.

WHY THIS REPLACED A FORMULA
    The gate this supersedes reasoned about a pool from two numbers: how
    volatile the pair is, and how much a round trip costs. That is a model of
    the policy, and it answered a question nobody asked — "how often would a
    random walk trip this threshold" — while ignoring what the exits were
    worth. Here the policy is simply run: day by day, over the prices that
    actually happened, paying what an exit actually costs and earning what
    Aave actually pays, and the result is compared against doing nothing.

WHAT IS SIMULATED
    One dollar, in at day zero, out at the end, under three policies:

      A  the zap: in the pool earning fees; when IL crosses the exit
         threshold, sell to USDT (paying the exit cost), park in Aave; when
         IL recovers to the re-entry threshold, buy back in (paying the
         entry cost).
      B  the same LP, held the whole period, no exits.
      C  the RWA alone, held the whole period.

    A pool "passes" at a threshold when A >= B: the exits earned their keep
    against simply staying in. C is reported alongside because a user who is
    told an LP strategy beat another LP strategy still deserves to see what
    holding the share would have done.

WHAT IT IS NOT
    Not a forecast. Thirty days of one pair's history, with no claim that the
    next thirty look like the last. A threshold that passes here is one that
    would have been worth setting over a period that is already over, and
    every caller is expected to say so.

MODELLING NOTES, ALL OF THEM CONSERVATIVE
    - LP value uses the constant-product identity  V = 2*sqrt(a*b), where a
      and b are what each half would be worth if simply held. That is exact
      for a 50/50 pool ex-fees, and it is where impermanent loss comes from
      in the first place.
    - Fees accrue only while in the pool, at the measured 7-day rate. A pool
      that was busier than usual last week will look better here than it
      deserves; that is the one place this leans optimistic, and it leans the
      same way for A and B.
    - Exits crystallise nothing by hand: leaving converts the LP to dollars at
      that day's prices, which IS the loss being realised, and re-entry buys
      back at the prices of the day it happens.
    - Costs are the pool's own measured exit and entry costs, charged per
      event rather than per round trip, so a run that ends parked is not
      charged for a re-entry it never made.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# A pool needs at least this much paired history before a backtest is worth
# believing; below it the caller falls back to the volatility formula.
MIN_HISTORY_DAYS = 14


def il_fraction(ratio: float) -> float:
    """Impermanent loss of a 50/50 pool for a price ratio change, as a
    negative fraction. ratio = (rwa/other now) / (rwa/other at entry)."""
    if ratio <= 0:
        return 0.0
    return 2 * math.sqrt(ratio) / (1 + ratio) - 1


@dataclass(frozen=True)
class Run:
    threshold_bps: int
    reentry_bps: int
    zap: float           # A, net return as a fraction
    lp_hold: float       # B
    rwa_hold: float      # C
    exits: int
    days_in_pool: int
    days_parked: int

    # A threshold that never fires computes the same path twice, once by
    # repeated multiplication and once by exponentiation, so the two can
    # differ in the last bits of a float. Without a tolerance that dust
    # decides whether a user is allowed to deposit.
    TIE = 1e-9

    @property
    def passes(self) -> bool:
        return self.zap >= self.lp_hold - self.TIE

    @property
    def fired(self) -> bool:
        """Did the policy actually do anything?

        A threshold IL never reached costs nothing and saves nothing, so A and
        B come out identical and it "passes" on a technicality. That is worth
        knowing rather than dressing up as protection.
        """
        return self.exits > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "threshold_bps": self.threshold_bps,
            "reentry_bps": self.reentry_bps,
            "zap_net_pct": round(self.zap * 100, 2),
            "lp_hold_net_pct": round(self.lp_hold * 100, 2),
            "rwa_hold_net_pct": round(self.rwa_hold * 100, 2),
            "exits": self.exits,
            "days_in_pool": self.days_in_pool,
            "days_parked": self.days_parked,
            "passes": self.passes,
            "fired": self.fired,
            "confidence": confidence(self.days_in_pool + self.days_parked, self.exits),
        }


def run_policy(rwa_usd: list[float], other_usd: list[float], *, threshold_bps: int,
               reentry_bps: int | None = None, fee_apy_pct: float, aave_apy_pct: float,
               exit_cost_bps: int, entry_cost_bps: int) -> Run:
    """One pass of the policy over one price history. Pure arithmetic."""
    n = min(len(rwa_usd), len(other_usd))
    if n < 2:
        raise ValueError("need at least two paired closes")
    reentry = reentry_bps if reentry_bps is not None else threshold_bps // 2
    fee_daily = fee_apy_pct / 100 / 365
    aave_daily = aave_apy_pct / 100 / 365
    exit_cost = exit_cost_bps / 10000
    entry_cost = entry_cost_bps / 10000

    r0, o0 = rwa_usd[0], other_usd[0]
    entry_ratio = r0 / o0

    # Half a dollar into each leg at day 0; units stay fixed for the hold
    # baselines, which is what makes them baselines.
    rwa_units, other_units = 0.5 / r0, 0.5 / o0
    rwa_only_units = 1.0 / r0

    def lp_value(i: int) -> float:
        a, b = rwa_units * rwa_usd[i], other_units * other_usd[i]
        return 2 * math.sqrt(a * b)

    # --- A: the policy ---------------------------------------------------
    in_pool = True
    # Value is carried as a multiplier on the LP path while in the pool, and
    # as dollars while parked, so re-entry can be priced at the day it happens.
    pool_mult = 1.0          # fees and costs accumulated on the LP leg
    parked_usd = 0.0
    entry_index = 0
    exits = 0
    days_in = 0
    days_parked = 0

    for i in range(1, n):
        if in_pool:
            days_in += 1
            pool_mult *= 1 + fee_daily
            ratio = (rwa_usd[i] / other_usd[i]) / entry_ratio
            if -il_fraction(ratio) * 10000 >= threshold_bps:
                # Leave: the LP becomes dollars at today's prices, less the
                # exit cost. Nothing is "crystallised" separately — this is it.
                value_now = lp_value(i) / lp_value(entry_index) * pool_mult
                parked_usd = value_now * (1 - exit_cost)
                in_pool, pool_mult = False, 1.0
                exits += 1
        else:
            days_parked += 1
            parked_usd *= 1 + aave_daily
            ratio = (rwa_usd[i] / other_usd[i]) / entry_ratio
            if -il_fraction(ratio) * 10000 <= reentry:
                # Back in, at today's prices, less the entry cost.
                parked_usd *= 1 - entry_cost
                entry_index = i
                pool_mult = parked_usd      # carry the dollars onto the LP leg
                parked_usd = 0.0
                in_pool = True

    if in_pool:
        zap_final = lp_value(n - 1) / lp_value(entry_index) * pool_mult
    else:
        zap_final = parked_usd

    # --- B: the same LP, left alone --------------------------------------
    lp_final = lp_value(n - 1) * (1 + fee_daily) ** (n - 1)

    # --- C: the RWA alone -------------------------------------------------
    rwa_final = rwa_only_units * rwa_usd[n - 1]

    return Run(threshold_bps=threshold_bps, reentry_bps=reentry,
               zap=zap_final - 1, lp_hold=lp_final - 1, rwa_hold=rwa_final - 1,
               exits=exits, days_in_pool=days_in, days_parked=days_parked)


def sweep(rwa_usd: list[float], other_usd: list[float], *, fee_apy_pct: float,
          aave_apy_pct: float, exit_cost_bps: int, entry_cost_bps: int,
          low_bps: int = 500, high_bps: int = 3500, step_bps: int = 100) -> list[Run]:
    """Every threshold from low to high, inclusive."""
    return [
        run_policy(rwa_usd, other_usd, threshold_bps=t, fee_apy_pct=fee_apy_pct,
                   aave_apy_pct=aave_apy_pct, exit_cost_bps=exit_cost_bps,
                   entry_cost_bps=entry_cost_bps)
        for t in range(low_bps, high_bps + 1, step_bps)
    ]


# How much weight the window can bear. One exit is an anecdote: it says what
# happened once, and the same 30 days with the move a day earlier could say
# the opposite. These labels travel with every number so nobody reads a
# single-exit backtest as a measurement.
def confidence(days: int, exits: int) -> str:
    if exits < 3 or days < 21:
        return "low"
    if exits < 10 or days < 30:
        return "medium"
    return "high"


def confidence_note(days: int, exits: int) -> str:
    level = confidence(days, exits)
    return {
        "low": (f"Low confidence: {exits} exit{'s' if exits != 1 else ''} in {days} days. "
                "Too little to conclude much — treat it as an anecdote, not a measurement."),
        "medium": (f"Medium confidence: {exits} exits in {days} days. Enough to be "
                   "suggestive, not enough to be sure."),
        "high": (f"Higher confidence: {exits} exits in {days} days, though still one pair "
                 "over one month."),
    }[level]


def every_threshold_loses(runs: list[Run]) -> bool:
    """True only when the policy lost at EVERY threshold tested.

    The one condition that justifies refusing a deposit. Anything short of it
    means some line was worth drawing, and the user gets to draw it.
    """
    return bool(runs) and not any(r.passes for r in runs)


def max_exits(runs: list[Run]) -> int:
    return max((r.exits for r in runs), default=0)


def lowest_passing(runs: list[Run]) -> Run | None:
    """The tightest threshold whose exits beat holding the LP, or None.

    Prefers a threshold that actually fired: one that beat the baseline by
    doing something is evidence, and one that tied by never triggering is
    only the absence of harm. Falls back to the tie if nothing fired, because
    "this never needed to exit" is still a true and useful answer.
    """
    for r in runs:
        if r.passes and r.fired:
            return r
    for r in runs:
        if r.passes:
            return r
    return None
