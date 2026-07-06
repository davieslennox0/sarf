"""Asset/market registry, sourced from the sidecar (i.e. the protocol's own
market config) at startup and cached. Validation fails closed while it is
unavailable: no registry, no proposals."""

from __future__ import annotations

import asyncio
import time

from .config import settings
from .txclient import TxBuilder, TxBuilderError
from .validation import AssetInfo, ValidationError


class AssetRegistry:
    REFRESH_SECONDS = 3600

    def __init__(self, txbuilder: TxBuilder):
        self._tx = txbuilder
        self._assets: dict[tuple[str, str], AssetInfo] = {}
        self._markets: set[str] = set()
        self._market_by_type: dict[str, str] = {}
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        if self._assets and time.time() - self._loaded_at < self.REFRESH_SECONDS:
            return
        async with self._lock:
            if self._assets and time.time() - self._loaded_at < self.REFRESH_SECONDS:
                return
            try:
                data = await self._tx.markets()
            except TxBuilderError:
                if self._assets:
                    return  # keep serving the previous snapshot
                raise ValidationError(
                    "asset registry unavailable (tx-builder not reachable); refusing to proceed"
                )
            assets: dict[tuple[str, str], AssetInfo] = {}
            markets: set[str] = set()
            by_type: dict[str, str] = {}
            for m in data["markets"]:
                markets.add(m["name"])
                by_type[m["type"] if m["type"].startswith("0x") else "0x" + m["type"]] = m["name"]
                for a in m["assets"]:
                    sym = a["symbol"].upper()
                    if settings.asset_whitelist and sym not in settings.asset_whitelist:
                        continue
                    assets[(m["name"], sym)] = AssetInfo(
                        symbol=sym,
                        coin_type=a["coinType"],
                        decimals=int(a["decimals"]),
                        market=m["name"],
                    )
            self._assets = assets
            self._markets = markets
            self._market_by_type = by_type
            self._loaded_at = time.time()

    @property
    def assets(self) -> dict[tuple[str, str], AssetInfo]:
        return self._assets

    @property
    def markets(self) -> set[str]:
        return self._markets

    def market_name_for_type(self, market_type: str) -> str | None:
        t = market_type if market_type.startswith("0x") else "0x" + market_type
        return self._market_by_type.get(t)
