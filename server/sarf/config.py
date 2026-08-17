"""Configuration and risk parameters.

Everything here is server-side policy: none of it can be overridden by tool
arguments coming from the LLM layer. Values arrive via environment (.env is
loaded by pm2/uvicorn invocation, never committed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


# ---------------------------------------------------------------------------
# Leverage risk cap.
#
# Why 3.0 default / 5.0 absolute: Current Multiply loops correlated pairs
# (mostly LST/SUI) where e-mode liquidation LTVs run ~0.90-0.95. At target
# multiplier N the position LTV is (N-1)/N, so the collateral/debt price
# ratio can fall by 1 - (N-1)/(N*L) before liquidation:
#   N=3, L=0.95  -> ~30% buffer
#   N=5, L=0.95  -> ~16% buffer
#   N=10, L=0.95 -> ~5% buffer (one bad LST depeg event away)
# Historical LST depegs on Sui-like venues have reached a few percent in
# minutes; 3x keeps a wide margin for an LLM-mediated flow where the user may
# not be watching a chart, and 5.0 is the hard ceiling no env var can raise
# (enforced in validation.py, not just here).
# ---------------------------------------------------------------------------
LEVERAGE_ABSOLUTE_MAX = 5.0
LEVERAGE_DEFAULT_MAX = 3.0

# Proposals are quotes against live prices/rates; stale ones must not be
# broadcastable. 10 minutes covers a human read-and-confirm round trip.
PROPOSAL_TTL_DEFAULT = 600

# ---------------------------------------------------------------------------
# Session TTL: 30 min default, 60 min absolute ceiling (code, not tunable).
#
# Why 30: a session covers one propose→review→sign conversation plus some
# slack; the token also rides in the MCP connector URL, so a leaked/pasted
# URL must go stale on its own within the hour. Why not shorter: sub-15-minute
# expiry would interrupt a single leverage discussion mid-flow.
#
# An OAuth connector no longer pays a wallet signature for each renewal — it
# holds a rotating refresh token (auth.py) and swaps it silently. That is what
# stopped Claude disconnecting several times a day. ?key= connectors still have
# no renewal path and still go stale within the hour, which is the point of
# them being pasteable.
# ---------------------------------------------------------------------------
SESSION_TTL_DEFAULT = 1800
SESSION_TTL_ABSOLUTE_MAX = 3600

# The outer bound on a connection that keeps being used. Not extended by
# rotation, revoked wholesale by "End session", and useless for signing.
REFRESH_TTL_DEFAULT = 30 * 24 * 3600
REFRESH_TTL_ABSOLUTE_MAX = 90 * 24 * 3600


# NOTE: every field uses default_factory so a fresh Settings() re-reads the
# environment — required for the production fail-closed startup check to be
# testable (tests construct Settings() under patched env vars).
@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _env("SARF_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("SARF_PORT", "8760")))
    db_path: str = field(default_factory=lambda: _env("SARF_DB_PATH", "./data/sarf.db"))
    proposal_ttl_seconds: int = field(
        default_factory=lambda: int(_env("PROPOSAL_TTL_SECONDS", str(PROPOSAL_TTL_DEFAULT)))
    )

    # Public base URL (Caddy) — used to build the in-chat signer link on every
    # proposal. Empty = no sign_url in responses.
    public_url: str = field(default_factory=lambda: _env("SARF_PUBLIC_URL", "").rstrip("/"))

    # Public MCP connector origin (e.g. https://sarf-mcp.managerx.xyz) — only
    # used to show users their personal connector URL after sign-in.
    mcp_public_url: str = field(default_factory=lambda: _env("SARF_MCP_PUBLIC_URL", "").rstrip("/"))

    # --- Session auth (see auth.py for the model) ---------------------------
    # "production" fails closed: startup refuses without a signing secret and
    # the MCP endpoint refuses unauthenticated calls. Anything else is dev
    # mode, which warns loudly on every unauthenticated request.
    env: str = field(default_factory=lambda: _env("SARF_ENV", "dev").lower())
    session_secret: str = field(default_factory=lambda: _env("SARF_SESSION_SECRET", ""))
    session_ttl_seconds: int = field(
        default_factory=lambda: min(
            int(_env("SESSION_TTL_SECONDS", str(SESSION_TTL_DEFAULT))),
            SESSION_TTL_ABSOLUTE_MAX,
        )
    )
    # How long a connector may keep renewing itself before the wallet has to
    # sign again. Access tokens stay short; this is the outer bound on a
    # connection that is being used, and it is not extended by renewal. 30 days
    # matches what every OAuth connector the user already has does, and the
    # thing it renews cannot sign or move anything (see auth.mint_refresh).
    refresh_ttl_seconds: int = field(
        default_factory=lambda: min(
            int(_env("REFRESH_TTL_SECONDS", str(REFRESH_TTL_DEFAULT))),
            REFRESH_TTL_ABSOLUTE_MAX,
        )
    )

    # How often the TVL/user-count snapshot refreshes. On-chain reads across
    # all tracked users are too slow/rate-limited to do per page load.
    stats_refresh_seconds: int = field(default_factory=lambda: int(_env("STATS_REFRESH_SECONDS", "90")))

    # 0 disables the USD cap (not recommended; see SECURITY.md).
    max_proposal_usd: float = field(default_factory=lambda: float(_env("MAX_PROPOSAL_USD", "250000")))

    leverage_max_multiplier: float = field(default_factory=lambda: min(
        float(_env("LEVERAGE_MAX_MULTIPLIER", str(LEVERAGE_DEFAULT_MAX))),
        LEVERAGE_ABSOLUTE_MAX,
    ))

    # Optional extra narrowing of the per-market asset registry ("USDC,SUI").
    # Empty = every asset the protocol itself lists for the market.
    asset_whitelist: frozenset[str] = field(
        default_factory=lambda: frozenset(
            s.strip().upper() for s in _env("ASSET_WHITELIST", "").split(",") if s.strip()
        )
    )

    # --- X Layer RWA trading (xStocks) --------------------------------------
    # Orders are quotes against live DEX pool state; they go stale faster than
    # a lending proposal because an AMM price moves with every fill. 5 minutes
    # covers review-and-sign without letting a stale price reach the chain.
    order_ttl_seconds: int = field(default_factory=lambda: int(_env("ORDER_TTL_SECONDS", "300")))

    # Per-order USD ceiling, same fail-closed rule as MAX_PROPOSAL_USD.
    # Lower than the lending cap on purpose: xStocks pools on X Layer are
    # ~$200k-750k deep, so a large order is a price-impact event, not just size.
    max_order_usd: float = field(default_factory=lambda: float(_env("MAX_ORDER_USD", "25000")))

    # Slippage tolerance handed to the aggregator, in percent. Hard-capped in
    # validation so a misconfigured env cannot authorize an unbounded fill.
    default_slippage_pct: float = field(
        default_factory=lambda: float(_env("DEFAULT_SLIPPAGE_PCT", "1.0"))
    )

    # Refuse to build an order whose quoted price impact exceeds this. Thin
    # pools make a "market buy" quietly become a donation to arbitrageurs.
    max_price_impact_pct: float = field(
        default_factory=lambda: float(_env("MAX_PRICE_IMPACT_PCT", "5.0"))
    )

    # Optional narrowing of the tradable universe ("AAPLx,SPYx"). Empty = all
    # 40 assets in the on-chain-verified registry.
    rwa_allowlist: frozenset[str] = field(
        default_factory=lambda: frozenset(
            s.strip().upper() for s in _env("RWA_ALLOWLIST", "").split(",") if s.strip()
        )
    )

    # --- Platform fee -------------------------------------------------------
    # Flat fee per swap, charged in the STABLECOIN leg of the trade (USDT/USDC/
    # USDG), so the user is never charged in a volatile asset. The aggregator
    # collects it natively via feePercent + a referrer address, inside the same
    # transaction the user signs — there is no second transfer and no custody.
    #
    # It is expressed in dollars but applied as a percentage, so a small order
    # would otherwise carry an absurd rate: $0.01 on a $0.20 order is 5%. That
    # is handled by max_fee_percent, which caps the RATE rather than refusing
    # the order, so a very small trade pays a fair fraction instead of a cent.
    platform_fee_usd: float = field(
        default_factory=lambda: float(_env("PLATFORM_FEE_USD", "0.01"))
    )
    # Where the fee lands.
    #
    # Left unset it now falls back to the RELAYER — the wallet that pays gas
    # for every in-chat trade — so the fee funds the thing it is charged
    # alongside instead of accumulating somewhere that has to be swept back.
    # See fee_plan() in providers/xlayer_rwa.py, which resolves it: config
    # cannot import the delegation module without a cycle.
    #
    # Note the trade-off this creates, because it is a real one: the relayer
    # is documented as a gas-only wallet precisely so that compromising the
    # server buys an attacker a gas bill and nothing else. Fees landing there
    # give it a balance worth taking. It stays small (a cent a trade) and
    # should be swept, but "gas-only" is now an aspiration rather than a fact.
    #
    # An explicit address still wins, and an unset address with NO relayer
    # configured means no fee is charged at all — it fails to ZERO fee, never
    # to an unowned address.
    platform_fee_address: str = field(
        default_factory=lambda: _env("PLATFORM_FEE_ADDRESS", "").strip().lower()
    )
    # No minimum: any amount that is representable on-chain is accepted. The
    # fee stays proportionate at every size because max_fee_percent caps the
    # rate, so a floor is not needed to keep it fair. Amounts that round to
    # zero base units are still rejected (validation.py) — that is not a small
    # trade, it is a transaction that would move nothing.
    #
    # Set MIN_ORDER_USD above 0 to reinstate a floor.
    min_order_usd: float = field(default_factory=lambda: float(_env("MIN_ORDER_USD", "0")))

    # OKX caps referral commission at 3%; keep our own ceiling well under it so
    # a mis-set fee/tiny order can never quietly take a big bite.
    max_fee_percent: float = field(default_factory=lambda: float(_env("MAX_FEE_PERCENT", "1.0")))

    # --- Session-key delegation (EIP-7702) — see xlayer/delegation.py --------
    # The deployed SarfSessionKey implementation users delegate to. Empty
    # disables in-chat execution entirely: with no delegate there is nothing
    # to grant, so the feature fails closed rather than half-on.
    # v2 (2026-08-12). v1 was 0x30eeC302C6D98253dCcA7d970343dBb95c920D76, which
    # approved the swap router rather than the aggregator's TokenApprove — the
    # router never spends that allowance, so every trade reverted with
    # SwapFailed at the user's expense. Grants signed against v1 cannot be
    # repaired server-side; execute_order refuses them by name.
    delegate_address: str = field(
        default_factory=lambda: _env(
            "SARF_DELEGATE_ADDRESS", "0xaeBc963A2e8c3e42d070f5767Def5Fe430151946"
        ).strip()
    )
    # Gas-only wallet that submits signed swaps. Deliberately NOT any wallet
    # that holds funds — it pays for transactions the session key already
    # authorised, so compromising it buys an attacker nothing but a gas bill.
    relayer_private_key: str = field(
        default_factory=lambda: _env("SARF_RELAYER_PRIVATE_KEY", "").strip()
    )
    # Warn below this. A swap costs ~300k gas at ~0.02 gwei on X Layer, so
    # 0.002 OKB is still hundreds of trades — the threshold is about noticing
    # early, not about running close to empty.
    relayer_min_okb: float = field(
        default_factory=lambda: float(_env("RELAYER_MIN_OKB", "0.002"))
    )
    # Re-key this often even when the grant runs longer. Rotation shortens how
    # long any single key is worth stealing; it cannot extend the grant,
    # because re-keying needs the user's wallet signature again.
    session_key_rotate_seconds: int = field(
        default_factory=lambda: int(_env("SESSION_KEY_ROTATE_SECONDS", str(24 * 3600)))
    )
    # Orders at or below this USD value execute in chat under a live grant.
    # Above it, a fresh passkey assertion is required regardless of the grant's
    # caps — the on-chain cap is the hard ceiling, this is the "are you sure"
    # line the user drew. 0 means every execution needs a passkey.
    delegated_auto_usd: float = field(
        default_factory=lambda: float(_env("DELEGATED_AUTO_USD", "250"))
    )

    # --- Passkey (WebAuthn) — see passkey.py for the threat model ------------
    # The passkey gates EVERY transaction, not just large ones.
    #
    # PASSKEY_STEPUP_USD used to be a threshold — only orders above it needed an
    # assertion, and a $999 order needed nothing. That left the common case
    # ungated while the rare case was gated twice (passkey AND a wallet
    # signature). The passkey is now the gate on all of them, and this value
    # survives only as an escape hatch: above 0 it restores threshold behaviour,
    # which is not the intended configuration.
    passkey_stepup_usd: float = field(
        default_factory=lambda: float(_env("PASSKEY_STEPUP_USD", "0"))
    )
    # Whether an account with no registered passkey can transact at all.
    #
    # Defaults ON, which is safe here only because registration happens during
    # login rather than on a settings page nobody visits: a user without a
    # passkey registers on the way in instead of hitting a wall. Do not turn
    # this on without that property holding — it is the difference between a
    # prompt and a lockout.
    passkey_required: bool = field(
        default_factory=lambda: _env("PASSKEY_REQUIRED", "true").lower() in ("1", "true", "yes")
    )
    # How long one assertion covers, in seconds. This is the "per session" in
    # "the passkey gates transactions per session" — it should track the session
    # grant's own lifetime rather than a single order.
    passkey_session_seconds: int = field(
        default_factory=lambda: int(_env("PASSKEY_SESSION_SECONDS", "3600"))
    )

    # Simple per-client rate limit for the MCP endpoint.
    rate_limit_per_minute: int = field(default_factory=lambda: int(_env("RATE_LIMIT_PER_MINUTE", "60")))

    # Public hostnames Caddy fronts this process with (comma-separated).
    # The MCP transport's DNS-rebinding protection rejects any other Host
    # header; loopback is always allowed in main.py so local dev works
    # without this being set.
    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip().lower() for h in _env("SARF_ALLOWED_HOSTS", "").split(",") if h.strip()
        )
    )

    def __post_init__(self) -> None:
        # Production fails closed: no session-signing secret, no server. A
        # weak/absent secret would make session tokens forgeable, silently
        # reopening the "act as any address" hole this layer exists to close.
        if self.env == "production":
            if len(self.session_secret) < 32:
                raise RuntimeError(
                    "SARF_ENV=production requires SARF_SESSION_SECRET (>= 32 chars; "
                    "e.g. `openssl rand -hex 32`). Refusing to start unauthenticated."
                )
        elif not self.session_secret:
            # Dev convenience: ephemeral secret, so sessions die with the
            # process. Every unauthenticated MCP request also logs a warning.
            import secrets as _secrets

            object.__setattr__(self, "session_secret", _secrets.token_hex(32))


settings = Settings()
