"""Pool gating: a pool is open only if its measured 7-day fees cover the round
trips it is expected to cost in a year — expected exits x round-trip cost, both
measured. Nothing here extrapolates, and nothing here touches the network."""

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
    # $100/day of volume on a $70k pool: 0.3% of that is $0.30 a day.
    _fill(engine, pool.key, daily_usd=100.0, days=7)
    _state(engine, pool, price=230.0, tvl_usd=70_000)

    v = asyncio.run(engine.pool_viability(pool))
    assert v["gated"] is True and v["viable"] is False
    assert v["fee_apy_pct"] is not None and v["fee_apy_pct"] < v["hurdle_pct"]
    assert v["expected_exits_per_year"] > 1
    assert "below" in v["reason"] and "times a year" in v["reason"]
    assert v["label"] == "fees only, incentives ended"


def test_a_pool_that_earns_its_keep_is_open(engine):
    pool = engine.pool("LAIKA-wSPCXx")
    # ~$180k/day on a $205k pool is roughly what this pool actually did.
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)

    v = asyncio.run(engine.pool_viability(pool))
    assert v["viable"] is True and v["gated"] is False
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
    object.__setattr__(pool, "daily_sigma", None)
    _fill(engine, pool.key, daily_usd=1.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    v = asyncio.run(engine.pool_viability(pool))
    assert v["viable"] is True and v["gated"] is False
    assert v["expected_exits_per_year"] is None
    assert "volatility has not been measured" in v["reason"]


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
    _fill(engine, pool.key, daily_usd=100.0, days=7)
    _state(engine, pool, price=230.0, tvl_usd=70_000)

    with pytest.raises(ValidationError) as e:
        asyncio.run(engine.create(address="0x" + "11" * 20, asset="NVDAx", amount="1",
                                  il_threshold_bps=500, pool_key=pool.key))
    assert "closed to new deposits" in str(e.value)
    assert "below" in str(e.value)


def test_a_healthy_pool_still_gets_past_the_gate(engine):
    """The gate must not be the reason a good pool fails; it should fall over
    later, on the balance check, with its own message."""
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine._balance = lambda *a, **k: _async(0)

    with pytest.raises(Exception) as e:
        asyncio.run(engine.create(address="0x" + "11" * 20, asset="SPCXx", amount="1",
                                  il_threshold_bps=800, pool_key=pool.key))
    assert "closed to new deposits" not in str(e.value)


def test_a_deposit_is_judged_at_the_users_own_threshold(engine):
    """Same pool, same week of fees: refused at a tight threshold, allowed at
    a wide one, because the tight one is the one that churns."""
    pool = engine.pool("LAIKA-wSPCXx")
    _fill(engine, pool.key, daily_usd=184_000.0, days=7)
    _state(engine, pool, price=147.25, tvl_usd=205_000)
    engine._balance = lambda *a, **k: _async(0)

    with pytest.raises(ValidationError) as e:
        asyncio.run(engine.create(address="0x" + "11" * 20, asset="SPCXx", amount="1",
                                  il_threshold_bps=300, pool_key=pool.key))
    assert "closed to new deposits" in str(e.value)
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
