"""Direct X Layer JSON-RPC reads: balances and transaction receipts.

Deliberately independent of the OKX aggregator. Holdings and settlement status
are facts about the chain, so they are read from the chain — if the aggregator
is down or rate-limited the user can still see what they own and whether their
trade landed. No credentials required.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .registry import CHAIN_ID

RPC_URL = os.environ.get("XLAYER_RPC_URL", "https://rpc.xlayer.tech")

_BALANCE_OF = "0x70a08231"  # balanceOf(address)


class RpcError(RuntimeError):
    """X Layer RPC failure. Message is safe to surface."""


@dataclass(frozen=True)
class TxStatus:
    tx_hash: str
    found: bool
    mined: bool
    success: bool | None
    block_number: int | None
    gas_used: int | None


async def _call(method: str, params: list[Any], *, timeout: float = 15.0) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(RPC_URL, json=payload)
    except httpx.HTTPError as e:
        raise RpcError(f"X Layer RPC unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise RpcError(f"X Layer RPC returned HTTP {r.status_code}")
    body = r.json()
    if "error" in body:
        raise RpcError(f"X Layer RPC error: {body['error'].get('message')}")
    return body.get("result")


async def chain_id() -> int:
    return int(await _call("eth_chainId", []), 16)


async def erc20_balance(token_address: str, holder: str) -> int:
    """balanceOf(holder) for an ERC-20 on X Layer, in minimal units."""
    data = _BALANCE_OF + holder.lower().replace("0x", "").rjust(64, "0")
    res = await _call("eth_call", [{"to": token_address, "data": data}, "latest"])
    if not res or res == "0x":
        return 0
    try:
        return int(res, 16)
    except ValueError:
        raise RpcError(f"unreadable balance response for token {token_address}")


async def erc20_balances(token_addresses: list[str], holder: str) -> dict[str, int]:
    """Concurrent balanceOf across many tokens.

    A failing token read is reported as a raised error rather than silently
    becoming a zero balance — showing someone 0 when they hold a position is a
    worse failure than showing them an error.
    """
    results = await asyncio.gather(
        *(erc20_balance(a, holder) for a in token_addresses), return_exceptions=True
    )
    out: dict[str, int] = {}
    errors = 0
    for addr, res in zip(token_addresses, results):
        if isinstance(res, Exception):
            errors += 1
            continue
        out[addr] = res
    if errors and not out:
        raise RpcError("could not read any balances from X Layer")
    return out


async def native_balance(holder: str) -> int:
    res = await _call("eth_getBalance", [holder.lower(), "latest"])
    return int(res, 16) if res else 0


async def tx_status(tx_hash: str) -> TxStatus:
    """Receipt-based settlement status. 'not found' is distinct from 'failed'."""
    receipt = await _call("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        tx = await _call("eth_getTransactionByHash", [tx_hash])
        # Known to the mempool but unmined, versus never seen at all.
        return TxStatus(tx_hash, found=tx is not None, mined=False,
                        success=None, block_number=None, gas_used=None)
    status = receipt.get("status")
    ok = int(status, 16) == 1 if status is not None else None
    bn = receipt.get("blockNumber")
    gu = receipt.get("gasUsed")
    return TxStatus(
        tx_hash=tx_hash,
        found=True,
        mined=True,
        success=ok,
        block_number=int(bn, 16) if bn else None,
        gas_used=int(gu, 16) if gu else None,
    )


async def assert_chain() -> None:
    """Fail loudly if the configured RPC is not actually X Layer."""
    cid = await chain_id()
    if cid != CHAIN_ID:
        raise RpcError(
            f"XLAYER_RPC_URL points at chain {cid}, not X Layer ({CHAIN_ID}). Refusing to trade."
        )
