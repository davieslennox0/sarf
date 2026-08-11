"""The xStocks asset registry for X Layer.

Symbols are resolved here and nowhere else. The tool layer never accepts a
raw contract address from the model — an LLM can be prompted into passing an
attacker's address, and on an EVM chain that is indistinguishable from a real
one at a glance. Callers pass a symbol; this module maps it to an address from
a file that was verified against chain state when it was generated.

THE NAMING TRAP (read before touching this): the on-chain ERC-20 symbols use
an "x" SUFFIX (AAPLx, TSLAx, SPYx). OKX's *centralized* order book lists the
same underlyings with an "X" PREFIX (XAAPL, XTSLA, XSPY). They are different
identifiers on different venues, and searching X Layer for "XAAPL" returns
nothing — which reads exactly like "xStocks aren't deployed here". They are.
`cex_ticker` is recorded per asset for display only; it must never be used to
resolve an on-chain address.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..validation import ValidationError
from .evm import validate_evm_address

_REGISTRY_PATH = Path(__file__).with_name("xstocks_registry.json")

CHAIN_ID = 196
CHAIN_NAME = "X Layer"
EXPLORER_TX = "https://web3.okx.com/explorer/x-layer/tx/{}"
EXPLORER_TOKEN = "https://web3.okx.com/explorer/x-layer/token/{}"


@dataclass(frozen=True)
class RwaAsset:
    symbol: str          # on-chain ERC-20 symbol, x-suffixed: "AAPLx"
    name: str            # "Apple xStock"
    address: str         # lowercase 0x, verified on-chain at registry build
    decimals: int
    cex_ticker: str      # "XAAPL" — display only, NEVER an address key
    # OKX CDN token image, resolved once at registry-build time from
    # `onchainos token info` and baked in, so rendering a card never depends on
    # a live lookup. Optional: a missing logo falls back to a generated
    # monogram rather than an empty box.
    logo_url: str = ""

    @property
    def explorer_url(self) -> str:
        return EXPLORER_TOKEN.format(self.address)


@dataclass(frozen=True)
class QuoteAsset:
    symbol: str
    onchain_symbol: str
    address: str
    decimals: int
    is_native: bool = False


# The aggregator's sentinel for a chain's native coin. It is not a contract:
# nothing answers balanceOf() or transfer() at this address, so every code path
# that touches it has to branch on is_native rather than treating it as an
# ERC-20. Getting that wrong reads a balance of zero and refuses valid orders.
NATIVE_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

# OKB is X Layer's gas coin. It is tradable through the aggregator like any
# other asset, but it can never route through the session-key delegate:
# SarfSessionKey reads balances with balanceOf() and calls the router with
# value: 0, deliberately, so that a session key can never touch gas. Native
# swaps are therefore always signed by the user.
NATIVE = QuoteAsset(
    symbol="OKB",
    onchain_symbol="OKB",
    address=NATIVE_SENTINEL,
    decimals=18,
    is_native=True,
)


class XStocksRegistry:
    """Immutable, load-once view of the tradable universe."""

    def __init__(self, path: Path | None = None):
        raw = json.loads((path or _REGISTRY_PATH).read_text())
        if int(raw["chain_id"]) != CHAIN_ID:
            raise RuntimeError(
                f"registry is for chain {raw['chain_id']}, expected {CHAIN_ID} (X Layer)"
            )
        q = raw["quote_asset"]
        self.quote = QuoteAsset(
            symbol=q["symbol"],
            onchain_symbol=q["onchain_symbol"],
            address=validate_evm_address(q["address"], what="quote asset"),
            decimals=int(q["decimals"]),
        )
        # The address the aggregator's swap executes AGAINST. Distinct from the
        # approval spender below, and confusing the two is not cosmetic: the
        # session-key contract enforces `target == grant.router`, so a grant
        # authorising the approve address makes every swap revert with
        # TargetNotAllowed. That shipped, and every in-chat trade failed on it.
        self.dex_router_address = validate_evm_address(
            raw.get("dex_router_address") or raw["dex_token_approve_address"]
        )
        self.dex_approve_address = validate_evm_address(
            raw["dex_token_approve_address"], what="dex approve address"
        )
        self.generated_at = int(raw.get("generated_at", 0))

        assets: dict[str, RwaAsset] = {}
        for a in raw["assets"]:
            asset = RwaAsset(
                symbol=a["symbol"],
                name=a["name"],
                address=validate_evm_address(a["address"], what=f"{a['symbol']} address"),
                decimals=int(a["decimals"]),
                cex_ticker=a["cex_ticker"],
                logo_url=a.get("logo_url", ""),
            )
            assets[asset.symbol.upper()] = asset
        if not assets:
            raise RuntimeError("xStocks registry is empty")
        self._by_symbol = assets
        # Reverse index for explaining the prefix/suffix trap back to a user
        # who typed the CEX ticker. Resolution still returns the x-suffix asset.
        self._by_cex = {a.cex_ticker.upper(): a for a in assets.values()}

    @property
    def symbols(self) -> list[str]:
        # The on-chain symbols as actually cased (AAPLx), NOT the uppercased
        # lookup keys. Anything user-facing must show the x-suffix form —
        # echoing "AAPLX" would push people toward the CEX-style identifier
        # this class exists to keep separate.
        return sorted((a.symbol for a in self._by_symbol.values()), key=str.upper)

    @property
    def assets(self) -> list[RwaAsset]:
        return sorted(self._by_symbol.values(), key=lambda a: a.symbol.upper())

    def get(self, symbol: str) -> RwaAsset | None:
        return self._by_symbol.get(symbol.strip().upper())

    def resolve(self, symbol: object, *, allowlist: frozenset[str] = frozenset()) -> RwaAsset:
        """Symbol -> asset, or a ValidationError that teaches the caller.

        Accepts the on-chain x-suffix symbol. If the caller passes the OKX
        centralized ticker (XAAPL), we do NOT silently resolve it: we name the
        correct symbol and make them ask again, because the two identifiers
        diverge per venue and quietly translating would hide that from the user.
        """
        if not isinstance(symbol, str):
            raise ValidationError("asset must be a string symbol like 'AAPLx'")
        sym = symbol.strip().upper()
        if not sym:
            raise ValidationError("asset symbol must not be empty")

        asset = self._by_symbol.get(sym)
        if asset is None:
            alt = self._by_cex.get(sym)
            if alt is not None:
                raise ValidationError(
                    f"{symbol!r} is OKX's centralized order-book ticker. On X Layer "
                    f"the on-chain token is {alt.symbol!r} ({alt.name}) — ask for that. "
                    "The two are different identifiers on different venues."
                )
            raise ValidationError(
                f"{symbol!r} is not a tokenized stock available on X Layer. "
                f"Available: {', '.join(self.symbols)}"
            )
        if allowlist and asset.symbol.upper() not in allowlist:
            raise ValidationError(f"asset {asset.symbol} is not enabled on this server")
        return asset

    def resolve_tradable(
        self, symbol: object, *, allowlist: frozenset[str] = frozenset()
    ) -> RwaAsset | QuoteAsset:
        """Like resolve(), but also accepts the USDT quote asset by symbol."""
        if isinstance(symbol, str) and symbol.strip().upper() in {
            self.quote.symbol.upper(), self.quote.onchain_symbol.upper(), "USDT",
        }:
            return self.quote
        return self.resolve(symbol, allowlist=allowlist)


@lru_cache(maxsize=1)
def registry() -> XStocksRegistry:
    return XStocksRegistry()
