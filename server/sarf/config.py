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
# URL must go stale on its own within the hour. Why not shorter: minting a
# new token costs a wallet signature (deliberately — no silent renewal), and
# sub-15-minute expiry would interrupt a single leverage discussion mid-flow.
# ---------------------------------------------------------------------------
SESSION_TTL_DEFAULT = 1800
SESSION_TTL_ABSOLUTE_MAX = 3600


# NOTE: every field uses default_factory so a fresh Settings() re-reads the
# environment — required for the production fail-closed startup check to be
# testable (tests construct Settings() under patched env vars).
@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _env("SARF_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("SARF_PORT", "8760")))
    db_path: str = field(default_factory=lambda: _env("SARF_DB_PATH", "./data/sarf.db"))
    txbuilder_url: str = field(default_factory=lambda: "http://%s:%s" % (
        _env("TXBUILDER_HOST", "127.0.0.1"),
        _env("TXBUILDER_PORT", "8761"),
    ))

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
