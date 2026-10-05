"""OKX DEX aggregator client for X Layer (chain 196).

This module READS prices and BUILDS unsigned transactions. It never signs and
never broadcasts — that stays with the user's own wallet, same custody model
Sarf has always had. Nothing here touches a keyring.

Transport: OKX's DEX REST API refuses anonymous calls (`50103 OK-ACCESS-KEY
can not be empty`), so calls go through one of two transports:

  * `http`  — OKX DEX API v6 with the operator's own credentials from env.
              (v5 is deprecated and answers 50050.)
              The production path; set OKX_API_KEY / OKX_API_SECRET /
              OKX_API_PASSPHRASE.
  * `cli`   — the locally-installed, already-authenticated `onchainos`
              binary. Used when no API credentials are configured.

If neither is available every call fails closed with a clear operator-facing
message rather than silently returning a fake price — a trading surface that
guesses is worse than one that is down.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .registry import CHAIN_ID

_OKX_BASE = os.environ.get("OKX_BASE_URL", "https://web3.okx.com").rstrip("/")

# --- pacing ------------------------------------------------------------------
# OKX rate-limits per key, and the whole product fans out: a list card prices
# forty assets, a portfolio prices every holding. Fired at once those become
# forty simultaneous requests and OKX answers 429 to most of them, which
# surfaced as assets that "cannot be priced" rather than as the throttle it was.
#
# One shared pacer for the whole process, not per client instance: the limit is
# per API key, so two clients spacing themselves independently would still
# collide. Requests go out at most one per _MIN_INTERVAL, and a 429 is retried
# with exponential backoff and jitter rather than surfaced as an unpriceable
# asset. Serialised this way, forty quotes take a few seconds instead of
# failing in one.
_MIN_INTERVAL = float(os.environ.get("OKX_MIN_REQUEST_INTERVAL", "0.22"))
_MAX_RETRIES = int(os.environ.get("OKX_MAX_RETRIES", "4"))
_RETRY_BASE = float(os.environ.get("OKX_RETRY_BASE_SECONDS", "0.4"))

# Transient upstream conditions, not verdicts about the pair. 50026 is OKX's
# generic "System error. Try again later" and arrives in bursts under load —
# treated as fatal it becomes an asset that cannot be priced, which is a claim
# about the asset that is simply untrue.
_RETRYABLE_CODES = {"50026", "50011", "50013"}

_pace_lock = asyncio.Lock()
_last_request = 0.0


async def _pace() -> None:
    """Space outbound calls so a fan-out does not trip the rate limit."""
    global _last_request
    async with _pace_lock:
        wait = _last_request + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()


_CLI = os.environ.get("ONCHAINOS_BIN") or shutil.which("onchainos") or "/root/.local/bin/onchainos"

# A quote is a price snapshot. Anything older than this must not be used to
# build an order the user is about to sign.
QUOTE_MAX_AGE_SECONDS = 30


class DexError(RuntimeError):
    """Upstream aggregator failure. Message is safe to surface."""


@dataclass(frozen=True)
class Quote:
    from_address: str
    to_address: str
    from_amount: int          # minimal units
    to_amount: int            # minimal units
    price_impact_pct: float | None
    route: list[str]          # human-readable DEX names, in order
    estimated_gas: int | None
    fetched_at: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    @property
    def stale(self) -> bool:
        return self.age_seconds > QUOTE_MAX_AGE_SECONDS


@dataclass(frozen=True)
class UnsignedTx:
    """An X Layer transaction for the user's wallet to sign. No signature."""

    to: str
    data: str
    value: str
    gas: int
    gas_price: str
    min_receive: int | None
    chain_id: int = CHAIN_ID

    def as_dict(self) -> dict[str, Any]:
        return {
            "chainId": self.chain_id,
            "to": self.to,
            "data": self.data,
            "value": self.value,
            "gas": self.gas,
            "gasPrice": self.gas_price,
            "minReceiveAmount": self.min_receive,
        }


def _router_names(router_list: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for hop in router_list or []:
        for sub in hop.get("subRouterList") or []:
            for dex in sub.get("dexProtocol") or []:
                n = dex.get("dexName")
                if n and n not in names:
                    names.append(n)
    return names


class _McpSession:
    """One long-lived `onchainos mcp` subprocess, reused for every CLI-transport
    call for the rest of this process's life, instead of spawning a fresh
    process per call.

    A fresh subprocess cost ~0.6s of process-start overhead alone, before any
    network round trip. Fanned out over the 20+ symbols a single market page
    prices concurrently, that is what stacked into the multi-second "Reading
    X Layer..." stalls: not one slow call, but a pile of competing process
    spawns on a box that was already short on CPU. A warm MCP round trip over
    one process's stdio answers in whatever the network call itself takes
    (tens to a few hundred ms), because there is no process to start.

    Every call is serialised behind `_call_lock`, one request in flight at a
    time. That was not the original design — JSON-RPC ids should let
    concurrent callers pipeline through one connection — but sending several
    `tools/call` requests before reading their replies measurably breaks this
    particular binary: it either hung mid-batch or the whole process died
    with "Connection lost" partway through, confirmed by driving it directly
    outside of asyncio's scheduling. A queued 300ms-ish round trip per symbol
    is the real, verified cost of this transport; a crashed shared process
    taking every in-flight caller down with it is not an acceptable trade for
    shaving that down.
    """

    def __init__(self, *, timeout: float):
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            self._proc = await asyncio.create_subprocess_exec(
                _CLI, "mcp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await self._request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "sarf", "version": "1"},
            })
            await self._notify("notifications/initialized")

    async def _write(self, obj: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self._timeout)
        if not line:
            # The process ended (crashed, or `onchainos` was upgraded under
            # us). Let the NEXT call respawn rather than retry a connection
            # that is already dead.
            dead, self._proc = self._proc, None
            if dead.returncode is None:
                dead.kill()
            raise DexError("quote backend (mcp) connection closed")
        return json.loads(line)

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        async with self._call_lock:
            await self._ensure_started()
            try:
                msg = await self._request("tools/call", {"name": tool, "arguments": arguments})
            except asyncio.TimeoutError:
                raise DexError(f"quote backend (mcp) timed out calling {tool}")
            except json.JSONDecodeError:
                raise DexError("quote backend (mcp) returned malformed output")
        if "error" in msg:
            raise DexError(f"quote backend (mcp) error: {msg['error'].get('message')}")
        result = msg.get("result") or {}
        items = result.get("content") or [{}]
        text = items[0].get("text", "")
        if result.get("isError"):
            raise DexError(str(text)[:200] or f"{tool} rejected the request")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


class OkxDexClient:
    def __init__(self, *, timeout: float = 20.0):
        self._timeout = timeout
        self._key = os.environ.get("OKX_API_KEY", "").strip()
        self._secret = os.environ.get("OKX_API_SECRET", "").strip()
        self._passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
        self._project = os.environ.get("OKX_PROJECT_ID", "").strip()
        # Set once the configured key/secret/passphrase have been rejected
        # outright: any 401 from this signed endpoint, whatever OKX's own
        # error code inside the body says (seen both 50125 "no access" on
        # market/candles and 50114 "Invalid Authority" on aggregator/quote
        # for the same dead key). That is not a transient failure — retrying
        # it is pointless for the rest of this process's life, so treat it
        # exactly like no credential at all and fall through to the CLI
        # transport, same as the docstring above already promises for an
        # unconfigured key.
        self._http_dead = False
        self._mcp = _McpSession(timeout=timeout)

    @property
    def transport(self) -> str:
        if self._key and self._secret and self._passphrase and not self._http_dead:
            return "http"
        if os.path.exists(_CLI):
            return "cli"
        return "none"

    # ------------------------------------------------------------- transports

    def _sign_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        msg = f"{ts}{method.upper()}{path}{body}"
        sign = base64.b64encode(
            hmac.new(self._secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()
        h = {
            "OK-ACCESS-KEY": self._key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }
        if self._project:
            h["OK-ACCESS-PROJECT"] = self._project
        return h

    async def _http(self, path: str, params: dict[str, Any]) -> Any:
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        full = f"{path}?{qs}" if qs else path
        last: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            await _pace()
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                r = await c.get(_OKX_BASE + full, headers=self._sign_headers("GET", full))
            # Not a failure of the pair — a failure of how fast we asked. Priced
            # one asset at a time neither of these appears; pricing forty for a
            # list card, they took 37 of them, and the card showed a column of
            # dashes as though the assets themselves were unpriceable.
            if r.status_code == 429:
                last = DexError("OKX DEX API returned HTTP 429")
            elif r.status_code != 200:
                if r.status_code == 401:
                    # A signed-HMAC request failing auth is never about this
                    # one call — it is the key itself. Confirmed two distinct
                    # OKX error codes for the exact same dead key depending on
                    # endpoint (50125 on market/candles, 50114 "Invalid
                    # Authority" on aggregator/quote), so the reliable signal
                    # is the 401 itself, not any one code inside the body.
                    self._http_dead = True
                raise DexError(f"OKX DEX API returned HTTP {r.status_code}")
            else:
                body = r.json()
                code = str(body.get("code"))
                if code == "0":
                    return body.get("data")
                if code not in _RETRYABLE_CODES:
                    raise DexError(f"OKX DEX API error {code}: {body.get('msg')}")
                last = DexError(f"OKX DEX API error {code}: {body.get('msg')}")
            if attempt >= _MAX_RETRIES:
                raise last
            await asyncio.sleep(_RETRY_BASE * (2 ** attempt) * (0.5 + random.random()))
        raise last or DexError("OKX DEX API did not answer")

    async def _cli(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call one `onchainos` MCP tool through the shared warm session.

        Replaces a per-call `onchainos <args>` subprocess (see _McpSession's
        docstring for why that was the thing actually making prices slow).
        """
        return await self._mcp.call(tool, arguments)

    async def _dispatch(self, *, http_path: str, http_params: dict[str, Any],
                        cli_tool: str, cli_args: dict[str, Any]) -> Any:
        """HTTP first; if it dies mid-call (the key just got rejected), retry
        the SAME request over CLI immediately rather than surfacing a failure
        the caller would read as "this pair has no route" — a transport
        falling over has nothing to do with the asset it was asked about.

        Only retries once and only for that specific transition, so a pair
        that genuinely has no route still fails the normal way, and a
        persistently broken CLI cannot turn into a retry loop.
        """
        if self.transport == "http":
            try:
                return await self._http(http_path, http_params)
            except DexError:
                if self._http_dead and os.path.exists(_CLI):
                    return await self._cli(cli_tool, cli_args)
                raise
        return await self._cli(cli_tool, cli_args)

    @staticmethod
    def _first(data: Any) -> dict[str, Any]:
        if isinstance(data, list):
            if not data:
                raise DexError("no route available for this pair on X Layer")
            return data[0]
        if isinstance(data, dict):
            return data
        raise DexError("unexpected response from the quote backend")

    # ----------------------------------------------------------------- public

    async def quote(self, from_address: str, to_address: str, amount_min_units: int) -> Quote:
        """Read-only price estimate. Never used as an execution authorization."""
        if amount_min_units <= 0:
            raise DexError("quote amount must be positive")
        if self.transport == "none":
            raise DexError(
                "no quote backend configured: set OKX_API_KEY/OKX_API_SECRET/"
                "OKX_API_PASSPHRASE, or install the onchainos CLI"
            )
        params = {
            "chainIndex": CHAIN_ID, "chainId": CHAIN_ID,
            "fromTokenAddress": from_address, "toTokenAddress": to_address,
            "amount": amount_min_units,
        }
        raw = await self._dispatch(
            http_path="/api/v6/dex/aggregator/quote", http_params=params,
            cli_tool="swap_quote", cli_args={
                "chain": str(CHAIN_ID), "from": from_address, "to": to_address,
                "amount": str(amount_min_units),
            })
        d = self._first(raw)
        try:
            to_amount = int(d["toTokenAmount"])
        except (KeyError, TypeError, ValueError):
            raise DexError("quote backend did not return an output amount")
        impact = d.get("priceImpactPercentage", d.get("priceImpactPercent"))
        try:
            impact_f = float(impact) if impact not in (None, "") else None
        except (TypeError, ValueError):
            impact_f = None
        gas = d.get("estimateGasFee")
        return Quote(
            from_address=from_address,
            to_address=to_address,
            from_amount=amount_min_units,
            to_amount=to_amount,
            price_impact_pct=impact_f,
            route=_router_names(d.get("dexRouterList")),
            estimated_gas=int(gas) if str(gas or "").isdigit() else None,
            fetched_at=time.time(),
        )

    async def candles(self, address: str, *, bar: str = "1H", limit: int = 24) -> list[dict[str, float]]:
        """Recent OHLC candles for a token on X Layer, oldest first.

        Display only: sparklines and a 24h change on the market pages. Nothing
        sizes an order or checks a cap from these. The HTTP API answers rows as
        [ts, o, h, l, c, vol, volUsd, confirm]; the CLI answers objects. Both
        come back in the same shape.
        """
        if self.transport == "none":
            raise DexError("no market data backend configured")
        limit = max(1, min(int(limit), 299))
        raw = await self._dispatch(
            http_path="/api/v6/dex/market/candles",
            http_params={"chainIndex": CHAIN_ID, "tokenContractAddress": address,
                        "bar": bar, "limit": limit},
            cli_tool="market_kline",
            cli_args={"chain": str(CHAIN_ID), "address": address, "bar": bar, "limit": limit})
        out = []
        for row in raw or []:
            try:
                if isinstance(row, dict):
                    ts, o, h, l_, c, vu = (row["ts"], row["o"], row["h"], row["l"], row["c"],
                                           row.get("volUsd", 0))
                else:
                    ts, o, h, l_, c, _, vu = row[:7]
                out.append({"ts": int(ts) / 1000, "o": float(o), "h": float(h), "l": float(l_),
                            "c": float(c), "vol_usd": float(vu or 0)})
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        out.sort(key=lambda r: r["ts"])
        return out

    async def _gas_limit(self, *, user_address: str, to: str, data: str,
                         value: int, quoted: int) -> int:
        """The gas limit to hand the wallet.

        The aggregator's own estimate is routinely too small: a native-OKB
        multi-hop swap it quoted at 550,258 needed 804,299 on-chain, and a
        wallet that signs the quoted figure runs out of gas mid-route. That is
        a reverted transaction the user paid for, and it looks like the site
        did nothing. So the chain is asked, and the answer gets a quarter more
        headroom for a route whose cost moves between building and signing.

        Estimation reverts for an ERC-20 that has not been approved yet, which
        is an ordinary state on the way to a first sell, so a failure widens
        the quoted figure instead of refusing to build the order.
        """
        from . import rpc  # here, not at import time: rpc imports the registry

        try:
            measured = await rpc.estimate_gas(
                from_address=user_address, to=to, data=data, value=value)
        except Exception:
            measured = 0
        if measured:
            return max(int(measured * 1.25), quoted)
        # Estimation failed. Widen, but only mildly when OKB is the sell leg:
        # a wallet refuses to sign when value + gas x price exceeds the
        # balance, so an inflated limit can block a swap the user can afford.
        widen = 1.25 if value > 0 else 1.8
        return int(quoted * widen) or 900_000

    def supports_fee(self) -> bool:
        """Only the HTTP transport can attach referral fee parameters.

        The CLI does not expose them, so a fee-configured server running on the
        CLI transport trades WITHOUT a fee rather than pretending to collect
        one. Callers must surface that instead of assuming the fee applied.
        """
        return self.transport == "http"

    async def build_swap(
        self,
        *,
        from_address: str,
        to_address: str,
        amount_min_units: int,
        user_address: str,
        slippage_pct: float,
        fee_percent: float | None = None,
        fee_recipient: str | None = None,
        fee_on: str = "from",
    ) -> tuple[UnsignedTx, Quote, bool]:
        """Build the unsigned swap transaction for `user_address` to sign.

        `user_address` is the session's verified wallet — it comes from the
        auth layer, never from a tool argument, so the built transaction can
        only ever spend from the account that asked for it.

        Returns (tx, quote, fee_applied). `fee_applied` is False whenever the
        transport could not attach the fee, so the caller can tell the user the
        truth rather than inferring it from config.
        """
        t = self.transport
        if t == "none":
            raise DexError("no quote backend configured; cannot build a transaction")

        want_fee = bool(fee_percent and fee_recipient and fee_percent > 0)
        fee_applied = False
        if t == "http":
            params: dict[str, Any] = {
                "chainIndex": CHAIN_ID, "chainId": CHAIN_ID,
                "fromTokenAddress": from_address, "toTokenAddress": to_address,
                "amount": amount_min_units, "userWalletAddress": user_address,
                # v6 renamed this and takes PERCENT ("1" = 1%), not the
                # fraction v5 wanted. Sending the old name 400s (50014).
                "slippagePercent": slippage_pct,
            }
            if want_fee:
                # OKX allows commission on the from-token OR the to-token, not
                # both. We always pick whichever leg is the stablecoin so the
                # fee is denominated in dollars, never in a volatile equity.
                params["feePercent"] = f"{fee_percent:.6f}".rstrip("0").rstrip(".")
                key = ("fromTokenReferrerWalletAddress" if fee_on == "from"
                       else "toTokenReferrerWalletAddress")
                params[key] = fee_recipient
                fee_applied = True
            cli_args = {
                "chain": str(CHAIN_ID), "from": from_address, "to": to_address,
                "amount": str(amount_min_units),
                "wallet": user_address, "slippage": str(slippage_pct),
            }
            try:
                raw = await self._http("/api/v6/dex/aggregator/swap", params)
            except DexError:
                if not (self._http_dead and os.path.exists(_CLI)):
                    raise
                # The key died mid-call: retry over CLI, but the fee cannot
                # ride along on that transport, so say so truthfully rather
                # than claiming a fee that was never actually attached.
                fee_applied = False
                raw = await self._cli("swap_swap", cli_args)
        else:
            raw = await self._cli("swap_swap", {
                "chain": str(CHAIN_ID), "from": from_address, "to": to_address,
                "amount": str(amount_min_units),
                "wallet": user_address, "slippage": str(slippage_pct),
            })
        d = self._first(raw)
        tx = d.get("tx") or {}
        rr = d.get("routerResult") or {}
        if not tx.get("to") or not tx.get("data"):
            raise DexError("quote backend did not return transaction data")

        # Defence in depth: the aggregator echoes the wallet it built for. If
        # that is not our session's address, something is wrong upstream and we
        # refuse rather than hand the user a transaction that spends elsewhere.
        built_for = str(tx.get("from") or user_address).lower()
        if built_for != user_address.lower():
            raise DexError("quote backend built a transaction for a different wallet")

        min_recv = tx.get("minReceiveAmount")
        gas = await self._gas_limit(
            user_address=user_address, to=str(tx["to"]), data=str(tx["data"]),
            value=int(tx.get("value") or 0), quoted=int(tx.get("gas") or 0),
        )
        unsigned = UnsignedTx(
            to=str(tx["to"]),
            data=str(tx["data"]),
            value=str(tx.get("value", "0")),
            gas=gas,
            gas_price=str(tx.get("gasPrice") or "0"),
            min_receive=int(min_recv) if str(min_recv or "").isdigit() else None,
        )
        impact = rr.get("priceImpactPercent")
        try:
            impact_f = float(impact) if impact not in (None, "") else None
        except (TypeError, ValueError):
            impact_f = None
        quote = Quote(
            from_address=from_address,
            to_address=to_address,
            from_amount=amount_min_units,
            to_amount=int(rr.get("toTokenAmount") or 0),
            price_impact_pct=impact_f,
            route=_router_names(rr.get("dexRouterList")),
            estimated_gas=unsigned.gas or None,
            fetched_at=time.time(),
        )
        return unsigned, quote, fee_applied


dex = OkxDexClient()
