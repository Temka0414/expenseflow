"""Monthly report: approved expenses of a month as a UTF-8-with-BOM CSV + ``Report Generated`` (SPEC 7.6)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
import pytest

from expenseflow import reports
from handlers import report_generator
from tests.builders import ADMIN_EMAIL, MANAGER_EMAIL, expense_item
from tests.conftest import EventsSink, FakeClock, FakeLambdaContext

APPROVED_COLUMNS = (
    "expense_id,owner_name,owner_email,title,merchant,category,expense_date,amount,currency,"
    "approved_at,approver_email,auto_approved"
)


def approved(decided_at: str, **overrides: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "status": "APPROVED",
        "updatedAt": decided_at,
        "decidedAt": decided_at,
        "approverEmail": MANAGER_EMAIL,
        "autoApproved": False,
        **overrides,
    }
    return expense_item(**attributes)


def report_object(reports_bucket: str, month: str) -> dict[str, Any]:
    obj: dict[str, Any] = boto3.client("s3").get_object(Bucket=reports_bucket, Key=f"reports/{month}.csv")
    return obj


def rows_of(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))


@pytest.fixture
def september(table: Any) -> list[dict[str, Any]]:
    """Two approvals in September 2026 (UTC), plus expenses that must NOT be in its report."""
    taxi = approved("2026-09-03T01:00:00.000Z", ownerName="佐藤 花子", merchant="日本交通")
    dinner = approved(
        "2026-09-30T23:59:59.999Z",
        title='=HYPERLINK("http://evil.example","click")',
        amount=Decimal("12.50"),
        currency="USD",
        approverEmail="auto-approval",
        autoApproved=True,
    )
    for item in (
        taxi,
        dinner,
        approved("2026-10-01T00:00:00.000Z"),  # October
        expense_item(status="PENDING_APPROVAL", updatedAt="2026-09-10T00:00:00.000Z"),  # not approved
        expense_item(status="REJECTED", updatedAt="2026-09-11T00:00:00.000Z"),
    ):
        table.put_item(Item=item)
    return [taxi, dinner]


def test_writes_the_months_csv_with_a_bom(
    september: list[dict[str, Any]],
    reports_bucket: str,
    events_sink: EventsSink,
    clock: FakeClock,
    lambda_context: FakeLambdaContext,
) -> None:
    taxi, dinner = september

    result = report_generator.handler({"month": "2026-09", "requestedBy": ADMIN_EMAIL}, lambda_context)

    assert result == {
        "month": "2026-09",
        "key": "reports/2026-09.csv",
        "rowCount": 2,
        "totals": {"JPY": 1280, "USD": 12.5},
    }
    obj = report_object(reports_bucket, "2026-09")
    body = obj["Body"].read()
    assert body.startswith(b"\xef\xbb\xbf")  # the UTF-8 byte-order mark Excel needs
    assert obj["ContentType"] == "text/csv; charset=utf-8"
    assert b"\r\n" in body  # RFC 4180 line endings
    header, *rows = rows_of(body)
    assert ",".join(header) == APPROVED_COLUMNS
    assert rows == [
        [
            taxi["id"],
            "佐藤 花子",
            taxi["ownerEmail"],
            "Taxi to client meeting",
            "日本交通",
            "TRANSPORT",
            "2026-10-09",
            "1280",
            "JPY",
            "2026-09-03T01:00:00.000Z",
            MANAGER_EMAIL,
            "false",
        ],
        [
            dinner["id"],
            dinner["ownerName"],
            dinner["ownerEmail"],
            '\'=HYPERLINK("http://evil.example","click")',  # neutralised: shown as text, never run as a formula
            "Nihon Kotsu",
            "TRANSPORT",
            "2026-10-09",
            "12.50",
            "USD",
            "2026-09-30T23:59:59.999Z",
            "auto-approval",
            "true",
        ],
    ]


def test_publishes_report_generated(
    september: list[dict[str, Any]],
    reports_bucket: str,
    events_sink: EventsSink,
    clock: FakeClock,
    lambda_context: FakeLambdaContext,
) -> None:
    clock.set(datetime(2026, 10, 1, 0, 0, 3, tzinfo=UTC))

    report_generator.handler({"month": "2026-09", "requestedBy": ADMIN_EMAIL}, lambda_context)

    [envelope] = events_sink()
    assert (envelope["source"], envelope["detail-type"]) == ("expenseflow.reports", "Report Generated")
    assert envelope["detail"] == {
        "month": "2026-09",
        "key": "reports/2026-09.csv",
        "rowCount": 2,
        "totals": {"JPY": 1280, "USD": 12.5},
        "requestedBy": ADMIN_EMAIL,
        "idempotencyKey": "2026-09:REPORT:2026-10-01T00:00:03.000Z",
    }


def test_scheduled_run_reports_the_previous_month_in_japan(
    september: list[dict[str, Any]],
    reports_bucket: str,
    events_sink: EventsSink,
    clock: FakeClock,
    lambda_context: FakeLambdaContext,
) -> None:
    clock.set(datetime(2026, 10, 1, 0, 0, tzinfo=UTC))  # the schedule: 09:00 Asia/Tokyo on the 1st

    result = report_generator.handler({"month": "previous", "requestedBy": "scheduler"}, lambda_context)

    assert (result["month"], result["rowCount"]) == ("2026-09", 2)
    assert events_sink()[0]["detail"]["requestedBy"] == "scheduler"


def test_a_month_without_approvals_gives_a_header_only_report(
    table: Any, reports_bucket: str, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    result = report_generator.handler({"month": "2025-01", "requestedBy": ADMIN_EMAIL}, lambda_context)

    assert (result["rowCount"], result["totals"]) == (0, {})
    assert rows_of(report_object(reports_bucket, "2025-01")["Body"].read()) == [APPROVED_COLUMNS.split(",")]


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 13, 5, 0, tzinfo=UTC), "2026-09"),
        (datetime(2026, 10, 31, 15, 30, tzinfo=UTC), "2026-10"),  # already 1 November 00:30 in Tokyo
        (datetime(2027, 1, 1, 0, 0, tzinfo=UTC), "2026-12"),  # across the year boundary
    ],
)
def test_previous_month_is_computed_in_japan_time(now: datetime, expected: str) -> None:
    assert reports.resolve_month("previous", now) == expected


@pytest.mark.parametrize("month", ["2026-13", "2026-9", "September", "2026-09-01", ""])
def test_invalid_months_are_rejected(month: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        reports.resolve_month(month)
