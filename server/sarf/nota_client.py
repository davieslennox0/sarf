"""Client for the Nota trade-receipt protocol (github.com/davieslennox0/nota).

Reused, not rebuilt, per the brief -- and it is worth being precise about what
"reuse" means here, because Nota is not a drop-in for an EVM/X Layer service:

- Nota anchors receipts on SUI + Walrus (Move package `receipts.move`,
  registry/package IDs on Sui Mainnet). Sarf is X Layer/EVM-only (Sui support
  was retired). A receipt about an X Layer trade is still issuable -- `issue()`
  just takes txHash/asset/action/amount/currency/recipient as plain fields, so
  an X Layer tx hash and an X Layer address pass through as opaque strings --
  but the receipt itself lives on Sui, a second chain Sarf otherwise has no
  footprint on.
- Issuing (write) only exists via the `@sykeclone/nota-sdk` Node/TS SDK, which
  needs a funded Sui signer. There is no write-capable REST API. This module
  shells out to issue_receipt.mjs for that path -- a real call into the real
  SDK, not a reimplementation of its logic.
- Verifying/reading (github.com/davieslennox0/nota-gateway) IS a plain REST
  API and is called directly over HTTP below -- no SDK, no subprocess, no
  Sui key needed for reads.

Disabled by default (settings.nota_enabled=False). With it off, or with any of
the Sui credentials unset, issue_receipt() returns status
'skipped_not_configured' and does not fabricate a receipt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("sarf.nota")

_SHIM = Path(__file__).parent / "nota" / "issue_receipt.mjs"


def _configured() -> bool:
    return bool(
        settings.nota_enabled
        and settings.nota_sui_private_key
        and settings.nota_package_id
        and settings.nota_registry_id
    )


async def issue_receipt(*, tx_hash: str, asset: str, action: str, amount: str,
                        currency: str, recipient: str) -> dict[str, Any]:
    """Issue a signed receipt for a confirmed trade. Never raises -- a receipt
    is an add-on to a trade that already settled, not a condition of it, so a
    Nota outage must not turn a successful trade into an error for the user.
    """
    if not _configured():
        return {
            "status": "skipped_not_configured",
            "detail": "NOTA_ENABLED / NOTA_SUI_PRIVATE_KEY / NOTA_PACKAGE_ID / "
                      "NOTA_REGISTRY_ID not set -- no receipt issued, none faked.",
        }
    req = {
        "suiRpcUrl": settings.nota_sui_rpc_url,
        "privateKey": settings.nota_sui_private_key,
        "packageId": settings.nota_package_id,
        "registryId": settings.nota_registry_id,
        "walrusAggregatorUrl": settings.nota_walrus_aggregator_url,
        "walrusPublisherUrl": settings.nota_walrus_publisher_url,
        "namespace": settings.nota_namespace,
        "txHash": tx_hash, "asset": asset, "action": action,
        "amount": amount, "currency": currency, "recipient": recipient,
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", str(_SHIM), json.dumps(req),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        result = json.loads(out.decode() or "{}")
    except FileNotFoundError:
        return {"status": "failed", "detail": "node runtime not found on this host"}
    except Exception as e:
        log.warning("nota issue_receipt failed: %s", e, exc_info=True)
        return {"status": "failed", "detail": f"{type(e).__name__}: {e}"}
    if not result.get("ok"):
        return {"status": "failed", "detail": result.get("error", "unknown error")}
    return {
        "status": "issued",
        "receipt_id": result.get("receipt_id"),
        "blob_id": result.get("blob_id"),
        "view_url": result.get("view_url"),
        "tx_digest": result.get("tx_digest"),
    }


async def verify_receipt(receipt_id: str) -> dict[str, Any]:
    """Read-only proof check against the real, live nota-gateway service.
    Works even when issuance is disabled here -- verification never needed a
    Sui key, only whichever receipt_id an issuer (this Sarf deployment or any
    other Nota-issuing protocol) handed out.
    """
    url = f"{settings.nota_gateway_url}/verify/{receipt_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
