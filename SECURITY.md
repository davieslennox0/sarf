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

**`server/sarf/validation.py`, not the LLM prompt.** Tool arguments are
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

## Auth model

Identity is the Sui address; funds-safety never depends on server auth
because:

1. proposals are unsigned and unusable without the user's wallet;
2. ownership checks bind proposals to the address's live on-chain state;
3. the broadcast path only accepts wallet-signed bytes matching a proposal.

On top of that, the dashboard implements **challenge–response wallet
authentication**: the server issues a one-time nonce, the user's wallet signs
it as a personal message (the text states it authorizes no transaction), and
the sidecar verifies the signature — including zkLogin signatures via
on-chain JWK lookup — before a 24h bearer session is minted. Sessions gate
only the private view of the audit trail; the MCP endpoint can additionally
be gated with `MCP_AUTH_TOKEN` for private deployments. Login nonces are
single-use with a 5-minute TTL.

## In-chat signer & ephemeral keys

The signer page (`/sign?p=<proposal_id>`) changes UX, not custody:

- It renders the **simulated** outcome (summary, gas, LTV/liquidation risk
  notes) from the stored proposal — never just raw parameters — and refuses
  expired or already-consumed proposals.
- It verifies the connected wallet matches the proposal's address before
  offering to sign, and the wallet itself shows a second review surface.
- Signing happens in the wallet (`@mysten/dapp-kit`, zkLogin wallets
  included); the page then POSTs to `/api/submit`, which is the same
  `execute_submit()` code path as the MCP tool — byte-match against the
  stored PTB, TTL, single-use, audit log.
- Proposal IDs are 128-bit random capabilities: knowing one lets you *view*
  that proposal (needed for the signer link to work from chat); executing it
  still requires the owner's wallet signature over the exact bytes.
- It is self-hosted (not a Claude artifact) because the Artifacts sandbox
  CSP blocks all external network calls — verified before building.

Ephemeral key rules (native zkLogin module, config-gated): generated
in-browser, stored only in `sessionStorage` with an explicit expiry, wiped on
expiry and by the always-visible **End session** control, never sent to any
server, never in localStorage or any database. There is **no** "remember my
key" or unlocked-signing mode: every action requires the user to see that
specific proposal and confirm it in their wallet, session or no session.

Also present: per-IP rate limiting, loopback-only sidecar (must never be
reverse-proxied), Caddy allowlist routing only `/mcp`, `/healthz`, `/api`,
and the dashboard, request body size limits, and an append-style SQLite audit
log of every proposal and its outcome.

## Secrets

`.env` is gitignored from the first commit. `scripts/check-secrets.sh` runs
as a pre-commit hook (staged files + content patterns: `suiprivkey1…`, PEM
blocks, `PRIVATE_KEY=`-style assignments) and CI re-scans the tree on every
push, so a hook-less commit still fails the build. RPC endpoints and any
future OAuth/quote-server credentials live only in `.env`.

## Reporting

Found something? Open a private security advisory or contact the maintainer
directly. Please do not file public issues for exploitable problems.
