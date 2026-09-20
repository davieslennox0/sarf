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


# --- transport ---------------------------------------------------------------
#
# One pooled client, retries on throttling, and batched reads. All three exist
# for the same incident: a portfolio load used to make 43 separate HTTPS POSTs
# — one per tradable asset plus USDT, USDC and the native balance — fired
# concurrently, each opening and discarding its own connection. The public
# endpoint answered a burst like that with HTTP 429, and because the three
# single reads had no tolerance for failure, a rate-limit reply surfaced to the
# user as "internal server error" on a page that was working seconds earlier.
#
# Batching is the actual cure: the same 40 balances now travel as one request.
# The pool and the retry are what keep a wobble from being a failure.

# Requests per JSON-RPC batch. TEN, because that is the documented-by-experiment
# ceiling on rpc.xlayer.tech: 10 is accepted, 11 comes back as
# {"code": -32014, "message": "too many RPC calls in batch request"} — and the
# rejection applies to the WHOLE batch, so guessing high loses every balance in
# it rather than trimming the excess. Forty assets therefore travel as four
# requests instead of forty.
BATCH_SIZE = 10
_RETRY_STATUS = (429, 500, 502, 503, 504)
_RETRY_BACKOFF = (0.4, 1.2, 2.5)   # attempts = len + 1

# Keyed by event loop, not a bare global. A pooled client owns TCP connections
# that belong to the loop they were opened on, so one cached across loops fails
# with "Event loop is closed" the moment a second loop uses it. The server runs
# a single long-lived loop and would never have noticed; a test suite calling
# asyncio.run() twice notices immediately, which is how this was found.
_clients: dict[object, httpx.AsyncClient] = {}


async def _http() -> httpx.AsyncClient:
    """The pooled client for the running loop. Created once per loop, reused.

    Keep-alive matters more here than it looks: without it every balance read
    paid a fresh TLS handshake, which is both the latency and a large part of
    what made 43 reads look like an attack to the far end.
    """
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"content-type": "application/json"},
        )
        _clients[loop] = client
        # Loops that have gone away take their clients with them, so a
        # long-running process does not accumulate dead pools.
        for dead in [k for k, v in _clients.items()
                     if k is not loop and getattr(k, "is_closed", lambda: False)()]:
            _clients.pop(dead, None)
    return client


async def _post(payload: Any, *, timeout: float) -> Any:
    """POST a JSON-RPC payload with backoff on throttling. -> decoded body.

    Retries only statuses that mean "ask again" (429 and the 5xx family). A
    400 is a malformed request and asking twice will not fix it. Retry-After
    is honoured when the server sends one, because guessing an interval the
    endpoint already told us is rude and usually wrong.
    """
    client = await _http()
    last: str = "unknown error"
    for attempt in range(len(_RETRY_BACKOFF) + 1):
        try:
            r = await client.post(RPC_URL, json=payload, timeout=timeout)
        except httpx.HTTPError as e:
            last = f"unreachable: {type(e).__name__}"
        else:
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code not in _RETRY_STATUS:
                raise RpcError(f"X Layer RPC returned {last}")
            retry_after = r.headers.get("retry-after")
            if retry_after and attempt < len(_RETRY_BACKOFF):
                try:
                    await asyncio.sleep(min(float(retry_after), 5.0))
                    continue
                except ValueError:
                    pass
        if attempt < len(_RETRY_BACKOFF):
            await asyncio.sleep(_RETRY_BACKOFF[attempt])
    raise RpcError(f"X Layer RPC returned {last} after "
                   f"{len(_RETRY_BACKOFF) + 1} attempts")


async def _call(method: str, params: list[Any], *, timeout: float = 15.0) -> Any:
    body = await _post({"jsonrpc": "2.0", "id": 1, "method": method,
                        "params": params}, timeout=timeout)
    if not isinstance(body, dict):
        raise RpcError("X Layer RPC returned an unexpected response shape")
    if "error" in body:
        raise RpcError(f"X Layer RPC error: {body['error'].get('message')}")
    return body.get("result")


async def _batch(calls: list[tuple[str, list[Any]]], *,
                 timeout: float = 20.0) -> list[Any]:
    """Run many JSON-RPC calls in ONE request. -> results positionally.

    A per-call failure is returned as an RpcError instance in that slot rather
    than raised, so one bad token cannot lose the other twenty-four. Responses
    are matched by `id` and never by arrival order — a JSON-RPC server is free
    to reorder a batch, and trusting position here would silently attribute one
    wallet's balance to another asset.
    """
    if not calls:
        return []
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
               for i, (m, p) in enumerate(calls)]
    body = await _post(payload, timeout=timeout)
    if not isinstance(body, list):
        # A batch refused as a unit answers with a single error object rather
        # than a list. "Too many calls" is recoverable by sending fewer, so it
        # is halved and retried instead of costing the caller every balance in
        # the chunk — BATCH_SIZE should make this unreachable, and it is here
        # so that a change at the far end degrades instead of breaking.
        detail = ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            detail = str(body["error"].get("message") or "")
        if len(calls) > 1 and "too many" in detail.lower():
            mid = len(calls) // 2
            return (await _batch(calls[:mid], timeout=timeout)
                    + await _batch(calls[mid:], timeout=timeout))
        raise RpcError(f"X Layer RPC rejected the batch{f': {detail}' if detail else ''}")
    out: list[Any] = [RpcError("no response for this call in the batch")] * len(calls)
    for item in body:
        if not isinstance(item, dict):
            continue
        idx = item.get("id")
        if not isinstance(idx, int) or not 0 <= idx < len(calls):
            continue
        if "error" in item:
            out[idx] = RpcError(f"X Layer RPC error: {item['error'].get('message')}")
        else:
            out[idx] = item.get("result")
    return out


async def chain_id() -> int:
    return int(await _call("eth_chainId", []), 16)


def _balance_data(holder: str) -> str:
    """Calldata for balanceOf(holder)."""
    return _BALANCE_OF + holder.lower().replace("0x", "").rjust(64, "0")


def _decode_balance(res: Any, token_address: str) -> int:
    """An eth_call result -> units. Empty return data is a real zero."""
    if not res or res == "0x":
        return 0
    try:
        return int(res, 16)
    except (ValueError, TypeError):
        raise RpcError(f"unreadable balance response for token {token_address}") from None


async def erc20_balance(token_address: str, holder: str) -> int:
    """balanceOf(holder) for an ERC-20 on X Layer, in minimal units."""
    res = await _call("eth_call",
                      [{"to": token_address, "data": _balance_data(holder)}, "latest"])
    return _decode_balance(res, token_address)


async def erc20_balances(
    token_addresses: list[str], holder: str
) -> tuple[dict[str, int], list[str]]:
    """Batched balanceOf across many tokens -> ({address: units}, unread).

    A read that failed is NOT a zero balance, and the difference is the whole
    point of returning two things. This used to return the dict alone and drop
    failures out of it; the caller then read a missing address as "holds none"
    and the position vanished from the portfolio — during an RPC wobble, a
    wallet could be shown as empty while holding everything it always had.

    Failures are handed back by address so the caller can say which assets it
    could not read. Only a total failure raises: one token flaking should not
    take down a page that can still show the other thirty-nine.

    Sent as JSON-RPC batches rather than one request per token. Forty separate
    concurrent POSTs is what earned an HTTP 429 from the public endpoint and
    turned this page into a 500; the same forty balances now travel in two.
    """
    if not token_addresses:
        return {}, []

    calls = [("eth_call", [{"to": a, "data": _balance_data(holder)}, "latest"])
             for a in token_addresses]
    results: list[Any] = []
    for i in range(0, len(calls), BATCH_SIZE):
        chunk = calls[i:i + BATCH_SIZE]
        try:
            results.extend(await _batch(chunk))
        except RpcError as e:
            # The whole chunk failed (transport, not per-call). Its addresses
            # go to `unread` — emphatically NOT to zero.
            results.extend([e] * len(chunk))

    out: dict[str, int] = {}
    unread: list[str] = []
    for addr, res in zip(token_addresses, results, strict=True):
        if isinstance(res, BaseException):
            unread.append(addr)
            continue
        try:
            out[addr] = _decode_balance(res, addr)
        except RpcError:
            unread.append(addr)
    if unread and not out:
        raise RpcError("could not read any balances from X Layer")
    return out, unread


async def erc20_allowance(token_address: str, owner: str, spender: str) -> int:
    """allowance(owner, spender): how much of this token the spender may pull.
    Zero is the normal state before the first sell of an asset."""
    data = ("0xdd62ed3e" + owner.lower()[2:].rjust(64, "0")
            + spender.lower()[2:].rjust(64, "0"))
    res = await _call("eth_call", [{"to": token_address, "data": data}, "latest"])
    try:
        return int(res, 16)
    except (ValueError, TypeError):
        raise RpcError(f"unreadable allowance response for token {token_address}") from None


async def native_balance(holder: str) -> int:
    res = await _call("eth_getBalance", [holder.lower(), "latest"])
    return int(res, 16) if res else 0


async def transaction_count(address: str) -> int:
    """Pending nonce — pending, not latest, so two relayed swaps issued back to
    back don't collide on the same nonce and drop one of them."""
    res = await _call("eth_getTransactionCount", [address.lower(), "pending"])
    return int(res, 16) if res else 0


async def gas_price() -> int:
    res = await _call("eth_gasPrice", [])
    return int(res, 16) if res else 0


async def estimate_gas(*, from_address: str, to: str, data: str, value: int = 0) -> int:
    """What the chain says this call costs. Raises RpcError if it would revert,
    which callers treat as "cannot estimate", never as "refuse the order"."""
    res = await _call("eth_estimateGas", [{
        "from": from_address, "to": to, "data": data, "value": hex(int(value)),
    }])
    return int(res, 16) if res else 0


async def send_raw_transaction(raw: str) -> str:
    return await _call("eth_sendRawTransaction", [raw])


async def code_at(address: str) -> str:
    return await _call("eth_getCode", [address.lower(), "latest"]) or "0x"


async def delegated_to(address: str) -> str | None:
    """The implementation an EOA is delegated to under EIP-7702, or None.

    A delegated account's code is exactly the 23-byte marker 0xef0100 followed
    by the implementation address (EIP-7702 §"Delegation Designation"). Reading
    it is how we check a user's grant is actually installed rather than trusting
    that their authorisation transaction landed.
    """
    code = await code_at(address)
    if not code.startswith("0xef0100") or len(code) != 48:
        return None
    return "0x" + code[8:]


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
