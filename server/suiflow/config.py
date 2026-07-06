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


@dataclass(frozen=True)
class Settings:
    host: str = _env("SUIFLOW_HOST", "127.0.0.1")
    port: int = int(_env("SUIFLOW_PORT", "8760"))
    db_path: str = _env("SUIFLOW_DB_PATH", "./data/suiflow.db")
    txbuilder_url: str = "http://%s:%s" % (
        _env("TXBUILDER_HOST", "127.0.0.1"),
        _env("TXBUILDER_PORT", "8761"),
    )

    proposal_ttl_seconds: int = int(_env("PROPOSAL_TTL_SECONDS", str(PROPOSAL_TTL_DEFAULT)))

    # 0 disables the USD cap (not recommended; see SECURITY.md).
    max_proposal_usd: float = float(_env("MAX_PROPOSAL_USD", "250000"))

    leverage_max_multiplier: float = min(
        float(_env("LEVERAGE_MAX_MULTIPLIER", str(LEVERAGE_DEFAULT_MAX))),
        LEVERAGE_ABSOLUTE_MAX,
    )

    # Optional extra narrowing of the per-market asset registry ("USDC,SUI").
    # Empty = every asset the protocol itself lists for the market.
    asset_whitelist: frozenset[str] = field(
        default_factory=lambda: frozenset(
            s.strip().upper() for s in _env("ASSET_WHITELIST", "").split(",") if s.strip()
        )
    )

    # Simple per-client rate limit for the MCP endpoint.
    rate_limit_per_minute: int = int(_env("RATE_LIMIT_PER_MINUTE", "60"))


settings = Settings()
