"""Generate ``sample-receipt.pdf``: a small, realistic Tokyo cafe receipt for trying the AI extraction.

Pure standard library (no PDF package): the file is written object by object, which also shows what a minimal
valid PDF contains - a catalog, a page tree, one page, two standard fonts, one content stream, an info dictionary,
and the cross-reference table (``xref``) that records the byte offset of every object.

Run it from anywhere::

    python samples/make_sample_receipt.py

The receipt totals 1,280 yen including 10% consumption tax (subtotal 1,164 + tax 116). Upload it in the app (or let
``scripts/smoke_test.py`` do it) and Claude should suggest merchant, date, amount, currency JPY and category MEALS.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

OUTPUT: Final = Path(__file__).resolve().parent / "sample-receipt.pdf"

PAGE_WIDTH: Final = 226  # points (1/72 inch): about 80 mm, the width of thermal receipt paper
PAGE_HEIGHT: Final = 400
MARGIN: Final = 14

REGULAR: Final = "F1"
BOLD: Final = "F2"

# Helvetica glyph widths (1/1000 em, from the Adobe AFM metrics) of the characters in right-aligned amounts.
# Helvetica-Bold shares the digit, yen, comma and space widths (only "x" differs: 556), and bold text here is
# amounts only. The 14 standard PDF fonts need no embedding, so the file stays tiny.
_AMOUNT_GLYPH_WIDTHS: Final = {**dict.fromkeys("0123456789", 556), "¥": 556, ",": 278, " ": 278, "x": 500}


@dataclass(frozen=True)
class Item:
    name: str
    quantity: int
    unit_price: int

    @property
    def amount(self) -> int:
        return self.quantity * self.unit_price


ITEMS: Final = (Item("Cafe Latte (M)", 2, 420), Item("Matcha Madeleine", 1, 324))
TAX_RATE_PERCENT: Final = 10  # eat-in food and drink; take-out would be the reduced 8% rate


def yen(amount: int) -> str:
    """``1280`` -> ``"¥1,280"``. (In WinAnsiEncoding the yen sign is the single byte 0xA5.)"""
    return f"¥{amount:,}"


def _pdf_string(text: str) -> bytes:
    """A PDF literal string in WinAnsiEncoding (cp1252) with ``\\``, ``(`` and ``)`` escaped."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("cp1252") + b")"


@dataclass
class Canvas:
    """Collects PDF content-stream operators; ``y`` is measured from the bottom of the page, as in PDF."""

    operators: list[bytes] = field(default_factory=list)

    def text(self, x: float, y: float, text: str, *, font: str = REGULAR, size: float = 8) -> None:
        self.operators.append(b"BT /%s %g Tf %g %g Td %s Tj ET" % (font.encode(), size, x, y, _pdf_string(text)))

    def text_right(self, right: float, y: float, text: str, *, font: str = REGULAR, size: float = 8) -> None:
        width = sum(_AMOUNT_GLYPH_WIDTHS[char] for char in text) * size / 1000
        self.text(right - width, y, text, font=font, size=size)

    def rule(self, y: float, *, dashed: bool = True) -> None:
        dash = b"[2 2] 0 d" if dashed else b"[] 0 d"
        self.operators.append(b"%s 0.5 w %g %g m %g %g l S" % (dash, MARGIN, y, PAGE_WIDTH - MARGIN, y))

    def content(self) -> bytes:
        return b"\n".join(self.operators) + b"\n"


def draw_receipt(items: Sequence[Item]) -> bytes:
    subtotal = sum(item.amount for item in items)
    tax = subtotal * TAX_RATE_PERCENT // 100  # Japanese receipts usually round consumption tax down
    total = subtotal + tax
    right = PAGE_WIDTH - MARGIN

    canvas = Canvas()
    canvas.text(MARGIN, 372, "CAFE SAKURA", font=BOLD, size=15)
    canvas.text(MARGIN, 358, "Shibuya Jinnan", size=8)
    canvas.text(MARGIN, 347, "1-2-3 Jinnan, Shibuya-ku, Tokyo 150-0041", size=7)
    canvas.text(MARGIN, 337, "TEL 03-1234-5678", size=7)
    canvas.text(MARGIN, 327, "Registration No. T1234567890123", size=7)  # qualified-invoice issuer number

    canvas.text(MARGIN, 305, "RECEIPT", font=BOLD, size=11)
    canvas.text(MARGIN, 292, "Date: 2026/10/12 (Mon) 09:41", size=8)
    canvas.text(MARGIN, 281, "Receipt No. 0042    Eat-in    Staff: Sato", size=8)
    canvas.rule(272)

    y = 259
    for item in items:
        canvas.text(MARGIN, y, item.name)
        canvas.text_right(150, y, f"{item.quantity} x {yen(item.unit_price)}")
        canvas.text_right(right, y, yen(item.amount))
        y -= 13
    canvas.rule(y + 4)

    y -= 9
    for label, amount in (
        (f"Subtotal ({len(items)} items)", subtotal),
        (f"Consumption tax ({TAX_RATE_PERCENT}%)", tax),
    ):
        canvas.text(MARGIN, y, label)
        canvas.text_right(right, y, yen(amount))
        y -= 13
    canvas.text(MARGIN, y - 4, "TOTAL", font=BOLD, size=12)
    canvas.text_right(right, y - 4, yen(total), font=BOLD, size=12)
    y -= 20
    canvas.rule(y, dashed=False)

    y -= 13
    canvas.text(MARGIN, y, "Payment: Suica (IC card)")
    canvas.text_right(right, y, yen(total))
    canvas.text(MARGIN, y - 13, "Change")
    canvas.text_right(right, y - 13, yen(0))
    canvas.text(MARGIN, y - 40, "Thank you! Please come again.", size=8)
    return canvas.content()


def build_pdf(content: bytes) -> bytes:
    """Assemble a single-page PDF 1.4 file around one content stream."""
    font = "<< /Type /Font /Subtype /Type1 /BaseFont /{} /Encoding /WinAnsiEncoding >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>" % (PAGE_WIDTH, PAGE_HEIGHT)
        ),
        font.format("Helvetica").encode(),
        font.format("Helvetica-Bold").encode(),
        b"<< /Length %d >>\nstream\n%sendstream" % (len(content), content),
        b"<< /Title (Sample receipt - Cafe Sakura) /Producer (ExpenseFlow make_sample_receipt.py) >>",
    ]

    # The second line holds bytes above 127 so tools treat the file as binary (PDF convention).
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

    xref_offset = len(out)
    # Every xref entry is exactly 20 bytes: 10-digit offset, 5-digit generation, n/f flag, 2-byte line end.
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_offset,
    )
    return bytes(out)


def verify(pdf: bytes) -> int:
    """Parse the file back like a PDF reader would and check every cross-reference; returns the object count.

    A reader starts at the end: ``startxref`` gives the xref table's offset, and each entry must point exactly at
    the ``N 0 obj`` header of that object. Stream ``/Length`` must match the bytes between the stream keywords.
    """
    if not pdf.startswith(b"%PDF-") or not pdf.rstrip().endswith(b"%%EOF"):
        raise ValueError("missing %PDF- header or %%EOF marker")
    startxref = re.search(rb"startxref\n(\d+)\n%%EOF\n$", pdf)
    if startxref is None:
        raise ValueError("missing startxref")
    xref_offset = int(startxref.group(1))
    table = re.match(rb"xref\n0 (\d+)\n", pdf[xref_offset:])
    if table is None:
        raise ValueError(f"startxref {xref_offset} does not point at an xref table")
    count = int(table.group(1))
    entries_start = xref_offset + table.end()
    for number in range(1, count):
        entry = pdf[entries_start + 20 * number : entries_start + 20 * (number + 1)]
        offset = int(entry[:10])
        if not pdf.startswith(b"%d 0 obj\n" % number, offset) or entry[17:18] != b"n":
            raise ValueError(f"xref entry for object {number} points at {offset}, not at its header")
    trailer_size = re.search(rb"trailer\n<< /Size (\d+) ", pdf)
    if trailer_size is None or int(trailer_size.group(1)) != count:
        raise ValueError("trailer /Size does not match the xref table")
    stream = re.search(rb"<< /Length (\d+) >>\nstream\n", pdf)
    if stream is None or pdf[stream.end() + int(stream.group(1)) :][:9] != b"endstream":
        raise ValueError("content stream /Length does not match its data")
    return count - 1


def main() -> None:
    pdf = build_pdf(draw_receipt(ITEMS))
    objects = verify(pdf)
    OUTPUT.write_bytes(pdf)
    print(f"Wrote {OUTPUT} ({len(pdf):,} bytes, {objects} objects, xref verified)")


if __name__ == "__main__":
    main()
