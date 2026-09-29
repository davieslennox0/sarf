"""Pool gating at the engine level.

Two paths, and which one runs depends on how much price history a pool has:
a backtest of the real policy where there is enough to replay, and the old
volatility formula as a fallback below 14 days. Nothing here touches the
network; the pure backtest arithmetic lives in test_zap_backtest.py."""

import asyncio
import time

import pytest

from sarf.db import Database
from sarf.validation import ValidationError
from sarf.xlayer.registry import registry
from sarf.xlayer.zap import ZapEngine

DAY = 86400


@pytest.fixture()
def engine():
    return ZapEngine(Database(":memory:"), None, registry())


def _fill(engine, pool_key, *, daily_usd, days):
    today = int(time.time() // DAY)
    for i in range(1, days + 1):
        engine.db.set_zap_pool_volume(pool_key, today - i, daily_usd, 10)


def _short_history(engine, pool):
    """Strip a pool's price history so the gate falls back to the formula."""
    engine.history[pool.key.upper()] = {"rwa_usd": [1.0] * 5, "other_usd": [1.0] * 5}


def _state(engine, pool, price, tvl_usd):
    """A pool_state/rwa_usd pair that produces the TVL we want to test at."""
    reserve = (tvl_usd / 2 / price) * 10 ** pool.rwa.decimals
    engine.pool_state = lambda p: _async({"reserve_rwa": reserve, "reserve_other": reserve,
                                          "total_supply": 1, "price": price})
    engine.rwa_usd = lambda p: _async(price)


def _async(v):
    async def f():
        return v
    return f()


def test_a_thin_pool_is_gated_and_says_why(engine):
    pool = engine.pool("STERLING-wNVDAx")
    _short_history(engine, pool)
    # $100/day of volume on a $70k pool: 0.3% of that is $0.30 a day.
    _fill(engine, pool.key, daily_usd=100.0, days=7)
    _state(engine, pool, price=230.0, tvl_usd=70_000)

    v = asyncio.run(engine.pool_viability(pool))
    assert v["basis_of_gate"] == "volatility formula"
    assert v["gated"] is True and v["viable"] is False
    assert v["fee_apy_pct"] is not None and v["fee_apy_pct"] < v["hurdle_pct"]
    assert v["expected_exits_per_year"] > 1
    assert "times a year" in v["reason"] and "falls" in v["reason"]
    assert v["label"] == "fees only, incentives ended"


def test_a_pool_that_earns_its_keep_is_open(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _short_history(engine, pool)
    # ~$180k/day on a $205k pool is roughly what this pool actually did.
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)

    v = asyncio.run(engine.pool_viability(pool))
    assert v["viable"] is True and v["gated"] is False
    assert v["basis_of_gate"] == "volatility formula"
    assert v["fee_apy_pct"] > v["hurdle_pct"] > v["round_trip_cost_pct"]
    assert v["days_measured"] == 7
    assert v["exit_threshold_bps"] == engine.DEFAULT_EXIT_THRESHOLD_BPS


def test_il_threshold_is_converted_to_the_move_that_trips_it(engine):
    """8% IL needs a ratio change of ~+129%/-56%, not 8%. Dividing the
    threshold by volatility directly would count exits by the thousand."""
    import math
    m = engine.move_for_il(800)
    assert 0.80 < m < 0.86
    assert abs((math.exp(m) - 1) - 1.289) < 0.01     # +128.9%
    assert abs((math.exp(-m) - 1) + 0.563) < 0.01    # -56.3%
    # Monotone: a wider threshold takes a bigger move, so fewer exits.
    assert engine.move_for_il(100) < engine.move_for_il(800) < engine.move_for_il(2000)


def test_a_tighter_threshold_raises_the_hurdle_and_can_gate_a_pool(engine):
    """The same pool, the same fees: what changes is how often the user's own
    threshold is tripped, and so how much churn the fees have to cover."""
    pool = engine.pool("LAIKA-wSPCXx")
    _short_history(engine, pool)
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)

    wide = asyncio.run(engine.pool_viability(pool, threshold_bps=800))
    tight = asyncio.run(engine.pool_viability(pool, threshold_bps=500))
    assert tight["expected_exits_per_year"] > wide["expected_exits_per_year"]
    assert tight["hurdle_pct"] > wide["hurdle_pct"]
    assert wide["viable"] is True and tight["viable"] is False


def test_a_volatile_pair_has_to_earn_far_more(engine):
    """IGNIX moves 34%/day against 16% for LAIKA, so it trips the same
    threshold ~4.5x as often and its hurdle is multiples higher."""
    laika = engine.expected_exits_per_year(engine.pool("LAIKA-wSPCXx"))
    ignix = engine.expected_exits_per_year(engine.pool("IGNIX-wSPCXx"))
    assert ignix > 4 * laika


def test_the_window_is_seven_days_and_older_volume_is_ignored(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    today = int(time.time() // DAY)
    engine.db.set_zap_pool_volume(pool.key, today - 1, 1_000.0, 5)
    engine.db.set_zap_pool_volume(pool.key, today - 30, 9_999_999.0, 5)  # last month's boom
    _state(engine, pool, price=147.25, tvl_usd=205_000)

    y = asyncio.run(engine.pool_yield(pool))
    assert y["volume_usd"] == 1_000.0, "a month-old week must not prop up today's number"
    assert y["window_days"] == 7


def test_an_unmeasured_volatility_is_not_gated_on(engine):
    """No measured volatility, no opinion about how often it would churn."""
    pool = engine.pool("LAIKA-wSPCXx")
    _short_history(engine, pool)
    object.__setattr__(pool, "daily_sigma", None)
    _fill(engine, pool.key, daily_usd=1.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    v = asyncio.run(engine.pool_viability(pool))
    assert v["viable"] is True and v["gated"] is False
    assert v["expected_exits_per_year"] is None
    assert "nothing is judged" in v["reason"]


def test_an_unmeasured_pool_is_not_gated(engine):
    """Absence of evidence is not evidence of a dead pool: with too little
    history the APY reads 'measuring' and the pool stays open."""
    pool = engine.pool("IGNIX-wSPCXx")
    _fill(engine, pool.key, daily_usd=1.0, days=2)  # below MIN_COVERAGE_DAYS
    _state(engine, pool, price=147.25, tvl_usd=250_000)

    v = asyncio.run(engine.pool_viability(pool))
    assert v["fee_apy_pct"] is None
    assert v["viable"] is True and v["gated"] is False
    assert "measur" in v["basis"]


def test_apy_scales_by_days_measured_not_by_seven(engine):
    """Six days of data must not be reported as a seventh short."""
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=10_000.0, days=6)
    _state(engine, pool, price=147.25, tvl_usd=100_000)
    six = asyncio.run(engine.pool_yield(pool))["fee_apy_pct"]

    engine.db.prune_zap_pool_volume(int(time.time() // DAY) + 1)
    _fill(engine, pool.key, daily_usd=10_000.0, days=7)
    seven = asyncio.run(engine.pool_yield(pool))["fee_apy_pct"]
    assert six == seven, "same daily rate, same APY, whatever the coverage"


def test_deposits_into_a_gated_pool_are_refused(engine):
    pool = engine.pool("STERLING-wNVDAx")
    _short_history(engine, pool)
    _fill(engine, pool.key, daily_usd=100.0, days=7)
    _state(engine, pool, price=230.0, tvl_usd=70_000)

    with pytest.raises(ValidationError) as e:
        asyncio.run(engine.create(address="0x" + "11" * 20, asset="NVDAx", amount="1",
                                  il_threshold_bps=500, pool_key=pool.key))
    assert "spend on exits" in str(e.value)


def test_a_healthy_pool_still_gets_past_the_gate(engine):
    """The gate must not be the reason a good pool fails; it should fall over
    later, on the balance check, with its own message."""
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine._balance = lambda *a, **k: _async(0)

    # 8% is refused by the backtest here (this pool protected from 13% up), so
    # the threshold that should sail through is a passing one.
    with pytest.raises(Exception) as e:
        asyncio.run(engine.create(address="0x" + "11" * 20, asset="SPCXx", amount="1",
                                  il_threshold_bps=2000, pool_key=pool.key))
    assert "did not pay for itself" not in str(e.value), str(e.value)


def test_a_deposit_is_judged_at_the_users_own_threshold(engine):
    """Same pool, same week of fees: refused at a tight threshold, allowed at
    a wide one, because the tight one is the one that churns."""
    pool = engine.pool("LAIKA-wSPCXx")
    _short_history(engine, pool)
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine._balance = lambda *a, **k: _async(0)

    with pytest.raises(ValidationError) as e:
        asyncio.run(engine.create(address="0x" + "11" * 20, asset="SPCXx", amount="1",
                                  il_threshold_bps=300, pool_key=pool.key))
    assert "did not pay" in str(e.value) or "spend on exits" in str(e.value)
    assert "times a year" in str(e.value)


def test_volume_buckets_are_additive_but_the_backfill_replaces(engine):
    db, key = engine.db, "LAIKA-wSPCXx"
    day = int(time.time() // DAY)
    db.add_zap_pool_volume(key, day, 100.0, 1)
    db.add_zap_pool_volume(key, day, 50.0, 2)
    assert db.zap_pool_volume(key, day)["volume_usd"] == 150.0, "a pass adds to the day"
    db.set_zap_pool_volume(key, day, 400.0, 9)
    assert db.zap_pool_volume(key, day)["volume_usd"] == 400.0, "a backfill replaces it"
    assert db.zap_pool_volume(key, day)["swaps"] == 9


# ------------------------------------------------- the backtest path

def test_a_pool_with_history_is_judged_by_the_backtest_not_the_formula(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool))
    assert v["basis_of_gate"] == "backtest"
    assert v["backtest"]["days_of_history"] >= 14
    assert "not a forecast" in v["reason"]
    assert "not a forecast" in v["backtest"]["caveat"]


def test_the_suggested_threshold_clears_the_one_that_passed(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool))
    if v["protects_from_bps"]:
        assert v["suggested_threshold_bps"] >= v["protects_from_bps"] + 200
        assert v["suggested_threshold_bps"] >= engine.DEFAULT_EXIT_THRESHOLD_BPS


def test_a_threshold_that_never_fired_is_not_called_protection(engine):
    """A pool whose IL never reached the threshold ties with holding. That is
    a pass, but claiming it protects anyone would be a lie."""
    pool = engine.pool("LAIKA-wSPCXx")
    flat = [100.0] * 30
    engine.history[pool.key.upper()] = {"rwa_usd": flat, "other_usd": flat}
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool))
    assert v["viable"] is True
    assert v["protection_demonstrated"] is False
    assert "never reached" in v["reason"] and "would not have cost you" in v["reason"]


def test_a_deposit_is_refused_only_for_the_threshold_that_fails(engine):
    """A pool is never closed outright: the refusal is about the line the
    user drew, and it says what would have been better."""
    pool = engine.pool("LAIKA-wSPCXx")
    # A pair that whipsaws: tight thresholds churn, wide ones sit still.
    other = [100.0 if i % 8 else 55.0 for i in range(30)]
    engine.history[pool.key.upper()] = {"rwa_usd": [100.0] * 30, "other_usd": other}
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})
    engine._balance = lambda *a, **k: _async(0)

    tight = asyncio.run(engine.pool_viability(pool, threshold_bps=500))
    wide = asyncio.run(engine.pool_viability(pool, threshold_bps=3500))
    assert wide["viable"], "a threshold too wide to fire must not be refused"
    if not tight["viable"]:
        with pytest.raises(ValidationError) as e:
            asyncio.run(engine.create(address="0x" + "11" * 20, asset="SPCXx", amount="1",
                                      il_threshold_bps=500, pool_key=pool.key))
        msg = str(e.value)
        assert "did not pay for itself" in msg
        assert "%" in msg and "holding the LP" in msg


# ------------------------------------- refusing only on clear, repeated failure

def _whipsaw(engine, pool, period=8, depth=55.0):
    """Prices that punish every exit: a dip that always recovers."""
    other = [100.0 if i % period else depth for i in range(30)]
    engine.history[pool.key.upper()] = {"rwa_usd": [100.0] * 30, "other_usd": other}


def test_a_single_exit_never_refuses_a_deposit(engine):
    """One exit is an anecdote. It may lose at every threshold and still must
    not close the pool."""
    pool = engine.pool("LAIKA-wSPCXx")
    # One dip, late, that recovers: any threshold that fires, fires once.
    other = [100.0] * 20 + [50.0] * 3 + [100.0] * 7
    engine.history[pool.key.upper()] = {"rwa_usd": [100.0] * 30, "other_usd": other}
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool, threshold_bps=500))
    assert v["exits_observed"] <= 2
    assert v["viable"] is True, "a one-exit result must never refuse"
    assert v["confidence"] == "low"
    if v["backtest"]["best"] is None:
        assert "too thin to call" in v["reason"]


def test_refusal_needs_every_threshold_to_lose_and_enough_exits(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _whipsaw(engine, pool)
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool, threshold_bps=500))
    if v["backtest"]["best"] is None and v["exits_observed"] >= 3:
        assert v["gated"] is True
        assert "never beat staying in" in v["reason"]
        assert v["recommend_no_auto_exit"] is True
    else:
        assert v["gated"] is False


def test_a_losing_threshold_is_allowed_when_another_one_works(engine):
    """Only clear failure refuses. A bad line in a pool that has a good line
    is the user's call, with the numbers shown."""
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool, threshold_bps=800))
    if v["protects_from_bps"] and v["protects_from_bps"] > 800:
        assert v["viable"] is True, "allowed, even though 8% itself lost"
        assert "Allowed either way" in v["reason"]


def test_confidence_is_reported_with_the_evidence_behind_it(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool))
    assert v["confidence"] in ("low", "medium", "high")
    assert str(v["exits_observed"]) in v["confidence_note"]
    assert str(v["backtest"]["days_of_history"]) in v["confidence_note"]


def test_every_response_carries_the_gap_limit(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool))
    assert "sudden gaps" in v["gap_caveat"] and "one-day drop" in v["gap_caveat"]


# ------------------------------------------------------- the no-auto-exit mode

def test_no_auto_exit_is_offered_with_its_own_return(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    v = asyncio.run(engine.pool_viability(pool))
    assert v["no_auto_exit"]["net_pct"] is not None
    assert "never exiting" in v["no_auto_exit"]["what"]


def test_a_zero_threshold_means_the_watcher_leaves_it_alone(engine):
    il, re = engine._validate_thresholds(0, None)
    assert (il, re) == (0, 0)
    rows = [{"position_id": "a", "p_initial": 1.0, "il_threshold_bps": 0, "state": "in_pool"},
            {"position_id": "b", "p_initial": 1.0, "il_threshold_bps": 800, "state": "in_pool"}]
    watched = [p for p in rows if p["p_initial"] and p["il_threshold_bps"] != 0]
    assert [p["position_id"] for p in watched] == ["b"]


def test_no_auto_exit_is_never_gated(engine):
    """It asks nothing of the exit policy, so there is nothing to refuse —
    even in a pool where every threshold lost."""
    pool = engine.pool("IGNIX-wSPCXx")
    _whipsaw(engine, pool)
    _fill(engine, pool.key, daily_usd=1000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=250_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})
    engine._balance = lambda *a, **k: _async(0)

    with pytest.raises(Exception) as e:
        asyncio.run(engine.create(address="0x" + "11" * 20, asset="SPCXx", amount="1",
                                  il_threshold_bps=0, pool_key=pool.key))
    assert "did not pay for itself" not in str(e.value)
    assert "never beat staying in" not in str(e.value)


# --------------------------------------------------------- auditable re-runs

def test_each_backtest_run_is_stored_and_never_overwritten(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine.aave_state = lambda: _async({"supply_apy_pct": 3.0})

    assert asyncio.run(engine.refresh_backtests(force=True)) == [pool.key]
    assert asyncio.run(engine.refresh_backtests()) == [], "weekly, not every tick"
    assert asyncio.run(engine.refresh_backtests(force=True)) == [pool.key]

    hist = engine.db.zap_backtest_history(pool.key)
    assert len(hist) == 2, "a re-run appends; it does not edit the old verdict"
    last = engine.db.last_zap_backtest(pool.key)
    assert last["result"]["days_of_history"] >= 14
    assert "every_threshold_loses" in last["result"]
