"""Portfolio analysis — methodology, and the guardrails that constrain it.

WHAT THIS IS
    Standard portfolio-construction arithmetic applied to on-chain holdings:
    position weights, concentration (HHI / effective position count), the
    single-name vs fund split, sector and theme clustering, and the stablecoin
    buffer. The same measures a CFA charterholder would reach for when sizing
    up a book.

WHAT THIS IS NOT
    Advice. Sarf is not a licensed adviser, this output is not a personalised
    recommendation, and nothing here is regulated financial advice. That is a
    hard product boundary, not a disclaimer bolted on at the end, so it is
    enforced in three places:

      1. Every observation is emitted as FACT + NORM, never as an instruction.
         "NVDAx is 41% of holdings; single-name concentration guidelines
         commonly sit at 15-20%" — the user draws the conclusion. There is no
         code path in this module that can emit "sell", "buy", "reduce",
         "trim" or "rebalance" as a directive; see `_observe`.
      2. `DISCLOSURE` and `MISSING_CONTEXT` ride on every response, because
         the model is what the user actually reads — a line on the website
         does not travel into the chat.
      3. `PRESENTATION_RULES` tells the model the same thing, since it is the
         component that renders the final words.

    No forecasts. Concentration and diversification are properties of a
    portfolio as it stands and can be measured. Where a price is going cannot,
    and this agent does not guess — there is deliberately no momentum, trend
    or target-price computation anywhere in this file.

WHAT IT CANNOT SEE
    On-chain balances, and nothing else. Not income, not the rest of someone's
    net worth, not their goals, time horizon, tax position or risk tolerance —
    the inputs that decide whether any given concentration is reckless or
    completely deliberate. `MISSING_CONTEXT` says so on every response rather
    than letting a confident-sounding read imply a full financial picture.
"""

from __future__ import annotations

from typing import Any

# --- Standing disclosures ----------------------------------------------------

DISCLOSURE = (
    "Informational only. This is portfolio analysis, not personalised investment "
    "advice, and Sarf is not a licensed or registered financial adviser. xStocks "
    "are tokenized SYNTHETIC exposure: they track the underlying share price and "
    "convey no ownership, dividends, or voting rights, and redemption depends on "
    "the issuer (Backed Assets)."
)

MISSING_CONTEXT = (
    "This analysis sees only the tokens held by this wallet on X Layer. It does "
    "not know your income, savings, other accounts, debts, goals, time horizon, "
    "tax position, or risk tolerance — the things that decide whether any allocation "
    "is appropriate for you. Treat the figures as a description of this wallet, "
    "not a verdict on your finances."
)

PRESENTATION_RULES = (
    "Relay `disclosure` and `missing_context` in every reply that uses this tool "
    "— not a paraphrase that drops the 'not advice' and 'synthetic exposure' "
    "points. State each observation as a fact next to the norm it is measured "
    "against and let the user draw the conclusion: say 'NVDAx is 41% of "
    "holdings, above the 15-20% single-name band commonly used as a "
    "concentration limit', never 'you should sell NVDAx'. Do not tell the user "
    "to buy, sell, trim, hold, or rebalance, and do not forecast prices, "
    "returns, or where any asset is headed — that is out of scope for this "
    "agent. If the user asks what to do, give them the trade-offs and let them "
    "choose; you can build any order they ask for with place_order."
)

# --- Norms ------------------------------------------------------------------
# Conventional reference points, stated as such. They are not Sarf's rules and
# not thresholds anyone is obliged to follow; they exist so a number has
# something to be compared against instead of floating free.

SINGLE_NAME_BAND = (15.0, 20.0)   # common single-issuer concentration limit, %
TOP3_CONCENTRATED = 60.0          # top-3 weight above which a book is "narrow", %
SECTOR_BAND = 25.0                # common single-sector guideline, %
THIN_BOOK = 4                     # effective positions below which breadth is low
BUFFER_LOW = 5.0                  # stablecoin buffer considered thin, %

# --- Classification ----------------------------------------------------------
# Fund/single-name and sector for each registry asset. Facts about the
# underlying, not opinions about it. Anything unlisted falls back to
# ("single_name", "Unclassified") and is reported as unclassified rather than
# silently bucketed somewhere convenient.

_ETF = "fund"
_NAME = "single_name"

CLASSIFICATION: dict[str, tuple[str, str]] = {
    # Broad-market and sector funds
    "SPYx":  (_ETF,  "US large-cap (broad)"),
    "QQQx":  (_ETF,  "US large-cap (tech-weighted)"),
    "IWMx":  (_ETF,  "US small-cap"),
    "EWYx":  (_ETF,  "South Korea equity"),
    "XLEx":  (_ETF,  "Energy"),
    "GLDx":  (_ETF,  "Gold (commodity)"),
    # Bucketed with the semis it tracks, not into a sector of its own: a
    # wallet holding NVDAx, AMDx and SOXLx has 80% semiconductor exposure, and
    # splitting the leveraged fund out would report 65% and understate it. The
    # leverage is surfaced separately, via LEVERAGED.
    "SOXLx": (_ETF,  "Semiconductors"),
    # Semiconductors
    "NVDAx": (_NAME, "Semiconductors"),
    "TSMx":  (_NAME, "Semiconductors"),
    "INTCx": (_NAME, "Semiconductors"),
    "AMDx":  (_NAME, "Semiconductors"),
    "AVGOx": (_NAME, "Semiconductors"),
    "MRVLx": (_NAME, "Semiconductors"),
    "MUx":   (_NAME, "Semiconductors"),
    "ASMLx": (_NAME, "Semiconductors"),
    "SNDKx": (_NAME, "Semiconductors"),
    # Software and services
    "MSFTx": (_NAME, "Software & services"),
    "ORCLx": (_NAME, "Software & services"),
    "ADBEx": (_NAME, "Software & services"),
    "CRWDx": (_NAME, "Software & services"),
    "PLTRx": (_NAME, "Software & services"),
    "IBMx":  (_NAME, "Software & services"),
    "MSTRx": (_NAME, "Software & services"),
    # Hardware
    "AAPLx": (_NAME, "Technology hardware"),
    "DELLx": (_NAME, "Technology hardware"),
    "CSCOx": (_NAME, "Technology hardware"),
    # Communication services
    "GOOGLx": (_NAME, "Communication services"),
    "METAx":  (_NAME, "Communication services"),
    "NFLXx":  (_NAME, "Communication services"),
    # Consumer
    "AMZNx": (_NAME, "Consumer discretionary"),
    "TSLAx": (_NAME, "Consumer discretionary"),
    "GMEx":  (_NAME, "Consumer discretionary"),
    # Financials
    "GSx":   (_NAME, "Financials"),
    "COINx": (_NAME, "Financials"),
    "HOODx": (_NAME, "Financials"),
    "CRCLx": (_NAME, "Financials"),
    # Health care
    "LLYx":  (_NAME, "Health care"),
    "HIMSx": (_NAME, "Health care"),
    # Crypto infrastructure
    "BMNRx": (_NAME, "Crypto mining & infrastructure"),
    "IRENx": (_NAME, "Crypto mining & infrastructure"),
}

# Businesses whose revenue or balance sheet is tied to crypto prices. Holding
# several of these alongside crypto is a correlation the sector buckets above
# do not surface, because they sit in three different GICS sectors.
CRYPTO_LINKED = {"COINx", "MSTRx", "HOODx", "CRCLx", "BMNRx", "IRENx"}

# Funds that use leverage. Daily-reset leverage compounds against the holder in
# choppy markets, so the multi-day return is not the multiple of the index
# return; a holder who does not know that is holding something else than they
# think. Stated as a property of the instrument, not as a warning to act on.
LEVERAGED = {"SOXLx": "3x daily leveraged"}


def classify(symbol: str) -> tuple[str, str]:
    return CLASSIFICATION.get(symbol, (_NAME, "Unclassified"))


def _observe(kind: str, observation: str, norm: str | None = None) -> dict[str, str]:
    """One finding: what is true, and what it is measured against.

    Keeping the norm in a separate field is the point — there is no template
    here that can produce an instruction, because the module never writes the
    sentence that joins them. The model does, under PRESENTATION_RULES.
    """
    out = {"type": kind, "observation": observation}
    if norm:
        out["reference_point"] = norm
    return out


def analyze(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Measure a portfolio dict (as returned by the provider) and describe it.

    Pure and side-effect free: no network, no clock, no state. Given the same
    holdings it always says the same thing.
    """
    positions = [p for p in portfolio.get("positions") or [] if p.get("value_usd")]
    unpriced = list(portfolio.get("unpriced_positions") or [])
    equity = float(portfolio.get("positions_value_usd") or 0.0)
    try:
        stable = float(portfolio.get("usdt_balance") or 0)
    except (TypeError, ValueError):
        stable = 0.0
    # OKB carries real value, so it belongs in the wallet total. It is kept out
    # of `equity` on purpose: concentration is measured against the tokenized-
    # stock sleeve, and folding a gas coin in would dilute every weight by
    # whatever OKB happened to be worth that day.
    try:
        okb = float(portfolio.get("okb_value_usd") or 0)
    except (TypeError, ValueError):
        okb = 0.0
    total = equity + stable + okb

    findings: list[dict[str, str]] = []
    breakdown: dict[str, Any] = {}

    if not positions and stable <= 0 and okb <= 0:
        return {
            "holdings_value_usd": 0.0,
            "position_count": 0,
            "findings": [_observe(
                "empty",
                "This wallet holds no tokenized stocks, no USDT and no OKB on X "
                "Layer, so there is nothing to measure yet.",
            )],
            "weights": [],
            "concentration": {},
            "composition": {},
            "disclosure": DISCLOSURE,
            "missing_context": MISSING_CONTEXT,
            "presentation_rules": PRESENTATION_RULES,
        }

    # --- Weights -------------------------------------------------------------
    # Weighted against the equity sleeve, not total: mixing the cash buffer in
    # would flatter every concentration figure by whatever happens to be
    # sitting in USDT that day. The buffer is reported separately below.
    weights = []
    for p in sorted(positions, key=lambda x: -float(x["value_usd"])):
        w = float(p["value_usd"]) / equity * 100.0 if equity > 0 else 0.0
        kind, sector = classify(p["symbol"])
        weights.append({
            "symbol": p["symbol"],
            "name": p.get("name"),
            "value_usd": round(float(p["value_usd"]), 2),
            "weight_percent": round(w, 2),
            "instrument": kind,
            "sector": sector,
        })

    # --- Concentration -------------------------------------------------------
    # HHI over weights; 1/HHI is the "effective number of positions" — how many
    # equally-sized holdings would give the same concentration. Ten positions
    # where one is 90% is not a ten-position portfolio, and this is the
    # standard way to say so in a single number.
    hhi = sum((w["weight_percent"] / 100.0) ** 2 for w in weights)
    effective_n = round(1.0 / hhi, 2) if hhi > 0 else 0.0
    top1 = weights[0] if weights else None
    top3 = round(sum(w["weight_percent"] for w in weights[:3]), 2)

    concentration = {
        "herfindahl_index": round(hhi, 4),
        "effective_positions": effective_n,
        "largest_position": top1["symbol"] if top1 else None,
        "largest_position_percent": top1["weight_percent"] if top1 else None,
        "top_3_percent": top3,
    }

    if top1 and top1["weight_percent"] > SINGLE_NAME_BAND[1]:
        # A broad-market fund at 80% is one *holding*, but it is not one
        # issuer — SPYx is 500 companies. Calling that "single-name
        # concentration" would be measuring the wallet's row count instead of
        # its exposure, so funds get the accurate framing instead.
        if top1["instrument"] == _ETF and top1["sector"].endswith("(broad)"):
            findings.append(_observe(
                "concentration",
                f"{top1['symbol']} is {top1['weight_percent']:.1f}% of the equity "
                f"sleeve ({_usd(top1['value_usd'])} of {_usd(equity)}), held as a "
                "single position.",
                "It is a broad-market fund, so it is diversified across its "
                "constituents internally — the concentration here is in one "
                "instrument and one issuer of the token, not in one company.",
            ))
        else:
            findings.append(_observe(
                "concentration",
                f"{top1['symbol']} is {top1['weight_percent']:.1f}% of the equity "
                f"sleeve ({_usd(top1['value_usd'])} of {_usd(equity)}).",
                f"Single-name concentration limits are commonly set around "
                f"{SINGLE_NAME_BAND[0]:.0f}-{SINGLE_NAME_BAND[1]:.0f}%; above that a "
                f"single issuer drives most of the portfolio's movement.",
            ))
    if len(weights) >= 3 and top3 > TOP3_CONCENTRATED:
        findings.append(_observe(
            "concentration",
            f"The three largest positions are {top3:.1f}% of holdings "
            f"({', '.join(w['symbol'] for w in weights[:3])}).",
            f"Above roughly {TOP3_CONCENTRATED:.0f}% in the top three, portfolio "
            "outcomes are mostly decided by those three.",
        ))
    if weights and effective_n < THIN_BOOK:
        findings.append(_observe(
            "breadth",
            f"{len(weights)} position{'s' if len(weights) != 1 else ''} held, but "
            f"the effective number of positions is {effective_n:g} once sizes are "
            "accounted for.",
            "Effective positions (1/HHI) is what diversification a portfolio "
            "actually has, as opposed to how many tickers it lists.",
        ))

    # --- Composition ---------------------------------------------------------
    by_sector: dict[str, float] = {}
    fund_pct = 0.0
    for w in weights:
        by_sector[w["sector"]] = by_sector.get(w["sector"], 0.0) + w["weight_percent"]
        if w["instrument"] == _ETF:
            fund_pct += w["weight_percent"]

    sectors = sorted(
        ({"sector": s, "weight_percent": round(v, 2)} for s, v in by_sector.items()),
        key=lambda x: -x["weight_percent"],
    )
    breakdown = {
        "by_sector": sectors,
        "fund_percent": round(fund_pct, 2),
        "single_name_percent": round(100.0 - fund_pct, 2) if weights else 0.0,
    }

    for s in sectors:
        # Broad-market funds are single holdings but not single-sector bets;
        # calling SPYx a 100% "US large-cap (broad)" concentration would be
        # measuring the label rather than the exposure.
        if s["sector"].endswith("(broad)"):
            continue
        if s["weight_percent"] > SECTOR_BAND and len(weights) > 1:
            findings.append(_observe(
                "sector",
                f"{s['sector']} is {s['weight_percent']:.1f}% of holdings.",
                f"Single-sector guidelines commonly sit near {SECTOR_BAND:.0f}%; "
                "names in one sector tend to fall together on sector-wide news.",
            ))

    crypto = [w for w in weights if w["symbol"] in CRYPTO_LINKED]
    if crypto:
        pct = sum(w["weight_percent"] for w in crypto)
        if pct > SECTOR_BAND or len(crypto) >= 2:
            findings.append(_observe(
                "correlation",
                f"{pct:.1f}% of holdings ({', '.join(w['symbol'] for w in crypto)}) "
                "are businesses whose earnings or balance sheet track crypto prices.",
                "These sit in different sectors but tend to move together, so "
                "sector buckets understate how correlated they are — and the "
                "wallet holding them is itself denominated in crypto.",
            ))

    lev = [w for w in weights if w["symbol"] in LEVERAGED]
    for w in lev:
        findings.append(_observe(
            "instrument",
            f"{w['symbol']} is a {LEVERAGED[w['symbol']]} fund and is "
            f"{w['weight_percent']:.1f}% of holdings.",
            "Daily-reset leverage compounds, so returns held over more than a "
            "day are not the stated multiple of the index's return over that "
            "period — in choppy markets they can be materially worse.",
        ))

    # --- Buffer and operability ---------------------------------------------
    buffer_pct = round(stable / total * 100.0, 2) if total > 0 else 0.0
    if weights and buffer_pct < BUFFER_LOW:
        findings.append(_observe(
            "liquidity",
            f"USDT is {buffer_pct:.1f}% of the wallet ({_usd(stable)}).",
            "A stablecoin buffer is what lets a position be added to without "
            "first selling something else.",
        ))
    try:
        gas = float(portfolio.get("gas_balance_okb") or 0)
    except (TypeError, ValueError):
        gas = 0.0
    if gas <= 0:
        # "No OKB means you cannot transact" stopped being true once trades
        # could run under a session grant: there the relayer submits and pays,
        # so a wallet with zero gas still trades. Saying otherwise would send
        # someone to buy OKB they do not need.
        if portfolio.get("gas_sponsored"):
            findings.append(_observe(
                "operability",
                "This wallet holds no OKB, but trades placed in chat run under your "
                "session grant, where Sarf pays the gas.",
                "Signing a trade yourself still needs OKB, so a wallet with none is "
                "reliant on the grant staying live.",
            ))
        else:
            findings.append(_observe(
                "operability",
                "This wallet holds no OKB.",
                "Gas on X Layer is paid in OKB; with a zero balance no transaction "
                "can be sent, whatever the holdings are worth.",
            ))

    if unpriced:
        findings.append(_observe(
            "data_quality",
            f"{', '.join(unpriced)} could not be priced from X Layer pools just "
            "now and {} excluded from every figure above.".format(
                "is" if len(unpriced) == 1 else "are"),
            "The percentages describe the priced holdings only, so treat them as "
            "incomplete rather than as a full picture of this wallet.",
        ))

    if not findings:
        findings.append(_observe(
            "balanced",
            f"No single position exceeds {SINGLE_NAME_BAND[1]:.0f}% of the equity "
            f"sleeve and no sector exceeds {SECTOR_BAND:.0f}%; effective positions "
            f"is {effective_n:g}.",
            "Measured against the concentration reference points above, nothing "
            "in this wallet stands out.",
        ))

    return {
        "holdings_value_usd": round(total, 2),
        "equity_value_usd": round(equity, 2),
        "stablecoin_usd": round(stable, 2),
        "okb_value_usd": round(okb, 2),
        "stablecoin_buffer_percent": buffer_pct,
        "position_count": len(weights),
        "weights": weights,
        "concentration": concentration,
        "composition": breakdown,
        "findings": findings,
        "method": (
            "Position weights over the equity sleeve; concentration via the "
            "Herfindahl index and its reciprocal (effective positions); sector "
            "and instrument buckets from a fixed classification of the 40 "
            "registry assets. No forecasting, and no price or return "
            "projections of any kind."
        ),
        "disclosure": DISCLOSURE,
        "missing_context": MISSING_CONTEXT,
        "presentation_rules": PRESENTATION_RULES,
    }


def _usd(x: float) -> str:
    return f"${x:,.2f}"
