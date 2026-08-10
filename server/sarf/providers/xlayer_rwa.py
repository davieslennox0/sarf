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

import json
import time
from decimal import Decimal
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from pydantic import Field

from .. import passkey
from ..auth import require_address
from ..config import settings
from ..db import Database
from ..validation import ValidationError, validate_amount, validate_usd_cap
from ..xlayer import delegation, rpc
from ..xlayer.analysis import analyze
from ..xlayer.card import render_order_card, render_order_card_text
from ..xlayer.widget import UI_MIME, WIDGETS
from ..xlayer.evm import validate_evm_address, validate_tx_hash
from ..xlayer.okx_dex import DexError, OkxDexClient, Quote
from ..xlayer.registry import (
    CHAIN_ID, EXPLORER_TX, NATIVE, RwaAsset, XStocksRegistry,
)

SYNTHETIC_DISCLOSURE = (
    "xStocks are tokenized SYNTHETIC exposure to the underlying share price. "
    "Holding one does NOT give you share ownership, dividends, or voting rights, "
    "and redemption depends on the issuer (Backed Assets). Tell the user this "
    "before they trade."
)

_SIGN_STEP = (
    "Print the `card` field VERBATIM, exactly as given including its code "
    "fence, as the first thing in your reply — do not reformat it, summarise "
    "it, or drop lines from it. It already contains the amounts, the platform "
    "fee and the disclosures in the wording they have to keep. Then give the "
    "user sign_url. They sign the unsigned transaction in their own wallet — "
    "this server cannot execute it. Never say a trade happened until a "
    "tx_hash exists."
)

Side = Literal["buy", "sell"]

# Slippage the caller may request, hard-bounded here regardless of config.
_SLIPPAGE_MIN, _SLIPPAGE_MAX = 0.05, 5.0


def _fmt_units(amount: int, decimals: int) -> str:
    d = Decimal(amount) / (Decimal(10) ** decimals)
    return f"{d:.8f}".rstrip("0").rstrip(".") or "0"


def _usd(x: float | None) -> str:
    return f"${x:,.2f}" if x is not None else "n/a"


def _label(asset: Any) -> str:
    """Display name for either an equity or the quote asset. QuoteAsset has no
    `name` — only the 40 equities do — and a swap can name USDT on either leg."""
    return getattr(asset, "name", None) or asset.symbol


def _resolve_any(reg: XStocksRegistry, symbol: str) -> Any:
    """Resolve a symbol to an asset, allowing the quote asset on either leg.

    reg.resolve() covers the 40 equities only, because everywhere else USDT is
    implied by the side. A swap names both legs, so USDT has to be nameable —
    but still only by symbol, never by address: accepting an address from the
    model is how it gets talked into routing into an attacker's token.
    """
    s = (symbol or "").strip()
    if s.upper() in ("OKB", "XLAYER", "ETH"):
        # ETH accepted as an alias only because wallets label the gas coin that
        # way out of habit; on X Layer the gas coin is OKB and that is what the
        # response says back, so nobody is left thinking they hold ether.
        return NATIVE
    if s.upper() in ("USDT", "USDT0", "USD₮0", reg.quote.symbol.upper()):
        return reg.quote
    return reg.resolve(s, allowlist=settings.rwa_allowlist)


def _grant_from_row(row: dict[str, Any]) -> delegation.Grant:
    return delegation.Grant(
        address=row["address"], session_address=row["session_address"],
        delegate=row["delegate"], router=row["router"], stable=row["stable"],
        expiry=row["expiry"], per_trade_cap=row["per_trade_cap"],
        daily_cap=row["daily_cap"], created_at=row["created_at"],
        rotated_at=row["rotated_at"], revoked_at=row["revoked_at"],
    )


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

        The aggregator charges a percentage, so a fixed $0.10 would be 5% of a
        $2 trade. max_fee_percent caps the rate instead of the order being
        refused, so small orders pay proportionally rather than being turned
        away: a $2 order is charged $0.02.

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

    async def build_transfer(self, address: str, symbol: str, amount: str,
                             to_address: str, *, source: str = "mcp") -> dict[str, Any]:
        """Build (never send) a transfer, with every gate applied.

        Shared verbatim by the MCP tool and the website endpoint. Two copies of
        this would be two places for the passkey gate, the balance check and the
        gas reserve to drift apart — and the one that drifts is the one nobody
        is testing.
        """
        reg, db = self.reg, self.db
        db.upsert_user(address, source)

        # A transfer is the one operation that moves funds to somebody else.
        # The session key is built so it can never do this, and the passkey is
        # what proves a human is present for it — so this gate is unconditional
        # rather than threshold-based, and fails CLOSED when passkeys are
        # unavailable rather than waving the transfer through the way
        # check_stepup would.
        if not passkey.enabled():
            raise ValueError(
                "transfers need passkey verification and passkeys are not "
                "configured on this server, so transfers are unavailable here."
            )
        if not db.passkeys_for_address(address):
            raise ValueError(
                "transfers need a passkey. Register one at "
                f"{settings.public_url}/security, then try again."
            )
        last = db.last_passkey_verification(address)
        if last is None or (time.time() - last) > passkey.STEPUP_VALIDITY_SECONDS:
            raise ValueError(
                f"verify with your passkey first — open {settings.public_url}/security "
                f"and press Verify, then try again within "
                f"{passkey.STEPUP_VALIDITY_SECONDS // 60} minutes. Transfers always "
                "require this, whatever the amount."
            )

        asset = _resolve_any(reg, symbol)
        native = bool(getattr(asset, "is_native", False))
        # Checksum-validated, and never resolved from anything a caller
        # invented: a wrong recipient is unrecoverable, so a corrupted paste
        # must fail here rather than on-chain.
        to = validate_evm_address(to_address, what="to_address")
        if to.lower() == address.lower():
            raise ValidationError("that is your own address — this transfer would do nothing")
        units = validate_amount(amount, asset.decimals, what=f"amount ({asset.symbol})")

        held = (await rpc.native_balance(address) if native
                else await rpc.erc20_balance(asset.address, address))
        if held < units:
            raise ValueError(
                f"insufficient {asset.symbol}: you hold {_fmt_units(held, asset.decimals)} "
                f"but this sends {_fmt_units(units, asset.decimals)}"
            )
        if native:
            reserve = int(0.002 * 10 ** 18)
            if held - units < reserve:
                raise ValueError(
                    f"that would leave under {_fmt_units(reserve, 18)} OKB for gas, and "
                    "this transfer itself costs gas. Send a little less."
                )

        if native:
            tx = {"to": to, "data": "0x", "value": str(units),
                  "gas": 30000, "chainId": CHAIN_ID}
        else:
            # transfer(address,uint256) — built here, never taken from a
            # caller, so the recipient in the calldata is the one validated
            # above and shown to the user.
            data = ("0x" + "a9059cbb" + to.lower()[2:].rjust(64, "0") + f"{units:064x}")
            tx = {"to": asset.address, "data": data, "value": "0",
                  "gas": 120000, "chainId": CHAIN_ID}

        if asset.address.lower() == reg.quote.address.lower():
            usd: float | None = float(Decimal(units) / (Decimal(10) ** reg.quote.decimals))
        else:
            # OKB prices through the aggregator like anything else — the native
            # sentinel is a valid `from` token. Skipping it made every OKB
            # transfer unpriceable, which the USD cap then refused (correctly,
            # given it had no price) and so blocked outright.
            unit_price = await self._unit_price_usd(asset)
            usd = (None if unit_price is None else unit_price
                   * float(Decimal(units) / (Decimal(10) ** asset.decimals)))
        validate_usd_cap(usd, settings.max_order_usd, what=f"transfer {asset.symbol}")

        sending = f"{_fmt_units(units, asset.decimals)} {asset.symbol}"
        risk_notes = [
            f"This sends {sending} OUT of your wallet to {to}. Transfers are final "
            "once mined — there is no recall and no reversal.",
            "Check the recipient address character by character. An address that is "
            "wrong but valid will accept the funds and nobody can return them.",
            "Sarf cannot execute this. It is signed in your own wallet, and no "
            "session grant can ever perform a transfer.",
        ]
        if not native:
            risk_notes.append(SYNTHETIC_DISCLOSURE)

        fee_view = {"charged": False, "usd": 0.0,
                    "denominated_in": reg.quote.symbol,
                    "note": "no platform fee on transfers"}
        summary = f"TRANSFER {sending} to {to} on X Layer"
        order_id = db.create_order(
            address=address, side="transfer", symbol=asset.symbol,
            amount_in=units, quoted_out=0, est_usd=usd, tx=tx,
            ttl_seconds=settings.order_ttl_seconds,
            display={
                "name": f"Transfer to {to}", "human_summary": summary,
                "risk_notes": risk_notes, "spending": sending,
                "receiving_estimated": f"{to} receives {sending}",
                "recipient": to,
                "platform_fee": fee_view, "disclosure": SYNTHETIC_DISCLOSURE,
            },
        )
        payload = {
            "order_id": order_id, "chain_id": CHAIN_ID, "side": "transfer",
            "symbol": asset.symbol, "name": f"Transfer to {to}",
            "recipient": to, "spending": sending,
            "receiving_estimated": f"{to} receives {sending}",
            "estimated_usd": round(usd, 2) if usd is not None else None,
            "platform_fee": fee_view,
            "human_summary": summary, "risk_notes": risk_notes,
            "unsigned_transaction": tx,
            "expires_at": int(time.time()) + settings.order_ttl_seconds,
            "sign_url": (f"{settings.public_url}/sign?o={order_id}"
                         if settings.public_url else None),
            "status": "awaiting_signature", "can_execute": False,
            "next_step": (
                "Print the `card` verbatim and read the FULL recipient address back "
                "to the user before they sign. This moves funds out and cannot be "
                "undone. Give them sign_url — Sarf cannot execute it."
            ),
            "disclosure": SYNTHETIC_DISCLOSURE,
        }
        payload["card"] = render_order_card_text(payload)
        return payload

    def _gas_sponsored(self, address: str) -> bool:
        """True when this wallet has a live grant, so the relayer pays gas.

        Drives copy only. It changes what the cards claim about needing OKB,
        which stopped being true for delegated trades — telling someone to buy
        gas they do not need is its own kind of wrong answer.
        """
        g = self.db.get_grant(address)
        return bool(
            g and not g.get("revoked_at") and g["expiry"] > time.time()
            and settings.relayer_private_key
        )

    async def portfolio(self, address: str, *, record: bool = True) -> dict[str, Any]:
        """Holdings for `address`, read straight from X Layer state.

        Shared by the MCP tool and the dashboard so the numbers a user
        sees in chat and on the site can never disagree.

        record=False for the public lookup, which reads an address nobody has
        authenticated as. Recording there would let anyone grow the users table
        without limit just by iterating addresses, and would file strangers as
        Sarf users on the strength of somebody having typed their address.
        """
        reg, db = self.reg, self.db
        if record:
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

        # OKB is the gas coin, but it is also a real holding with a real price,
        # and reporting it as a bare quantity understated every wallet by
        # whatever it was worth. It is priced through the same 1-unit sell quote
        # as everything else, so its mark agrees with what it would actually
        # fill at.
        #
        # Deliberately NOT appended to `positions`: that list is the equity
        # sleeve that analyze() computes concentration weights against and runs
        # through classify(), which maps stock symbols to sectors. A gas coin
        # given a sector and a single-name concentration weight would be noise
        # dressed as a finding. It sits beside usdt_balance as a valued
        # non-equity holding instead.
        okb_qty = float(Decimal(okb_raw) / (Decimal(10) ** 18))
        okb_price = await self._unit_price_usd(NATIVE) if okb_raw > 0 else None
        okb_value = okb_qty * okb_price if okb_price is not None else None
        if okb_raw > 0 and okb_value is None:
            # Same fail-closed rule as the equities: an unpriceable holding
            # suppresses the total rather than being silently counted as zero.
            unpriced.append(NATIVE.symbol)

        usdt_value = float(Decimal(usdt_raw) / (Decimal(10) ** reg.quote.decimals))
        return {
            "address": address,
            "chain": {"name": "X Layer", "chain_id": CHAIN_ID},
            "positions": positions,
            "usdt_balance": _fmt_units(usdt_raw, reg.quote.decimals),
            # Kept under its original key so the dashboard and any stored
            # snapshots keep reading the balance they already read.
            "gas_balance_okb": _fmt_units(okb_raw, 18),
            "okb": {
                "symbol": NATIVE.symbol,
                "quantity": _fmt_units(okb_raw, 18),
                "price_usdt": round(okb_price, 6) if okb_price is not None else None,
                "value_usd": round(okb_value, 2) if okb_value is not None else None,
                "note": "OKB pays for gas on X Layer as well as being a holding, "
                        "so some of this balance has to stay put to transact.",
            },
            "okb_value_usd": round(okb_value, 2) if okb_value is not None else None,
            "gas_sponsored": self._gas_sponsored(address),
            "positions_value_usd": round(total_usd, 2),
            # Stated explicitly so a partial pricing outage can never be
            # mistaken for "these positions are worth nothing".
            "unpriced_positions": unpriced,
            "total_value_usd": round(
                total_usd + usdt_value + (okb_value or 0.0), 2
            ) if not unpriced else None,
            "disclosure": SYNTHETIC_DISCLOSURE,
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

        # --- MCP Apps UI resources ------------------------------------------
        # Registering the widget HTML as ui:// resources and pointing tools at
        # them with _meta.ui.resourceUri is what makes a card render in chat.
        # Without it the host has only content blocks to work with and the
        # model paraphrases the result — which is how the fee line and the
        # disclosure stopped appearing in the words we wrote them in.
        for uri, (wname, html, desc) in WIDGETS.items():
            def _make(body: str):
                def _read() -> str:
                    return body
                return _read
            mcp.resource(uri, name=wname, description=desc, mime_type=UI_MIME)(_make(html))

        def ui(uri: str) -> dict[str, Any]:
            return {"ui": {"resourceUri": uri}}

        @mcp.tool()
        async def get_rwa_list(
            symbol: Annotated[str | None, Field(
                default=None,
                description="Optional on-chain symbol, e.g. 'AAPLx' — returns a live "
                            "price for just that asset instead of the whole list")] = None,
        ) -> dict[str, Any]:
            """Tradable tokenized stocks and ETFs on X Layer, or one asset's price.

            Read-only. With no argument: the full universe, each with on-chain
            symbol, name, contract address and explorer link. With a symbol: a
            live price for that one asset, quoted from the same X Layer pools a
            trade would fill against, so the quote and the fill agree.
            """
            if symbol is not None:
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

        @mcp.tool(meta=ui("ui://sarf/portfolio-card"))
        async def get_portfolio() -> dict[str, Any]:
            """The authenticated wallet's tokenized-stock holdings on X Layer.

            Read-only, read straight from chain state. Balances are the
            authenticated session's wallet — no address argument exists.
            """
            return await self.portfolio(require_address())

        @mcp.tool(meta=ui("ui://sarf/order-card"))
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
            # Returns TextContent + ImageContent (the rendered card), so the
            # annotation cannot be dict -- FastMCP validates the return value
            # against it and rejects a content list.
        ) -> Any:
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

            # Off by default (min_order_usd = 0): any amount is accepted, and
            # the fee stays proportionate because max_fee_percent caps the rate.
            # Only fires if an operator sets a floor back.
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
                    f"Open {settings.public_url}/security to verify, then ask again."
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

            summary = (
                f"{side.upper()} {_fmt_units(quote.to_amount, out_dec)} {out_sym} "
                f"for {_fmt_units(amount_min_units, sell_asset.decimals)} {in_sym} "
                f"on X Layer (~{_usd(est_usd)})"
            )
            fee_view = {
                "charged": bool(fee["charge"] and fee_applied),
                "usd": fee["usd"] if (fee["charge"] and fee_applied) else 0.0,
                "percent_of_order": round(fee["percent"], 4) if fee_applied else 0.0,
                "denominated_in": reg.quote.symbol,
                "note": (None if (fee["charge"] and fee_applied)
                         else "no platform fee applied to this order"),
            }
            risk_notes = self._risk_notes(
                asset=asset, side=side, est_usd=est_usd,
                quote=quote, slippage=slippage, stepup=stepup, fee=fee_view,
            )
            # Can this be executed in chat, or must it go to the wallet? Purely
            # for presentation — execute_order re-checks all of it, since a
            # grant can be revoked between building an order and running it.
            _g = db.get_grant(address)
            can_execute = bool(
                _g and not _g.get("revoked_at") and _g["expiry"] > time.time()
                and est_usd is not None and est_usd <= settings.delegated_auto_usd
                and est_usd * 10 ** reg.quote.decimals <= _g["per_trade_cap"]
            )
            if can_execute:
                risk_notes.insert(1, (
                    f"This order is within your session grant (under "
                    f"{_usd(settings.delegated_auto_usd)}), so it can be executed here "
                    "without a wallet prompt. Revoke the grant any time from the "
                    "Security page."
                ))
            # Persist everything the signer page must re-display. It is the last
            # review surface before the user signs, so it has to show exactly
            # what the assistant showed them.
            order_id = db.create_order(
                address=address, side=side, symbol=asset.symbol,
                amount_in=amount_min_units, quoted_out=quote.to_amount,
                est_usd=est_usd, tx=unsigned.as_dict(),
                ttl_seconds=settings.order_ttl_seconds,
                display={
                    "name": asset.name,
                    "human_summary": summary,
                    "risk_notes": risk_notes,
                    "platform_fee": fee_view,
                    "spending": f"{_fmt_units(amount_min_units, sell_asset.decimals)} {in_sym}",
                    "receiving_estimated": f"{_fmt_units(quote.to_amount, out_dec)} {out_sym}",
                    "minimum_received": (
                        _fmt_units(unsigned.min_receive, out_dec) if unsigned.min_receive else None
                    ),
                    "price_impact_percent": impact,
                    "slippage_percent": slippage,
                    "disclosure": SYNTHETIC_DISCLOSURE,
                    # Raw values the delegated execute path needs. Persisted
                    # rather than recomputed so execution uses the exact terms
                    # the user was shown — a re-quote at execute time would
                    # settle a different trade than the one they approved.
                    "_exec": {
                        "sell_token": sell_addr, "buy_token": buy_addr,
                        "sell_amount": str(amount_min_units),
                        "min_buy_amount": str(unsigned.min_receive or 0),
                        "router": unsigned.to,
                        "data": unsigned.data,
                        "out_decimals": out_dec, "out_symbol": out_sym,
                    },
                },
            )
            payload = {
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
                # Reports what was ACTUALLY attached, not what was configured:
                # the CLI transport cannot carry fee parameters, and telling the
                # user they paid a fee they did not pay is a lie either way.
                "platform_fee": fee_view,
                "price_impact_percent": impact,
                "slippage_percent": slippage,
                "route": quote.route or None,
                "human_summary": summary,
                # Same list persisted with the order, so the model and the
                # signer page can never drift apart on what the risks are.
                "risk_notes": risk_notes,
                "unsigned_transaction": unsigned.as_dict(),
                "expires_at": int(time.time()) + settings.order_ttl_seconds,
                "sign_url": (
                    f"{settings.public_url}/sign?o={order_id}" if settings.public_url else None
                ),
                "status": "awaiting_signature",
                # Drives the widget's "Execute now" chip. Advisory only —
                # execute_order re-checks the grant, the threshold and the
                # chain, because a flag computed here is a flag a caller
                # could have gone stale on.
                "can_execute": can_execute,
                "next_step": _SIGN_STEP,
                "disclosure": SYNTHETIC_DISCLOSURE,
            }

            # Presentation. Every fact on the cards is also in `payload`, so
            # nothing is lost if neither renders — but the fee line and the
            # disclosure get shown as we wrote them rather than however the
            # model chose to paraphrase them.
            #
            # Two copies on purpose. The PNG is the nice one, and the server
            # does emit it as a proper image content block; but whether a chat
            # host actually displays images returned by a tool is the host's
            # decision, not ours, and several simply don't. The text card is
            # the one that always arrives, so it carries the same facts and
            # `next_step` asks for it verbatim.
            card = {**payload, "side": side, "symbol": asset.symbol, "name": asset.name}
            payload["card"] = render_order_card_text(card)
            png = render_order_card(card)
            text = TextContent(type="text", text=json.dumps(payload, default=str))
            if not png:
                return [text]
            return [text, ImageContent(type="image", data=png, mimeType="image/png")]

        @mcp.tool(meta=ui("ui://sarf/order-card"))
        async def swap(
            from_symbol: Annotated[str, Field(
                description="On-chain symbol to sell, e.g. 'AAPLx' or 'USDT'")],
            to_symbol: Annotated[str, Field(
                description="On-chain symbol to buy, e.g. 'NVDAx' or 'USDT'")],
            amount: Annotated[str, Field(
                description="Decimal string, denominated in from_symbol")],
            slippage_percent: Annotated[float | None, Field(
                default=None, description="Optional slippage tolerance, 0.05-5.0")] = None,
        ) -> Any:
            """Build an UNSIGNED swap between any two tradable assets on X Layer.

            The general case of place_order: rotate AAPLx into NVDAx directly
            without routing through USDT, or trade either against USDT. Does
            NOT execute — returns an unsigned transaction and a sign_url, or an
            order_id that execute_order can settle under a live session grant.
            """
            address = require_address()
            db.upsert_user(address, "mcp")
            sell_asset = _resolve_any(reg, from_symbol)
            buy_asset = _resolve_any(reg, to_symbol)
            if sell_asset.address.lower() == buy_asset.address.lower():
                raise ValidationError("from_symbol and to_symbol are the same asset")
            slippage = self._validate_slippage(slippage_percent)
            amount_min_units = validate_amount(
                amount, sell_asset.decimals, what=f"amount ({sell_asset.symbol})"
            )

            held = (await rpc.native_balance(address)
                    if getattr(sell_asset, "is_native", False)
                    else await rpc.erc20_balance(sell_asset.address, address))
            if held < amount_min_units:
                raise ValueError(
                    f"insufficient {sell_asset.symbol}: you hold "
                    f"{_fmt_units(held, sell_asset.decimals)} but this swap spends "
                    f"{_fmt_units(amount_min_units, sell_asset.decimals)}"
                )
            try:
                quote = await dex.quote(sell_asset.address, buy_asset.address, amount_min_units)
            except DexError as e:
                raise ValueError(f"could not price this swap on X Layer: {e}")

            # Value the SELL leg in USD. For an asset-to-asset rotation neither
            # side is the stable, so the cap has to be priced rather than read
            # off the amount — and it fails closed if it cannot be.
            stable = reg.quote.address.lower()
            if sell_asset.address.lower() == stable:
                est_usd: float | None = float(
                    Decimal(amount_min_units) / (Decimal(10) ** reg.quote.decimals))
            elif buy_asset.address.lower() == stable:
                est_usd = float(Decimal(quote.to_amount) / (Decimal(10) ** reg.quote.decimals))
            else:
                unit = await self._unit_price_usd(sell_asset)
                est_usd = (
                    None if unit is None
                    else unit * float(Decimal(amount_min_units)
                                      / (Decimal(10) ** sell_asset.decimals))
                )
            validate_usd_cap(est_usd, settings.max_order_usd,
                             what=f"swap {sell_asset.symbol}->{buy_asset.symbol}")

            impact = quote.price_impact_pct
            if impact is not None and impact > settings.max_price_impact_pct:
                raise ValueError(
                    f"price impact ~{impact:.2f}% exceeds this server's "
                    f"{settings.max_price_impact_pct:g}% limit — one of these pools is too "
                    "thin for a swap this size. Try a smaller amount."
                )

            # The platform fee always rides the stablecoin leg so nobody is ever
            # charged in a volatile asset. An asset-to-asset rotation has no
            # stablecoin leg, so it carries no fee — stated rather than silently
            # moved onto an equity.
            touches_stable = stable in (sell_asset.address.lower(), buy_asset.address.lower())
            fee = self.fee_plan(est_usd) if touches_stable else {
                "charge": False, "usd": 0.0, "percent": 0.0, "recipient": None,
                "reason": "no stablecoin leg — Sarf does not charge fees in a volatile asset",
            }
            # Off by default — see the note on the same guard in place_order.
            if (touches_stable and est_usd is not None and fee["charge"]
                    and est_usd < settings.min_order_usd):
                raise ValueError(
                    f"minimum is ${settings.min_order_usd:,.2f} — the flat "
                    f"${settings.platform_fee_usd:.2f} fee would be "
                    f"{settings.platform_fee_usd / est_usd * 100:.1f}% of a "
                    f"{_usd(est_usd)} swap, which is not a fair rate."
                )

            if getattr(sell_asset, "is_native", False):
                # Selling the gas coin can strand the wallet: spend it all and
                # there is nothing left to pay for the next transaction, or even
                # for this one. Reserve a float rather than letting a "sell
                # everything" land somebody with an unusable account.
                reserve = int(0.002 * 10 ** 18)
                if held - amount_min_units < reserve:
                    raise ValueError(
                        f"that would leave under {_fmt_units(reserve, 18)} OKB for gas. "
                        "OKB pays for every transaction on X Layer, including this one — "
                        f"keep at least that much back (you hold {_fmt_units(held, 18)})."
                    )

            stepup = passkey.check_stepup(db, address, est_usd)
            if stepup.blocked:
                raise ValueError(
                    f"passkey verification required before this swap: {stepup.reason}. "
                    f"Open {settings.public_url}/security to verify, then ask again."
                )
            try:
                unsigned, quote, fee_applied = await dex.build_swap(
                    from_address=sell_asset.address, to_address=buy_asset.address,
                    amount_min_units=amount_min_units, user_address=address,
                    slippage_pct=slippage,
                    fee_percent=fee["percent"] if fee["charge"] else None,
                    fee_recipient=fee["recipient"] if fee["charge"] else None,
                    fee_on="from" if sell_asset.address.lower() == stable else "to",
                )
            except DexError as e:
                raise ValueError(f"could not build this swap: {e}")

            spending = f"{_fmt_units(amount_min_units, sell_asset.decimals)} {sell_asset.symbol}"
            receiving = f"{_fmt_units(quote.to_amount, buy_asset.decimals)} {buy_asset.symbol}"
            fee_view = {
                "charged": bool(fee["charge"] and fee_applied),
                "usd": fee["usd"] if (fee["charge"] and fee_applied) else 0.0,
                "percent_of_order": round(fee["percent"], 4) if fee_applied else 0.0,
                "denominated_in": reg.quote.symbol,
                "note": fee.get("reason") or (
                    None if (fee["charge"] and fee_applied)
                    else "no platform fee applied to this swap"),
            }
            risk_notes = [SYNTHETIC_DISCLOSURE]
            if fee_view["charged"]:
                risk_notes.append(
                    f"Sarf platform fee: ${fee_view['usd']:.2f} in {reg.quote.symbol}, taken "
                    "inside the same transaction. Network gas is separate and paid in OKB.")
            elif not touches_stable:
                risk_notes.append(
                    "No platform fee on this swap: it has no stablecoin leg, and Sarf does "
                    "not take fees in a volatile asset.")
            if impact is not None and impact >= 1.0:
                risk_notes.append(
                    f"Price impact ~{impact:.2f}% — this swap is large relative to the pools "
                    "it routes through. Consider splitting it.")
            risk_notes.append(
                f"Slippage tolerance {slippage:g}%: you may receive up to {slippage:g}% less "
                "than quoted, and the transaction reverts beyond that.")
            if not touches_stable:
                risk_notes.append(
                    "Both legs are equities, so you are exposed to both prices for the whole "
                    "route — this is a rotation, not a way out of the market.")
            risk_notes.append(
                f"Settlement is on X Layer (chain {CHAIN_ID}). You pay gas in OKB and the "
                "swap is final once mined.")

            summary = (f"SWAP {spending} -> {receiving} on X Layer"
                       + (f" (~{_usd(est_usd)})" if est_usd is not None else ""))
            # A native leg can never be delegated: SarfSessionKey reads
            # balances with balanceOf() and calls the router with value: 0, so
            # that a session key can never reach the gas coin. That is a
            # property worth keeping, so these swaps go to the wallet instead.
            native_leg = (getattr(sell_asset, "is_native", False)
                          or getattr(buy_asset, "is_native", False))
            _g = db.get_grant(address)
            can_execute = bool(
                not native_leg
                and _g and not _g.get("revoked_at") and _g["expiry"] > time.time()
                and est_usd is not None and est_usd <= settings.delegated_auto_usd
                and est_usd * 10 ** reg.quote.decimals <= _g["per_trade_cap"])
            if native_leg:
                risk_notes.append(
                    "This swap involves OKB, the gas coin, so it is signed in your own "
                    "wallet even if you have a session grant — the session key is built "
                    "so that it can never touch your gas.")

            order_id = db.create_order(
                address=address, side="swap", symbol=buy_asset.symbol,
                amount_in=amount_min_units, quoted_out=quote.to_amount,
                est_usd=est_usd, tx=unsigned.as_dict(),
                ttl_seconds=settings.order_ttl_seconds,
                display={
                    "name": f"{sell_asset.symbol} -> {buy_asset.symbol}",
                    "human_summary": summary, "risk_notes": risk_notes,
                    "platform_fee": fee_view, "spending": spending,
                    "receiving_estimated": receiving,
                    "minimum_received": (
                        f"{_fmt_units(unsigned.min_receive, buy_asset.decimals)} "
                        f"{buy_asset.symbol}" if unsigned.min_receive else None),
                    "price_impact_percent": impact, "slippage_percent": slippage,
                    "disclosure": SYNTHETIC_DISCLOSURE,
                    "_exec": {
                        "sell_token": sell_asset.address, "buy_token": buy_asset.address,
                        "sell_amount": str(amount_min_units),
                        "min_buy_amount": str(unsigned.min_receive or 0),
                        "router": unsigned.to, "data": unsigned.data,
                        "out_decimals": buy_asset.decimals, "out_symbol": buy_asset.symbol,
                    },
                },
            )
            payload = {
                "order_id": order_id, "chain_id": CHAIN_ID, "side": "swap",
                "symbol": f"{sell_asset.symbol} → {buy_asset.symbol}",
                "name": f"{_label(sell_asset)} to {_label(buy_asset)}",
                "spending": spending, "receiving_estimated": receiving,
                "minimum_received": (
                    f"{_fmt_units(unsigned.min_receive, buy_asset.decimals)} "
                    f"{buy_asset.symbol}" if unsigned.min_receive else None),
                "estimated_usd": round(est_usd, 2) if est_usd is not None else None,
                "platform_fee": fee_view, "price_impact_percent": impact,
                "slippage_percent": slippage, "route": quote.route or None,
                "human_summary": summary, "risk_notes": risk_notes,
                "unsigned_transaction": unsigned.as_dict(),
                "expires_at": int(time.time()) + settings.order_ttl_seconds,
                "sign_url": (f"{settings.public_url}/sign?o={order_id}"
                             if settings.public_url else None),
                "status": "awaiting_signature", "can_execute": can_execute,
                "next_step": _SIGN_STEP, "disclosure": SYNTHETIC_DISCLOSURE,
            }
            payload["card"] = render_order_card_text(payload)
            png = render_order_card(payload)
            text = TextContent(type="text", text=json.dumps(payload, default=str))
            if not png:
                return [text]
            return [text, ImageContent(type="image", data=png, mimeType="image/png")]

        @mcp.tool(meta=ui("ui://sarf/order-card"))
        async def transfer(
            symbol: Annotated[str, Field(
                description="What to send: 'OKB', 'USDT', or an on-chain symbol like 'AAPLx'")],
            amount: Annotated[str, Field(description="Decimal string, in units of symbol")],
            to_address: Annotated[str, Field(
                description="Recipient 0x address on X Layer, EIP-55 checksummed")],
        ) -> Any:
            """Build an UNSIGNED transfer of tokens or OKB to another address.

            Requires a fresh passkey verification, and is ALWAYS signed by the
            user in their own wallet — a transfer can never run under a session
            grant, whatever limits are set. Does not execute anything.

            A transfer sends funds OUT and cannot be undone. Read the recipient
            address back to the user, in full, before they sign.
            """
            payload = await self.build_transfer(
                require_address(), symbol, amount, to_address)
            png = render_order_card(payload)
            text = TextContent(type="text", text=json.dumps(payload, default=str))
            return [text] if not png else [
                text, ImageContent(type="image", data=png, mimeType="image/png")]

        # One read-only status tool instead of three. Three separate lookups
        # answering variations of "where does this stand" made the toolset wider
        # than the capability warranted, and a wide toolset is genuinely harder
        # for an assistant to discover by keyword search — which is how tools
        # get found when several connectors are enabled at once. Fewer, richer
        # tools beat many narrow ones for discoverability.
        @mcp.tool()
        async def get_status(
            tx_hash: Annotated[str | None, Field(
                default=None,
                description="Optional X Layer tx hash (0x + 64 hex) to check confirmation of")] = None,
            history_limit: Annotated[int | None, Field(
                default=None, description="Optional: include this many recent orders, 1-100")] = None,
        ) -> dict[str, Any]:
            """Session grant, transaction confirmation, and order history.

            Read-only, and every section is optional except the session one:
            call with no arguments for whether in-chat execution is live and
            under what limits; pass tx_hash to confirm a settlement; pass
            history_limit for recent orders. Combine them freely.
            """
            address = require_address()
            row = db.get_grant(address)
            installed = await rpc.delegated_to(address)
            on_chain = bool(installed and settings.delegate_address
                            and installed.lower() == settings.delegate_address.lower())
            base = {
                "address": address,
                "delegation_installed": on_chain,
                "auto_execute_under_usd": settings.delegated_auto_usd,
                "passkey_registered": bool(db.passkeys_for_address(address)),
                "setup_url": f"{settings.public_url}/security" if settings.public_url else None,
            }
            if not row or not on_chain:
                base["grant"] = None
                base["note"] = (
                    "No live session grant. Orders here are BUILT only — give the user "
                    "sign_url and they sign in their own wallet. They can set up in-chat "
                    "execution at setup_url; it is optional and revocable."
                )
            else:
                g = _grant_from_row(row)
                base["grant"] = g.view(reg.quote.decimals)
                base["note"] = (
                    "A live grant exists: orders at or under auto_execute_under_usd can be "
                    "executed with execute_order. Larger ones need a fresh passkey. The "
                    "on-chain caps are the hard ceiling and Sarf cannot raise them."
                )
            if tx_hash is not None:
                base["settlement"] = await _settlement_view(tx_hash)
            if history_limit is not None:
                base["history"] = _history_view(address, history_limit)
            return base

        @mcp.tool(meta=ui("ui://sarf/order-card"))
        async def execute_order(
            order_id: Annotated[str, Field(description="An order_id returned by place_order")],
        ) -> Any:
            """Execute an order the user already reviewed, under their session grant.

            Only works if the user has installed a grant and the order is within
            the limits they set. Settles on X Layer and returns a real tx hash.
            Never call this without showing the user the order first — it moves
            their funds.
            """
            address = require_address()
            order = db.get_order(order_id)
            if not order or order["address"].lower() != address.lower():
                raise ValueError("no such order for this wallet")
            if order.get("status") not in (None, "proposed"):
                raise ValueError(
                    f"order {order_id} is already {order.get('status')}"
                    + (f" (tx {order['tx_hash']})" if order.get("tx_hash") else "")
                )
            if order["expires_at"] < time.time():
                raise ValueError("this quote has expired — ask for a fresh order")

            # db.get_order merges the display fields into the top level (they
            # never override the authoritative columns), so read them from the
            # order itself rather than a nested key that does not exist.
            ex = order.get("_exec")
            if not ex:
                raise ValueError(
                    "this order predates in-chat execution — ask for a fresh one"
                )

            row = db.get_grant(address)
            if not row or row.get("revoked_at") or row["expiry"] < time.time():
                raise ValueError(
                    "no live session grant — the user signs this one in their own wallet. "
                    "Give them the sign_url from place_order instead."
                )
            installed = await rpc.delegated_to(address)
            if not (installed and installed.lower() == row["delegate"].lower()):
                raise ValueError(
                    "the session grant is not installed on-chain for this wallet — "
                    "the authorisation transaction may not have landed. Re-run setup."
                )

            est = order.get("est_usd")
            # Threshold is the user's "are you sure" line, separate from the
            # on-chain cap. Fails CLOSED on an unpriceable order: a trade we
            # cannot value is exactly the one not to auto-execute.
            if est is None or est > settings.delegated_auto_usd:
                # check_stepup treats "no passkey registered" as "step-up not
                # enforced", which is right for a flow that ends in a wallet
                # prompt — the wallet is the real gate there. Here there is no
                # wallet prompt, so that default would auto-execute an
                # unlimited order for anyone without a passkey. Require the
                # credential to exist before consulting the decision at all.
                if not db.passkeys_for_address(address):
                    raise ValueError(
                        "orders above the auto-execute threshold need a passkey, and "
                        "this account has none registered. Register one at "
                        f"{settings.public_url}/security, or sign this order in your "
                        "wallet instead."
                    )
                stepup = passkey.check_stepup(db, address, est)
                if not stepup.satisfied:
                    raise ValueError(
                        f"this order is {_usd(est)}, over the {_usd(settings.delegated_auto_usd)} "
                        "auto-execute threshold. Verify with your passkey at "
                        f"{settings.public_url}/security, then ask again."
                        if est is not None else
                        "this order could not be priced, so it will not auto-execute. "
                        "Sign it in your wallet instead."
                    )

            deadline = int(time.time()) + 300
            signature, nonce = delegation.sign_swap(
                row["sealed_key"], account=address,
                sell_token=ex["sell_token"], buy_token=ex["buy_token"],
                sell_amount=int(ex["sell_amount"]), min_buy_amount=int(ex["min_buy_amount"]),
                target=ex["router"], data=ex["data"], deadline=deadline,
            )
            calldata = delegation.encode_execute_swap(
                sell_token=ex["sell_token"], buy_token=ex["buy_token"],
                sell_amount=int(ex["sell_amount"]), min_buy_amount=int(ex["min_buy_amount"]),
                target=ex["router"], data=ex["data"], nonce=nonce,
                deadline=deadline, signature=signature,
            )
            try:
                # Sent to the USER's address: under EIP-7702 that address is
                # running the delegate's code, so this is their account
                # executing their own grant. The relayer only pays the gas.
                tx_hash = await delegation.relay(to=address, data=calldata)
            except Exception as e:
                raise ValueError(f"could not submit this trade to X Layer: {e}")

            db.mark_order(order_id, "submitted", tx_hash=tx_hash)
            payload = {
                "order_id": order_id,
                "tx_hash": tx_hash,
                "explorer_url": EXPLORER_TX.format(tx_hash),
                "chain_id": CHAIN_ID,
                "side": order["side"], "symbol": order["symbol"],
                "name": order.get("name"),
                "spending": order.get("spending"),
                "receiving_estimated": order.get("receiving_estimated"),
                "estimated_usd": est,
                "platform_fee": order.get("platform_fee"),
                "status": "submitted",
                "executed": True,
                "next_step": (
                    "Show the tx_hash and explorer_url. It is submitted, not yet "
                    "confirmed — use get_settlement_status to confirm it mined "
                    "successfully before saying the trade completed."
                ),
                "disclosure": SYNTHETIC_DISCLOSURE,
            }
            payload["card"] = render_order_card_text(payload)
            return [TextContent(type="text", text=json.dumps(payload, default=str))]

        # Plain helpers, not tools. Their capability is still reachable — it
        # moved into get_status — but it no longer costs a slot in the client's
        # tool budget.
        async def _settlement_view(tx_hash: str) -> dict[str, Any]:
            """An X Layer transaction's confirmation status, by hash."""
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

        def _history_view(address: str, limit: int) -> dict[str, Any]:
            """Orders this wallet has created through Sarf, newest first."""
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

        # Registered last simply because it is the least load-bearing tool here:
        # it moves no money and re-reads holdings get_portfolio already returns.
        #
        # It is NOT last because of any client tool cap. That theory was floated
        # on 2026-08-10 to explain an assistant insisting Sarf had no `swap`,
        # and it was wrong — the assistant had run a keyword tool search capped
        # at 20 results across three connectors exposing 71 tools between them,
        # and read the partial result as a complete registry. No truncation was
        # ever involved; `swap` was registered and callable throughout.
        @mcp.tool(meta=ui("ui://sarf/analysis-card"))
        async def analyze_portfolio() -> dict[str, Any]:
            """Analyse the authenticated wallet's holdings: concentration,
            diversification, sector and instrument mix.

            Read-only. Applies standard portfolio-analysis methodology — the
            measures a CFA charterholder would reach for — but this is NOT
            regulated financial advice and Sarf is not a licensed adviser.

            Every field of the response must be relayed under the rules the
            response itself carries: state each finding as a fact beside the
            norm it is measured against and let the user draw the conclusion;
            never instruct them to buy, sell, trim or rebalance; never forecast
            a price; always relay `disclosure` and `missing_context`, because
            this tool sees on-chain holdings only and knows nothing about the
            user's income, goals, horizon or risk tolerance.
            """
            return analyze(await self.portfolio(require_address()))
