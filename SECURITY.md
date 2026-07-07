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
| Obligation ownership | the cap's **live on-chain owner** must equal the session's verified address at proposal time, re-checked from the builder's view (SQLite is only a cache, never the decision input) |
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

## Auth model — proof of address ownership before any tool call

v1 shipped with identity = claimed Sui address (wallet signature only at the
final signing step). That never exposed funds — unsigned proposals are
unusable, and broadcast requires the owner's signature — but it let anyone
read another address's portfolio through `get_portfolio` and spam proposal
generation (simulation load, audit-log noise) against arbitrary addresses.
That gap is closed; this section replaces the old one entirely.

**Session establishment (the only way in):**

1. The client requests a one-time login nonce for an address
   (`/api/auth/challenge`; single-use, 5-minute TTL, the message text states
   it authorizes no transaction).
2. The user's wallet signs it as a personal message. The sidecar verifies the
   signature with `verifyPersonalMessageSignature` from `@mysten/sui/verify`,
   bound to the claimed address. For **zkLogin** wallets this performs the
   full spec checks — the Groth16 proof binding the OAuth JWT to the address
   seed, on-chain JWK lookup for the provider, and `maxEpoch` freshness
   against the chain. Nothing is hand-rolled; a missing, expired, or
   wrong-address proof fails verification and no session exists.
3. Only then does the server mint its own token:
   `sarf_sess_<128-bit id>.<HMAC-SHA256(secret, id)>` — unforgeable without
   `SARF_SESSION_SECRET`, revocable and expirable via its DB row (both checks
   must pass on every request).

**Session lifetime: 30 minutes** (`SESSION_TTL_SECONDS`, hard-capped at 60 in
code). Long enough for one propose→review→sign conversation; short enough
that a leaked connector URL dies the same hour. Expiry requires signing in
again with the wallet — there is deliberately no refresh or silent renewal.

**Every tool call is bound to the verified session.** The MCP middleware
resolves the token (Bearer header or `?key=`) and binds the proven address to
the request; `get_portfolio` and all `propose_*` tools take **no
`user_address` argument at all** — they act on the session's address, so a
caller (or a prompted-into-it model) cannot name someone else's address. The
same applies to the dashboard's authenticated REST endpoints.
`submit_signed_transaction` keeps its byte-match/TTL/single-use invariants
and additionally refuses proposals that belong to a different account than
the submitting session.

**Production fails closed.** With `SARF_ENV=production` the server refuses to
start without `SARF_SESSION_SECRET` (≥32 chars), and /mcp requests with no
token or a forged one get a transport-level 401 — there is no
unauthenticated fallback. One deliberate nuance: a token that is
**HMAC-authentic but expired** passes the transport and fails at the tool
layer instead, with a labeled in-band error
(`session_expired: … sign in again at …`). Per the MCP spec, a transport 401
is consumed by the client's OAuth machinery (we run no OAuth server) and
reaches the model as an opaque failure, while `result.isError` text is
routed to the model — so the expired case, the only auth failure a
legitimate user routinely hits, is the one place we speak on the channel
the user can actually hear. Nothing executes on an expired session; tools
have no address to act on. Dev mode (`SARF_ENV=dev`) permits anonymous
*endpoint* access for local testing but logs a warning on **every** such
request, and tools still refuse to act without a session address. The former
static `MCP_AUTH_TOKEN` gate is superseded by per-user session tokens and
has been removed.

Funds-safety still does not *depend* on any of this — proposals remain
unsigned and the broadcast path still demands the owner's wallet signature
over exact proposal bytes. The session layer protects read privacy and
prevents resource abuse; the wallet signature remains the authorization to
move funds.

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
