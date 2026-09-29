#!/usr/bin/env python3
"""Backfill zap_pool_volume with the last N days of measured swap volume.

The zap gates new deposits on a pool's trailing 7-day fee APY, and the live
scanner (ZapEngine.scan_pool_volume) only walks forward from the moment it
first runs. Without a backfill, every pool reads as "still measuring" for a
week and nothing is gated. This fills that week in from the pair's own Swap
events.

    python3 scripts/seed_pool_volume.py --db data/sarf.db [--days 7]

Run it against a COPY first if the server is live; it only writes to
zap_pool_volume, but a backfill is slow (X Layer's public RPC caps eth_getLogs
at 100 blocks and blocks are ~1s, so a week is ~6,000 calls per pool) and
there is no reason to do that against a production file twice.

Day buckets are derived from block numbers: X Layer blocks are 1.00s apart,
measured over 1,000 blocks, and the script checks that assumption against real
timestamps before it uses it.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sarf.db import Database  # noqa: E402
from sarf.xlayer.registry import registry  # noqa: E402
from sarf.xlayer.zap import ZapEngine  # noqa: E402

RPCS = ["https://rpc.xlayer.tech", "https://xlayerrpc.okx.com"]
SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
WINDOW = 100


def rpc(method: str, params: list, tries: int = 8):
    for i in range(tries):
        try:
            req = urllib.request.Request(
                RPCS[i % len(RPCS)],
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params}).encode(),
                {"Content-Type": "application/json", "User-Agent": "sarf-seed"})
            return json.load(urllib.request.urlopen(req, timeout=30)).get("result")
        except Exception:
            time.sleep(min(1.0 * (i + 1), 8))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--price", action="append", default=[],
                    help="SYMBOL=USD to skip the price lookup (e.g. wSPCXx=147.25)")
    args = ap.parse_args()

    overrides = dict(p.split("=", 1) for p in args.price)
    db = Database(args.db)
    engine = ZapEngine(db, None, registry())

    head = int(rpc("eth_blockNumber", []), 16)
    head_ts = int(rpc("eth_getBlockByNumber", [hex(head), False])["timestamp"], 16)
    # Check the 1s assumption rather than trusting it.
    back = 50_000
    old_ts = int(rpc("eth_getBlockByNumber", [hex(head - back), False])["timestamp"], 16)
    secs_per_block = (head_ts - old_ts) / back
    print(f"head {head} at {head_ts}; {secs_per_block:.3f}s/block over {back} blocks")
    if not 0.5 <= secs_per_block <= 3.0:
        print("!! block time outside the expected range; aborting", file=sys.stderr)
        return 1

    start = head - int(args.days * 86400 / secs_per_block)
    for key, pool in engine.pools.items():
        price = float(overrides.get(pool.rwa.symbol) or overrides.get(pool.underlying.symbol)
                      or 0) or None
        if price is None:
            print(f"!! no price for {pool.rwa.symbol}; pass --price {pool.rwa.symbol}=<usd>",
                  file=sys.stderr)
            return 1
        t0 = rpc("eth_call", [{"to": pool.pair, "data": "0x0dfe1681"}, "latest"])
        rwa_is0 = t0[-40:].lower() == pool.rwa.address[2:].lower()

        buckets: dict[int, list] = {}
        misses = 0

        def fetch(s: int):
            return s, rpc("eth_getLogs", [{"address": pool.pair, "topics": [SWAP],
                                           "fromBlock": hex(s), "toBlock": hex(s + WINDOW - 1)}])

        with cf.ThreadPoolExecutor(8) as ex:
            for s, logs in ex.map(fetch, range(start, head, WINDOW)):
                if logs is None:
                    misses += 1
                    continue
                for entry in logs:
                    blk = int(entry["blockNumber"], 16)
                    ts = head_ts - (head - blk) * secs_per_block
                    day = int(ts // 86400)
                    d = entry["data"][2:]
                    a = [int(d[i * 64:(i + 1) * 64], 16) for i in range(4)]
                    units = (a[0] + a[2]) if rwa_is0 else (a[1] + a[3])
                    b = buckets.setdefault(day, [0.0, 0])
                    b[0] += units / 10 ** pool.rwa.decimals * price
                    b[1] += 1

        for day, (vol, n) in sorted(buckets.items()):
            db.set_zap_pool_volume(key, day, vol, n)
        total = sum(v for v, _ in buckets.values())
        swaps = sum(n for _, n in buckets.values())
        print(f"{key:22} {len(buckets)} day(s), ${total:,.0f} volume, {swaps} swaps"
              + (f", {misses} windows unreadable" if misses else ""))
        # Park the live scanner's cursor at the head it was filled to, so it
        # picks up exactly where this left off instead of rescanning.
        db.set_stat(f"zap_vol_cursor_{key}", {"block": head})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
