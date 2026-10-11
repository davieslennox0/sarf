#!/usr/bin/env python3
"""Measure real daily price history + ratio volatility for a zap pool pair,
in the exact shape zap_pools.json's existing pools already carry.

Referenced by the original 4 pools' own comments ("refreshed by
scripts/measure_pool_volatility.py") but never actually committed — this is
that script, built the same way the numbers it's replacing were originally
measured: real OKX 1D klines, paired by day, nothing modelled or guessed.

    .venv/bin/python scripts/measure_pool_history.py <rwa_address> <other_address> <days>

Prints a JSON object with "volatility" and "price_history" blocks ready to
paste into a pool's entry in zap_pools.json.
"""
import json
import math
import statistics
import subprocess
import sys
import time


def klines(address: str, days: int) -> list[dict]:
    out = subprocess.run(
        ["/root/.local/bin/onchainos", "market", "kline", "--chain", "196",
         "--address", address, "--bar", "1D", "--limit", str(days + 5)],
        capture_output=True, text=True)
    body = json.loads(out.stdout)
    if not body.get("ok"):
        raise SystemExit(f"kline fetch failed for {address}: {out.stdout[:300]}")
    rows = body["data"]
    # onchainos returns newest-first; keep closes, oldest first, de-duped by day.
    seen = {}
    for r in rows:
        day = int(int(r["ts"]) / 1000 // 86400)
        seen[day] = float(r["c"])
    return sorted(seen.items())


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
           if closes[i - 1] > 0 and closes[i] > 0]


def main():
    rwa_addr, other_addr, days = sys.argv[1], sys.argv[2], int(sys.argv[3])
    rwa_rows = klines(rwa_addr, days)
    other_rows = klines(other_addr, days)
    rwa_days = {d: c for d, c in rwa_rows}
    other_days = {d: c for d, c in other_rows}
    common = sorted(set(rwa_days) & set(other_days))
    if len(common) < 14:
        print(f"WARNING: only {len(common)} paired days of history (below the "
             "14-day backtest floor)", file=sys.stderr)

    rwa_closes = [rwa_days[d] for d in common]
    other_closes = [other_days[d] for d in common]
    ratio = [r / o for r, o in zip(rwa_closes, other_closes)]  # rwa per other

    ratio_lr = log_returns(ratio)
    rwa_lr = log_returns(rwa_closes)
    other_lr = log_returns(other_closes)

    daily_sigma_pct = round(statistics.pstdev(ratio_lr) * 100, 2) if len(ratio_lr) > 1 else None
    rwa_sigma_pct = round(statistics.pstdev(rwa_lr) * 100, 2) if len(rwa_lr) > 1 else None
    other_sigma_pct = round(statistics.pstdev(other_lr) * 100, 2) if len(other_lr) > 1 else None
    corr = None
    if len(rwa_lr) > 1 and len(rwa_lr) == len(other_lr):
        try:
            corr = round(statistics.correlation(rwa_lr, other_lr), 2)
        except statistics.StatisticsError:
            corr = None

    today = time.strftime("%Y-%m-%d", time.gmtime())
    out = {
        "volatility": {
            "daily_sigma_pct": daily_sigma_pct,
            "_comment": ("Standard deviation of the daily log change in the POOL RATIO "
                        "(rwa per other token) — the quantity impermanent loss actually "
                        "depends on, not either leg's USD price. Measured from paired daily "
                        "closes of both legs, so the correlation between them is in the "
                        "number rather than assumed."),
            "window_days": len(common),
            "leg_sigma_pct": {"rwa": rwa_sigma_pct, "other": other_sigma_pct},
            "correlation": corr,
            "measured_at": today,
            "source": "OKX 1D klines per leg (onchainos market kline), refreshed by scripts/measure_pool_history.py",
        },
        "price_history": {
            "_comment": ("Daily closes of both legs in USD, same days, oldest first. The "
                        "backtest replays the real exit/re-entry policy over these; the ratio "
                        "between them is what impermanent loss depends on. Refresh with "
                        "scripts/measure_pool_history.py."),
            "days_utc": common,
            "rwa_usd": rwa_closes,
            "other_usd": other_closes,
            "measured_at": today,
            "source": "OKX 1D klines per leg (onchainos market kline)",
        },
    }
    print(json.dumps(out, indent=1))


main()
