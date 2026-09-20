"""Single-asset zap into X Layer's RWA incentive pools, with an impermanent-
loss watcher that exits to Aave and re-enters when the price comes back.

WHAT IT DOES
    A user brings one asset (SPCXx, say). Sarf wraps it into the ERC-4626
    wrapper the incentive pools actually list (wSPCXx), swaps the fee-exact
    optimal share of it into the pool's other token *through that same pool*,
    and adds both sides as Uniswap V2 liquidity. It then tracks the pool price
    against the entry price, and when impermanent loss crosses the user's
    threshold it takes the position apart, converts it to USDT and supplies it
    to Aave V3. When IL falls back under the re-entry threshold, it does the
    reverse and resets the entry price.

    IL is the textbook constant-product figure, implemented here from the
    formula rather than taken from any library or hook contract:

        r  = P_current / P_initial
        IL = 1 - 2*sqrt(r) / (1 + r)            (x 10 000 for bps)

    where P is the pool price of the RWA leg in units of the other leg.

WHY THE USER'S WALLET SIGNS THE TRANSITIONS
    Sarf is non-custodial, and the only standing authority it can hold is the
    SarfSessionKey grant (contracts/src/SarfSessionKey.sol). That contract
    can call exactly one target, the aggregator router pinned in the grant,
    and it proves each call safe by a two-token balance post-condition. Uniswap's
    removeLiquidity and Aave's supply/withdraw are different targets. Their
    effects (an LP token burned for two tokens, USDT swapped for an aToken)
    are not the sell-X-receive-Y shape the post-condition checks. So nothing
    Sarf holds today can sign an exit, and widening the contract to do it is a
    new deployment with its own audit, not something to slip in here.

    So the watcher decides automatically and the wallet executes. On a breach
    the position moves to exit_pending (or reentry_pending), the transition is
    logged, and the next visit to the position page, or the next
    get_zap_position in chat, offers the exact transactions to sign. The watcher
    never needs a key, and a stolen Sarf database cannot move a cent of it.

HOW STEPS ARE BUILT
    Every flow is a fixed sequence of step kinds (FLOWS). Each step is built
    only when the wallet is about to sign it, from live chain state, and uses
    the amounts the PREVIOUS step's receipt actually credited (ERC-20 Transfer
    logs to the user), not an estimate. Approval steps are skipped when the
    allowance is already enough, and every approval is for the exact amount. A
    submitted hash is accepted only if the mined transaction is from the owner
    and carries the same `to` and calldata the server built.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from eth_abi import decode, encode
from eth_utils import keccak

from ..config import settings
from ..db import Database
from ..validation import ValidationError
from . import rpc
from .evm import validate_evm_address, validate_tx_hash
from .okx_dex import DexError, OkxDexClient
from .registry import EXPLORER_TX, XStocksRegistry

log = logging.getLogger("sarf.zap")

POOLS_PATH = Path(__file__).with_name("zap_pools.json")

TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
SYNC_TOPIC = "0x" + keccak(text="Sync(uint112,uint112)").hex()
RAY = 10 ** 27
DEADLINE_SECONDS = 20 * 60
RECEIPT_WAIT_SECONDS = 45.0
PRICE_TTL = 60.0

IL_THRESHOLD_MIN_BPS = 1
IL_THRESHOLD_MAX_BPS = 5000

# The IL watcher's view of a position. 'entering', 'exiting' and 'reentering'
# mean the wallet is partway through signing a flow; the watcher leaves those
# alone, because a half-built position must never be re-routed underneath the
# person signing it.
WATCHED_STATES = ("in_pool", "exit_pending", "parked", "reentry_pending")
LIVE_STATES = WATCHED_STATES + ("entering", "exiting", "reentering")

STATE_LABELS = {
    "entering": "Depositing: waiting for your wallet to sign the entry steps",
    "in_pool": "In the pool, earning trading fees and incentive rewards",
    "exit_pending": "IL crossed your threshold. Exit is ready to sign",
    "exiting": "Exiting: moving the position to Aave",
    "parked": "Parked in Aave V3, earning supply yield",
    "reentry_pending": "Price has normalised. Re-entry is ready to sign",
    "reentering": "Re-entering the pool",
    "close_pending": "Closing: the way out is ready to sign",
    "closing": "Closing: moving the position back to your wallet",
    "closed": "Closed. The proceeds are in your wallet",
    "cancelled": "Cancelled before anything was signed",
}

# Step kinds, in order. See the module docstring for how they are built.
FLOWS: dict[str, list[str]] = {
    "enter": ["approve_wrap", "wrap", "approve_rwa_router", "swap_in",
              "approve_other_router", "add_liquidity"],
    "exit": ["approve_lp_router", "remove_liquidity", "approve_other_router_exit",
             "swap_other_to_rwa", "approve_rwa_okx", "swap_rwa_to_park",
             "approve_park_aave", "aave_supply"],
    "reenter": ["aave_withdraw", "approve_park_okx", "swap_park_to_rwa",
                "approve_rwa_router", "swap_in", "approve_other_router", "add_liquidity"],
    # Closing is the exit without its last two steps: instead of supplying the
    # USDT to Aave, it stops once the USDT is in the wallet. From Aave there is
    # nothing to unwind but the withdrawal itself.
    "close": ["approve_lp_router", "remove_liquidity", "approve_other_router_exit",
              "swap_other_to_rwa", "approve_rwa_okx", "swap_rwa_to_park"],
    "close_parked": ["aave_withdraw"],
}
FLOW_STATE = {"enter": "entering", "exit": "exiting", "reenter": "reentering",
              "close": "closing", "close_parked": "closing"}


# --------------------------------------------------------------------- math

def il_bps(r: float) -> float:
    """Impermanent loss of a 50/50 constant-product position, in bps, for a
    price ratio r = P_current / P_initial. 0 at r == 1, rising toward 10 000
    as r goes to 0 or infinity."""
    if not (r > 0 and math.isfinite(r)):
        raise ValueError("price ratio must be positive and finite")
    return 10000.0 * (1.0 - 2.0 * math.sqrt(r) / (1.0 + r))


def optimal_swap_in(amount_in: int, reserve_in: int, fee_bps: int = 30) -> int:
    """How much of `amount_in` to swap through a V2 pair so the remainder and
    the output sit in the pair's post-swap ratio, i.e. addLiquidity uses both
    in full. Closed form of the quadratic, in integers:

        s = (sqrt(((2d-f)R)^2 + 4(d-f)d*A*R) - (2d-f)R) / (2(d-f)),  d = 10 000

    Exactly half is wrong by the fee plus the price impact of the swap itself,
    and the difference would otherwise be left in the wallet."""
    if amount_in <= 0 or reserve_in <= 0:
        raise ValueError("amount and reserve must be positive")
    d, f = 10000, fee_bps
    b = (2 * d - f) * reserve_in
    return (math.isqrt(b * b + 4 * (d - f) * d * amount_in * reserve_in) - b) // (2 * (d - f))


def v2_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    """UniswapV2Library.getAmountOut, exactly."""
    a = amount_in * (10000 - fee_bps)
    return a * reserve_out // (reserve_in * 10000 + a)


def zap_split(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30,
              buy_tax_bps: int = 0) -> int:
    """optimal_swap_in for a pair whose output token taxes transfers out of
    the pair. The tax shrinks what arrives, so the split has to swap a little
    more to leave the two sides matched. There is no tidy closed form with
    the tax in it, so this bisects on the exact V2 integer maths: find the s
    where what is left of amount_in and what arrived sit in the pair's
    post-swap ratio."""
    if buy_tax_bps <= 0:
        return optimal_swap_in(amount_in, reserve_in, fee_bps)
    keep = 10000 - buy_tax_bps

    def excess(s: int) -> int:  # > 0: too little swapped
        out = v2_out(s, reserve_in, reserve_out, fee_bps)
        got = out * keep // 10000
        return (amount_in - s) * (reserve_out - out) - got * (reserve_in + s)

    lo, hi = 0, amount_in
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if excess(mid) > 0:
            lo = mid
        else:
            hi = mid
    return hi


def pairable(a_have: int, b_have: int, reserve_a: int, reserve_b: int) -> tuple[int, int]:
    """The most of (a_have, b_have) that goes into the pair at its current
    ratio: the same amounts UniswapV2Router.addLiquidity would pick. Passing
    exactly these as the desired amounts means the router cannot choose
    something that falls outside the minimums."""
    b_opt = a_have * reserve_b // reserve_a
    if b_opt <= b_have:
        return a_have, b_opt
    return b_have * reserve_a // reserve_b, b_have


# ------------------------------------------------------------------ encoding

def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _calldata(sig: str, types: list[str], args: list[Any]) -> str:
    return "0x" + (_sel(sig) + encode(types, args)).hex()


async def _eth_call(to: str, sig: str, types: list[str] | None = None,
                    args: list[Any] | None = None, out: list[str] | None = None) -> tuple:
    res = await rpc._call("eth_call", [
        {"to": to, "data": _calldata(sig, types or [], args or [])}, "latest"])
    return decode(out or ["uint256"], bytes.fromhex(res[2:]))


def _units(amount: object, decimals: int) -> int:
    try:
        d = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        raise ValidationError("amount must be a decimal number, e.g. '1.5'")
    if not d.is_finite() or d <= 0:
        raise ValidationError("amount must be positive")
    units = int(d * (Decimal(10) ** decimals))
    if units <= 0:
        raise ValidationError("amount is below the token's smallest unit")
    return units


def _fmt(units: int | str | None, decimals: int, places: int = 6) -> str | None:
    if units is None:
        return None
    v = Decimal(int(units)) / (Decimal(10) ** decimals)
    return f"{v:.{places}f}".rstrip("0").rstrip(".") or "0"


def _addr_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def transfers_in(receipt: dict[str, Any], to: str) -> dict[str, int]:
    """token -> total ERC-20 amount transferred TO `to` in this receipt."""
    out: dict[str, int] = {}
    want = _addr_topic(to)
    for lg in receipt.get("logs") or []:
        t = lg.get("topics") or []
        if len(t) == 3 and t[0].lower() == TRANSFER_TOPIC and t[2].lower() == want:
            tok = lg["address"].lower()
            out[tok] = out.get(tok, 0) + int(lg["data"], 16)
    return out


def transfers_between(receipt: dict[str, Any], frm: str, to: str) -> dict[str, int]:
    out: dict[str, int] = {}
    f, t_ = _addr_topic(frm), _addr_topic(to)
    for lg in receipt.get("logs") or []:
        t = lg.get("topics") or []
        if (len(t) == 3 and t[0].lower() == TRANSFER_TOPIC
                and t[1].lower() == f and t[2].lower() == t_):
            tok = lg["address"].lower()
            out[tok] = out.get(tok, 0) + int(lg["data"], 16)
    return out


def last_sync(receipt: dict[str, Any], pair: str) -> tuple[int, int] | None:
    """(reserve0, reserve1) from the pair's last Sync in this receipt: the
    exact reserves the transaction left behind, which is the price the
    position actually entered or left at."""
    found = None
    for lg in receipt.get("logs") or []:
        t = lg.get("topics") or []
        if lg.get("address", "").lower() == pair.lower() and t and t[0].lower() == SYNC_TOPIC:
            found = decode(["uint112", "uint112"], bytes.fromhex(lg["data"][2:]))
    return found


# --------------------------------------------------------------------- pools

@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int
    name: str = ""
    # Transfer tax taken when the token leaves / enters the pair. Every paired
    # token in the current incentive round has one (see zap_pools.json).
    buy_tax_bps: int = 0
    sell_tax_bps: int = 0


@dataclass(frozen=True)
class Pool:
    key: str
    pair: str
    rwa: Token
    other: Token
    underlying: Token

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key, "dex": "Uniswap V2", "pair_address": self.pair,
            "pair": f"{self.other.symbol}/{self.rwa.symbol}",
            "rwa": {"symbol": self.rwa.symbol, "address": self.rwa.address,
                    "wraps": self.underlying.symbol},
            "other": {"symbol": self.other.symbol, "address": self.other.address},
            "explorer": f"https://web3.okx.com/explorer/x-layer/address/{self.pair}",
        }


@dataclass
class Step:
    kind: str
    title: str
    to: str
    data: str
    value: str = "0"
    ctx: dict[str, Any] | None = None  # values fixed at build time, merged on confirm


class ZapEngine:
    def __init__(self, db: Database, dex: OkxDexClient, reg: XStocksRegistry,
                 path: Path = POOLS_PATH):
        self.db, self.dex, self.reg = db, dex, reg
        raw = json.loads(path.read_text())
        self.cfg = raw
        v2 = raw["uniswap_v2"]
        self.router = validate_evm_address(v2["router02"], what="v2 router")
        self.fee_bps = int(v2["fee_bps"])
        a = raw["aave_v3"]
        self.aave_pool = validate_evm_address(a["pool"], what="aave pool")
        self.park = Token(a["park_asset_symbol"], validate_evm_address(a["park_asset"]),
                          int(a["park_asset_decimals"]))
        self.a_token = validate_evm_address(a["a_token"], what="aToken")
        self.incentive = raw["incentive_program"]
        self.pools: dict[str, Pool] = {}
        for p in raw["pools"]:
            r, o = p["rwa"], p["other"]
            self.pools[p["key"].upper()] = Pool(
                key=p["key"], pair=validate_evm_address(p["pair"], what="pair"),
                rwa=Token(r["symbol"], validate_evm_address(r["address"]), int(r["decimals"]), r["name"]),
                other=Token(o["symbol"], validate_evm_address(o["address"]), int(o["decimals"]),
                            o["name"], int(o.get("buy_tax_bps", 0)), int(o.get("sell_tax_bps", 0))),
                underlying=Token(r["underlying_symbol"], validate_evm_address(r["underlying_address"]),
                                 int(r["decimals"])),
            )
        self._price_cache: dict[str, tuple[float, float | None]] = {}

    # ----------------------------------------------------------- chain reads

    def pool(self, key: str) -> Pool:
        p = self.pools.get((key or "").strip().upper())
        if not p:
            raise ValidationError(
                f"unknown pool '{key}'. Pools: {', '.join(p.key for p in self.pools.values())}")
        return p

    def pools_for(self, symbol: str) -> list[Pool]:
        s = symbol.strip().lower()
        return [p for p in self.pools.values()
                if s in (p.rwa.symbol.lower(), p.underlying.symbol.lower())]

    async def pool_state(self, pool: Pool) -> dict[str, Any]:
        (t0,) = await _eth_call(pool.pair, "token0()", out=["address"])
        r0, r1, _ = await _eth_call(pool.pair, "getReserves()", out=["uint112", "uint112", "uint32"])
        (ts,) = await _eth_call(pool.pair, "totalSupply()")
        rwa_first = t0.lower() == pool.rwa.address.lower()
        r_rwa, r_other = (r0, r1) if rwa_first else (r1, r0)
        return {
            "rwa_is_token0": rwa_first, "reserve_rwa": r_rwa, "reserve_other": r_other,
            "total_supply": ts, "price": self._price(pool, r_rwa, r_other),
            # Value of one LP token in sqrt(k) units. Only trading fees
            # grow it, so its growth since entry is the fee return measured
            # on-chain rather than estimated from volume.
            "lp_index": math.sqrt(r_rwa * r_other) / ts if ts else None,
        }

    @staticmethod
    def _price(pool: Pool, r_rwa: int, r_other: int) -> float:
        """Pool price of one whole RWA token, in whole `other` tokens."""
        return (r_other / 10 ** pool.other.decimals) / (r_rwa / 10 ** pool.rwa.decimals)

    def _price_from_sync(self, pool: Pool, sync: tuple[int, int], rwa_first: bool) -> float:
        r_rwa, r_other = (sync[0], sync[1]) if rwa_first else (sync[1], sync[0])
        return self._price(pool, r_rwa, r_other)

    async def rwa_usd(self, pool: Pool) -> float | None:
        """USD price of one RWA-leg token, from a 1-token quote on the same
        aggregator Sarf trades through. None means no price. Callers show
        values as unknown, never as zero."""
        hit = self._price_cache.get(pool.rwa.address)
        if hit and time.monotonic() - hit[0] < PRICE_TTL:
            return hit[1]
        try:
            q = await self.dex.quote(pool.rwa.address, self.park.address, 10 ** pool.rwa.decimals)
            price = q.to_amount / 10 ** self.park.decimals if q.to_amount > 0 else None
        except DexError:
            price = None
        self._price_cache[pool.rwa.address] = (time.monotonic(), price)
        return price

    async def aave_state(self) -> dict[str, Any]:
        raw = await rpc._call("eth_call", [{"to": self.aave_pool, "data": _calldata(
            "getReserveData(address)", ["address"], [self.park.address])}, "latest"])
        words = [int(raw[2 + i:2 + i + 64], 16) for i in range(0, len(raw) - 2, 64)]
        apr = words[2] / RAY  # currentLiquidityRate, a per-year rate in ray
        (income,) = await _eth_call(self.aave_pool, "getReserveNormalizedIncome(address)",
                                    ["address"], [self.park.address])
        return {"supply_apy_pct": round(((1 + apr / 31536000) ** 31536000 - 1) * 100, 3),
                "income_index": income}

    async def _balance(self, token: str, owner: str) -> int:
        return await rpc.erc20_balance(token, owner)

    async def _allowance(self, token: str, owner: str, spender: str) -> int:
        (a,) = await _eth_call(token, "allowance(address,address)", ["address", "address"],
                               [owner, spender])
        return a

    async def _float(self, pool: Pool) -> int:
        """Collected tax the paired token holds in its own balance, which it
        sells into the pair on the next transfer into it."""
        return await self._balance(pool.other.address, pool.other.address)

    async def _wrap_rate(self, pool: Pool) -> float:
        """Underlying tokens per wrapped token (ERC-4626 convertToAssets)."""
        (a,) = await _eth_call(pool.rwa.address, "convertToAssets(uint256)", ["uint256"],
                               [10 ** pool.rwa.decimals])
        return a / 10 ** pool.underlying.decimals

    # -------------------------------------------------------------- create

    async def create(self, address: str, asset: str, amount: str, il_threshold_bps: int,
                     reentry_threshold_bps: int | None = None,
                     pool_key: str | None = None) -> dict[str, Any]:
        il_t, re_t = self._validate_thresholds(il_threshold_bps, reentry_threshold_bps)
        if pool_key:
            pool = self.pool(pool_key)
            if asset.strip().lower() not in (pool.rwa.symbol.lower(), pool.underlying.symbol.lower()):
                raise ValidationError(
                    f"{pool.key} takes {pool.underlying.symbol} or {pool.rwa.symbol}, not {asset}")
        else:
            candidates = self.pools_for(asset)
            if not candidates:
                supported = sorted({p.underlying.symbol for p in self.pools.values()}
                                   | {p.rwa.symbol for p in self.pools.values()})
                raise ValidationError(
                    f"no incentivised pool takes {asset}. Zap-able assets: {', '.join(supported)}")
            # Deepest pool by RWA-side reserve: the least price impact for the
            # swap leg and the most stable price for the IL watcher.
            states = await asyncio.gather(*(self.pool_state(p) for p in candidates))
            pool = max(zip(candidates, states), key=lambda ps: ps[1]["reserve_rwa"])[0]
        is_underlying = asset.strip().lower() == pool.underlying.symbol.lower()
        token = pool.underlying if is_underlying else pool.rwa
        units = _units(amount, token.decimals)

        held = await self._balance(token.address, address)
        if held < units:
            raise ValidationError(
                f"this wallet holds {_fmt(held, token.decimals)} {token.symbol}; "
                f"the zap needs {_fmt(units, token.decimals)}")
        rwa_units = units
        if is_underlying:
            rate = await self._wrap_rate(pool)
            rwa_units = int(units / rate) if rate > 0 else units
        price = await self.rwa_usd(pool)
        usd = rwa_units / 10 ** pool.rwa.decimals * price if price else None
        if usd is None:
            raise ValueError(f"could not price {pool.rwa.symbol} right now, so the order "
                             "cap cannot be checked. Try again in a minute")
        if usd > settings.max_order_usd:
            raise ValidationError(f"~${usd:,.2f} is over the ${settings.max_order_usd:,.0f} cap")

        pid = self.db.create_zap_position(
            address=address.lower(), pool_key=pool.key, deposit_symbol=token.symbol,
            deposit_amount=str(units), deposit_usd=usd, il_threshold_bps=il_t,
            reentry_bps=re_t, state="entering", flow="enter", flow_step=0,
            flow_ctx={"deposit_units": str(units), "deposit_is_underlying": is_underlying},
        )
        self.db.log_zap_event(pid, "created", {
            "pool": pool.key, "deposit": f"{_fmt(units, token.decimals)} {token.symbol}",
            "deposit_usd": round(usd, 2), "il_threshold_bps": il_t, "reentry_bps": re_t})
        return self.db.get_zap_position(pid)

    @staticmethod
    def _validate_thresholds(il_t: object, re_t: object | None) -> tuple[int, int]:
        if isinstance(il_t, bool) or not isinstance(il_t, (int, float)):
            raise ValidationError("il_threshold_bps must be a number of basis points")
        il_t = int(il_t)
        if not IL_THRESHOLD_MIN_BPS <= il_t <= IL_THRESHOLD_MAX_BPS:
            raise ValidationError(
                f"il_threshold_bps must be {IL_THRESHOLD_MIN_BPS}-{IL_THRESHOLD_MAX_BPS} "
                "(100 bps = 1% impermanent loss)")
        if re_t is None:
            re_t = il_t // 2
        if isinstance(re_t, bool) or not isinstance(re_t, (int, float)):
            raise ValidationError("reentry_threshold_bps must be a number of basis points")
        re_t = int(re_t)
        if not 0 <= re_t < il_t:
            raise ValidationError(
                "reentry_threshold_bps must be at least 0 and below il_threshold_bps. "
                "Otherwise a parked position would re-enter straight into another exit")
        return il_t, re_t

    # ----------------------------------------------------------- owner actions

    def _owned(self, position_id: str, address: str) -> dict[str, Any]:
        pos = self.db.get_zap_position((position_id or "").strip())
        if not pos:
            raise LookupError("unknown zap position")
        if pos["address"] != address.lower():
            raise PermissionError("this position belongs to a different account")
        return pos

    def set_thresholds(self, position_id: str, address: str, il_threshold_bps: int,
                       reentry_threshold_bps: int | None = None) -> dict[str, Any]:
        pos = self._owned(position_id, address)
        il_t, re_t = self._validate_thresholds(il_threshold_bps, reentry_threshold_bps)
        self.db.update_zap_position(pos["position_id"], il_threshold_bps=il_t, reentry_bps=re_t)
        self.db.log_zap_event(pos["position_id"], "thresholds_changed", {
            "from": [pos["il_threshold_bps"], pos["reentry_bps"]], "to": [il_t, re_t]})
        return self.db.get_zap_position(pos["position_id"])

    def request_exit(self, position_id: str, address: str) -> dict[str, Any]:
        pos = self._owned(position_id, address)
        if pos["state"] != "in_pool":
            raise ValueError(f"only a position that is in the pool can exit (state: {pos['state']})")
        if self.db.update_zap_position(pos["position_id"], expect_state="in_pool",
                                       state="exit_pending"):
            self.db.log_zap_event(pos["position_id"], "exit_requested", {
                "reason": "manual", "il_bps": pos["last_il_bps"], "p_initial": pos["p_initial"],
                "p_current": pos["last_price"]})
        return self.db.get_zap_position(pos["position_id"])

    def request_reentry(self, position_id: str, address: str) -> dict[str, Any]:
        pos = self._owned(position_id, address)
        if pos["state"] != "parked":
            raise ValueError(f"only a parked position can re-enter (state: {pos['state']})")
        if self.db.update_zap_position(pos["position_id"], expect_state="parked",
                                       state="reentry_pending"):
            self.db.log_zap_event(pos["position_id"], "reentry_requested", {
                "reason": "manual", "il_bps": pos["last_il_bps"]})
        return self.db.get_zap_position(pos["position_id"])

    def request_close(self, position_id: str, address: str) -> dict[str, Any]:
        """Take the whole position back to the wallet and stop watching it.

        Allowed from the pool and from Aave, because those are the two places
        the money can actually be sitting. A position mid-flow has a half-built
        transaction outstanding and has to finish or fail first.
        """
        pos = self._owned(position_id, address)
        if pos["state"] not in ("in_pool", "parked"):
            raise ValueError(
                "a position can only be closed from the pool or from Aave "
                f"(state: {pos['state']})")
        if self.db.update_zap_position(pos["position_id"], expect_state=pos["state"],
                                       state="close_pending",
                                       flow_ctx={"close_from": pos["state"]}):
            self.db.log_zap_event(pos["position_id"], "close_requested", {
                "from": pos["state"], "il_bps": pos["last_il_bps"]})
        return self.db.get_zap_position(pos["position_id"])

    def cancel(self, position_id: str, address: str) -> dict[str, Any]:
        pos = self._owned(position_id, address)
        if pos["state"] != "entering" or pos["flow_step"] > 0 or self._pending(pos).get("tx_hash"):
            raise ValueError("only an entry nobody has signed a step of yet can be cancelled")
        self.db.update_zap_position(pos["position_id"], expect_state="entering",
                                    state="cancelled", flow=None, pending_tx=None)
        self.db.log_zap_event(pos["position_id"], "cancelled", {})
        return self.db.get_zap_position(pos["position_id"])

    # ----------------------------------------------------------------- costs

    AGGREGATOR_COST_BPS = 10  # wSPCXx <-> USDT, measured at 1-11 bps of impact

    def entry_cost_bps(self, pool: Pool) -> int:
        """Cost of going into the pool, in bps of the position. Swapping the
        RWA half in pays the pool fee and the token's buy tax. Adding liquidity
        then pays its sell tax TWICE over: Router02 sends both tokens in the
        pre-tax ratio, so the pair receives ts less of the taxed token than it
        pairs against, and the matching share of the RWA leg is donated to the
        pool as well. Measured on mainnet 2026-09-18 in LAIKA-wSPCXx: 3.1% and
        3.4% against the model's 3.15% (+0.1% aggregator on re-entry). The
        donation can only be avoided by transferring to the pair directly
        across separate transactions, which lets anyone skim() the tokens in
        between."""
        o = pool.other
        return int((self.fee_bps + o.buy_tax_bps) / 2 + o.sell_tax_bps)

    def exit_cost_bps(self, pool: Pool) -> int:
        """Removing liquidity pays the buy tax on the taxed half as it leaves
        the pair. Swapping it back pays its sell tax and the pool fee. Then the
        USDT conversion. Measured 2.1% against the model's 2.25% for LAIKA."""
        o = pool.other
        return int((o.buy_tax_bps + o.sell_tax_bps + self.fee_bps) / 2 + self.AGGREGATOR_COST_BPS)

    def cycle_cost_bps(self, pool: Pool) -> int:
        """One exit plus one re-entry. For LAIKA-wSPCXx that is 550 bps, which
        is what the mainnet cycle measured ($9.96 in the pool -> $9.42 back in)."""
        return self.exit_cost_bps(pool) + self.entry_cost_bps(pool) + self.AGGREGATOR_COST_BPS

    def costs(self, pool: Pool, il_threshold_bps: int,
              st: dict[str, Any] | None = None, float_: int | None = None) -> dict[str, Any]:
        cycle = self.cycle_cost_bps(pool)
        o = pool.other
        warn = None
        if il_threshold_bps <= cycle:
            warn = (f"Your exit threshold ({il_threshold_bps} bps) is at or below the ~{cycle} bps "
                    "one exit-and-re-entry costs in this pool, so an exit only pays off if the "
                    "price keeps moving away afterwards. A threshold well above "
                    f"{cycle} bps protects against large moves without paying to churn.")
        return {
            "paired_token_tax": (f"{o.symbol} charges {o.buy_tax_bps / 100:g}% when it leaves the "
                                 f"pool and {o.sell_tax_bps / 100:g}% when it enters it"),
            "pool_fee_bps": self.fee_bps,
            "estimated_exit_and_reentry_cost_bps": cycle,
            "estimated_entry_cost_bps": self.entry_cost_bps(pool),
            "estimated_exit_cost_bps": self.exit_cost_bps(pool),
            "why_the_sell_tax_counts_twice": (
                f"Adding liquidity sends {o.symbol} and {pool.rwa.symbol} in the pool's ratio, "
                f"then {o.symbol} loses its tax on arrival, so the pool keeps the matching part "
                f"of your {pool.rwa.symbol} as well"),
            # Price move the token's pending swap-back would cause if it fired
            # on the next transfer into the pair: roughly 2x its float as a
            # share of the reserve. It lands on whoever's transfer sets it off.
            "swap_back_float_pct_of_reserve": (
                round(100 * float_ / st["reserve_other"], 2)
                if (st and float_ is not None and st["reserve_other"]) else None),
            "swap_back_note": (f"{o.symbol} sells its collected tax into this pool on the next "
                               "transfer into it. When that is your exit or deposit, the price "
                               "moves against you by about twice the float above"),
            "warning": warn,
        }

    # ----------------------------------------------------------------- steps

    @staticmethod
    def _pending(pos: dict[str, Any]) -> dict[str, Any]:
        return json.loads(pos["pending_tx"]) if pos.get("pending_tx") else {}

    async def next_step(self, position_id: str, address: str) -> dict[str, Any]:
        """The next transaction for the owner's wallet to sign, built from
        live state, or a note that nothing is waiting."""
        pos = self._owned(position_id, address)
        if pos["state"] in ("exit_pending", "reentry_pending", "close_pending"):
            if pos["state"] == "close_pending":
                # Where the money is decides which way out it takes.
                flow = "close_parked" if pos["flow_ctx"].get("close_from") == "parked" else "close"
            else:
                flow = "exit" if pos["state"] == "exit_pending" else "reenter"
            ctx = {"trigger_il_bps": pos["last_il_bps"], "trigger_price": pos["last_price"]}
            if flow.startswith("close"):
                ctx["close_from"] = pos["flow_ctx"].get("close_from")
            if not self.db.update_zap_position(
                    pos["position_id"], expect_state=pos["state"], state=FLOW_STATE[flow],
                    flow=flow, flow_step=0, flow_ctx=ctx, pending_tx=None):
                raise ValueError("the position changed state; reload it")
            self.db.log_zap_event(pos["position_id"], f"{flow}_started", {})
            pos = self.db.get_zap_position(pos["position_id"])
        if not pos["flow"]:
            return {"status": "nothing_to_sign", "state": pos["state"]}
        pend = self._pending(pos)
        if pend.get("tx_hash"):
            return {"status": "awaiting_confirmation", "tx_hash": pend["tx_hash"],
                    "step": pend["kind"]}
        pool = self.pool(pos["pool_key"])
        steps = FLOWS[pos["flow"]]
        idx, ctx = pos["flow_step"], dict(pos["flow_ctx"])
        while idx < len(steps):
            built = await self._build(steps[idx], pos, pool, ctx)
            if isinstance(built, Step):
                pend = {"step": idx, "kind": built.kind, "to": built.to.lower(),
                        "data": built.data.lower(), "ctx": built.ctx or {}}
                self.db.update_zap_position(pos["position_id"], flow_step=idx, flow_ctx=ctx,
                                            pending_tx=json.dumps(pend))
                return {
                    "status": "sign", "flow": pos["flow"], "step_index": idx,
                    "step_count": len(steps), "kind": built.kind, "title": built.title,
                    "tx": {"chainId": 196, "to": built.to, "data": built.data,
                           "value": built.value},
                }
            ctx.update(built or {})  # a skipped step can still set context
            idx += 1
        # Every remaining step was skippable, so the flow completes here.
        self.db.update_zap_position(pos["position_id"], flow_step=idx, flow_ctx=ctx)
        await self._finish(self.db.get_zap_position(pos["position_id"]), pool, ctx, None)
        return {"status": "complete", "state": self.db.get_zap_position(pos["position_id"])["state"]}

    async def _approve(self, token: Token, spender: str, amount: int, owner: str,
                       why: str) -> Step | None:
        if amount <= 0 or await self._allowance(token.address, owner, spender) >= amount:
            return None
        return Step("approve", f"Approve {_fmt(amount, token.decimals)} {token.symbol} {why}",
                    token.address, _calldata("approve(address,uint256)", ["address", "uint256"],
                                             [spender, amount]))

    def _min(self, amount: int) -> int:
        return int(amount * (1 - settings.zap_slippage_pct / 100))

    async def _v2_swap(self, pool: Pool, sell: Token, buy: Token, amount: int,
                       owner: str) -> Step:
        # A taxed token loses its sell tax on the way into the pair and its
        # buy tax on the way out, and getAmountsOut knows about neither. The
        # minimum has to, or the swap reverts on INSUFFICIENT_OUTPUT_AMOUNT
        # every time, which is exactly what it did on a fork before this.
        arrives = amount * (10000 - sell.sell_tax_bps) // 10000
        st = await self.pool_state(pool)
        if sell.address == pool.other.address:
            # Selling the taxed token. These tokens keep their collected tax
            # in their own balance and, on a transfer into the pair, sell all
            # of it through that pair first (a "swap-back"), untaxed. Measured
            # on a fork, output under that model matched to the wei, while
            # getAmountsOut was 3.5% high and the swap reverted. The model is
            # the worst case: a token that sells less leaves the user better
            # off, never worse.
            r_in, r_out = st["reserve_other"], st["reserve_rwa"]
            x = await self._float(pool)
            if x > 0:
                x_out = v2_out(x, r_in, r_out, self.fee_bps)
                r_in, r_out = r_in + x, r_out - x_out
            received = v2_out(arrives, r_in, r_out, self.fee_bps)
        else:
            (amounts,) = await _eth_call(self.router, "getAmountsOut(uint256,address[])",
                                         ["uint256", "address[]"],
                                         [arrives, [sell.address, buy.address]], out=["uint256[]"])
            received = amounts[-1] * (10000 - buy.buy_tax_bps) // 10000
        r_in = st["reserve_rwa"] if sell.address == pool.rwa.address else st["reserve_other"]
        impact = amount / (r_in + amount) * 100
        if impact > settings.max_price_impact_pct:
            raise ValueError(f"swapping {_fmt(amount, sell.decimals)} {sell.symbol} would move "
                             f"this pool {impact:.2f}%, over the {settings.max_price_impact_pct}% limit")
        # The fee-on-transfer variant: several tokens in these pairs are
        # launchpad tokens, and the plain swap reverts on any token that taxes
        # transfers. For an untaxed token the two behave identically.
        data = _calldata(
            "swapExactTokensForTokensSupportingFeeOnTransferTokens("
            "uint256,uint256,address[],address,uint256)",
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [amount, self._min(received), [sell.address, buy.address], owner,
             int(time.time()) + DEADLINE_SECONDS])
        tax = sell.sell_tax_bps or buy.buy_tax_bps
        return Step("swap", f"Swap {_fmt(amount, sell.decimals)} {sell.symbol} for "
                            f"~{_fmt(received, buy.decimals)} {buy.symbol} in the "
                            f"{pool.key} pool ({impact:.2f}% impact"
                            + (f", {tax / 100:g}% {pool.other.symbol} transfer tax)" if tax else ")"),
                    self.router, data)

    async def _okx_swap(self, sell: Token, buy: Token, amount: int, owner: str) -> Step:
        try:
            unsigned, quote, _ = await self.dex.build_swap(
                from_address=sell.address, to_address=buy.address, amount_min_units=amount,
                user_address=owner, slippage_pct=settings.default_slippage_pct)
        except DexError as e:
            raise ValueError(f"could not route {sell.symbol} to {buy.symbol}: {e}")
        impact = quote.price_impact_pct
        if impact is not None and abs(impact) > settings.max_price_impact_pct:
            raise ValueError(f"{sell.symbol} to {buy.symbol} would cost {abs(impact):.2f}% in "
                             f"price impact, over the {settings.max_price_impact_pct}% limit")
        return Step("swap", f"Swap {_fmt(amount, sell.decimals)} {sell.symbol} for "
                            f"~{_fmt(quote.to_amount, buy.decimals)} {buy.symbol} via the OKX "
                            "DEX aggregator", unsigned.to, unsigned.data, str(unsigned.value or "0"))

    async def _build(self, kind: str, pos: dict[str, Any], pool: Pool,
                     ctx: dict[str, Any]) -> Step | dict[str, Any] | None:
        owner = pos["address"]
        rwa, other, park = pool.rwa, pool.other, self.park
        c = lambda k: int(ctx.get(k) or 0)  # noqa: E731

        if kind == "approve_wrap":
            if not ctx.get("deposit_is_underlying"):
                return None
            return await self._approve(pool.underlying, rwa.address, c("deposit_units"), owner,
                                       f"for wrapping into {rwa.symbol}")
        if kind == "wrap":
            if not ctx.get("deposit_is_underlying"):
                return {"rwa_amount": ctx["deposit_units"]}
            return Step("wrap", f"Wrap {_fmt(c('deposit_units'), pool.underlying.decimals)} "
                                f"{pool.underlying.symbol} into {rwa.symbol} (ERC-4626, the "
                                "token the incentive pool lists)",
                        rwa.address, _calldata("deposit(uint256,address)", ["uint256", "address"],
                                               [c("deposit_units"), owner]))
        if kind == "approve_rwa_router":
            return await self._approve(rwa, self.router, c("rwa_amount"), owner,
                                       "for the Uniswap V2 router")
        if kind == "swap_in":
            st = await self.pool_state(pool)
            s = zap_split(c("rwa_amount"), st["reserve_rwa"], st["reserve_other"],
                          self.fee_bps, other.buy_tax_bps)
            step = await self._v2_swap(pool, rwa, other, s, owner)
            step.ctx = {"swap_in": str(s)}
            return step
        if kind == "approve_other_router":
            return await self._approve(other, self.router, c("other_amount"), owner,
                                       "for the Uniswap V2 router")
        if kind == "add_liquidity":
            st = await self.pool_state(pool)
            a, b = pairable(c("rwa_amount") - c("swap_in"), c("other_amount"),
                            st["reserve_rwa"], st["reserve_other"])
            # The taxed token goes FIRST (tokenA). The router moves tokenA
            # into the pair, then tokenB, then mints. If the taxed token's
            # transfer sets off its swap-back while our RWA leg already sits
            # in the pair unsynced, the swap-back's swap() syncs that RWA
            # into the reserves and our mint no longer counts it: the whole
            # leg becomes a gift to the other LPs. With the taxed token first,
            # a swap-back runs before any of our tokens are in the pair, and
            # the worst case is the price move it causes.
            return Step("add_liquidity",
                        f"Add {_fmt(a, rwa.decimals)} {rwa.symbol} + {_fmt(b, other.decimals)} "
                        f"{other.symbol} to the {pool.key} pool",
                        self.router, _calldata(
                            "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)",
                            ["address", "address", "uint256", "uint256", "uint256", "uint256",
                             "address", "uint256"],
                            [other.address, rwa.address, b, a, self._min(b), self._min(a), owner,
                             int(time.time()) + DEADLINE_SECONDS]))

        if kind == "approve_lp_router":
            lp = int(pos["lp_amount"] or 0)
            held = await self._balance(pool.pair, owner)
            if held < lp:
                raise ValueError(f"this wallet now holds {_fmt(held, 18)} LP tokens, less than "
                                 f"the position's {_fmt(lp, 18)}. They may have been moved")
            return await self._approve(Token("UNI-V2", pool.pair, 18), self.router, lp, owner,
                                       "(pool LP) for the Uniswap V2 router")
        if kind == "remove_liquidity":
            lp = int(pos["lp_amount"] or 0)
            st = await self.pool_state(pool)
            min_rwa = self._min(lp * st["reserve_rwa"] // st["total_supply"])
            min_other = self._min(lp * st["reserve_other"] // st["total_supply"])
            return Step("remove_liquidity", f"Withdraw the position from the {pool.key} pool",
                        self.router, _calldata(
                            "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)",
                            ["address", "address", "uint256", "uint256", "uint256", "address", "uint256"],
                            [rwa.address, other.address, lp, min_rwa, min_other, owner,
                             int(time.time()) + DEADLINE_SECONDS]))
        if kind == "approve_other_router_exit":
            return await self._approve(other, self.router, c("other_out"), owner,
                                       "for the Uniswap V2 router")
        if kind == "swap_other_to_rwa":
            if c("other_out") <= 0:
                return {"rwa_to_sell": ctx.get("rwa_out", "0")}
            return await self._v2_swap(pool, other, rwa, c("other_out"), owner)
        if kind == "approve_rwa_okx":
            return await self._approve(rwa, self.reg.dex_approve_address, c("rwa_to_sell"), owner,
                                       "for the OKX DEX aggregator")
        if kind == "swap_rwa_to_park":
            return await self._okx_swap(rwa, park, c("rwa_to_sell"), owner)
        if kind == "approve_park_aave":
            return await self._approve(park, self.aave_pool, c("park_amount"), owner,
                                       "for Aave V3")
        if kind == "aave_supply":
            return Step("aave_supply", f"Supply {_fmt(c('park_amount'), park.decimals)} "
                                       f"{park.symbol} to Aave V3",
                        self.aave_pool, _calldata("supply(address,uint256,address,uint16)",
                                                  ["address", "uint256", "address", "uint16"],
                                                  [park.address, c("park_amount"), owner, 0]))

        if kind == "aave_withdraw":
            aave = await self.aave_state()
            owed = int(pos["parked_amount"]) * aave["income_index"] // int(pos["parked_index"])
            amount = min(owed, await self._balance(self.a_token, owner))
            if amount <= 0:
                raise ValueError("no Aave balance left to withdraw for this position")
            return Step("aave_withdraw", f"Withdraw {_fmt(amount, park.decimals)} {park.symbol} "
                                         "(principal + interest) from Aave V3",
                        self.aave_pool, _calldata("withdraw(address,uint256,address)",
                                                  ["address", "uint256", "address"],
                                                  [park.address, amount, owner]))
        if kind == "approve_park_okx":
            return await self._approve(park, self.reg.dex_approve_address, c("park_amount"), owner,
                                       "for the OKX DEX aggregator")
        if kind == "swap_park_to_rwa":
            return await self._okx_swap(park, rwa, c("park_amount"), owner)
        raise RuntimeError(f"unknown step kind {kind}")

    async def _receipt(self, tx_hash: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + RECEIPT_WAIT_SECONDS
        while True:
            r = await rpc._call("eth_getTransactionReceipt", [tx_hash])
            if r is not None or time.monotonic() > deadline:
                return r
            await asyncio.sleep(1.5)

    async def submit_step(self, position_id: str, address: str, tx_hash: str) -> dict[str, Any]:
        """Bind a wallet-broadcast hash to the step it was built for, wait
        for it to mine, and advance the flow on the amounts it credited."""
        tx_hash = validate_tx_hash(tx_hash)
        pos = self._owned(position_id, address)
        pend = self._pending(pos)
        if not pend:
            raise ValueError("no step is waiting for a signature. Ask for the next step first")
        if pend.get("tx_hash") and pend["tx_hash"] != tx_hash:
            raise ValueError(f"step already bound to {pend['tx_hash']}")
        # The public RPC is load-balanced and a node a block behind answers
        # null for a hash the wallet broadcast a second ago, so a miss is
        # retried for a while before being reported.
        deadline = time.monotonic() + RECEIPT_WAIT_SECONDS
        while (tx := await rpc._call("eth_getTransactionByHash", [tx_hash])) is None:
            if time.monotonic() > deadline:
                raise ValueError("that transaction is not on X Layer (yet). Wait a moment and retry")
            await asyncio.sleep(1.5)
        if ((tx.get("from") or "").lower() != pos["address"]
                or (tx.get("to") or "").lower() != pend["to"]
                or (tx.get("input") or "").lower() != pend["data"]):
            raise ValueError("that transaction is not the step Sarf built for this position")
        if not pend.get("tx_hash"):
            pend["tx_hash"] = tx_hash
            self.db.update_zap_position(pos["position_id"], pending_tx=json.dumps(pend))
        receipt = await self._receipt(tx_hash)
        if receipt is None:
            return {"status": "pending", "tx_hash": tx_hash}
        if int(receipt.get("status", "0x0"), 16) != 1:
            self.db.update_zap_position(pos["position_id"], pending_tx=None)
            self.db.log_zap_event(pos["position_id"], "step_failed", {
                "step": pend["kind"], "tx_hash": tx_hash})
            return {"status": "failed", "tx_hash": tx_hash,
                    "detail": "the transaction reverted on-chain. Nothing moved; ask for the "
                              "step again for a fresh build"}

        pool = self.pool(pos["pool_key"])
        ctx = {**pos["flow_ctx"], **pend.get("ctx", {})}
        got = transfers_in(receipt, pos["address"])
        g = lambda t: got.get(t.address.lower(), 0)  # noqa: E731
        kind = FLOWS[pos["flow"]][pend["step"]]
        if kind == "wrap":
            ctx["rwa_amount"] = str(g(pool.rwa))
        elif kind == "swap_in":
            ctx["other_amount"] = str(g(pool.other))
        elif kind == "add_liquidity":
            put = transfers_between(receipt, pos["address"], pool.pair)
            ctx["lp"] = str(got.get(pool.pair.lower(), 0))
            ctx["hold_rwa"] = str(put.get(pool.rwa.address.lower(), 0))
            ctx["hold_other"] = str(put.get(pool.other.address.lower(), 0))
            ctx["entry_sync"] = last_sync(receipt, pool.pair)
        elif kind == "remove_liquidity":
            ctx["rwa_out"], ctx["other_out"] = str(g(pool.rwa)), str(g(pool.other))
            ctx["rwa_to_sell"] = ctx["rwa_out"]
            ctx["exit_sync"] = last_sync(receipt, pool.pair)
        elif kind == "swap_other_to_rwa":
            ctx["rwa_to_sell"] = str(int(ctx.get("rwa_out") or 0) + g(pool.rwa))
        elif kind == "swap_rwa_to_park":
            ctx["park_amount"] = str(g(self.park))
        elif kind == "aave_withdraw":
            ctx["park_amount"] = str(g(self.park))
        elif kind == "swap_park_to_rwa":
            ctx["rwa_amount"] = str(g(pool.rwa))
        ctx.setdefault("txs", []).append({"step": kind, "tx_hash": tx_hash})

        nxt = pend["step"] + 1
        self.db.update_zap_position(pos["position_id"], flow_step=nxt, flow_ctx=ctx, pending_tx=None)
        self.db.log_zap_event(pos["position_id"], "step_confirmed", {
            "flow": pos["flow"], "step": kind, "tx_hash": tx_hash,
            "explorer_url": EXPLORER_TX.format(tx_hash)})
        if nxt >= len(FLOWS[pos["flow"]]):
            await self._finish(self.db.get_zap_position(pos["position_id"]), pool, ctx, receipt)
            return {"status": "complete", "tx_hash": tx_hash,
                    "state": self.db.get_zap_position(pos["position_id"])["state"]}
        return {"status": "confirmed", "tx_hash": tx_hash, "next_step_index": nxt}

    async def _finish(self, pos: dict[str, Any], pool: Pool, ctx: dict[str, Any],
                      receipt: dict[str, Any] | None) -> None:
        pid, flow = pos["position_id"], pos["flow"]
        st = await self.pool_state(pool)
        if flow in ("enter", "reenter"):
            sync = ctx.get("entry_sync")
            price = (self._price_from_sync(pool, tuple(sync), st["rwa_is_token0"])
                     if sync else st["price"])
            self.db.update_zap_position(
                pid, state="in_pool", flow=None, flow_step=0, flow_ctx={}, pending_tx=None,
                p_initial=price, lp_index_initial=st["lp_index"], entered_at=time.time(),
                lp_amount=ctx.get("lp"), hold_rwa=ctx.get("hold_rwa"),
                hold_other=ctx.get("hold_other"), parked_amount=None, parked_index=None,
                last_price=price, last_il_bps=0.0, last_checked_at=time.time())
            self.db.log_zap_event(pid, "entered" if flow == "enter" else "reentered", {
                "p_initial": price, "p_unit": f"{pool.other.symbol} per {pool.rwa.symbol}",
                "lp": ctx.get("lp"),
                "deposited": f"{_fmt(ctx.get('hold_rwa'), pool.rwa.decimals)} {pool.rwa.symbol} + "
                             f"{_fmt(ctx.get('hold_other'), pool.other.decimals)} {pool.other.symbol}",
                "note": None if flow == "enter" else "entry price reset to the re-entry price"})
        elif flow in ("close", "close_parked"):
            park_units = int(ctx.get("park_amount") or 0)
            proceeds = park_units / 10 ** self.park.decimals
            deposit_usd = pos.get("deposit_usd")
            self.db.update_zap_position(
                pid, state="closed", flow=None, flow_step=0, flow_ctx={}, pending_tx=None,
                lp_amount=None, parked_amount=None, parked_index=None,
                closed_at=time.time(), realized_amount=str(park_units),
                realized_usd=round(proceeds, 2), last_checked_at=time.time())
            self.db.log_zap_event(pid, "closed", {
                "proceeds": f"{_fmt(park_units, self.park.decimals)} {self.park.symbol} in your wallet",
                "realized_usd": round(proceeds, 2),
                "vs_deposit_usd": (round(proceeds - deposit_usd, 2)
                                   if deposit_usd is not None else None),
                "from": ctx.get("close_from"),
                "note": ("the position is closed; Sarf has stopped watching it and will not "
                         "re-enter")})
        else:
            aave = await self.aave_state()
            sync = ctx.get("exit_sync")
            p_exit = (self._price_from_sync(pool, tuple(sync), st["rwa_is_token0"])
                      if sync else st["price"])
            il_exit = il_bps(p_exit / pos["p_initial"]) if pos.get("p_initial") else None
            self.db.update_zap_position(
                pid, state="parked", flow=None, flow_step=0, flow_ctx={}, pending_tx=None,
                lp_amount=None, parked_amount=ctx.get("park_amount"),
                parked_index=str(aave["income_index"]), last_price=p_exit,
                last_il_bps=il_exit, last_checked_at=time.time())
            self.db.log_zap_event(pid, "exited", {
                "reason": "il_threshold" if (ctx.get("trigger_il_bps") or 0) > pos["il_threshold_bps"]
                          else "manual",
                "il_bps_at_trigger": ctx.get("trigger_il_bps"),
                "il_bps_at_exit": round(il_exit, 2) if il_exit is not None else None,
                "il_threshold_bps": pos["il_threshold_bps"],
                "p_initial": pos["p_initial"], "p_at_exit": p_exit,
                "parked": f"{_fmt(ctx.get('park_amount'), self.park.decimals)} {self.park.symbol} in Aave V3",
                "aave_supply_apy_pct": aave["supply_apy_pct"]})

    # ------------------------------------------------------------------ view

    async def view(self, pos: dict[str, Any], *, public_url: str | None = None,
                   for_owner: bool = False) -> dict[str, Any]:
        pool = self.pool(pos["pool_key"])
        st, usd, aave, flt = await asyncio.gather(
            self.pool_state(pool), self.rwa_usd(pool), self.aave_state(), self._float(pool),
            return_exceptions=True)
        flt = None if isinstance(flt, BaseException) else flt
        st = None if isinstance(st, BaseException) else st
        usd = None if isinstance(usd, BaseException) else usd
        aave = None if isinstance(aave, BaseException) else aave
        price = st["price"] if st else pos.get("last_price")
        p0 = pos.get("p_initial")
        il_now = il_bps(price / p0) if (price and p0) else None
        other_usd = usd / price if (usd and price) else None
        R, O = pool.rwa.decimals, pool.other.decimals

        value = hold = single = None
        lp_fee_pct = lp_fee_apr = None
        if pos["state"] in ("in_pool", "exit_pending") and st and pos.get("lp_amount"):
            share = int(pos["lp_amount"]) / st["total_supply"]
            amt_rwa = share * st["reserve_rwa"] / 10 ** R
            amt_other = share * st["reserve_other"] / 10 ** O
            if usd and other_usd:
                value = amt_rwa * usd + amt_other * other_usd
            if pos.get("lp_index_initial") and st["lp_index"]:
                lp_fee_pct = (st["lp_index"] / pos["lp_index_initial"] - 1) * 100
                days = (time.time() - (pos.get("entered_at") or time.time())) / 86400
                if days >= 1 / 24:
                    lp_fee_apr = lp_fee_pct * 365 / days
        elif pos["state"] == "closed":
            value = pos.get("realized_usd")
        elif pos["state"] in ("parked", "reentry_pending") and pos.get("parked_amount"):
            owed = int(pos["parked_amount"])
            if aave and pos.get("parked_index"):
                owed = owed * aave["income_index"] // int(pos["parked_index"])
            value = owed / 10 ** self.park.decimals
        if pos.get("hold_rwa") and usd and other_usd:
            hold = (int(pos["hold_rwa"]) / 10 ** R * usd
                    + int(pos["hold_other"]) / 10 ** O * other_usd)
        if usd:
            dep_units = int(pos["deposit_amount"])
            dep_rwa = dep_units / 10 ** R
            if pos["deposit_symbol"] == pool.underlying.symbol:
                try:
                    dep_rwa /= await self._wrap_rate(pool)
                except Exception:
                    pass
            single = dep_rwa * usd

        r2 = lambda x: round(x, 2) if x is not None else None  # noqa: E731
        il_block = {
            "current_bps": r2(il_now),
            "exit_threshold_bps": pos["il_threshold_bps"],
            "reentry_threshold_bps": pos["reentry_bps"],
            "price_ratio_r": round(price / p0, 6) if (price and p0) else None,
            "p_initial": p0, "p_current": price,
            "price_unit": f"{pool.other.symbol} per {pool.rwa.symbol}",
            "formula": "IL = 1 - 2*sqrt(r)/(1+r), r = P_current/P_initial",
            "tracking": ("vs the entry price, while parked: it decides when to re-enter"
                         if pos["state"] in ("parked", "reentry_pending") else
                         "vs the entry price of the live position"),
        }
        yield_block = {
            "aave_usdt_supply_apy_pct": aave["supply_apy_pct"] if aave else None,
            "pool_fee_return_since_entry_pct": r2(lp_fee_pct),
            "pool_fee_apr_pct_measured": r2(lp_fee_apr),
            "incentive_rewards": ("USDG paid by X Layer to LPs in this pool during the program "
                                  f"window ({self.incentive['window']}). Claimed on OKX's side, "
                                  "not tracked by Sarf"),
            # Never shown without the IL beside it. See the headline.
            "il_bps_now": r2(il_now),
        }

        # --- what this position has actually earned -------------------------
        #
        # Three sources, and they behave differently. Pool fees and Aave
        # interest accrue INTO the position: they need no claim and they are
        # already inside the value above. The X Layer incentive does not — it
        # is paid by OKX, off this chain, and has to be collected there.
        #
        # There is deliberately no estimate of the USDG amount. Rewards are a
        # share of a pot split across the pools X Layer selects, and we do not
        # know how many that is; a number derived from a guess at the divisor
        # would look like a measurement. What IS knowable is the share of the
        # pool you hold, which is what the reward is proportional to, so that
        # is what gets reported.
        pool_fees_usd = None
        if value is not None and lp_fee_pct:
            g = lp_fee_pct / 100
            pool_fees_usd = value * g / (1 + g)
        aave_interest_usd = None
        if pos["state"] in ("parked", "reentry_pending") and value is not None and pos.get("parked_amount"):
            aave_interest_usd = value - int(pos["parked_amount"]) / 10 ** self.park.decimals
        pool_share_pct = None
        if st and st.get("total_supply") and pos.get("lp_amount"):
            pool_share_pct = 100 * int(pos["lp_amount"]) / st["total_supply"]
        # What the programme has actually paid this wallet, counted from the
        # token's own Transfer logs since the position first entered a pool.
        # Nothing here is apportioned or projected: each drop is a hash.
        drops = self.db.zap_rewards_for(pos["address"], since=pos.get("entered_at"))
        received = sum(int(d["amount"]) for d in drops) / 10 ** self.REWARD_DECIMALS
        realized_usd = pos.get("realized_usd")
        earned = [x for x in (pool_fees_usd, aave_interest_usd) if x is not None]
        earnings = {
            "accrues_into_the_position": {
                "pool_fees_usd": r2(pool_fees_usd),
                "pool_fee_return_pct": r2(lp_fee_pct),
                "pool_fee_apr_pct_measured": r2(lp_fee_apr),
                "aave_interest_usd": r2(aave_interest_usd),
                "aave_supply_apy_pct": aave["supply_apy_pct"] if aave else None,
                "note": ("Both are already inside the position value and need no claim. "
                         "Uniswap fees compound into the pool position itself"),
            },
            "paid_separately": {
                "programme": self.incentive["name"],
                "window": self.incentive["window"],
                "pot": self.incentive.get("rewards"),
                "your_pool_share_pct": round(pool_share_pct, 4) if pool_share_pct is not None else None,
                # Received, not estimated. None of these are apportioned to a
                # single position: they are payments to the wallet, and the
                # wallet is what the programme pays.
                "received_usdg": round(received, 6),
                "drops": [{"amount": int(d["amount"]) / 10 ** d["decimals"],
                           "symbol": d["symbol"], "tx_hash": d["tx_hash"],
                           "from": d["sender"], "block": d["block"], "seen_at": d["at"]}
                          for d in drops[:20]],
                "drop_count": len(drops),
                "counted_since": pos.get("entered_at"),
                "amount_owed_usdg": None,
                "why_no_owed_amount": ("What is OWED cannot be read: the pot is split across the "
                                       "pools X Layer selects and worked out on its side. What has "
                                       "ARRIVED is above, counted from the token's own transfer "
                                       "log, so every figure is a transaction you can open"),
                "claimable_here": False,
                "claim": {
                    "where": "OKX Wallet",
                    "steps": ["Hold USDG on X Layer via OKX Wallet",
                              "Connect your wallet to the exchange to activate the rewards programme",
                              "Claim under the USDG token page in OKX Wallet"],
                    "url": "https://web3.okx.com/portfolio",
                    "instructions_url": "https://www.okx.com/en-us/help/usdg-on-x-layer-faq",
                    "programme_url": "https://www.okx.com/en-us/learn/xlayer-blog-incentive-programm-2",
                    "note": ("Rewards are credited by OKX, not by a contract Sarf can call, so "
                             "there is nothing here to sign. Verified 2026-09-20: no on-chain "
                             "distributor and no claim contract is published"),
                },
            },
            "realized_usd": realized_usd,
            "total_accrued_usd": r2(sum(earned)) if earned else None,
            # The rule for this feature: no yield figure without the IL that
            # paid for it standing next to it.
            "il_bps_now": r2(il_now),
        }
        earning = ("Aave supply APY " + (f"{aave['supply_apy_pct']:.2f}%" if aave else "unavailable")
                   if pos["state"] in ("parked", "reentry_pending")
                   else "pool fees " + (f"{lp_fee_pct:+.3f}% since entry" if lp_fee_pct is not None
                                        else "not measured yet"))
        if pos["state"] == "closed":
            back = pos.get("realized_usd")
            dep = pos.get("deposit_usd")
            headline = (f"Closed. {_fmt(pos.get('realized_amount'), self.park.decimals)} "
                        f"{self.park.symbol} back in your wallet"
                        + (f" against ${dep:,.2f} deposited" if dep is not None else "")
                        + (f" ({back - dep:+,.2f})" if (back is not None and dep is not None) else ""))
        else:
            headline = (f"IL {il_now / 100:.2f}% (exit above {pos['il_threshold_bps'] / 100:.2f}%, "
                        f"re-enter below {pos['reentry_bps'] / 100:.2f}%) · {earning}"
                        if il_now is not None else
                        f"IL not measured yet: no entry price until the deposit is signed · {earning}")
        pend = self._pending(pos)
        out = {
            "position_id": pos["position_id"],
            "position_url": public_url,
            "owner": pos["address"] if for_owner else pos["address"][:6] + "…" + pos["address"][-4:],
            "state": pos["state"],
            "state_label": STATE_LABELS.get(pos["state"], pos["state"]),
            "headline": headline,
            "pool": {**pool.public(), "incentive_program": self.incentive["name"],
                     "incentive_window": self.incentive["window"]},
            "deposit": {"asset": pos["deposit_symbol"],
                        "amount": _fmt(pos["deposit_amount"], R),
                        "usd_at_deposit": r2(pos.get("deposit_usd"))},
            "il": il_block,
            "yield": yield_block,
            "earnings": earnings,
            "costs": self.costs(pool, pos["il_threshold_bps"], st, flt),
            "value": {
                "current_usd": r2(value),
                "hold_50_50_usd": r2(hold),
                "hold_single_asset_usd": r2(single),
                "vs_hold_50_50_usd": r2(value - hold) if (value is not None and hold) else None,
                "vs_hold_single_asset_usd": r2(value - single) if (value is not None and single) else None,
                "note": ("hold_50_50 is the IL benchmark: the same two amounts held in a wallet. "
                         "hold_single_asset is what the original deposit would be worth untouched. "
                         "Pool values include fees earned; incentive rewards are paid separately"),
            },
            "flow": ({"name": pos["flow"], "step": pos["flow_step"],
                      "steps": FLOWS[pos["flow"]],
                      "awaiting_confirmation": pend.get("tx_hash")} if pos["flow"] else None),
            "action_needed": self._action(pos),
            "auto_watch": settings.zap_autoexit_enabled,
            "last_checked_at": pos.get("last_checked_at"),
            "history": self.db.zap_events(pos["position_id"]),
            "created_at": pos["created_at"],
            "disclosure": ("xStocks track a share price and convey no ownership, dividends or voting "
                           "rights. Liquidity provision carries impermanent loss, and the paired "
                           f"token ({pool.other.symbol}) is a volatile ecosystem token."),
        }
        return out

    @staticmethod
    def _action(pos: dict[str, Any]) -> str | None:
        return {
            "entering": "Sign the deposit steps on the position page",
            "exit_pending": "Sign the exit on the position page: withdraw, convert to USDT, supply to Aave",
            "exiting": "Finish signing the exit steps on the position page",
            "reentry_pending": "Sign the re-entry on the position page: withdraw from Aave, re-split, re-deposit",
            "reentering": "Finish signing the re-entry steps on the position page",
            "close_pending": "Sign the close on the position page: unwind, convert to USDT, into your wallet",
            "closing": "Finish signing the close on the position page",
        }.get(pos["state"])

    # --------------------------------------------------------------- watcher

    async def tick(self) -> list[tuple[str, str]]:
        """One pass of the IL watcher. -> [(position_id, transition)]."""
        moved: list[tuple[str, str]] = []
        positions = [p for p in self.db.zap_positions_in(LIVE_STATES) if p.get("p_initial")]
        states: dict[str, dict[str, Any]] = {}
        for pos in positions:
            key = pos["pool_key"]
            if key not in states:
                try:
                    states[key] = await self.pool_state(self.pool(key))
                except Exception as e:
                    log.warning("zap watch: %s unreadable: %s", key, e)
                    continue
            price = states[key]["price"]
            il = il_bps(price / pos["p_initial"])
            self.db.update_zap_position(pos["position_id"], last_price=price, last_il_bps=il,
                                        last_checked_at=time.time())
            snap = {"il_bps": round(il, 2), "p_initial": pos["p_initial"], "p_current": price,
                    "il_threshold_bps": pos["il_threshold_bps"], "reentry_bps": pos["reentry_bps"]}
            s, pid = pos["state"], pos["position_id"]
            rules = [
                ("in_pool", il > pos["il_threshold_bps"], "exit_pending", "exit_triggered"),
                ("exit_pending", il <= pos["reentry_bps"], "in_pool", "exit_trigger_cleared"),
                ("parked", il <= pos["reentry_bps"], "reentry_pending", "reentry_triggered"),
                ("reentry_pending", il > pos["il_threshold_bps"], "parked", "reentry_trigger_cleared"),
            ]
            for frm, cond, to, event in rules:
                if s == frm and cond and self.db.update_zap_position(pid, expect_state=frm, state=to):
                    self.db.log_zap_event(pid, event, snap)
                    moved.append((pid, event))
        return moved

    # ------------------------------------------------------- reward arrivals

    # USDG on X Layer, read from the aggregator's own token list on
    # 2026-09-20 rather than a third-party listing, and checked on-chain:
    # symbol USDG, name "Global Dollar", 6 decimals.
    REWARD_TOKEN = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
    REWARD_SYMBOL = "USDG"
    REWARD_DECIMALS = 6
    # The public RPC refuses wider eth_getLogs ranges, and silently enough
    # that an earlier scan of mine "found" zero transfers for a whole day
    # because every 2000-block window was a 400 nobody looked at.
    LOG_WINDOW = 100
    MAX_WINDOWS_PER_PASS = 30

    async def scan_rewards(self) -> int:
        """Record incentive payments that have landed, -> rows added.

        Reads the reward token's Transfer logs at the head and keeps the ones
        addressed to somebody who holds a zap position. Nothing is estimated
        and nothing is attributed: a row exists only because a transfer does.
        """
        holders = {a.lower() for a in self.db.zap_reward_addresses()}
        if not holders:
            return 0
        head = int(await rpc._call("eth_blockNumber", []), 16)
        mark = self.db.get_stat("zap_reward_cursor")
        cursor = int(mark[0]["block"]) if mark else head - self.LOG_WINDOW
        # A long outage must not turn into a thousand calls in one pass; the
        # scan catches up over several, oldest first.
        cursor = max(cursor, head - self.LOG_WINDOW * self.MAX_WINDOWS_PER_PASS)
        added, now = 0, time.time()
        while cursor < head:
            to_block = min(cursor + self.LOG_WINDOW - 1, head)
            try:
                logs = await rpc._call("eth_getLogs", [{
                    "address": self.REWARD_TOKEN, "topics": [TRANSFER_TOPIC],
                    "fromBlock": hex(cursor), "toBlock": hex(to_block)}])
            except Exception:
                # Leave the cursor where it is and try again next pass, rather
                # than skipping a window and losing whatever landed in it.
                logging.getLogger("sarf.zap").warning(
                    "reward scan failed for blocks %s-%s", cursor, to_block, exc_info=True)
                break
            rows = []
            for lg in logs or []:
                topics = lg.get("topics") or []
                if len(topics) < 3:
                    continue
                to_addr = "0x" + topics[2][-40:]
                if to_addr.lower() not in holders:
                    continue
                rows.append({
                    "tx_hash": lg["transactionHash"], "log_index": int(lg["logIndex"], 16),
                    "address": to_addr.lower(), "token": self.REWARD_TOKEN,
                    "symbol": self.REWARD_SYMBOL, "amount": str(int(lg["data"], 16)),
                    "decimals": self.REWARD_DECIMALS,
                    "sender": ("0x" + topics[1][-40:]).lower(),
                    "block": int(lg["blockNumber"], 16), "at": now,
                })
            added += self.db.record_zap_rewards(rows)
            cursor = to_block + 1
            self.db.set_stat("zap_reward_cursor", {"block": cursor})
        return added

    async def watch_rewards_forever(self, every: float = 90.0) -> None:
        """Runs whether or not auto-exit does: this only reads logs."""
        log = logging.getLogger("sarf.zap")
        while True:
            try:
                n = await self.scan_rewards()
                if n:
                    log.info("recorded %d incentive arrival(s)", n)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("reward scan pass failed", exc_info=True)
            await asyncio.sleep(every)

    async def watch_forever(self) -> None:
        while True:
            try:
                for pid, event in await self.tick():
                    log.info("zap %s: %s", pid, event)
            except Exception:
                log.exception("zap watch pass failed")
            await asyncio.sleep(settings.zap_watch_interval_seconds)
