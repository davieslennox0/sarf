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
