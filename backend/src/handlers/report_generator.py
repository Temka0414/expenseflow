"""Monthly expense report: CSV of the month's approved expenses in S3 + a ``Report Generated`` event (SPEC 7.6).

Two callers, one input shape:

* **EventBridge Scheduler** at 09:00 Asia/Tokyo on the 1st: ``{"month": "previous", "requestedBy": "scheduler"}``.
  Scheduler (unlike the older "scheduled rules") understands time zones, so "9 am in Tokyo" stays 9 am in Tokyo.
* **The API** (``POST /reports``), asynchronously: ``{"month": "2026-09", "requestedBy": "<admin e-mail>"}``.

Regenerating a month overwrites its CSV, so running the function twice is harmless (idempotent by key).
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

from expenseflow import events, ids, reports
from expenseflow.repository import ExpenseRepository
from expenseflow.serialization import to_json_number

logger = Logger()
tracer = Tracer()


@tracer.capture_method(capture_response=False)
def generate(month: str, requested_by: str) -> dict[str, Any]:
    """Build, store and announce the report of ``month``; returns ``{month, key, rowCount, totals}``."""
    report = reports.build_report(month, ExpenseRepository().iter_approved_in_month(month))
    key = reports.store(report)
    detail = events.build_report_detail(
        month=month,
        key=key,
        row_count=report.row_count,
        totals=report.totals,
        requested_by=requested_by,
        generated_at=ids.now_iso(),
    )
    events.publish([events.report_event(detail)])
    return {
        "month": month,
        "key": key,
        "rowCount": report.row_count,
        "totals": {currency: to_json_number(amount) for currency, amount in report.totals.items()},
    }


@logger.inject_lambda_context(log_event=True, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    # A missing month means "previous", so a test invocation from the Lambda console with `{}` just works.
    month = reports.resolve_month(event.get("month") or reports.PREVIOUS_MONTH)
    result = generate(month, event.get("requestedBy") or "unknown")
    logger.info("report generated", extra=result)
    return result
