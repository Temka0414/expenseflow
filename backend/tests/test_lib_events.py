from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from botocore.stub import ANY, Stubber

from expenseflow import aws, events
from expenseflow.events import (
    DETAIL_TYPE_EXPENSE_APPROVED,
    DETAIL_TYPE_EXPENSE_REJECTED,
    DETAIL_TYPE_EXPENSE_SUBMITTED,
    DETAIL_TYPE_EXPENSE_WITHDRAWN,
    DETAIL_TYPE_RECEIPT_PROCESSED,
    SOURCE_EXPENSES,
    EventEntry,
    EventPublishError,
)
from tests.builders import EMPLOYEE_EMAIL, EMPLOYEE_ID, MANAGER_EMAIL, MANAGER_ID, expense_item
from tests.conftest import EventsSink

_DETAIL_KEYS = {
    "expenseId", "ownerId", "ownerEmail", "ownerName", "title", "amount", "currency", "status", "previousStatus",
    "actorId", "actorEmail", "comment", "autoApproved", "extractionStatus", "occurredAt", "idempotencyKey",
}  # fmt: skip


def test_names_match_the_spec() -> None:
    assert (events.SOURCE_EXPENSES, events.SOURCE_APPROVALS, events.SOURCE_REPORTS) == (
        "expenseflow.expenses",
        "expenseflow.approvals",
        "expenseflow.reports",
    )
    assert events.DETAIL_TYPE_APPROVAL_ESCALATED == "Approval Escalated"
    assert events.DETAIL_TYPE_REPORT_GENERATED == "Report Generated"
    assert DETAIL_TYPE_RECEIPT_PROCESSED == "Receipt Processed"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("DRAFT", "PENDING_APPROVAL", DETAIL_TYPE_EXPENSE_SUBMITTED),
        ("PENDING_APPROVAL", "APPROVED", DETAIL_TYPE_EXPENSE_APPROVED),
        ("PENDING_APPROVAL", "REJECTED", DETAIL_TYPE_EXPENSE_REJECTED),
        ("PENDING_APPROVAL", "DRAFT", DETAIL_TYPE_EXPENSE_WITHDRAWN),
        (None, "DRAFT", None),  # a new draft
        ("REJECTED", "DRAFT", None),  # re-opened for editing
        ("DRAFT", "DRAFT", None),  # an edit
        ("APPROVED", "APPROVED", None),
        ("DRAFT", None, None),
    ],
)
def test_detail_type_for_transition(old: str | None, new: str | None, expected: str | None) -> None:
    assert events.detail_type_for_transition(old, new) == expected


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("PROCESSING", "SUCCEEDED", True),
        ("PROCESSING", "FAILED", True),
        (None, "SKIPPED", True),
        ("SUCCEEDED", "SUCCEEDED", False),  # some other attribute changed
        (None, "PROCESSING", False),
        ("SUCCEEDED", None, False),
    ],
)
def test_receipt_processed(old: str | None, new: str | None, expected: bool) -> None:
    old_image = expense_item(extractionStatus=old)
    new_image = expense_item(extractionStatus=new)

    assert events.receipt_processed(old_image, new_image) is expected


def test_receipt_processed_on_insert() -> None:
    assert events.receipt_processed(None, expense_item(extractionStatus="SKIPPED")) is True


def test_status_detail_of_an_approval() -> None:
    old = expense_item(id="E1", status="PENDING_APPROVAL", amount=Decimal("1280.50"))
    new = expense_item(
        id="E1",
        status="APPROVED",
        amount=Decimal("1280.50"),
        updatedAt="2026-10-13T05:01:02.345Z",
        decidedAt="2026-10-13T05:01:02.345Z",
        decisionComment="Looks good",
        autoApproved=False,
        lastActorId=MANAGER_ID,
        lastActorEmail=MANAGER_EMAIL,
        extractionStatus="SUCCEEDED",
    )

    detail = events.build_expense_detail(new, old, "STATUS")

    assert set(detail) == _DETAIL_KEYS
    assert detail == {
        "expenseId": "E1",
        "ownerId": EMPLOYEE_ID,
        "ownerEmail": EMPLOYEE_EMAIL,
        "ownerName": "Hanako Sato",
        "title": "Taxi to client meeting",
        "amount": 1280.5,
        "currency": "JPY",
        "status": "APPROVED",
        "previousStatus": "PENDING_APPROVAL",
        "actorId": MANAGER_ID,
        "actorEmail": MANAGER_EMAIL,
        "comment": "Looks good",
        "autoApproved": False,
        "extractionStatus": "SUCCEEDED",
        "occurredAt": "2026-10-13T05:01:02.345Z",
        "idempotencyKey": "E1:APPROVED:2026-10-13T05:01:02.345Z",
    }


def test_status_detail_of_a_new_item_has_nulls_for_unknowns() -> None:
    new = expense_item(id="E2", amount=None, updatedAt="2026-10-13T00:00:00.000Z")

    detail = events.build_expense_detail(new, None, "STATUS")

    assert set(detail) == _DETAIL_KEYS
    assert detail["previousStatus"] is None
    assert detail["amount"] is None
    assert detail["autoApproved"] is None
    assert detail["comment"] is None
    assert detail["extractionStatus"] is None
    assert detail["idempotencyKey"] == "E2:DRAFT:2026-10-13T00:00:00.000Z"


def test_receipt_detail() -> None:
    old = expense_item(id="E3", extractionStatus="PROCESSING")
    new = expense_item(
        id="E3",
        extractionStatus="SUCCEEDED",
        extractedAt="2026-10-13T05:02:00.000Z",
        decisionComment="ignored for receipt events",
    )

    detail = events.build_expense_detail(new, old, "RECEIPT")

    assert detail["status"] == "DRAFT"
    assert detail["previousStatus"] == "DRAFT"
    assert detail["extractionStatus"] == "SUCCEEDED"
    assert detail["comment"] is None
    assert detail["occurredAt"] == "2026-10-13T05:02:00.000Z"
    assert detail["idempotencyKey"] == "E3:RECEIPT-SUCCEEDED:2026-10-13T05:02:00.000Z"


def test_detail_kind_is_validated() -> None:
    with pytest.raises(ValueError, match="BOGUS"):
        events.build_expense_detail(expense_item(), None, "BOGUS")


def test_report_detail() -> None:
    detail = events.build_report_detail(
        month="2026-09",
        key="reports/2026-09.csv",
        row_count=2,
        totals={"JPY": Decimal("12345"), "USD": Decimal("12.50")},
        requested_by="scheduler",
        generated_at="2026-10-01T00:00:00.123Z",
    )

    assert detail == {
        "month": "2026-09",
        "key": "reports/2026-09.csv",
        "rowCount": 2,
        "totals": {"JPY": 12345, "USD": 12.5},
        "requestedBy": "scheduler",
        "idempotencyKey": "2026-09:REPORT:2026-10-01T00:00:00.123Z",
    }
    assert events.report_event(detail).source == events.SOURCE_REPORTS


def test_request_entry_serialises_decimals_as_json_numbers() -> None:
    entry = EventEntry(SOURCE_EXPENSES, DETAIL_TYPE_EXPENSE_SUBMITTED, {"amount": Decimal("1280"), "title": "寿司"})

    request = entry.to_request_entry("bus")

    assert request == {
        "Source": "expenseflow.expenses",
        "DetailType": "Expense Submitted",
        "Detail": '{"amount":1280,"title":"寿司"}',
        "EventBusName": "bus",
    }


def test_publish_puts_events_on_the_custom_bus(events_sink: EventsSink) -> None:
    detail = events.build_expense_detail(expense_item(id="E9", status="PENDING_APPROVAL"), None, "STATUS")

    event_ids = events.publish([events.expense_event(DETAIL_TYPE_EXPENSE_SUBMITTED, detail)])

    assert len(event_ids) == 1
    (received,) = events_sink()
    assert received["source"] == "expenseflow.expenses"
    assert received["detail-type"] == "Expense Submitted"
    assert received["detail"] == json.loads(json.dumps(detail))


def test_publish_sends_batches_of_ten(events_sink: EventsSink) -> None:
    entries = [EventEntry(SOURCE_EXPENSES, DETAIL_TYPE_EXPENSE_SUBMITTED, {"n": n}) for n in range(23)]
    client = aws.client("events")
    calls: list[int] = []
    # `provide-client-params` sees each call's API parameters before they are serialised.
    client.meta.events.register(
        "provide-client-params.events.PutEvents", lambda params, **_: calls.append(len(params["Entries"]))
    )

    event_ids = events.publish(entries)

    assert calls == [10, 10, 3]
    assert len(event_ids) == 23
    assert sorted(event["detail"]["n"] for event in events_sink()) == list(range(23))


def test_publish_nothing_makes_no_call(event_bus: str) -> None:
    with Stubber(aws.client("events")):  # any call would fail: no response is queued
        assert events.publish([]) == []


def test_publish_raises_when_an_entry_fails(event_bus: str) -> None:
    entries = [
        EventEntry(SOURCE_EXPENSES, DETAIL_TYPE_EXPENSE_SUBMITTED, {"n": 1}),
        EventEntry(SOURCE_EXPENSES, DETAIL_TYPE_EXPENSE_APPROVED, {"n": 2}),
    ]
    response: dict[str, Any] = {
        "FailedEntryCount": 1,
        "Entries": [{"EventId": "11111111-2222-3333-4444-555555555555"}, {"ErrorCode": "ThrottlingException"}],
    }
    with Stubber(aws.client("events")) as stubber:
        stubber.add_response("put_events", response, {"Entries": ANY})

        with pytest.raises(EventPublishError, match=r"1 of 2 entries: Expense Approved: ThrottlingException"):
            events.publish(entries)
