"""Server-rendered order card for the chat surface.

Why this exists: an MCP tool result is JSON, which the SDK hands to the client
as TextContent. The model then paraphrases it in its own prose style — so we
control the *content* of an order but not how it is *presented*, and the two
most important lines (the fee and the synthetic-exposure disclosure) end up
however the model felt like phrasing them.

MCP does support ImageContent, so we render the card ourselves and return it
alongside the text. The image is presentation only: every fact on it is also
in the JSON, so a client that cannot display images loses nothing.

...except the presentation, which was the whole point. Verified end to end:
the server does emit `image/png` in the tool result content, but not every
chat host displays image blocks that come back from a tool — and we cannot
make one that doesn't. So `render_order_card_text` renders the same card in
monospace, which travels as text and therefore renders everywhere. The tool
asks the model to print it verbatim; a fenced block is the one thing a model
reliably reproduces without rewording it, which is how the fee line and the
disclosure survive in the words we wrote them in.

Both are shipped on every order: the image where it works, the text card
always. Design tokens are the website's, so a card in chat and the signer
page it links to are visibly the same product.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# Website tokens (styles.css). Stainless on black — there is no gold in this
# product, and that has to hold in the rendered card too: this is the one
# surface a user sees as a flat image, with no stylesheet to fix it afterwards.
BG = (10, 10, 11)
PANEL = (19, 19, 21)
PANEL_2 = (26, 27, 29)
LINE = (42, 43, 46)
ACCENT = (244, 246, 249)
PAPER = (211, 216, 222)
PAPER_DIM = (139, 146, 154)
GREEN = (107, 158, 125)
RED = (194, 99, 90)

_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_MONO_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

W = 900
PAD = 40


def _f(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_MONO_BOLD if bold else _MONO, size)


def _text(d: ImageDraw.ImageDraw, xy, s: str, font, fill, anchor=None) -> None:
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _wrap(s: str, width: int) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if len(trial) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# --- Text card ---------------------------------------------------------------
# 66 columns: wide enough for "0.31809375 AAPLx" next to its label, narrow
# enough not to wrap in a chat column or on a phone. Box-drawing characters
# rather than ASCII pipes because every surface that shows a monospace block
# renders them, and they make the panel read as one object.

TW = 66


def _row(left: str, right: str = "") -> str:
    """One bordered line, padded to TW. Truncates rather than wrapping: a
    broken border reads as a rendering bug, which undermines the card."""
    space = TW - 4 - len(right)
    if len(left) > space:
        left = left[: max(0, space - 1)] + "…"
    return f"│ {left.ljust(space)}{right} │"


def _rule(l: str = "├", r: str = "┤") -> str:
    return f"{l}{'─' * (TW - 2)}{r}"


def render_order_card_text(o: dict[str, Any]) -> str:
    """-> the order card as a fenced monospace block, or "" if it cannot be
    built. Same facts as the PNG and the JSON; this is the copy that renders
    on every host."""
    try:
        return _render_text(o)
    except Exception:  # pragma: no cover - cosmetic path
        return ""


def _render_text(o: dict[str, Any]) -> str:
    side = str(o.get("side", "")).upper()
    symbol = str(o.get("symbol", ""))
    name = (o.get("name") or "").replace(" xStock", "")
    fee = o.get("platform_fee") or {}
    charged = bool(fee.get("charged"))
    usd = o.get("estimated_usd")

    lines = [
        f"┌{'─' * (TW - 2)}┐",
        _row("SARF · X LAYER RWA", "REVIEW & SIGN"),
        _rule(),
        _row(f"{side} {symbol}" + (f" — {name}" if name else "")),
        _row(""),
        _row("You pay", str(o.get("spending") or "—")),
        _row("You receive (est.)", str(o.get("receiving_estimated") or "—")),
        _row("Minimum received", str(o.get("minimum_received") or "—")),
        _row(""),
        _row("Order value", f"${float(usd):,.2f}" if usd is not None else "—"),
        # Gas sponsorship in place of the platform-fee row. The fee is still
        # computed, still returned on the tool response, and still disclosed —
        # it is just not what a user needs in front of them at the moment of
        # signing. What leaves their wallet does: and for gas, nothing does.
        _row("Gas", "paid from your OKB balance"
             if o.get("gas_sponsored") is False else "sponsored by Sarf"),
        _row("Network", f"X Layer · {o.get('chain_id', 196)}"),
        _rule(),
        _row("READ BEFORE SIGNING"),
    ]
    for n in [n for n in (o.get("risk_notes") or []) if n][:4]:
        # Three lines, and an ellipsis if a note runs past them. Cutting a
        # disclosure off mid-clause reads as a rendering fault and quietly
        # drops the half that matters; the full text is in risk_notes either
        # way, so the truncation has to be visible.
        wrapped = _wrap(n, TW - 6)
        shown, clipped = wrapped[:3], len(wrapped) > 3
        if clipped:
            shown[-1] = shown[-1] + " …"
        for i, seg in enumerate(shown):
            lines.append(_row(("• " if i == 0 else "  ") + seg))
    # The closing line has to agree with can_execute. It was hard-coded to
    # "Sarf holds no keys and cannot execute it" even on orders inside a live
    # session grant, so the card flatly contradicted the same payload's
    # can_execute flag — and an assistant reading both is right to trust the
    # card, which is what happened: it refused to execute an order it could
    # have executed. Both statements are true in their own case; neither is
    # true in both.
    if o.get("can_execute"):
        lines += [
            _rule(),
            _row("UNSIGNED — within your session grant."),
            _row("Approve here and Sarf submits it; caps are on-chain."),
            f"└{'─' * (TW - 2)}┘",
        ]
    else:
        lines += [
            _rule(),
            _row("UNSIGNED — you sign this in your own wallet."),
            _row("Sarf holds no keys and cannot execute it."),
            f"└{'─' * (TW - 2)}┘",
        ]
    return "```\n" + "\n".join(lines) + "\n```"


# Logo fetch for the PNG card. Cached in-process: the same forty images recur
# constantly, they are ~1.3KB each, and a card render must never wait on the
# network twice for the same asset.
_LOGO_CACHE: dict[str, "Image.Image | None"] = {}


def _logo(url: str, size: int) -> "Image.Image | None":
    """Fetch and square a token logo, or None. Never raises.

    The HTML widget layers a real image over a generated monogram so a failed
    load degrades to a filled square. The PNG needs the same property, and it
    gets it by returning None here and letting the caller draw the monogram.
    """
    if not url:
        return None
    if url in _LOGO_CACHE:
        cached = _LOGO_CACHE[url]
        return cached.resize((size, size)) if cached else None
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=4) as r:
            raw = r.read(512_000)
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        _LOGO_CACHE[url] = im
        return im.resize((size, size))
    except Exception:
        _LOGO_CACHE[url] = None
        return None


_DATA_URI_CACHE: dict[str, str] = {}

# On-disk copy of every logo ever fetched successfully.
#
# OKX's CDN is the only source that carries X Layer xStock icons — DexScreener
# does not index these tokens, Trustwallet has no X Layer, CoinGecko refuses the
# request — so "use another source" is not available as a fallback. What is
# available is not needing the source twice: once an icon has been fetched it is
# kept, and an outage upstream then costs nothing. Icons are immutable per
# contract address, so a stale copy is simply the correct copy.
_LOGO_DIR = Path(
    os.environ.get("SARF_LOGO_CACHE_DIR")
    or Path(__file__).resolve().parents[2] / "data" / "logos"
)


def _cache_path(url: str, size: int) -> Path:
    return _LOGO_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}-{size}.png"


def _read_cached(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except Exception:
        return b""


def _write_cached(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic, so a crash mid-write cannot leave a torn PNG
    except Exception:  # pragma: no cover - cosmetic path
        pass


def logo_data_uri(url: str, size: int = 72) -> str:
    """A token logo as a self-contained `data:` PNG, or "" if unavailable.

    The widget runs in the host's sandboxed iframe, and its content policy does
    not allow images from static.oklink.com — the load simply fails and every
    asset falls back to its monogram, which is why a bought position showed up
    as a letter in a coloured box. An inlined image is part of the document
    rather than a request, so there is no host to be allowed or blocked.

    72px, not the mark's own 38px: the card is rendered at whatever pixel
    density the reader's screen has, and a 1x icon on a 2x display is visibly
    soft. The whole map is ~145KB, paid once per session rather than once per
    message, which is what buying the extra resolution costs.
    """
    if not url:
        return ""
    key = f"{url}@{size}"
    if key in _DATA_URI_CACHE:
        return _DATA_URI_CACHE[key]
    path = _cache_path(url, size)
    data = b""
    try:
        im = _logo(url, size)
        if im is not None:
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            _write_cached(path, data)
    except Exception:  # pragma: no cover - cosmetic path
        data = b""
    if not data:
        # Upstream is unreachable or has changed. An icon we already hold is
        # better than a monogram, and better than a card that waits.
        data = _read_cached(path)
    if not data:
        # Deliberately not memoised: a miss is a condition upstream, not a fact
        # about the asset, so the next read gets to try again.
        return ""
    out = "data:image/png;base64," + base64.b64encode(data).decode()
    _DATA_URI_CACHE[key] = out
    return out


def _monogram(d: "ImageDraw.ImageDraw", box, symbol: str, size: int) -> None:
    """The fallback mark: two letters on a shade derived from the ticker, so an
    asset keeps the same tile here as in the HTML card.

    A lightness, not a hue — see markBg in the site's Home.jsx. Rotating a
    saturated hue off the hash meant a card could open with a gold tile on it,
    which is the one colour this product no longer has.
    """
    import colorsys
    base = (symbol or "?").rstrip("x").split(" ")[0]
    h = 0
    for ch in base:
        h = (h * 31 + ord(ch)) % 360
    r, g, b = colorsys.hls_to_rgb(214 / 360.0, 0.21 + (h % 17) / 100.0, 0.075)
    fill = (int(r * 255), int(g * 255), int(b * 255))
    d.rounded_rectangle(box, radius=max(4, size // 5), fill=fill)
    d.text(
        (box[0] + size / 2, box[1] + size / 2), base[:2].upper(),
        font=_f(max(10, size // 2 - 2), True), fill=(255, 255, 255), anchor="mm",
    )


def render_order_card(order: dict[str, Any]) -> str:
    """-> base64 PNG of the order card. Never raises: presentation must not
    be able to break an order the user is entitled to see."""
    try:
        return _render(order)
    except Exception:  # pragma: no cover - cosmetic path
        return ""


def _fit_size(value: str, *, big: int, small: int, threshold: int) -> int:
    """Step a value down one size rather than truncating it.

    An amount clipped to fit is worse than a smaller amount: the digits are
    what the card exists to show. Only two sizes, so the layout stays
    predictable — a recipient address takes the small one, "0.319 AAPLx" the
    large one.
    """
    return small if len(value) > threshold else big


def _cell(d: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str,
          *, anchor_right: bool = False, colour=PAPER) -> None:
    """A label above a value — the only repeated unit in the layout."""
    a = "ra" if anchor_right else "la"
    _text(d, (x, y), label, _f(10), PAPER_DIM, anchor=a)
    size = _fit_size(value, big=19, small=13, threshold=20)
    _text(d, (x, y + 22), value, _f(size, True), colour, anchor=a)


# The card's fixed skeleton. Every band is a constant so the height is derived
# from the layout rather than guessed at — the previous version computed it
# from a separate estimate, and the risk notes printed over the footer the
# first time a disclosure ran long.
_HEAD_RULE = 76
_IDENT_TOP = 104
_PANEL_TOP = 192
_PANEL_H = 96
_FACTS_TOP = 320
_TAIL_TOP = 384        # disclosure line
_FOOTER_H = 74


def _render(o: dict[str, Any]) -> str:
    """Four bands: who/what, the two amounts, the facts, the state.

    Deliberately shorter than what it replaced. The old card carried a
    READ BEFORE SIGNING block of up to eight wrapped lines, which made the
    image tall enough that the amounts — the reason anyone looks at it —
    competed with standing boilerplate nobody reads twice. The risk notes
    still travel on the tool response and the model relays them beside the
    card; what stays here is the one disclosure that must never be optional,
    plus a warning only when THIS order has something specific wrong with it.
    """
    side = str(o.get("side", "")).upper()
    symbol = str(o.get("symbol", "") or "")
    name = (o.get("name") or "").replace(" xStock", "")
    is_transfer = side == "TRANSFER"
    settled = bool(o.get("tx_hash"))
    usd = o.get("estimated_usd")

    impact = o.get("price_impact_percent")
    try:
        impact_f = float(impact) if impact is not None else None
    except (TypeError, ValueError):
        impact_f = None
    warn = (
        f"Price impact {impact_f:.2f}% — this order moves the pool. "
        "A smaller trade fills closer to quote."
        if (impact_f is not None and impact_f >= 1.0 and not settled) else ""
    )

    h = _TAIL_TOP + (30 if warn else 0) + _FOOTER_H
    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)

    # ---- header -----------------------------------------------------------
    _text(d, (PAD, 30), "SARF", _f(22, True), PAPER)
    _text(d, (PAD + 72, 36), "/  X LAYER RWA", _f(13), ACCENT)
    tag = "SETTLED" if settled else ("REVIEW & SEND" if is_transfer else "REVIEW & SIGN")
    _text(d, (W - PAD, 36), tag, _f(12), GREEN if settled else PAPER_DIM, anchor="ra")
    d.line([(PAD, _HEAD_RULE), (W - PAD, _HEAD_RULE)], fill=LINE, width=1)

    # ---- identity ---------------------------------------------------------
    MARK = 52
    mark_box = (PAD, _IDENT_TOP, PAD + MARK, _IDENT_TOP + MARK)
    logo = _logo(str(o.get("logo_url") or ""), MARK)
    if logo is not None:
        d.rounded_rectangle(mark_box, radius=11, fill=(255, 255, 255))
        img.paste(logo, (PAD, _IDENT_TOP), logo)
    else:
        _monogram(d, mark_box, symbol, MARK)

    tx = PAD + MARK + 18
    _text(d, (tx, _IDENT_TOP - 2), f"{side} {symbol}"[:34], _f(30, True), ACCENT)
    if name:
        _text(d, (tx, _IDENT_TOP + 38), name[:46], _f(14), PAPER_DIM)

    # The order's value, right-aligned against the ticker. It is the number
    # people check first, so it gets the opposite corner rather than a cell in
    # a strip of three.
    _text(d, (W - PAD, _IDENT_TOP - 2),
          f"${float(usd):,.2f}" if usd is not None else "—",
          _f(28, True), PAPER, anchor="ra")
    _text(d, (W - PAD, _IDENT_TOP + 38), "ORDER VALUE", _f(10), PAPER_DIM, anchor="ra")

    # ---- the two amounts --------------------------------------------------
    d.rounded_rectangle(
        [PAD, _PANEL_TOP, W - PAD, _PANEL_TOP + _PANEL_H],
        radius=10, fill=PANEL, outline=LINE,
    )
    left_label = "YOU SEND" if is_transfer else "YOU PAY"
    right_label = ("RECIPIENT" if is_transfer
                   else ("YOU RECEIVED" if settled else "YOU RECEIVE (EST.)"))
    right_value = (str(o.get("recipient") or "—") if is_transfer
                   else str(o.get("receiving_estimated") or "—"))
    _cell(d, PAD + 26, _PANEL_TOP + 26, left_label, str(o.get("spending") or "—"))
    _text(d, (W // 2, _PANEL_TOP + _PANEL_H // 2), "→", _f(22), ACCENT, anchor="mm")
    _cell(d, W - PAD - 26, _PANEL_TOP + 26, right_label, right_value, anchor_right=True)

    # ---- facts ------------------------------------------------------------
    # Three, never more. Gas is here rather than the platform fee because it is
    # the one people ask about before signing; the fee is disclosed in the text
    # card and on the tool response, where it can be read in full.
    gas = ("paid from your OKB" if o.get("gas_sponsored") is False
           else "sponsored by Sarf")
    facts = [
        ("MINIMUM RECEIVED", str(o.get("minimum_received") or "—")),
        ("GAS", gas),
        ("NETWORK", f"X Layer · {o.get('chain_id', 196)}"),
    ]
    if is_transfer:
        facts[0] = ("REVERSIBLE", "no — final once mined")
    cw = (W - 2 * PAD) / len(facts)
    for i, (label, value) in enumerate(facts):
        cx = int(PAD + cw * i)
        _text(d, (cx, _FACTS_TOP), label, _f(10), PAPER_DIM)
        _text(d, (cx, _FACTS_TOP + 20), value[:30], _f(13), PAPER)

    # ---- tail -------------------------------------------------------------
    y = _TAIL_TOP
    if warn:
        _text(d, (PAD, y), warn, _f(12), ACCENT)
        y += 30
    # One standing line, and it is the one that fits the operation. A transfer
    # of USDT is not synthetic equity exposure, so printing that disclosure
    # over it would be boilerplate in the literal sense — words that do not
    # describe what is on the card. What a transfer needs said is the part
    # that cannot be undone.
    _text(
        d, (PAD, y),
        "Final once mined — check the recipient character by character. "
        "No recall, no reversal."
        if is_transfer else
        "Synthetic exposure — tracks the share price only. No ownership, "
        "dividends or voting rights.",
        _f(11), PAPER_DIM,
    )

    # ---- footer -----------------------------------------------------------
    # Must agree with can_execute, same as the text card's closing line. This
    # one was missed when that was fixed, so the image kept saying "Sarf cannot
    # execute this" over an order the payload advertised as auto-executable —
    # the two halves of one response disagreeing about a fund-moving action,
    # which is a trust problem rather than a cosmetic one.
    fy = h - 30
    d.line([(PAD, fy - 22), (W - PAD, fy - 22)], fill=LINE, width=1)
    if settled:
        left, right = "SETTLED ON X LAYER", "confirm with get_status"
    elif is_transfer:
        left, right = "UNSIGNED — you sign in your own wallet", "never delegated"
    elif o.get("can_execute"):
        left, right = "UNSIGNED — approve in chat to execute", "within your session grant"
    else:
        left, right = "UNSIGNED — you sign in your own wallet", "Sarf holds no keys"
    _text(d, (PAD, fy), left, _f(12), GREEN if settled else PAPER)
    _text(d, (W - PAD, fy), right, _f(12), PAPER_DIM, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()
