"""Current Finance provider: lending + leverage MCP tools.

Every state-changing tool follows the same contract:
  validate inputs -> verify ownership on-chain -> build unsigned PTB in the
  sidecar -> dry-run -> enforce USD cap -> persist proposal -> return it.
Nothing in this file signs or broadcasts except submit_signed_transaction,
which only relays bytes the user's wallet already signed, and only if they
byte-match a stored, unexpired proposal (this server is not an open relay).
"""

from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from ..config import settings
from ..db import Database
from ..registry import AssetRegistry
from ..txclient import TxBuilder, TxBuilderError
from ..validation import (
    OwnershipError,
    ValidationError,
    check_cap_ownership,
    validate_address,
    validate_amount,
    validate_asset,
    validate_base64,
    validate_market,
    validate_multiplier,
    validate_object_id,
    validate_usd_cap,
)

_PROPOSAL_ID_RE = re.compile(r"^sarf_[0-9a-f]{32}$")

MarketName = Literal["MainMarket", "AltCoinMarket", "EmberMarket", "MatrixGoldMarket", "EthenaMarket"]

_SIGN_STEP = (
    "Present this proposal to the user for review. If they approve, they sign "
    "ptb_base64 in their own wallet (zkLogin session or extension — this server "
    "never sees keys), then call submit_signed_transaction(proposal_id, "
    "signed_tx_bytes_base64, signatures)."
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def _usd(x: float | None) -> str:
    return f"${x:,.2f}" if x is not None else "n/a"


class CurrentFinanceProvider:
    name = "current_finance"

    def __init__(self, db: Database, tx: TxBuilder, registry: AssetRegistry):
        self.db = db
        self.tx = tx
        self.registry = registry

    # ------------------------------------------------------------------ utils

    async def _verify_cap(self, user_address: str, cap_id: str) -> dict[str, Any]:
        """Ownership is decided by live chain state, never by our cache."""
        try:
            cap = await self.tx.cap(cap_id)
        except TxBuilderError as e:
            if e.status in (400, 404):
                raise ValidationError(f"obligation cap {cap_id} could not be resolved: {e}")
            raise
        check_cap_ownership(user_address, cap_id, cap.get("owner"))
        market_name = self.registry.market_name_for_type(cap["marketType"])
        if market_name is None:
            raise ValidationError(f"cap {cap_id} belongs to a market this server does not serve")
        cap["marketName"] = market_name
        self.db.upsert_cap(user_address, cap_id, cap["obligationId"], cap["marketType"], market_name)
        return cap

    def _risk_notes(self, build: dict[str, Any], action: str) -> list[str]:
        notes: list[str] = []
        risk = build.get("risk")
        if risk:
            after, before = risk["after"], risk["before"]
            notes.append(
                f"LTV after this {action}: {_pct(after['currentLtv'])} "
                f"(was {_pct(before['currentLtv'])}); borrowing limit at "
                f"{_pct(after['maxLtv'])}, liquidation begins at {_pct(after['liquidationLtv'])}."
            )
            for lp in risk.get("liquidationPrices", []):
                cur, liq = lp.get("currentPriceUsd"), lp.get("liquidationPriceUsd")
                if not cur or liq is None or liq <= 0:
                    continue
                sym = lp["coinType"].split("::")[-1]
                if lp["side"] == "collateral":
                    move = (liq - cur) / cur * 100
                    notes.append(
                        f"{sym} collateral: liquidation if price falls to {_usd(liq)} "
                        f"(now {_usd(cur)}, {move:+.1f}%)."
                    )
                else:
                    move = (liq - cur) / cur * 100
                    notes.append(
                        f"{sym} debt: liquidation if price rises to {_usd(liq)} "
                        f"(now {_usd(cur)}, {move:+.1f}%)."
                    )
        else:
            notes.append(
                "Live risk projection unavailable for this proposal; the dry-run below "
                "is still authoritative for what the transaction does."
            )
        return notes

    def _finish_proposal(
        self, *, user_address: str, tool: str, params: dict[str, Any],
        build: dict[str, Any], summary: str, risk_notes: list[str],
    ) -> dict[str, Any]:
        sim = build["simulation"]
        sim_ok = sim["status"] == "success"
        self.db.upsert_user(user_address, "mcp")
        prop = self.db.create_proposal(
            user_address=user_address,
            tool=tool,
            params=params,
            ptb_base64=build["txBytesBase64"],
            simulation=sim,
            risk=build.get("risk"),
            ttl_seconds=settings.proposal_ttl_seconds,
            summary_text=summary,
            risk_notes=risk_notes,
        )
        if not sim_ok:
            self.db.mark_proposal(prop.proposal_id, "simulation_failed")
        sign_url = (
            f"{settings.public_url}/sign?p={prop.proposal_id}" if settings.public_url else None
        )
        next_step = _SIGN_STEP
        if sign_url:
            # Inline signing as a Claude artifact is not possible — the
            # Artifacts sandbox CSP blocks all external network calls — so the
            # signer is a self-hosted page linked from chat (see README).
            next_step = (
                "Present this proposal to the user, then link them to the Sarf "
                f"signer to review and sign in their own wallet: {sign_url} "
                "This server never sees keys."
            )
        return {
            "proposal_id": prop.proposal_id,
            "status": "proposed" if sim_ok else "simulation_failed",
            "human_summary": summary if sim_ok else (
                f"NOT EXECUTABLE — the dry-run failed: {sim.get('error') or 'unknown error'}. "
                f"Original intent: {summary}"
            ),
            "ptb_base64": build["txBytesBase64"] if sim_ok else None,
            "sign_url": sign_url if sim_ok else None,
            "gas_estimate_sui": sim.get("gasUsedSui"),
            "estimated_usd_value": build.get("estUsd"),
            "risk_notes": risk_notes,
            "simulation": sim,
            "expires_at": _iso(prop.expires_at),
            "next_step": next_step if sim_ok else
                "Do not proceed; adjust the request and propose again.",
        }

    async def execute_submit(
        self, proposal_id: str, signed_tx_bytes_base64: str, signatures: list[str],
    ) -> dict[str, Any]:
        """Shared write path for the MCP tool and the signer page's REST call.

        All submit invariants live here exactly once: proposal must exist, be
        'proposed', unexpired, and the signed bytes must byte-match the PTB we
        built and simulated. This server is not an open relay.
        """
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID_RE.match(proposal_id.strip()):
            raise ValidationError("proposal_id must look like sarf_<32 hex>")
        pid = proposal_id.strip()
        tx_b64 = validate_base64(signed_tx_bytes_base64, what="signed_tx_bytes_base64")
        if not isinstance(signatures, list) or not (1 <= len(signatures) <= 16):
            raise ValidationError("signatures must be a list of 1-16 base64 strings")
        sigs = [validate_base64(s, what="signature", max_bytes=8192) for s in signatures]

        prop = self.db.get_proposal(pid)
        if prop is None:
            raise ValidationError(f"unknown proposal {pid}")
        if prop.status != "proposed":
            raise ValidationError(f"proposal {pid} is not executable (status: {prop.status})")
        if time.time() > prop.expires_at:
            self.db.mark_proposal(pid, "expired")
            raise ValidationError(
                f"proposal {pid} expired at {_iso(prop.expires_at)}; market state may have "
                f"moved — request a fresh proposal"
            )
        # Byte-for-byte binding to what we proposed (and simulated). A wallet
        # returns exactly the bytes it was given; any difference means the
        # payload was altered somewhere between proposal and signing.
        if base64.b64decode(tx_b64) != base64.b64decode(prop.ptb_base64):
            self.db.mark_proposal(pid, "rejected")
            raise ValidationError(
                "signed bytes do not match the proposed transaction; refusing to broadcast"
            )

        result = await self.tx.broadcast(prop.ptb_base64, sigs)
        ok = result.get("status") == "success"
        self.db.mark_proposal(pid, "submitted" if ok else "failed",
                              tx_digest=result.get("digest"), result=result)

        response: dict[str, Any] = {
            "tx_digest": result.get("digest"),
            "status": result.get("status"),
            "error": result.get("error"),
            "balance_changes": result.get("balanceChanges", []),
        }
        # Surface a newly created ObligationOwnerCap (enter-market flow) and
        # remember it for this user.
        for obj in result.get("createdObjects", []):
            if "ObligationOwnerCap" in obj.get("objectType", ""):
                response["obligation_cap_id"] = obj["objectId"]
                self.db.upsert_cap(prop.user_address, obj["objectId"], "", "", None)
                response["note"] = (
                    "New ObligationOwnerCap created — use this ID for future "
                    "deposit/borrow/repay/withdraw proposals."
                )
        return response

    # ------------------------------------------------------------------ tools

    def register_tools(self, mcp: FastMCP) -> None:  # noqa: C901 - tool bundle
        db, tx, registry = self.db, self.tx, self.registry
        provider = self

        @mcp.tool()
        async def get_portfolio(
            user_address: Annotated[str, Field(description="Sui address (0x + 64 hex)")],
        ) -> dict[str, Any]:
            """Read-only: current Current Finance lending positions for an address,
            including obligation cap IDs, supplied/borrowed balances, LTVs and
            per-asset liquidation prices. No auth required — this is public
            on-chain state."""
            addr = validate_address(user_address)
            db.upsert_user(addr, "mcp")
            data = await tx.portfolio(addr)
            for p in data.get("positions", []):
                if "obligationId" in p:
                    db.upsert_cap(addr, p["obligationCapId"], p["obligationId"], "", p.get("market"))
            data["recent_proposals"] = db.audit_trail(addr, limit=10)
            return data

        @mcp.tool()
        async def get_market_info(
            market: Annotated[MarketName, Field(description="Current Finance market")] = "MainMarket",
            asset: Annotated[str | None, Field(description="Optional asset symbol filter, e.g. 'USDC'")] = None,
        ) -> dict[str, Any]:
            """Read-only: live Current Finance market data — supply/borrow APR,
            utilization, prices, collateral & liquidation factors, pause flags."""
            await registry.ensure_loaded()
            m = validate_market(market, registry.markets)
            info = await tx.market_info(m)
            if asset is not None:
                a = validate_asset(asset, m, registry.assets, settings.asset_whitelist)
                info["assets"] = [x for x in info["assets"] if x["coinType"] == a.coin_type]
            return info

        @mcp.tool()
        async def propose_enter_market(
            user_address: Annotated[str, Field(description="Sui address that will sign")],
            asset: Annotated[str, Field(description="Asset symbol to deposit, e.g. 'USDC'")],
            amount: Annotated[str, Field(description='Amount in human units as a decimal string, e.g. "25.5"')],
            market: Annotated[MarketName, Field(description="Market to enter")] = "MainMarket",
        ) -> dict[str, Any]:
            """Build (never execute) an 'enter market + first deposit' transaction.
            Creates the user's ObligationOwnerCap. Returns an unsigned proposal
            for the user to review and sign in their own wallet."""
            await registry.ensure_loaded()
            addr = validate_address(user_address)
            m = validate_market(market, registry.markets)
            a = validate_asset(asset, m, registry.assets, settings.asset_whitelist)
            min_units = validate_amount(amount, a.decimals)

            build = await tx.build(
                action="enter_deposit", sender=addr, coinType=a.coin_type,
                amountMinUnits=str(min_units), marketName=m,
            )
            validate_usd_cap(build.get("estUsd"), settings.max_proposal_usd,
                             what=f"enter {m} with {amount} {a.symbol}")
            summary = (
                f"Enter Current Finance {m} and deposit {amount} {a.symbol} "
                f"(~{_usd(build.get('estUsd'))}). This creates your ObligationOwnerCap — "
                f"an object in your wallet that controls this lending position; keep it."
            )
            notes = provider._risk_notes(build, "deposit")
            notes.append(
                "After on-chain confirmation the new ObligationOwnerCap ID is reported "
                "by submit_signed_transaction and used for all follow-up actions."
            )
            return provider._finish_proposal(
                user_address=addr, tool="propose_enter_market",
                params={"market": m, "asset": a.symbol, "amount": amount},
                build=build, summary=summary, risk_notes=notes,
            )

        async def _simple_action(
            action: Literal["deposit", "borrow", "repay", "withdraw"],
            tool_name: str,
            user_address: str, obligation_cap_id: str, asset: str, amount: str,
        ) -> dict[str, Any]:
            await registry.ensure_loaded()
            addr = validate_address(user_address)
            cap_id = validate_object_id(obligation_cap_id, what="obligation_cap_id")
            cap = await provider._verify_cap(addr, cap_id)
            m = cap["marketName"]
            a = validate_asset(asset, m, registry.assets, settings.asset_whitelist)
            min_units = validate_amount(amount, a.decimals)

            build = await tx.build(
                action=action, sender=addr, coinType=a.coin_type,
                amountMinUnits=str(min_units), obligationCapId=cap_id,
            )
            # Defense in depth: the sidecar independently reports the cap owner
            # it saw while building; a mismatch means state changed mid-flight.
            if build.get("capOwner") and build["capOwner"].lower() != addr:
                raise OwnershipError(f"cap {cap_id} changed owner while building; aborting")
            validate_usd_cap(build.get("estUsd"), settings.max_proposal_usd,
                             what=f"{action} {amount} {a.symbol}")

            verb = {
                "deposit": f"Deposit {amount} {a.symbol} into",
                "borrow": f"Borrow {amount} {a.symbol} from",
                "repay": f"Repay {amount} {a.symbol} of debt in",
                "withdraw": f"Withdraw {amount} {a.symbol} of collateral from",
            }[action]
            summary = f"{verb} your {m} position (~{_usd(build.get('estUsd'))})."
            return provider._finish_proposal(
                user_address=addr, tool=tool_name,
                params={"market": m, "asset": a.symbol, "amount": amount, "cap": cap_id},
                build=build, summary=summary, risk_notes=provider._risk_notes(build, action),
            )

        @mcp.tool()
        async def propose_deposit(
            user_address: Annotated[str, Field(description="Sui address that will sign")],
            obligation_cap_id: Annotated[str, Field(description="ObligationOwnerCap object ID (must be owned by user_address)")],
            asset: Annotated[str, Field(description="Asset symbol, e.g. 'USDC'")],
            amount: Annotated[str, Field(description='Amount in human units, decimal string')],
        ) -> dict[str, Any]:
            """Build (never execute) a deposit into an existing lending position."""
            return await _simple_action("deposit", "propose_deposit",
                                        user_address, obligation_cap_id, asset, amount)

        @mcp.tool()
        async def propose_borrow(
            user_address: Annotated[str, Field(description="Sui address that will sign")],
            obligation_cap_id: Annotated[str, Field(description="ObligationOwnerCap object ID (must be owned by user_address)")],
            asset: Annotated[str, Field(description="Asset symbol to borrow")],
            amount: Annotated[str, Field(description='Amount in human units, decimal string')],
        ) -> dict[str, Any]:
            """Build (never execute) a borrow against existing collateral. The
            proposal states the post-borrow LTV and liquidation prices — always
            surface those to the user, not just the amounts."""
            return await _simple_action("borrow", "propose_borrow",
                                        user_address, obligation_cap_id, asset, amount)

        @mcp.tool()
        async def propose_repay(
            user_address: Annotated[str, Field(description="Sui address that will sign")],
            obligation_cap_id: Annotated[str, Field(description="ObligationOwnerCap object ID (must be owned by user_address)")],
            asset: Annotated[str, Field(description="Asset symbol of the debt")],
            amount: Annotated[str, Field(description='Amount in human units, decimal string')],
        ) -> dict[str, Any]:
            """Build (never execute) a debt repayment."""
            return await _simple_action("repay", "propose_repay",
                                        user_address, obligation_cap_id, asset, amount)

        @mcp.tool()
        async def propose_withdraw(
            user_address: Annotated[str, Field(description="Sui address that will sign")],
            obligation_cap_id: Annotated[str, Field(description="ObligationOwnerCap object ID (must be owned by user_address)")],
            asset: Annotated[str, Field(description="Asset symbol to withdraw")],
            amount: Annotated[str, Field(description='Amount in human units, decimal string')],
        ) -> dict[str, Any]:
            """Build (never execute) a collateral withdrawal."""
            return await _simple_action("withdraw", "propose_withdraw",
                                        user_address, obligation_cap_id, asset, amount)

        @mcp.tool()
        async def propose_leverage_position(
            user_address: Annotated[str, Field(description="Sui address that will sign")],
            collateral_asset: Annotated[str, Field(description="Collateral symbol for the loop, e.g. 'HASUI'")],
            principal_amount: Annotated[str, Field(description='Principal to commit, human units, decimal string')],
            target_multiplier: Annotated[float, Field(description=f"Leverage multiplier, capped server-side (<= {settings.leverage_max_multiplier})")],
        ) -> dict[str, Any]:
            """Build (never execute) a Current Multiply leveraged position via a
            flash-loan loop. Higher risk than plain deposits: the whole principal
            can be liquidated. The multiplier is hard-capped server-side; the cap
            in this tool's description is enforcement, not advice."""
            await registry.ensure_loaded()
            addr = validate_address(user_address)
            mult = validate_multiplier(target_multiplier, settings.leverage_max_multiplier)
            if not isinstance(collateral_asset, str) or not collateral_asset.strip():
                raise ValidationError("collateral_asset must be a symbol string")
            sym = collateral_asset.strip().upper()
            # Leverage pairs are operator-configured (quote-server pair IDs are
            # not derivable from the SDK); asset+decimals still validated
            # against the MainMarket registry so amounts can't be forged.
            a = validate_asset(sym, "MainMarket", registry.assets, settings.asset_whitelist)
            min_units = validate_amount(principal_amount, a.decimals)

            build = await tx.build_leverage(
                sender=addr, collateralSymbol=sym,
                principalMinUnits=str(min_units), multiplier=mult,
            )
            est_usd = None
            if build.get("currentPriceUsd"):
                est_usd = (min_units / 10 ** a.decimals) * build["currentPriceUsd"]
            validate_usd_cap(est_usd, settings.max_proposal_usd,
                             what=f"leverage {principal_amount} {sym} at {mult}x")

            liq, cur = build.get("liquidationPriceUsd"), build.get("currentPriceUsd")
            notes = [
                f"Leverage {mult}x via flash-loan loop: total collateral "
                f"{build['quote']['totalCollateral']} / total debt {build['quote']['totalDebt']} (min units).",
                f"WORST CASE: the entire principal (~{_usd(est_usd)}) can be lost to "
                f"liquidation, plus the liquidation penalty.",
            ]
            if liq and cur:
                notes.append(
                    f"Liquidation if {sym} falls to {_usd(liq)} — "
                    f"{(liq - cur) / cur * 100:+.1f}% from now ({_usd(cur)})."
                )
            else:
                notes.append("Liquidation price could not be computed — treat as high risk and verify manually.")
            if build.get("projected"):
                pj = build["projected"]
                notes.append(
                    f"Projected position LTV {_pct(pj['currentLtv'])} vs liquidation at {_pct(pj['liquidationLtv'])}."
                )
            notes.append(f"DEX price impact of the loop swap: {build['quote']['dexPriceImpact']:.3%}.")

            summary = (
                f"Open a {mult}x leveraged {sym} position on Current Multiply with "
                f"{principal_amount} {sym} (~{_usd(est_usd)}) principal. This is a "
                f"leveraged, liquidatable position — materially riskier than a deposit."
            )
            return provider._finish_proposal(
                user_address=addr, tool="propose_leverage_position",
                params={"asset": sym, "principal": principal_amount, "multiplier": mult},
                build=build, summary=summary, risk_notes=notes,
            )

        @mcp.tool()
        async def submit_signed_transaction(
            proposal_id: Annotated[str, Field(description="The proposal being executed (sarf_...)")],
            signed_tx_bytes_base64: Annotated[str, Field(description="Transaction bytes the wallet signed (must match the proposal's ptb_base64)")],
            signatures: Annotated[list[str], Field(description="Base64 Sui signatures from the user's wallet")],
        ) -> dict[str, Any]:
            """Broadcast a transaction the user already signed in their own wallet.
            The ONLY write path to the chain. Refuses anything that does not
            byte-match a live proposal from this server — it is not a relay."""
            return await provider.execute_submit(proposal_id, signed_tx_bytes_base64, signatures)
