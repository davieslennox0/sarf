# SECURITY

## Custody model — non-custodial, explicitly

- **No key material exists anywhere in this system.** No tool accepts a
  private key, mnemonic, or session key; the repo's pre-commit hook and CI
  reject key-shaped content outright.
- The server produces **unsigned** Sui PTB bytes. Signing happens in the
  user's own wallet (extension or zkLogin session). The only write path,
  `submit_signed_transaction`, relays bytes+signatures the wallet produced.
- There is no pooled wallet, no delegated signer, and no autonomous
  execution. Every state change requires a fresh, per-action user signature.
  Scheduled/auto-compounding execution is deliberately **not** implemented;
  adding it is a design-review decision, not a feature toggle.
- Proposals are useless to an attacker: an unsigned PTB for an address they
  don't control cannot be executed, and the on-chain Move layer enforces that
  only the `ObligationOwnerCap` holder can act on a position regardless of
  anything this server does.

## Where the security boundary is

**`server/suiflow/validation.py`, not the LLM prompt.** Tool arguments are
treated as untrusted-client input, because the model can be prompted into
sending anything. Enforced server-side on every call:

| Check | Detail |
|---|---|
| Address/object shape | `0x` + exactly 64 hex, normalized; homoglyphs/short forms rejected |
| Asset whitelist | symbols only, resolved against the protocol's own market config (via sidecar); optional `ASSET_WHITELIST` narrowing; raw Move types never accepted |
| Amount bounds | decimal-string only (JSON numbers rejected), regex-limited charset, > 0, ≤ u64, no exponents/signs/separators, sub-minimal-unit precision rejected rather than rounded |
| Obligation ownership | the cap's **live on-chain owner** must equal `user_address` at proposal time, re-checked from the builder's view (SQLite is only a cache, never the decision input) |
| Leverage cap | `target_multiplier` ≤ `LEVERAGE_MAX_MULTIPLIER` (default 3.0), hard-clamped at 5.0 in code — a misconfigured env cannot raise it |
| USD cap | per-proposal `MAX_PROPOSAL_USD` (default $250k); **fails closed** if the oracle price is unavailable |
| Simulation | every PTB is dry-run before being returned; failed simulations are marked non-executable and cannot be submitted |
| Submit binding | broadcast requires an unexpired stored proposal and **byte-for-byte equality** with its PTB — the server is not an open relay |
| Proposal TTL | default 10 min; expired proposals are refused (prices/rates move) |

Not validated server-side (by design): whether the action is *wise*. The
risk notes (LTV, liquidation prices, worst-case loss) exist so the human can
make that call; the server only refuses what is malformed, unauthorized, or
over its caps.

## Leverage risk cap — the number and why

`LEVERAGE_MAX_MULTIPLIER = 3.0` (env-tunable), `LEVERAGE_ABSOLUTE_MAX = 5.0`
(code, not tunable). At multiplier N the position LTV is (N−1)/N, so with an
e-mode liquidation LTV L≈0.95 the collateral/debt price ratio can drop by
`1 − (N−1)/(N·L)` before liquidation: ~30% at 3x, ~16% at 5x, ~5% at 10x.
LST/SUI pairs are tightly correlated but depeg events of a few percent happen
in minutes; an LLM-mediated flow shouldn't sit one such event from
liquidation, hence 3x default and an absolute ceiling well below the
protocol's own maximum. Every leverage proposal must state the liquidation
price and that the entire principal is at risk.

## Auth model and its assumption

Identity is the Sui address. The referenced zkLogin "session" pattern turned
out to be a frontend wallet-popup flow (no reusable server-side session
code), so v1 ships without per-user server auth. That is an explicit,
documented tradeoff, and it is safe for funds because:

1. proposals are unsigned and unusable without the user's wallet;
2. ownership checks bind proposals to the address's live on-chain state;
3. the broadcast path only accepts wallet-signed bytes matching a proposal.

What per-user auth *would* add: privacy for the audit trail and portfolio
convenience data (which is public on-chain anyway) and per-user rate
fairness. Deployments that want a private endpoint set `MCP_AUTH_TOKEN`
(shared bearer/key gate) today; a challenge–response wallet-signature session
is the planned upgrade path and slots into the middleware in `main.py`.

Also present: per-IP rate limiting, loopback-only sidecar (must never be
reverse-proxied), Caddy allowlist routing only `/mcp` and `/healthz`, request
body size limits, and an append-style SQLite audit log of every proposal and
its outcome.

## Secrets

`.env` is gitignored from the first commit. `scripts/check-secrets.sh` runs
as a pre-commit hook (staged files + content patterns: `suiprivkey1…`, PEM
blocks, `PRIVATE_KEY=`-style assignments) and CI re-scans the tree on every
push, so a hook-less commit still fails the build. RPC endpoints and any
future OAuth/quote-server credentials live only in `.env`.

## Reporting

Found something? Open a private security advisory or contact the maintainer
directly. Please do not file public issues for exploitable problems.
