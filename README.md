# SuiFlow

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

`server/suiflow/providers/` is the extension point: Aftermath Finance
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

1. Deploy behind Caddy (see `Caddyfile.example`) so the MCP endpoint is
   `https://your-domain/mcp`.
2. Claude → Settings → Connectors → *Add custom connector* → paste the URL.
   If you set `MCP_AUTH_TOKEN`, use `https://your-domain/mcp?key=<token>`
   or configure the Authorization header where supported.
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

## Development

```bash
cd server && .venv/bin/python -m pytest tests/ -q   # validation suite (offline)
cd txbuilder && npx tsc                             # typecheck
```

Git hooks: `git config core.hooksPath .githooks` (done by setup.sh). Every
commit runs `scripts/check-secrets.sh`; CI re-checks the whole tree.
