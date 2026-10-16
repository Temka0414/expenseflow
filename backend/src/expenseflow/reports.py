"""The monthly CSV report of approved expenses (SPEC 7.6).

Two details matter for a report that people in Japan open in Excel:

* **UTF-8 with a BOM.** Excel guesses the encoding of a double-clicked CSV; without the byte-order mark it assumes
  the legacy code page (Shift_JIS on Japanese Windows) and turns ``日本交通`` into mojibake. ``utf-8-sig`` writes it.
* **CSV (formula) injection.** Titles and merchants are typed by users. A cell starting with ``=``, ``+``, ``-``
  or ``@`` is executed as a formula by spreadsheet apps, so such text is prefixed with ``'`` (OWASP advice).

Months are UTC calendar months of the approval time, the same months as the GSI2 "approved in month" query and
the monthly totals. The schedule fires at 09:00 Asia/Tokyo on the 1st, which is exactly 00:00 UTC: by then the
previous month is complete in both time zones.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from expenseflow import aws, ids, keys, settings

PREVIOUS_MONTH: Final = "previous"
CSV_COLUMNS: Final = (
    "expense_id",
    "owner_name",
    "owner_email",
    "title",
    "merchant",
    "category",
    "expense_date",
    "amount",
    "currency",
    "approved_at",
    "approver_email",
    "auto_approved",
)
CONTENT_TYPE: Final = "text/csv; charset=utf-8"
_FORMULA_TRIGGERS: Final = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class Report:
    month: str
    body: bytes
    row_count: int
    totals: dict[str, Decimal]  # currency -> sum of approved amounts

    @property
    def key(self) -> str:
        return keys.report_object_key(self.month)


def resolve_month(value: str, now: datetime | None = None) -> str:
    """``YYYY-MM`` as given, or the previous calendar month in Japan for ``"previous"`` (the scheduled run)."""
    if value == PREVIOUS_MONTH:
        return ids.previous_month_jst(now)
    if not re.fullmatch(ids.MONTH_PATTERN, value):
        raise ValueError(f'month must be "YYYY-MM" or "{PREVIOUS_MONTH}", got {value!r}')
    return value


def build_report(month: str, expenses: Iterable[Mapping[str, Any]]) -> Report:
    """Render approved expenses as CSV (RFC 4180: quoted where needed, CRLF line ends) and sum them per currency."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    totals: dict[str, Decimal] = {}
    row_count = 0
    for expense in expenses:
        writer.writerow(_row(expense))
        currency = expense["currency"]
        totals[currency] = totals.get(currency, Decimal(0)) + expense["amount"]
        row_count += 1
    return Report(month=month, body=buffer.getvalue().encode("utf-8-sig"), row_count=row_count, totals=totals)


def store(report: Report) -> str:
    """Write the CSV to ``reports/{month}.csv`` in the reports bucket (a re-run overwrites it); returns the key."""
    aws.client("s3").put_object(
        Bucket=settings.reports_bucket(),
        Key=report.key,
        Body=report.body,
        ContentType=CONTENT_TYPE,
        # Presigned GET URLs then download as a file with a meaningful name instead of opening "2026-09.csv" inline.
        ContentDisposition=f'attachment; filename="expenseflow-{report.month}.csv"',
    )
    return report.key


def _row(expense: Mapping[str, Any]) -> list[str]:
    return [
        expense["id"],
        _text(expense.get("ownerName")),
        _text(expense.get("ownerEmail")),
        _text(expense.get("title")),
        _text(expense.get("merchant")),
        expense.get("category", ""),
        expense.get("expenseDate", ""),
        format(expense["amount"], "f"),  # Decimal as written: "1280", "12.50" - never "1.28E+3"
        expense["currency"],
        expense.get("decidedAt") or expense["updatedAt"],
        _text(expense.get("approverEmail")),
        "true" if expense.get("autoApproved") else "false",
    ]


def _text(value: str | None) -> str:
    if not value:
        return ""
    return f"'{value}" if value.startswith(_FORMULA_TRIGGERS) else value
