# Sarf — Your X Layer RWA Assistant

Non-custodial assistant for trading **tokenized stocks and ETFs (xStocks)** on
**X Layer** (OKX's EVM chain, id `196`). It prices, validates and **builds**
transactions; it never holds keys, never signs, and never executes anything
without the user signing in their own wallet.

Two surfaces over one implementation:

| Surface | Where | For |
|---|---|---|
| MCP connector | `https://mcp.getsarf.xyz/mcp` | Claude, ChatGPT and any MCP host |
| Website | [getsarf.xyz](https://getsarf.xyz) | swap, liquidity, portfolio, receipts |

They are not two products. The REST endpoints the site calls delegate to the
same provider methods and the same `ZapEngine` instance the MCP tools use, so a
position opened in chat is the same row the website edits, and neither can
drift from the other by being fixed in one place only.

```
User asks Claude to buy a tokenized stock
  → Claude calls place_order
  → Server resolves the symbol, quotes the DEX, enforces USD + price-impact
    caps, applies passkey step-up, builds an UNSIGNED X Layer transaction
  → Claude shows human_summary + risk_notes + sign_url
  → User signs in their own wallet → the wallet broadcasts → tx hash
  → get_status confirms it on X Layer
```

> **xStocks are synthetic exposure.** They track a share price. Holding one
> gives you **no** share ownership, **no** dividends and **no** voting rights;
> redemption depends on the issuer (Backed Assets). Sarf repeats this
> disclosure on every priced response, not just here.

## The identifier trap (read this first)

The same underlying has **two different tickers on two different venues**:

| Venue | Form | Example |
|---|---|---|
| On-chain, X Layer (what Sarf trades) | `x` **suffix** | `AAPLx`, `TSLAx`, `SPYx` |
| OKX centralized order book | `X` **prefix** | `XAAPL`, `XTSLA`, `XSPY` |

Searching X Layer for `XAAPL` returns nothing, which reads exactly like
"xStocks aren't deployed here" — they are. Sarf refuses the CEX form rather
than silently translating it, and tells the caller the correct on-chain symbol.

## Asset universe

57 xStocks, every contract address verified against X Layer RPC
(`eth_getCode` non-empty, `symbol()`/`decimals()` matching) before it entered
`server/sarf/xlayer/xstocks_registry.json`. A wrong address in a trading tool
costs real money, so none is trusted from an API alone.

Quote asset is **USDT** (`USD₮0`, 6 decimals). The registry is refreshed against
the aggregator rather than hand-edited: of 639 xStocks listed on X Layer only 44
actually route at $100 of size, and a symbol that cannot be filled is worse than
one that is absent.

## MCP tools

All tools act on **the session's wallet-verified address** — none accepts a
wallet argument, so a caller (or a model prompted into trying) cannot read or
trade against an account they have not proven control of.

**Trading**

| Tool | Kind | Notes |
|---|---|---|
| `get_rwa_list(symbol?)` | read | the tradable assets with contracts + explorer links; with a symbol, that one asset's live DEX price |
| `get_portfolio()` | read | holdings + USDT + OKB, all priced, read from chain |
| `place_order(symbol, side, amount, slippage_percent?)` | build | returns an **unsigned** tx + `sign_url`; never executes |
| `place_basket_order(assets[], total_amount)` | build | one weighted request, N independent orders; a leg that cannot price is reported, the rest still stand |
| `swap(from_symbol, to_symbol, amount, slippage_percent?)` | build | the general case of `place_order` — rotate any asset into any other without routing through USDT |
| `transfer(symbol, to, amount)` | build | send a holding out; always requires a fresh passkey and can never be delegated |
| `execute_order(order_id)` | execute | settles a built order under a live session grant, within its on-chain caps |
| `deposit()` | read | card → USDC on Base → CCTP → X Layer, with the gas for the burn covered |

**Liquidity (zap)** — see [The zap](#the-zap--single-asset-liquidity-with-a-line-for-impermanent-loss)

| Tool | Kind | Notes |
|---|---|---|
| `get_zap_pools()` | read | incentivised RWA pools, their token taxes and the round-trip cost of using them |
| `zap_deposit(asset, amount, il_threshold_bps?, …)` | build | one asset in, both legs paired, an IL line drawn |
| `get_zap_position(position_id)` | read | IL now against the line, what it has earned, what it cost |
| `set_zap_threshold(position_id, …)` | write | move the exit and re-entry lines |
| `zap_exit(position_id)` | build | unwind to USDT in Aave, whatever the IL |
| `zap_reenter(position_id)` | build | back into the pool, entry price reset |
| `zap_close(position_id)` | build | **realises** the position: unwind and return the deposited asset to the wallet |

**Account**

| Tool | Kind | Notes |
|---|---|---|
| `get_status(tx_hash?, history_limit?)` | read | session grant by default; optionally a tx's confirmation state and recent orders |
| `analyze_portfolio()` | read | concentration, diversification, sector and instrument mix — measurement only, not advice |
| `set_risk_params(symbol, stop_loss?, take_profit?)` | write | stop-loss / take-profit levels, flagged when you ask about the asset |
| `get_xpoints()` | read | points computed live from confirmed order history |

Nineteen, grouped. The count grew because the surface did, but the money paths
stay separate from one another on purpose: `transfer` never merges into a
general trade tool, because its passkey requirement is a security property and
not a parameter, and `zap_close` is distinct from `zap_exit` because one ends a
position and the other only parks it.

Discovery still matters more than tidiness. Once several MCP connectors are
enabled, an assistant finds tools by keyword search, and a capability nobody can
discover may as well not exist — on 2026-08-10 an assistant spent an afternoon
insisting Sarf had no `swap`, having read a capped search result as a complete
registry listing.

## The zap — single-asset liquidity with a line for impermanent loss

X Layer pays USDG to liquidity providers in RWA pools. Providing that liquidity
normally means holding both sides and watching impermanent loss yourself. A zap
takes one asset, splits it, pairs it, and draws a line: past your IL threshold
Sarf unwinds to USDT in Aave V3, and when the price normalises it goes back in.

`IL = 1 - 2·√r / (1 + r)`, where `r = P_now / P_entry` — computed here, not
taken from a service.

Three things about this are load-bearing and were learned the expensive way:

- **The paired tokens charge transfer taxes and run a swap-back.** LAIKA sells
  its collected tax into the pair on *some* transfers, which moves the price by
  roughly twice its float and costs far more gas than the light path. Gas limits
  are estimated and then widened 1.6× on those steps; an exact estimate reverted
  two of six steps in a real close, one of them needing 27% more gas than it had
  asked for.
- **The taxed token must be `tokenA` in `addLiquidity`.** Otherwise the swap-back
  syncs the RWA leg into the reserves and it is donated to the pool.
- **Costs are a fixed percentage, so small positions are eaten by them.** A $2
  test position lost 11% over a full exit-and-re-entry-and-close cycle with
  nothing going wrong. The round trip is shown on the pool before you deposit,
  and becomes a warning when it exceeds the band between your two lines — the
  band being exactly what the cycle exists to save.

Every response carries the current IL beside any yield figure. That is a rule
about this feature, not a layout preference: a yield number on its own is the
half of the story that sells.

**What is not automatic.** The watcher detects a breach and queues the exit;
your wallet still signs it. The session key can only call the swap router, not
withdraw LP or Aave positions, so full automation needs a different contract.
The limitation is disclosed on the position rather than papered over.

## Trade receipts — signed, anchored, checkable without us

A transaction proves something happened. It does not prove what was *meant*: a
swap's calldata shows an aggregator call, not the price Sarf quoted, the minimum
it promised, or when. If Sarf and a user disagreed about those, the chain would
not settle it.

So every settled trade gets a receipt binding the terms to the settlement:

- **EIP-712 typed data**, signed by a published key. Anyone can recover the
  signer and see Sarf issued it.
- **Tamper-evident.** Change any field and the digest moves and the signature no
  longer recovers. There is no version that says something else and verifies.
- **Anchored.** The digest is written into an ordinary X Layer transaction, so
  the receipt provably existed no later than that block. Sarf can issue one
  late; it cannot issue one into a block that has passed.

Deliberately EVM-only: `eth_account` for the signature and one self-send
carrying the digest as calldata, about $0.00008 each. No contract to deploy, no
second chain, no external service. Public at `/receipt/{order_id}` and
`/api/receipt/{order_id}` — a receipt nobody can fetch proves nothing to anybody
— and the page shows the domain, types, message, signature and anchor, because
the whole point is that a reader does not have to take our word for any of it.

## Deployment strategy — and why each choice was made

### The contract: `SarfSessionKey`

**Live at [`0xaeBc963A2e8c3e42d070f5767Def5Fe430151946`](https://web3.okx.com/explorer/x-layer/address/0xaeBc963A2e8c3e42d070f5767Def5Fe430151946)** on X Layer (v2, 2026-08-12).

v1 (`0x30eeC302…920D76`) is deployed and **must not be used**: it approved the
swap router rather than the aggregator's `TokenApprove`, which the router never
spends, so every trade reverted with `SwapFailed` at the user's expense. Grants
signed against v1 cannot be repaired server-side, and `execute_order` refuses
them by name. It is left here rather than quietly deleted because the failure is
the useful part: an allowance granted to the wrong one of two plausible spenders
fails at settlement, not at build time.

It is the EIP-7702 delegate a user points their EOA at to authorise a scoped,
expiring trading key — the thing that lets a trade happen inside a chat without
anyone holding the user's wallet key.

**Why EIP-7702 rather than a smart account.** X Layer runs `reth/v1.10.2-xlayer`
with Prague active — verified directly, not assumed: block headers carry
`requestsHash`, and the Prague-only BLS precompile at `0x0b` answers with
`PrecompileError` rather than an empty return. So a plain EOA can delegate
in place. The alternative — ERC-4337 or a Safe — means asking the user to move
their funds into a new account before they can trade, which is a far bigger ask
than signing one authorisation, and it strands anything they leave behind.

**Why a purpose-built contract rather than an existing one.** Kernel, Safe7579
and Biconomy Nexus are all fine, and all absent from chain 196 — probed and
confirmed empty. Deploying one of them means shipping someone else's general
account abstraction, with a module system and an upgrade path, to get a feature
that needs neither. This contract does one thing (swap, within limits, until
expiry) in ~180 lines with no owner and no admin.

**Why CREATE2.** Deployed through the canonical deterministic deployer
(`0x4e59b448…4956C`, verified present) with salt `0x…5361726601`, so the address
is a pure function of the salt and the init code. Anyone can recompute it from
this repo and confirm the address holds what it claims. A nonce-based deploy
would ask them to take our word for it.

**Why the deployer has no power.** No owner, no admin, no upgrade path, no
pause. The wallet that paid the gas has exactly the same authority over the
contract as any other address: none. That matters for the custody claim below —
there is no privileged party who could change the rules on a grant that is
already live.

**Why post-conditions instead of validating calldata.** `executeSwap` does not
try to understand what the router is being asked to do. Aggregator calldata is
opaque, multi-hop and version-dependent, and a field-by-field validator fails
*open* the first time OKX ships a router upgrade. So the contract measures
instead: snapshot both token balances, make the call, then require that no more
than `sellAmount` left and at least `minBuyAmount` arrived. Whatever the
calldata contained, that is what it is permitted to have done. The allowance is
exact and zeroed in the same transaction, and no value is ever sent, so OKB is
untouchable.

The practical consequence, stated plainly: **a stolen session key cannot move
funds out.** The worst it can do is trade allowed tokens, at prices bounded by
`minBuyAmount`, under per-trade and per-day caps, until the grant expires.

**Why it is still non-custodial.** The user's wallet key never leaves their
wallet. They sign the 7702 authorisation and the grant themselves. `revoke()` is
gated on a self-call, which under 7702 *is* a signature from their own wallet —
so revocation needs nothing from Sarf and cannot be withheld. Sarf holds a
session key with bounded authority; it does not hold keys or funds.

### The relayer: a gas-only wallet, deliberately

`executeSwap` is callable by anyone — the session signature is the authority,
not the sender — so a relayer submits the transaction and pays the OKB.

That relayer is a **dedicated wallet holding only gas**, and specifically *not*
the payout wallet that moves real USDT0/USDG/USDC. Reusing a funded wallet would
mean a compromise of the Sarf server exposes a wallet holding money, in exchange
for saving one funding transfer. Because a compromised relayer can only submit
swaps the session key already authorised, keeping it gas-only means compromising
it buys an attacker a gas bill and nothing else.

Sizing: a swap is ~300k gas at X Layer's ~0.02 gwei, so **0.01 OKB is roughly
1,600 trades**. `RELAYER_MIN_OKB` warns well before empty.

### Rotation and expiry

Two independent clocks, on purpose:

- **The grant** expires when the user said it should, capped at 30 days in the
  contract regardless of what the UI asks for.
- **The key** rotates every 24h (`SESSION_KEY_ROTATE_SECONDS`) even inside a
  longer grant, so the window in which any single key is worth stealing stays
  short. Re-keying requires the user's wallet signature again — rotation can
  only shrink exposure, never quietly extend what they agreed to.

Session private keys are sealed with AES-GCM under a key HKDF-derived from
`SARF_SESSION_SECRET` with its own info string, so a stolen database file is not
a set of usable keys, and the key that encrypts session tokens is not the key
that encrypts signing material.

### Where limits are enforced

**In Solidity, not in Python.** `server/sarf/xlayer/delegation.py` records caps
so they can be displayed, and enforces none of them. A cap checked in the server
process is a cap an attacker who reaches that process can skip. The module's
docstrings say so explicitly, because the natural instinct of the next person to
touch that file is to add a "safety check" there and believe it is doing work.

### Rendering in chat: MCP Apps

Tools return content blocks, and the host decides what to display. Sarf emits a
PNG order card as `ImageContent` and hosts are not obliged to render it — in
practice they did not, so the card arrived as JSON for the model to paraphrase,
losing the two lines the card existed to protect: the fee and the
synthetic-exposure disclosure.

The supported path is **MCP Apps**. `get_portfolio`, `analyze_portfolio` and
`place_order` declare `_meta.ui.resourceUri` pointing at `ui://sarf/*` resources
served as `text/html;profile=mcp-app`; the host renders them in a sandboxed
iframe and pushes the tool output in via `ui/notifications/tool-result`. Widgets
live in `server/sarf/xlayer/widget.py`, use the site's palette, and write every
value through `textContent` — asset names come from an on-chain `name()` call,
so a widget that interpolated them into markup would be an injection hole in a
surface that also shows balances.

A monospace text card still ships on every order, because a host that does not
implement MCP Apps ignores `_meta` entirely and would otherwise be back to
paraphrase. Nothing in either card is load-bearing: every fact is in the JSON.

## Where the security boundary is

`server/sarf/validation.py` + `server/sarf/xlayer/evm.py`, not the LLM prompt.

| Check | Detail |
|---|---|
| Address shape | 20-byte EVM address; a **failing EIP-55 checksum is rejected**, since that is the one cheap signal a paste was corrupted |
| Symbol resolution | symbols only, resolved against the on-chain-verified registry; raw contract addresses are never accepted from the model |
| Amount bounds | decimal-string only (JSON numbers rejected), no exponents/signs/separators, sub-minimal-unit precision rejected rather than rounded |
| Balance precheck | orders the wallet cannot fund are refused before a quote is spent |
| USD cap | per-order `MAX_ORDER_USD` (default $25k); **fails closed** if the order cannot be priced |
| Price impact | refuses above `MAX_PRICE_IMPACT_PCT` (default 5%) — these pools are ~$200k–750k deep, so size *is* a risk |
| Slippage | caller-supplied tolerance hard-bounded to 0.05–5% regardless of config |
| Passkey step-up | orders over `PASSKEY_STEPUP_USD` need a fresh WebAuthn assertion; an unpriceable order fails closed |
| Chain guard | server refuses to start in production if `XLAYER_RPC_URL` is not chain 196 |
| Order binding | a tx hash is only recorded against an unexpired order the session owns |

Not validated: whether a trade is *wise*. Risk notes exist so the human decides.

## Passkeys — what they are and are not for

A passkey is **not** a second signer; the wallet signature is what authorizes
funds. It closes two different gaps:

1. **Session binding** — a session token is a bearer credential riding in an
   MCP connector. Stolen alone it is inert if a registered passkey must be present.
2. **Step-up on size** — above a USD threshold an order needs a fresh
   assertion, so a compromised session cannot push a large order past a
   click-fatigued user.

Deliberately **not** per-action: the wallet already prompts on every trade, and
a second biometric on every small order trains reflexive approval, which costs
more security than it buys. Verification is delegated to `py_webauthn`.

**Required at sign-up.** Every signed-in account has one; the prompt's only
alternative is to stop being signed in. Signed out, the markets stay readable,
so a device that cannot register a passkey still has a site to look at — but no
account trades without one. Passkeys are bound to the domain they were made on,
so the move to getsarf.xyz invalidated every passkey created on the old host;
they are tagged with their origin rather than deleted, and only the current
domain's count as registered.

## Setup

```bash
./scripts/setup.sh
cp .env.example .env
pm2 start ecosystem.config.cjs
```

Key environment: `XLAYER_RPC_URL` (must be chain 196), `SARF_ENV` /
`SARF_SESSION_SECRET`, `MAX_ORDER_USD`, `MAX_PRICE_IMPACT_PCT`,
`PASSKEY_STEPUP_USD`. Quotes need either OKX API credentials
(`OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE`) or the locally
installed `onchainos` CLI; with neither, priced calls fail closed rather than
invent a number.

There is no Node sidecar — X Layer is reached over plain JSON-RPC and the
aggregator over HTTP. (The Sui/Current Finance build needed one; it is retired.)

### Building the frontend never takes the site down

The deploy box has 961 MB of RAM, most of it already spoken for, and this
dependency tree is large (Privy pulls in WalletConnect, viem and a wallet
graph). A build takes five to thirteen minutes and has died of a V8
out-of-memory abort more than once.

The heap size has to be chosen, not maximised: `--max-old-space-size=3072` is
killed by the kernel (a silent `exit 137` with no error text) and 700 is too
small for V8 to finish. 1800 works, which is what `VITE_BUILD_HEAP_MB` defaults
to. Run `/root/free-ram-for-sarf-build.sh` first on a loaded box and
`/root/restore-services.sh` after.

That failure is destructive rather than inconvenient, because `vite build`
empties its output directory *before* it starts: a build that dies half-way
would leave the server with nothing to serve and the site returning 500 until
one finally completed.

So the build never writes to the live bundle. It builds into `dist-next` and
swaps only once a complete tree with an `index.html` exists:

```bash
scripts/build-frontend-local.sh                # build, swap, restart
scripts/build-frontend-local.sh --no-restart   # asset-only change
```

A failed or interrupted build therefore changes nothing at all, and the bundle
it replaced is kept at `frontend/dist.previous` to roll back to.

**One build-time variable matters.** `VITE_PRIVY_APP_ID` (in `frontend/.env`)
is compiled into the bundle, and building without it produces a *different
application* — Privy disabled, no Google sign-in, no embedded wallet — with no
runtime error to say so. It is a public client identifier that ships in the
bundle regardless, so it is not a secret; it is still the one value worth
checking before a deploy.

## Development

```bash
cd server && .venv/bin/python -m pytest tests/ -q   # offline: no network, no credentials
```

304 tests, offline and credential-free. They cover the EVM/EIP-55 layer
(against official vectors), registry resolution including the CEX-ticker trap,
passkey step-up policy, the order audit trail, the zap's IL maths and the full
enter → exit → re-enter → close cycle against a fake chain that mines what the
test signs, receipt signing and tamper-detection, and the relayer nonce lock.

Two habits worth keeping when adding to them: a test for a concurrency fix
should be shown to fail with the fix removed, or it is testing nothing; and a
test that pins the shape of a real API response catches the class of bug where
a field is read from the wrong place and silently falls back — a live receipt
went out reading `paid: 1000000` for exactly that reason.
