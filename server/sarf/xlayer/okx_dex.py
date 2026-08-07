"""OKX DEX aggregator client for X Layer (chain 196).

This module READS prices and BUILDS unsigned transactions. It never signs and
never broadcasts — that stays with the user's own wallet, same custody model
Sarf has always had. Nothing here touches a keyring.

Transport: OKX's DEX REST API refuses anonymous calls (`50103 OK-ACCESS-KEY
can not be empty`), so calls go through one of two transports:

  * `http`  — OKX DEX API v5 with the operator's own credentials from env.
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
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .registry import CHAIN_ID

_OKX_BASE = os.environ.get("OKX_BASE_URL", "https://web3.okx.com").rstrip("/")
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


class OkxDexClient:
    def __init__(self, *, timeout: float = 20.0):
        self._timeout = timeout
        self._key = os.environ.get("OKX_API_KEY", "").strip()
        self._secret = os.environ.get("OKX_API_SECRET", "").strip()
        self._passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip()
        self._project = os.environ.get("OKX_PROJECT_ID", "").strip()

    @property
    def transport(self) -> str:
        if self._key and self._secret and self._passphrase:
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
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(_OKX_BASE + full, headers=self._sign_headers("GET", full))
        if r.status_code != 200:
            raise DexError(f"OKX DEX API returned HTTP {r.status_code}")
        body = r.json()
        if str(body.get("code")) != "0":
            raise DexError(f"OKX DEX API error {body.get('code')}: {body.get('msg')}")
        return body.get("data")

    async def _cli(self, args: list[str]) -> Any:
        proc = await asyncio.create_subprocess_exec(
            _CLI, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout * 3)
        except asyncio.TimeoutError:
            proc.kill()
            raise DexError("quote backend timed out")
        if proc.returncode != 0:
            raise DexError(f"quote backend failed: {(err or b'').decode()[:200]}")
        try:
            body = json.loads(out.decode())
        except json.JSONDecodeError:
            raise DexError("quote backend returned malformed output")
        if body.get("ok") is False:
            raise DexError(str(body.get("error") or "quote backend rejected the request")[:200])
        return body.get("data", body)

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
        t = self.transport
        if t == "none":
            raise DexError(
                "no quote backend configured: set OKX_API_KEY/OKX_API_SECRET/"
                "OKX_API_PASSPHRASE, or install the onchainos CLI"
            )
        params = {
            "chainIndex": CHAIN_ID, "chainId": CHAIN_ID,
            "fromTokenAddress": from_address, "toTokenAddress": to_address,
            "amount": amount_min_units,
        }
        if t == "http":
            raw = await self._http("/api/v5/dex/aggregator/quote", params)
        else:
            raw = await self._cli([
                "swap", "quote", "--chain", str(CHAIN_ID),
                "--from", from_address, "--to", to_address,
                "--amount", str(amount_min_units),
            ])
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

    async def build_swap(
        self,
        *,
        from_address: str,
        to_address: str,
        amount_min_units: int,
        user_address: str,
        slippage_pct: float,
    ) -> tuple[UnsignedTx, Quote]:
        """Build the unsigned swap transaction for `user_address` to sign.

        `user_address` is the session's verified wallet — it comes from the
        auth layer, never from a tool argument, so the built transaction can
        only ever spend from the account that asked for it.
        """
        t = self.transport
        if t == "none":
            raise DexError("no quote backend configured; cannot build a transaction")
        if t == "http":
            raw = await self._http("/api/v5/dex/aggregator/swap", {
                "chainIndex": CHAIN_ID, "chainId": CHAIN_ID,
                "fromTokenAddress": from_address, "toTokenAddress": to_address,
                "amount": amount_min_units, "userWalletAddress": user_address,
                "slippage": slippage_pct / 100.0,
            })
        else:
            raw = await self._cli([
                "swap", "swap", "--chain", str(CHAIN_ID),
                "--from", from_address, "--to", to_address,
                "--amount", str(amount_min_units),
                "--wallet", user_address, "--slippage", str(slippage_pct),
            ])
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
        unsigned = UnsignedTx(
            to=str(tx["to"]),
            data=str(tx["data"]),
            value=str(tx.get("value", "0")),
            gas=int(tx.get("gas") or 0),
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
        return unsigned, quote


dex = OkxDexClient()
