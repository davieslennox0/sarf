"""X Layer RWA provider: tokenized-equity (xStocks) MCP tools.

Custody model, unchanged from every previous Sarf provider: this file BUILDS
and PRICES transactions. It never signs, never broadcasts, and holds no key
material. `place_order` returns an unsigned X Layer transaction plus a
sign_url; the user's own wallet produces the signature and the tx hash.

Every tool acts on the address of the request's verified session
(auth.require_address()). No tool accepts a wallet address argument, so a
caller — or a model that has been prompted into trying — cannot read or trade
against an account they have not proven control of.

Order pipeline (`place_order`):
    resolve symbol -> validate amount -> quote -> price in USD
    -> USD cap -> price-impact cap -> passkey step-up -> build unsigned tx
    -> persist -> return with sign_url

Compliance: xStocks are synthetic price exposure. Holders get no shares, no
dividends and no voting rights. That disclosure rides on every priced response
rather than living only on the website, because the model is what most users
will actually read.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .. import passkey
from ..auth import require_address
from ..config import settings
from ..db import Database
from ..validation import ValidationError, validate_amount, validate_usd_cap
from ..xlayer import rpc
from ..xlayer.evm import validate_evm_address, validate_tx_hash
from ..xlayer.okx_dex import DexError, OkxDexClient, Quote
from ..xlayer.registry import CHAIN_ID, EXPLORER_TX, RwaAsset, XStocksRegistry

SYNTHETIC_DISCLOSURE = (
    "xStocks are tokenized SYNTHETIC exposure to the underlying share price. "
    "Holding one does NOT give you share ownership, dividends, or voting rights, "
    "and redemption depends on the issuer (Backed Assets). Tell the user this "
    "before they trade."
)

_SIGN_STEP = (
    "Show the user human_summary and every risk_note, then give them sign_url. "
    "They sign the unsigned transaction in their own wallet — this server "
    "cannot execute it. Never say a trade happened until a tx_hash exists."
)

Side = Literal["buy", "sell"]

# Slippage the caller may request, hard-bounded here regardless of config.
_SLIPPAGE_MIN, _SLIPPAGE_MAX = 0.05, 5.0


def _fmt_units(amount: int, decimals: int) -> str:
    d = Decimal(amount) / (Decimal(10) ** decimals)
    return f"{d:.8f}".rstrip("0").rstrip(".") or "0"


def _usd(x: float | None) -> str:
    return f"${x:,.2f}" if x is not None else "n/a"


class XLayerRwaProvider:
    name = "xlayer_rwa"

    def __init__(self, db: Database, dex: OkxDexClient, registry: XStocksRegistry):
        self.db = db
        self.dex = dex
        self.reg = registry

    # ------------------------------------------------------------------ util

    async def _unit_price_usd(self, asset: RwaAsset) -> float | None:
        """USD price of one whole token, via a 1-unit sell quote into USDT.

        Priced by asking the same venue the trade would execute on, so the cap
        and the fill agree. Returns None if the venue cannot price it — callers
        must then fail closed, never assume a value.
        """
        try:
            q = await self.dex.quote(asset.address, self.reg.quote.address, 10 ** asset.decimals)
        except DexError:
            return None
        if q.to_amount <= 0:
            return None
        return float(Decimal(q.to_amount) / (Decimal(10) ** self.reg.quote.decimals))

    @staticmethod
    def fee_plan(stable_leg_usd: float | None) -> dict[str, Any]:
        """Turn the flat dollar fee into a percentage of the stablecoin leg.

        The aggregator charges a percentage, so a fixed $0.10 is only sane
        above a minimum order size — on a $2 trade it would be 5%. Below
        min_order_usd we refuse the order rather than quietly overcharge.

        Fails to ZERO fee if no recipient address is configured: an unset
        address must never mean "send the fee somewhere else".
        """
        fee_usd = settings.platform_fee_usd
        recipient = settings.platform_fee_address
        if fee_usd <= 0 or not recipient:
            return {"charge": False, "usd": 0.0, "percent": 0.0, "recipient": None}
        if stable_leg_usd is None or stable_leg_usd <= 0:
            # Unpriceable leg — we cannot compute a percentage honestly.
            return {"charge": False, "usd": 0.0, "percent": 0.0, "recipient": None,
                    "reason": "order could not be priced"}
        pct = fee_usd / stable_leg_usd * 100.0
        capped = min(pct, settings.max_fee_percent)
        return {
            "charge": True,
            "usd": round(capped / 100.0 * stable_leg_usd, 4),
            "percent": capped,
            "recipient": recipient,
            "capped": capped < pct,
        }

    def _validate_slippage(self, value: object) -> float:
        if value is None:
            return settings.default_slippage_pct
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("slippage_percent must be a number")
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValidationError("slippage_percent must be finite")
        if not (_SLIPPAGE_MIN <= v <= _SLIPPAGE_MAX):
            raise ValidationError(
                f"slippage_percent must be between {_SLIPPAGE_MIN} and {_SLIPPAGE_MAX}"
            )
        return v

    def _risk_notes(self, *, asset: RwaAsset, side: str, est_usd: float | None,
                    quote: Quote, slippage: float, stepup: passkey.StepUpDecision,
                    fee: dict[str, Any] | None = None) -> list[str]:
        notes = [SYNTHETIC_DISCLOSURE]
        if fee and fee.get("charged"):
            notes.append(
                f"Sarf platform fee: ${fee['usd']:.2f} in {fee['denominated_in']} "
                f"({fee['percent_of_order']:.3f}% of this order), taken inside the "
                "same transaction. Network gas is separate and paid in OKB."
            )
        if quote.price_impact_pct is not None and quote.price_impact_pct >= 1.0:
            notes.append(
                f"Price impact ~{quote.price_impact_pct:.2f}% — this order is large "
                f"relative to the {asset.symbol} pool on X Layer. Consider splitting it."
            )
        notes.append(
            f"Slippage tolerance {slippage:g}%: you may receive up to {slippage:g}% less "
            "than quoted, and the transaction reverts beyond that."
        )
        if side == "buy":
            notes.append(
                "Equity markets close; this token trades 24/7. Outside market hours the "
                "on-chain price can drift from the last official close."
            )
        if stepup.required:
            notes.append(f"Passkey step-up: {stepup.reason}.")
        notes.append(
            f"Settlement is on X Layer (chain {CHAIN_ID}). You pay gas in OKB and the "
            "trade is final once mined."
        )
        return notes

    # ----------------------------------------------------------------- tools

    def register_tools(self, mcp: FastMCP) -> None:  # noqa: C901 - tool bundle
        reg, dex, db = self.reg, self.dex, self.db

        @mcp.tool()
        async def get_rwa_list() -> dict[str, Any]:
            """List the tokenized stocks and ETFs tradable on X Layer.

            Read-only. Returns each asset's on-chain symbol, name, contract
            address and X Layer explorer link.
            """
            allow = settings.rwa_allowlist
            assets = [a for a in reg.assets if not allow or a.symbol.upper() in allow]
            return {
                "chain": {"name": "X Layer", "chain_id": CHAIN_ID},
                "quote_asset": reg.quote.symbol,
                "count": len(assets),
                "assets": [
                    {
                        "symbol": a.symbol,
                        "name": a.name,
                        "address": a.address,
                        "decimals": a.decimals,
                        "okx_cex_ticker": a.cex_ticker,
                        "explorer_url": a.explorer_url,
                    }
                    for a in assets
                ],
                "note": (
                    "Trade using the on-chain symbol (the x-suffix form, e.g. AAPLx). "
                    "okx_cex_ticker is the different identifier OKX's centralized "
                    "order book uses for the same underlying — it is shown for "
                    "recognition only and is not tradable here."
                ),
                "disclosure": SYNTHETIC_DISCLOSURE,
            }

        @mcp.tool()
        async def get_rwa_price(
            symbol: Annotated[str, Field(description="On-chain symbol, e.g. 'AAPLx' or 'SPYx'")],
        ) -> dict[str, Any]:
            """Live price for one tokenized stock, quoted from X Layer DEX pools.

            Read-only. The price is what the aggregator would actually fill at
            right now, not a reference feed.
            """
            asset = reg.resolve(symbol, allowlist=settings.rwa_allowlist)
            price = await self._unit_price_usd(asset)
            if price is None:
                raise ValueError(
                    f"could not price {asset.symbol} on X Layer right now — the DEX "
                    "aggregator did not return a route. Try again shortly."
                )
            return {
                "symbol": asset.symbol,
                "name": asset.name,
                "price_usdt": round(price, 6),
                "address": asset.address,
                "chain_id": CHAIN_ID,
                "explorer_url": asset.explorer_url,
                "quoted_at": int(time.time()),
                "source": "OKX DEX aggregator (X Layer pools)",
                "disclosure": SYNTHETIC_DISCLOSURE,
            }

        @mcp.tool()
        async def get_portfolio() -> dict[str, Any]:
            """The authenticated wallet's tokenized-stock holdings on X Layer.

            Read-only, read straight from chain state. Balances are the
            authenticated session's wallet — no address argument exists.
            """
            address = require_address()
            db.upsert_user(address, "mcp")
            allow = settings.rwa_allowlist
            assets = [a for a in reg.assets if not allow or a.symbol.upper() in allow]

            balances = await rpc.erc20_balances([a.address for a in assets], address)
            usdt_raw = await rpc.erc20_balance(reg.quote.address, address)
            okb_raw = await rpc.native_balance(address)

            positions: list[dict[str, Any]] = []
            total_usd = 0.0
            unpriced: list[str] = []
            for a in assets:
                raw = balances.get(a.address, 0)
                if raw <= 0:
                    continue
                price = await self._unit_price_usd(a)
                qty = float(Decimal(raw) / (Decimal(10) ** a.decimals))
                value = qty * price if price is not None else None
                if value is None:
                    unpriced.append(a.symbol)
                else:
                    total_usd += value
                positions.append({
                    "symbol": a.symbol, "name": a.name,
                    "quantity": _fmt_units(raw, a.decimals),
                    "price_usdt": round(price, 6) if price is not None else None,
                    "value_usd": round(value, 2) if value is not None else None,
                    "address": a.address, "explorer_url": a.explorer_url,
                })

            return {
                "address": address,
                "chain": {"name": "X Layer", "chain_id": CHAIN_ID},
                "positions": positions,
                "usdt_balance": _fmt_units(usdt_raw, reg.quote.decimals),
                "gas_balance_okb": _fmt_units(okb_raw, 18),
                "positions_value_usd": round(total_usd, 2),
                # Stated explicitly so a partial pricing outage can never be
                # mistaken for "these positions are worth nothing".
                "unpriced_positions": unpriced,
                "total_value_usd": round(total_usd + float(
                    Decimal(usdt_raw) / (Decimal(10) ** reg.quote.decimals)
                ), 2) if not unpriced else None,
                "disclosure": SYNTHETIC_DISCLOSURE,
            }

        @mcp.tool()
        async def place_order(
            symbol: Annotated[str, Field(description="On-chain symbol, e.g. 'AAPLx'")],
            side: Annotated[Side, Field(description="'buy' spends USDT; 'sell' returns USDT")],
            amount: Annotated[str, Field(
                description="Decimal string. For 'buy' this is USDT to spend; "
                            "for 'sell' it is how many tokens to sell."
            )],
            slippage_percent: Annotated[float | None, Field(
                default=None, description="Optional slippage tolerance, 0.05-5.0"
            )] = None,
        ) -> dict[str, Any]:
            """Build an UNSIGNED buy/sell order for a tokenized stock on X Layer.

            This does NOT execute anything. It returns an unsigned transaction
            and a sign_url; the user reviews it and signs in their own wallet,
            which is what produces the X Layer tx hash. Always show the user
            human_summary and risk_notes first.
            """
            address = require_address()
            db.upsert_user(address, "mcp")
            asset = reg.resolve(symbol, allowlist=settings.rwa_allowlist)
            slippage = self._validate_slippage(slippage_percent)

            if side == "buy":
                sell_asset: Any = reg.quote
                amount_min_units = validate_amount(amount, reg.quote.decimals, what="amount (USDT)")
                buy_addr, sell_addr = asset.address, reg.quote.address
            elif side == "sell":
                sell_asset = asset
                amount_min_units = validate_amount(amount, asset.decimals,
                                                   what=f"amount ({asset.symbol})")
                buy_addr, sell_addr = reg.quote.address, asset.address
            else:  # pragma: no cover - Literal guards this
                raise ValidationError("side must be 'buy' or 'sell'")

            # Refuse before quoting if the wallet plainly cannot fund it. A
            # quote that can never fill is worse than an early, clear refusal.
            held = await rpc.erc20_balance(sell_addr, address)
            if held < amount_min_units:
                raise ValueError(
                    f"insufficient {getattr(sell_asset, 'symbol', 'balance')}: you hold "
                    f"{_fmt_units(held, sell_asset.decimals)} but this order spends "
                    f"{_fmt_units(amount_min_units, sell_asset.decimals)}"
                )

            try:
                quote = await dex.quote(sell_addr, buy_addr, amount_min_units)
            except DexError as e:
                raise ValueError(f"could not price this order on X Layer: {e}")

            # USD value of what is being spent — the figure the cap applies to.
            if side == "buy":
                est_usd: float | None = float(
                    Decimal(amount_min_units) / (Decimal(10) ** reg.quote.decimals)
                )
            else:
                est_usd = float(Decimal(quote.to_amount) / (Decimal(10) ** reg.quote.decimals))
            validate_usd_cap(est_usd, settings.max_order_usd, what=f"{side} {asset.symbol}")

            impact = quote.price_impact_pct
            if impact is not None and impact > settings.max_price_impact_pct:
                raise ValueError(
                    f"price impact ~{impact:.2f}% exceeds this server's "
                    f"{settings.max_price_impact_pct:g}% limit — the {asset.symbol} pool on "
                    "X Layer is too thin for an order this size. Try a smaller amount."
                )

            # A flat fee is only fair above a floor: $0.10 on a $2 order is 5%.
            if (est_usd is not None and settings.platform_fee_usd > 0
                    and settings.platform_fee_address and est_usd < settings.min_order_usd):
                raise ValueError(
                    f"minimum order is ${settings.min_order_usd:,.2f} — the flat "
                    f"${settings.platform_fee_usd:.2f} platform fee would be "
                    f"{settings.platform_fee_usd / est_usd * 100:.1f}% of a "
                    f"{_usd(est_usd)} order, which is not a fair rate."
                )

            stepup = passkey.check_stepup(db, address, est_usd)
            if stepup.blocked:
                raise ValueError(
                    f"passkey verification required before this order: {stepup.reason}. "
                    f"Open {settings.public_url}/dashboard/security to verify, then ask again."
                )

            # Fee always rides on the stablecoin leg so the user is never
            # charged in a volatile equity: on a buy that is the USDT they
            # spend, on a sell the USDT they receive.
            fee = self.fee_plan(est_usd)
            try:
                unsigned, quote, fee_applied = await dex.build_swap(
                    from_address=sell_addr, to_address=buy_addr,
                    amount_min_units=amount_min_units,
                    user_address=address, slippage_pct=slippage,
                    fee_percent=fee["percent"] if fee["charge"] else None,
                    fee_recipient=fee["recipient"] if fee["charge"] else None,
                    fee_on="from" if side == "buy" else "to",
                )
            except DexError as e:
                raise ValueError(f"could not build this order: {e}")

            out_dec = asset.decimals if side == "buy" else reg.quote.decimals
            out_sym = asset.symbol if side == "buy" else reg.quote.symbol
            in_sym = reg.quote.symbol if side == "buy" else asset.symbol

            order_id = db.create_order(
                address=address, side=side, symbol=asset.symbol,
                amount_in=amount_min_units, quoted_out=quote.to_amount,
                est_usd=est_usd, tx=unsigned.as_dict(),
                ttl_seconds=settings.order_ttl_seconds,
            )
            summary = (
                f"{side.upper()} {_fmt_units(quote.to_amount, out_dec)} {out_sym} "
                f"for {_fmt_units(amount_min_units, sell_asset.decimals)} {in_sym} "
                f"on X Layer (~{_usd(est_usd)})"
            )
            return {
                "order_id": order_id,
                "chain_id": CHAIN_ID,
                "side": side,
                "symbol": asset.symbol,
                "name": asset.name,
                "spending": f"{_fmt_units(amount_min_units, sell_asset.decimals)} {in_sym}",
                "receiving_estimated": f"{_fmt_units(quote.to_amount, out_dec)} {out_sym}",
                "minimum_received": (
                    _fmt_units(unsigned.min_receive, out_dec) if unsigned.min_receive else None
                ),
                "estimated_usd": round(est_usd, 2) if est_usd is not None else None,
                "platform_fee": {
                    # Report what was ACTUALLY attached, not what was configured:
                    # the CLI transport cannot carry fee parameters, and telling
                    # the user they paid a fee they did not pay is a lie either way.
                    "charged": bool(fee["charge"] and fee_applied),
                    "usd": fee["usd"] if (fee["charge"] and fee_applied) else 0.0,
                    "percent_of_order": round(fee["percent"], 4) if fee_applied else 0.0,
                    "denominated_in": reg.quote.symbol,
                    "note": (
                        None if (fee["charge"] and fee_applied)
                        else "no platform fee applied to this order"
                    ),
                },
                "price_impact_percent": impact,
                "slippage_percent": slippage,
                "route": quote.route or None,
                "human_summary": summary,
                "risk_notes": self._risk_notes(
                    asset=asset, side=side, est_usd=est_usd,
                    quote=quote, slippage=slippage, stepup=stepup,
                    fee={**fee, "charged": bool(fee["charge"] and fee_applied),
                         "denominated_in": reg.quote.symbol,
                         "percent_of_order": fee["percent"]},
                ),
                "unsigned_transaction": unsigned.as_dict(),
                "expires_at": int(time.time()) + settings.order_ttl_seconds,
                "sign_url": (
                    f"{settings.public_url}/sign?o={order_id}" if settings.public_url else None
                ),
                "status": "awaiting_signature",
                "next_step": _SIGN_STEP,
                "disclosure": SYNTHETIC_DISCLOSURE,
            }

        @mcp.tool()
        async def get_settlement_status(
            tx_hash: Annotated[str, Field(description="X Layer transaction hash (0x + 64 hex)")],
        ) -> dict[str, Any]:
            """Look up an X Layer transaction's confirmation status by hash."""
            h = validate_tx_hash(tx_hash)
            st = await rpc.tx_status(h)
            if not st.found:
                state = "unknown"
                detail = ("no transaction with this hash is known to the node — it may "
                          "never have been broadcast, or was dropped from the mempool")
            elif not st.mined:
                state = "pending"
                detail = "broadcast and waiting to be included in a block"
            elif st.success:
                state = "confirmed"
                detail = "included and executed successfully"
            else:
                state = "failed"
                detail = "included in a block but the transaction reverted; funds were not moved"
            return {
                "tx_hash": h, "chain_id": CHAIN_ID, "state": state, "detail": detail,
                "block_number": st.block_number, "gas_used": st.gas_used,
                "explorer_url": EXPLORER_TX.format(h),
            }

        @mcp.tool()
        async def get_order_history(
            limit: Annotated[int, Field(default=25, description="Max orders, 1-100")] = 25,
        ) -> dict[str, Any]:
            """Orders this wallet has created through Sarf, newest first."""
            address = require_address()
            n = max(1, min(int(limit), 100))
            rows = db.orders_for_address(address, n)
            out = []
            for r in rows:
                asset = reg.get(r["symbol"])
                dec = asset.decimals if asset else 18
                in_dec = reg.quote.decimals if r["side"] == "buy" else dec
                out.append({
                    "order_id": r["order_id"],
                    "created_at": int(r["created_at"]),
                    "side": r["side"], "symbol": r["symbol"],
                    "spent": _fmt_units(int(r["amount_in"]), in_dec),
                    "estimated_usd": round(r["est_usd"], 2) if r["est_usd"] else None,
                    "status": r["status"],
                    "tx_hash": r["tx_hash"],
                    "explorer_url": EXPLORER_TX.format(r["tx_hash"]) if r["tx_hash"] else None,
                })
            return {"address": address, "count": len(out), "orders": out}
