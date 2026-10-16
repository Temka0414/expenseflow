"""ApprovalWorkflow ``.waitForTaskToken`` task: park the task token on a pending expense (SPEC 7.5)."""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from expenseflow import keys
from handlers import approval_request
from tests.builders import expense_item
from tests.conftest import FakeClock, FakeLambdaContext

TASK_TOKEN = "AQCEAAAAKgAAAAMAAAAAAAAAAeR2kL0secret-task-token"


def stored(table: Any, expense_id: str) -> dict[str, Any]:
    item: dict[str, Any] = table.get_item(Key=keys.expense_key(expense_id))["Item"]
    return item


def request(expense_id: str, lambda_context: FakeLambdaContext, *, escalated: bool = False) -> dict[str, Any]:
    event = {"taskToken": TASK_TOKEN, "expenseId": expense_id, "escalated": escalated}
    return dict(approval_request.handler(event, lambda_context))


@pytest.mark.parametrize("escalated", [False, True])
def test_stores_the_task_token_on_a_pending_expense(
    escalated: bool, table: Any, clock: FakeClock, lambda_context: FakeLambdaContext
) -> None:
    expense = expense_item(status="PENDING_APPROVAL", version=Decimal(2))
    table.put_item(Item=expense)
    clock.set(datetime(2026, 10, 13, 6, 0, tzinfo=UTC))

    assert request(expense["id"], lambda_context, escalated=escalated) == {"stored": True}

    item = stored(table, expense["id"])
    assert item["approvalTaskToken"] == TASK_TOKEN
    assert item["approvalRequestedAt"] == "2026-10-13T06:00:00.000Z"
    assert item["escalated"] is escalated
    # Not a state change: status, version and updatedAt stay as they were (the stream processor ignores it).
    assert {name: item[name] for name in ("status", "version", "updatedAt")} == {
        "status": "PENDING_APPROVAL",
        "version": 2,
        "updatedAt": expense["updatedAt"],
    }


@pytest.mark.parametrize("status", ["DRAFT", "APPROVED", "REJECTED"])
def test_fails_with_expense_not_pending_when_no_longer_pending(
    status: str, table: Any, lambda_context: FakeLambdaContext
) -> None:
    expense = expense_item(status=status)
    table.put_item(Item=expense)

    with pytest.raises(approval_request.ExpenseNotPending):
        request(expense["id"], lambda_context)

    assert "approvalTaskToken" not in stored(table, expense["id"])


def test_a_deleted_expense_is_not_pending_and_is_not_recreated(table: Any, lambda_context: FakeLambdaContext) -> None:
    with pytest.raises(approval_request.ExpenseNotPending):
        request("01JA2B3C4D5E6F7G8H9JKMNPQR", lambda_context)

    assert "Item" not in table.get_item(Key=keys.expense_key("01JA2B3C4D5E6F7G8H9JKMNPQR"))


def test_the_error_name_is_what_the_workflow_catches() -> None:
    # Lambda reports a Python exception's class name as the error type; the state machine matches that string.
    assert approval_request.ExpenseNotPending.__name__ == "ExpenseNotPending"


def test_the_task_token_is_never_logged(
    table: Any, lambda_context: FakeLambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    expense = expense_item(status="PENDING_APPROVAL")
    table.put_item(Item=expense)
    log = io.StringIO()
    handler = approval_request.logger.registered_handler
    assert isinstance(handler, logging.StreamHandler)
    monkeypatch.setattr(handler, "stream", log)  # the JSON log lines Lambda would ship to CloudWatch

    request(expense["id"], lambda_context)

    output = log.getvalue()
    assert expense["id"] in output  # the log line is there...
    assert TASK_TOKEN not in output  # ...the credential is not
