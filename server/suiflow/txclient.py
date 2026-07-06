"""HTTP client for the loopback tx-builder sidecar."""

from __future__ import annotations

from typing import Any

import httpx

from .config import settings


class TxBuilderError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class TxBuilder:
    def __init__(self, base_url: str | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.txbuilder_url,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )

    async def _req(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        try:
            r = await self._client.request(method, path, json=json_body)
        except httpx.HTTPError as e:
            raise TxBuilderError(502, f"tx-builder unavailable: {e}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", r.text)
            except Exception:
                msg = r.text
            raise TxBuilderError(r.status_code, msg)
        return r.json()

    async def markets(self) -> dict[str, Any]:
        return await self._req("GET", "/markets")

    async def market_info(self, market: str) -> dict[str, Any]:
        return await self._req("GET", f"/market-info?market={market}")

    async def cap(self, cap_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/cap/{cap_id}")

    async def portfolio(self, address: str) -> dict[str, Any]:
        return await self._req("GET", f"/portfolio/{address}")

    async def build(self, **kwargs: Any) -> dict[str, Any]:
        return await self._req("POST", "/build", kwargs)

    async def build_leverage(self, **kwargs: Any) -> dict[str, Any]:
        return await self._req("POST", "/build/leverage", kwargs)

    async def broadcast(self, tx_bytes_base64: str, signatures: list[str]) -> dict[str, Any]:
        return await self._req(
            "POST", "/broadcast", {"txBytesBase64": tx_bytes_base64, "signatures": signatures}
        )


txbuilder = TxBuilder()
