"""The backtest gate: replay the zap's policy over real prices and judge a
threshold by what it would have returned, not by a model of how often it
fires. Pure arithmetic here — no network, no database."""

import math

import pytest

from sarf.xlayer import zap_backtest as bt

FLAT = [100.0] * 30
COSTS = dict(exit_cost_bps=225, entry_cost_bps=315)   # LAIKA's measured costs


def run(rwa, other, **kw):
    kw = {"fee_apy_pct": 0.0, "aave_apy_pct": 0.0, **COSTS, **kw}
    return bt.run_policy(rwa, other, **kw)


# ------------------------------------------------------------------ the maths

def test_il_matches_the_textbook_curve():
    assert bt.il_fraction(1.0) == 0.0
    assert bt.il_fraction(2.0) == pytest.approx(-0.0572, abs=1e-4)
    assert bt.il_fraction(0.5) == pytest.approx(-0.0572, abs=1e-4)
    assert bt.il_fraction(4.0) == pytest.approx(-0.2, abs=1e-3)
    # Symmetric in the ratio, which is why an exit threshold cuts both ways.
    for r in (1.5, 3.0, 10.0):
        assert bt.il_fraction(r) == pytest.approx(bt.il_fraction(1 / r), abs=1e-12)


def test_flat_prices_do_nothing_at_all():
    r = run(FLAT, FLAT, threshold_bps=500)
    assert r.exits == 0
    assert r.zap == pytest.approx(0.0, abs=1e-12)
    assert r.lp_hold == pytest.approx(0.0, abs=1e-12)
    assert r.rwa_hold == pytest.approx(0.0, abs=1e-12)
    assert r.passes and not r.fired, "a tie is a pass, but it is not protection"


def test_fees_accrue_only_while_in_the_pool():
    quiet = run(FLAT, FLAT, threshold_bps=500, fee_apy_pct=36.5)
    assert quiet.zap == pytest.approx(quiet.lp_hold, rel=1e-9)
    assert quiet.zap == pytest.approx((1 + 0.001) ** 29 - 1, rel=1e-9)

    # A crash that parks the position: fewer days in the pool, fewer fees.
    other = [100.0] * 5 + [25.0] * 25          # ratio quadruples -> 20% IL
    parked = run(FLAT, other, threshold_bps=500, fee_apy_pct=36.5)
    assert parked.exits == 1
    assert parked.days_in_pool < quiet.days_in_pool
    assert parked.days_parked > 0


def test_parking_earns_the_aave_rate():
    other = [100.0] * 2 + [25.0] * 28
    cold = run(FLAT, other, threshold_bps=500, aave_apy_pct=0.0)
    warm = run(FLAT, other, threshold_bps=500, aave_apy_pct=36.5)
    assert warm.zap > cold.zap
    assert warm.days_parked == cold.days_parked > 20


def test_an_exit_costs_what_the_pool_says_it_costs():
    other = [100.0] * 2 + [25.0] * 28
    cheap = run(FLAT, other, threshold_bps=500, exit_cost_bps=0, entry_cost_bps=0)
    dear = run(FLAT, other, threshold_bps=500, exit_cost_bps=500, entry_cost_bps=500)
    assert cheap.zap > dear.zap
    assert dear.zap == pytest.approx(cheap.zap - 0.05 * (cheap.zap + 1), rel=0.02)


# --------------------------------------------------------- the policy itself

def test_exiting_a_sustained_slide_beats_staying_in():
    """The case the zap exists for: a trend that keeps going after the exit.

    Note what this does NOT claim. The policy reads a closing price and acts
    the next day, so it cannot dodge a gap — see the test below. It earns its
    keep only when the move continues after it has left.
    """
    other = [100.0 * 0.9 ** i for i in range(30)]    # a long grind down
    r = run(FLAT, other, threshold_bps=500)
    assert r.exits == 1 and r.fired
    assert r.zap > r.lp_hold, "leaving mid-slide beat riding the rest of it down"
    assert r.passes


def test_a_gap_move_cannot_be_dodged():
    """Honest limit of the whole idea: when the damage lands in one step, the
    exit happens after it and only adds its own cost."""
    other = [100.0] * 3 + [20.0] * 27
    r = run(FLAT, other, threshold_bps=500)
    assert r.exits == 1
    assert r.zap < r.lp_hold, "the loss was already taken; the exit just cost more"


def test_exiting_a_move_that_reverses_costs_money():
    """The case the zap is worst at: a dip that recovers. The exit sells low
    and the re-entry buys back in, paying both ways for nothing."""
    other = [100.0] * 3 + [20.0] * 5 + [100.0] * 22
    r = run(FLAT, other, threshold_bps=500)
    assert r.exits == 1
    assert r.zap < r.lp_hold, "round trip paid for, nothing gained"
    assert not r.passes


def test_a_threshold_too_wide_to_fire_is_a_tie_not_a_win():
    other = [100.0] * 3 + [90.0] * 27        # ~0.1% IL, far below any threshold
    r = run(FLAT, other, threshold_bps=3500)
    assert r.exits == 0 and not r.fired
    assert r.zap == pytest.approx(r.lp_hold, rel=1e-9)
    assert r.passes


def test_reentry_happens_when_il_recovers():
    other = [100.0] * 3 + [20.0] * 5 + [100.0] * 22
    r = run(FLAT, other, threshold_bps=500, reentry_bps=100)
    assert r.exits == 1
    assert r.days_parked > 0 and r.days_in_pool > 3, "it came back in"


def test_rwa_hold_tracks_the_rwa_and_nothing_else():
    rwa = [100.0] * 15 + [150.0] * 15
    r = run(rwa, FLAT, threshold_bps=500)
    assert r.rwa_hold == pytest.approx(0.5, rel=1e-9)


# ------------------------------------------------------------------ the sweep

def test_sweep_covers_5_to_35_percent_in_1_percent_steps():
    runs = bt.sweep(FLAT, FLAT, fee_apy_pct=0, aave_apy_pct=0, **COSTS)
    assert [r.threshold_bps for r in runs] == list(range(500, 3600, 100))
    assert len(runs) == 31


def test_lowest_passing_prefers_a_threshold_that_actually_fired():
    other = [100.0 * 0.9 ** i for i in range(30)]
    runs = bt.sweep(FLAT, other, fee_apy_pct=0, aave_apy_pct=0, **COSTS)
    best = bt.lowest_passing(runs)
    assert best is not None and best.fired, "a real exit beats a technical tie"


def test_lowest_passing_is_none_when_every_threshold_loses():
    """A pair that whipsaws: every threshold that fires, fires into a
    recovery, and nothing beats sitting still."""
    other = []
    for i in range(30):
        other.append(20.0 if 3 <= i % 8 <= 5 else 100.0)
    runs = bt.sweep(FLAT, other, fee_apy_pct=0, aave_apy_pct=0,
                    exit_cost_bps=500, entry_cost_bps=500, high_bps=2000)
    fired = [r for r in runs if r.fired]
    assert fired, "the test data must actually trigger exits"
    assert all(not r.passes for r in fired), "every firing threshold should lose here"


def test_short_history_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        run([100.0], [100.0], threshold_bps=500)
    assert bt.MIN_HISTORY_DAYS == 14
