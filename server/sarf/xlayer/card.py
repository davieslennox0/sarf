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
import io
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# Website tokens (styles.css)
BG = (11, 12, 9)
PANEL = (21, 22, 15)
PANEL_2 = (28, 29, 20)
LINE = (44, 45, 34)
AMBER = (232, 163, 61)
PAPER = (237, 230, 214)
PAPER_DIM = (166, 161, 144)
GREEN = (107, 158, 125)
RED = (180, 83, 74)

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
        # Always printed, charged or not — "no fee" is information too, and
        # leaving the row out is how a user starts assuming there is one.
        _row("Platform fee",
             f"${float(fee.get('usd', 0)):.2f} {fee.get('denominated_in', '')}".strip()
             if charged else "none"),
        _row("Network", f"X Layer · {o.get('chain_id', 196)} · gas in OKB"),
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
    lines += [
        _rule(),
        _row("UNSIGNED — you sign this in your own wallet."),
        _row("Sarf holds no keys and cannot execute it."),
        f"└{'─' * (TW - 2)}┘",
    ]
    return "```\n" + "\n".join(lines) + "\n```"


def render_order_card(order: dict[str, Any]) -> str:
    """-> base64 PNG of the order card. Never raises: presentation must not
    be able to break an order the user is entitled to see."""
    try:
        return _render(order)
    except Exception:  # pragma: no cover - cosmetic path
        return ""


def _render(o: dict[str, Any]) -> str:
    side = str(o.get("side", "")).upper()
    symbol = o.get("symbol", "")
    name = (o.get("name") or "").replace(" xStock", "")
    fee = o.get("platform_fee") or {}
    notes = [n for n in (o.get("risk_notes") or []) if n]

    # Height is content-driven so a long risk list is never clipped. These
    # constants mirror the layout below: the notes start at NOTES_TOP and the
    # footer needs FOOTER_H beneath the last line. Deriving the height from a
    # guess instead is how the notes ended up printed over the footer.
    NOTES_TOP, LINE_H, FOOTER_H = 396, 22, 62
    note_lines: list[str] = []
    for n in notes[:4]:
        note_lines += _wrap(n, 76)[:2]
    h = NOTES_TOP + len(note_lines) * LINE_H + FOOTER_H

    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)

    # ---- header -----------------------------------------------------------
    _text(d, (PAD, 34), "SARF", _f(24, True), PAPER)
    _text(d, (PAD + 78, 40), "/  X LAYER RWA", _f(15), AMBER)
    _text(d, (W - PAD, 40), "REVIEW & SIGN", _f(13), PAPER_DIM, anchor="ra")
    d.line([(PAD, 76), (W - PAD, 76)], fill=LINE, width=1)

    # ---- headline ---------------------------------------------------------
    _text(d, (PAD, 100), f"{side} {symbol}", _f(34, True), AMBER)
    if name:
        _text(d, (PAD, 146), name, _f(15), PAPER_DIM)

    # ---- amounts panel ----------------------------------------------------
    y = 180
    d.rectangle([PAD, y, W - PAD, y + 92], fill=PANEL, outline=LINE)
    cells = [
        ("YOU PAY", o.get("spending") or "—"),
        ("YOU RECEIVE", o.get("receiving_estimated") or "—"),
        ("MINIMUM", o.get("minimum_received") or "—"),
    ]
    cw = (W - 2 * PAD) / len(cells)
    for i, (label, value) in enumerate(cells):
        cx = PAD + cw * i + 22
        _text(d, (cx, y + 20), label, _f(11), PAPER_DIM)
        _text(d, (cx, y + 44), str(value)[:22], _f(17, True), PAPER)
        if i:
            lx = PAD + cw * i
            d.line([(lx, y + 12), (lx, y + 80)], fill=LINE, width=1)

    # ---- fee + value strip ------------------------------------------------
    y += 116
    charged = bool(fee.get("charged"))
    fee_txt = (f"${float(fee.get('usd', 0)):.2f} {fee.get('denominated_in', '')}".strip()
               if charged else "none")
    strip = [
        ("ORDER VALUE", f"${float(o['estimated_usd']):,.2f}" if o.get("estimated_usd") is not None else "—", PAPER),
        # Stated on the card, not left to the model to mention or not.
        ("PLATFORM FEE", fee_txt, AMBER if charged else PAPER_DIM),
        ("NETWORK", "X LAYER · 196", PAPER_DIM),
    ]
    for i, (label, value, col) in enumerate(strip):
        cx = PAD + cw * i + 22
        _text(d, (cx, y), label, _f(11), PAPER_DIM)
        _text(d, (cx, y + 20), value, _f(15, True), col)

    # ---- risk notes -------------------------------------------------------
    y = NOTES_TOP - 58
    d.line([(PAD, y), (W - PAD, y)], fill=LINE, width=1)
    _text(d, (PAD, y + 16), "READ BEFORE SIGNING", _f(11, True), AMBER)
    y = NOTES_TOP
    for line in note_lines:
        _text(d, (PAD, y), line, _f(12), PAPER_DIM)
        y += 22

    # ---- footer -----------------------------------------------------------
    y = h - 46
    d.line([(PAD, y - 14), (W - PAD, y - 14)], fill=LINE, width=1)
    _text(d, (PAD, y), "UNSIGNED — you sign in your own wallet", _f(12), PAPER)
    _text(d, (W - PAD, y), "Sarf cannot execute this", _f(12), PAPER_DIM, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()
