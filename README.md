# Sarf — Your X Layer RWA Assistant

Non-custodial MCP server that lets Claude / ChatGPT trade **tokenized stocks
and ETFs (xStocks)** on **X Layer** (OKX's EVM chain, id `196`). It prices,
validates and **builds** transactions; it never holds keys, never signs, and
never executes anything without the user signing in their own wallet.

```
User asks Claude to buy a tokenized stock
  → Claude calls place_order
  → Server resolves the symbol, quotes the DEX, enforces USD + price-impact
    caps, applies passkey step-up, builds an UNSIGNED X Layer transaction
  → Claude shows human_summary + risk_notes + sign_url
  → User signs in their own wallet → the wallet broadcasts → tx hash
  → get_settlement_status confirms it on X Layer
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

40 xStocks, every contract address verified against X Layer RPC
(`eth_getCode` non-empty, `symbol()`/`decimals()` matching) before it entered
`server/sarf/xlayer/xstocks_registry.json`. A wrong address in a trading tool
costs real money, so none is trusted from an API alone.

Quote asset is **USDT** (`USD₮0`, 6 decimals). Deepest pools at time of build:
SPYx ~$757k, QQQx ~$500k, NVDAx ~$475k, IWMx ~$470k, GLDx ~$406k.

## MCP tools

All tools act on **the session's wallet-verified address** — none accepts a
wallet argument, so a caller (or a model prompted into trying) cannot read or
trade against an account they have not proven control of.

| Tool | Kind | Notes |
|---|---|---|
| `get_rwa_list()` | read | the 40 tradable assets, with contracts + explorer links |
| `get_rwa_price(symbol)` | read | live price quoted from X Layer DEX pools |
| `get_portfolio()` | read | holdings + USDT + OKB gas balance, read from chain |
| `place_order(symbol, side, amount, slippage_percent?)` | build | returns an **unsigned** tx + `sign_url`; never executes |
| `get_settlement_status(tx_hash)` | read | distinguishes unknown / pending / confirmed / reverted |
| `get_order_history(limit?)` | read | this wallet's orders, with tx hashes |

## Deployment strategy — and why each choice was made

### The contract: `SarfSessionKey`

**Live at [`0x30eeC302C6D98253dCcA7d970343dBb95c920D76`](https://web3.okx.com/explorer/x-layer/address/0x30eeC302C6D98253dCcA7d970343dBb95c920D76)** on X Layer
(tx `0xf720d8cb…32a7`, block 67378132, 843,942 gas ≈ 0.0000169 OKB).

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

## Development

```bash
cd server && .venv/bin/python -m pytest tests/ -q   # offline: no network, no credentials
```

97 tests cover the EVM/EIP-55 layer (against official vectors), registry
resolution including the CEX-ticker trap, passkey step-up policy, and the
order audit trail.
