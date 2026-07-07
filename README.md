# Sarf

Non-custodial MCP server that lets Claude / ChatGPT act as a trading & lending
assistant on **Sui**, starting with **Current Finance** (lending, leveraged
yield). It **builds and simulates** transactions; it never holds keys, never
signs, and never executes anything without the user signing in their own
wallet.

```
User asks Claude to do something
  → Claude calls the relevant propose_* tool
  → Server validates inputs, builds the PTB, dry-runs it, returns
    {proposal_id, human_summary, ptb_base64, gas_estimate, risk_notes}
  → Claude presents this to the user as a confirmation card, does not act further
  → User approves in their own wallet UI — wallet signs ptb_base64
  → Client calls submit_signed_transaction(proposal_id, signed bytes, signatures)
  → Server verifies the bytes match the proposal, broadcasts, returns tx digest
```

## Architecture

```
Claude / ChatGPT
      │  streamable-HTTP MCP  (Caddy, TLS)
      ▼
server/   Python FastAPI + MCP  ← the security boundary
      │     validation.py: address/asset/amount/leverage/ownership/USD caps
      │     SQLite: proposal audit log + obligation-cap index (never funds)
      │  loopback HTTP only
      ▼
txbuilder/  Node sidecar wrapping @current-finance/current-sdk
      │     builds unsigned PTBs, dry-runs, projects LTV/liquidation,
      │     broadcasts user-signed bytes
      ▼
Sui mainnet fullnode + Pyth Hermes
```

Why a Node sidecar instead of pysui: the vendor SDK embeds the Pyth oracle
refresh (VAA fetch + `updatePriceFeeds`), underlying→ctoken conversion, and
the flash-loan leverage PTB assembly. Reimplementing those in Python would be
error-prone and drift from upstream. Tradeoff documented in
`txbuilder/src/context.ts`.

`server/sarf/providers/` is the extension point: Aftermath Finance
(swaps/perps) lands as `providers/aftermath.py` + its own sidecar module,
with no change to existing tools or the validation layer.

## MCP tools

| Tool | Kind | Notes |
|---|---|---|
| `get_portfolio(user_address)` | read | positions, cap IDs, LTVs, liquidation prices |
| `get_market_info(market?, asset?)` | read | live rates, prices, risk params; no auth |
| `propose_enter_market(user_address, asset, amount, market?)` | proposal | creates ObligationOwnerCap |
| `propose_deposit(user_address, obligation_cap_id, asset, amount)` | proposal | |
| `propose_borrow(...)` | proposal | includes post-borrow LTV + liquidation prices |
| `propose_repay(...)` | proposal | |
| `propose_withdraw(...)` | proposal | underlying→ctoken conversion server-side |
| `propose_leverage_position(user_address, collateral_asset, principal_amount, target_multiplier)` | proposal | hard-capped multiplier; states liquidation price & worst case |
| `submit_signed_transaction(proposal_id, signed_tx_bytes_base64, signatures)` | write | only broadcasts byte-matched, unexpired proposals |

Deviation from the original tool sketch, on purpose:
`submit_signed_transaction` also takes `proposal_id` and `signatures`
(Sui broadcasts take signatures separately from bytes, and binding to a
stored proposal prevents the server being used as an open relay — see
SECURITY.md).

Amounts are **decimal strings in human units** (`"12.5"`), validated
strictly server-side; assets are **symbols** resolved against the protocol's
own market config — raw Move types are never accepted from the model.

## Setup

```bash
git clone <this repo> && cd sarf
./scripts/setup.sh          # vendor SDK, deps, venv, hooks, tests
cp .env.example .env        # fill in (already done by setup if missing)
pm2 start ecosystem.config.cjs
```

Environment variables: see `.env.example` (RPC endpoint, ports, risk caps,
optional `MCP_AUTH_TOKEN`, optional leverage quote-server config).

### Enabling leverage

`propose_leverage_position` needs two operator-supplied values that are not
derivable from the SDK repo: `CURRENT_QUOTE_SERVER_URL` (the protocol's quote
backend) and `LEVERAGE_PAIRS` (quote-server pair IDs; format in
`txbuilder/src/leverage.ts`). Until both are set the tool refuses cleanly
("leverage disabled") — it does not guess.

### Adding to Claude (custom connector)

1. Deploy behind Caddy (see `Caddyfile.example`). The MCP endpoint lives on
   its own host: `https://sarf-mcp.managerx.xyz/mcp` (the dashboard/signer
   host `https://sarf.managerx.xyz` does not expose `/mcp`, and vice versa —
   both proxy to the same process, split by path allowlist).
2. Claude → Settings → Connectors → *Add custom connector* → paste
   `https://sarf-mcp.managerx.xyz/mcp`. If you set `MCP_AUTH_TOKEN`, use
   `https://sarf-mcp.managerx.xyz/mcp?key=<token>` or configure the
   Authorization header where supported.
3. Tools appear under the connector; the assistant is instructed (via server
   instructions) to always show `human_summary` and `risk_notes` before the
   user decides.

ChatGPT: same URL via *Apps & Connectors* (developer mode) — the transport is
standard streamable HTTP MCP.

### Signing (wallet side)

This server returns `ptb_base64` (unsigned `TransactionData` bytes). Any Sui
wallet flow that can sign raw transaction bytes works, including zkLogin
wallets (Slush et al.) via the frontend `signTransaction` dapp-kit call; the
resulting `{bytes, signature}` go straight to `submit_signed_transaction`.
There is intentionally no signing capability anywhere in this repo.

## Dashboard & in-chat signer

The React/Vite app in `frontend/` is served by the FastAPI process at
`/dashboard/` (build with `cd frontend && npm run build`). Caddy exposes it
at **https://sarf.managerx.xyz** while the MCP connector endpoint lives at
**https://sarf-mcp.managerx.xyz/mcp** — one deployment, one process, two
hosts with per-audience path allowlists (see `Caddyfile.example`). The
dashboard, signer, and `/api` share the frontend origin, so there is no CORS
surface to open up.

- **Stats page (public)** — total users (a `COUNT(*)` over identities that
  actually connected: wallet sign-ins and MCP tool users) and **TVL supplied
  through Sarf-tracked positions** — deliberately *not* Current Finance's
  protocol-wide TVL, and the UI says so. Nothing is mocked: zeros render as
  zeros with a "last updated" timestamp.
- **Activity page (authenticated)** — the proposal audit trail for the
  signed-in address, with outcomes (awaiting signature / broadcast ✓ /
  simulation failed / expired unsigned / rejected).

### How TVL is computed and refreshed

A background task in the server (interval `STATS_REFRESH_SECONDS`, default
90s) iterates the distinct addresses holding Sarf-tracked obligation caps,
reads each obligation live through the Current Finance SDK, prices deposits
with **Pyth oracle prices at scan time** (never hardcoded), sums supplied
USD, and writes the snapshot to the `stats` table. `/api/stats` serves only
that snapshot — page loads never trigger on-chain scans — and reports the
snapshot's age so freshness is visible instead of faked.

### How the signer is hosted (and why it isn't a Claude artifact)

Every successful `propose_*` response carries a `sign_url`
(`$SARF_PUBLIC_URL/sign?p=<proposal_id>`). Claude presents the proposal and
links the user there. **Checked and confirmed:** Claude Artifacts run under a
CSP that blocks all external network requests from client-side JS, so an
inline artifact could neither fetch the proposal nor reach a wallet/fullnode
— the signer therefore lives on our own domain, same origin as the API.

The `/sign` page: fetches the stored proposal (simulated outcome, gas, risk
notes — the same simulate-and-summarize data, never just raw parameters),
enforces that the connected wallet matches the proposal's address, lets the
user sign **the exact server-built bytes** in their own wallet
(`@mysten/dapp-kit` — includes zkLogin wallets like Slush), and POSTs the
signature to `/api/submit`, which shares every invariant with the MCP
`submit_signed_transaction` tool (byte-match, TTL, single-use).

### Session & ephemeral key lifecycle

- **Wallet sessions**: connecting starts a visible session banner
  ("Signing session active — expires in Xm") with a hard 30-minute cap and an
  **End session** button that disconnects the wallet, revokes the API
  session, and wipes any cached material immediately. Auto-connect is off; a
  signing surface should never silently reconnect. Every action still shows
  its specific proposal and requires an explicit wallet confirmation — there
  is no unlocked-signing mode, by design.
- **Dashboard auth**: sign-in = wallet signs a one-time server nonce (the
  message states it authorizes no transaction); the server verifies the
  signature (zkLogin signatures included) and mints a 24h bearer token held
  in `sessionStorage`.
- **Native zkLogin (config-gated)**: `frontend/src/zklogin.js` implements the
  ephemeral-key lifecycle — created in-browser on login, stored only in
  `sessionStorage` with an explicit expiry, wiped on expiry or End-session,
  never sent anywhere. Activating the full in-page zkLogin flow (OAuth →
  prover → zkLoginSignature) requires `VITE_GOOGLE_CLIENT_ID` and
  `VITE_ZKLOGIN_PROVER_URL` (Mysten's mainnet prover needs enrollment; Enoki
  is the managed path). Until configured the UI never offers it — zkLogin
  users sign through their zkLogin wallet instead, which is the same
  custody model.

## Development

```bash
cd server && .venv/bin/python -m pytest tests/ -q   # validation suite (offline)
cd txbuilder && npx tsc                             # typecheck
cd frontend && npm run dev                          # dashboard w/ /api proxy to :8760
```

Git hooks: `git config core.hooksPath .githooks` (done by setup.sh). Every
commit runs `scripts/check-secrets.sh`; CI re-checks the whole tree.
