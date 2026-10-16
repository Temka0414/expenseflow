from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from expenseflow import keys
from expenseflow.models import (
    CATEGORIES,
    CURRENCIES,
    Category,
    CreateExpenseRequest,
    CreateReportRequest,
    DecisionRequest,
    ExpenseStatus,
    ExtractionStatus,
    ReceiptUploadRequest,
    UpdateExpenseRequest,
    history_to_api,
    missing_required_fields,
    parse_extraction_result,
    to_api,
    total_to_api,
)
from tests.builders import expense_item
from tests.conftest import FakeClock

# --- Vocabulary -------------------------------------------------------------------------------------------------


def test_vocabulary_matches_the_spec() -> None:
    assert [status.value for status in ExpenseStatus] == ["DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED"]
    assert [status.value for status in ExtractionStatus] == ["PROCESSING", "SUCCEEDED", "FAILED", "SKIPPED"]
    assert CATEGORIES == ("TRAVEL", "MEALS", "LODGING", "TRANSPORT", "SUPPLIES", "SOFTWARE", "TRAINING", "OTHER")
    assert CURRENCIES == (
        "JPY", "USD", "EUR", "GBP", "CNY", "KRW", "TWD", "HKD", "SGD", "AUD", "CAD", "CHF", "THB", "INR", "MNT",
    )  # fmt: skip


def _errors(error: ValidationError) -> set[tuple[tuple[str | int, ...], str]]:
    return {(tuple(detail["loc"]), detail["type"]) for detail in error.errors()}


# --- CreateExpenseRequest ---------------------------------------------------------------------------------------


def test_create_request_accepts_camel_case_and_converts_types(clock: FakeClock) -> None:
    request = CreateExpenseRequest.model_validate(
        {
            "title": "  Team lunch  ",
            "merchant": "すき家",
            "amount": 12.5,
            "currency": "USD",
            "category": "MEALS",
            "expenseDate": "2026-10-12",
            "description": "Sprint planning",
        }
    )

    assert request.title == "Team lunch"
    assert request.amount == Decimal("12.5")
    assert request.attributes() == {
        "title": "Team lunch",
        "merchant": "すき家",
        "amount": Decimal("12.5"),
        "currency": "USD",
        "category": "MEALS",
        "expenseDate": "2026-10-12",
        "description": "Sprint planning",
    }
    assert type(request.attributes()["category"]) is str  # plain value, ready for DynamoDB


def test_create_request_only_needs_a_title() -> None:
    request = CreateExpenseRequest.model_validate({"title": "Taxi", "merchant": "", "amount": None})

    assert request.attributes() == {"title": "Taxi"}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, {(("title",), "missing")}),
        ({"title": ""}, {(("title",), "string_too_short")}),
        ({"title": "x" * 121}, {(("title",), "string_too_long")}),
        ({"title": "t", "merchant": "m" * 121}, {(("merchant",), "string_too_long")}),
        ({"title": "t", "description": "d" * 1001}, {(("description",), "string_too_long")}),
        ({"title": "t", "amount": 0}, {(("amount",), "greater_than")}),
        ({"title": "t", "amount": -5}, {(("amount",), "greater_than")}),
        ({"title": "t", "amount": 100_000_000.01}, {(("amount",), "less_than_equal")}),
        ({"title": "t", "amount": 12.345}, {(("amount",), "decimal_max_places")}),
        ({"title": "t", "amount": "abc"}, {(("amount",), "decimal_parsing")}),
        ({"title": "t", "currency": "XYZ"}, {(("currency",), "literal_error")}),
        ({"title": "t", "category": "meals"}, {(("category",), "enum")}),
        ({"title": "t", "expenseDate": "2026/10/12"}, {(("expenseDate",), "string_pattern_mismatch")}),
        ({"title": "t", "expenseDate": "2026-02-30"}, {(("expenseDate",), "date_parsing")}),
        ({"title": "t", "expenseDate": "1999-12-31"}, {(("expenseDate",), "date_out_of_range")}),
        ({"title": "t", "expenseDate": "2026-10-15"}, {(("expenseDate",), "date_out_of_range")}),
        ({"title": "t", "expense_date_typo": "x"}, {(("expense_date_typo",), "extra_forbidden")}),
    ],
)
def test_create_request_validation(clock: FakeClock, body: dict[str, Any], expected: set[Any]) -> None:
    with pytest.raises(ValidationError) as raised:
        CreateExpenseRequest.model_validate(body)

    assert _errors(raised.value) == expected


def test_expense_date_allows_tomorrow_for_japan_time(clock: FakeClock) -> None:
    # The clock says 2026-10-13 UTC; in Tokyo it may already be the 14th.
    assert CreateExpenseRequest.model_validate({"title": "t", "expenseDate": "2026-10-14"}).expense_date == "2026-10-14"
    assert CreateExpenseRequest.model_validate({"title": "t", "expenseDate": "2000-01-01"}).expense_date == "2000-01-01"


def test_amount_limits_are_inclusive() -> None:
    assert CreateExpenseRequest.model_validate({"title": "t", "amount": 100_000_000}).amount == Decimal(100_000_000)
    assert CreateExpenseRequest.model_validate({"title": "t", "amount": "0.01"}).amount == Decimal("0.01")


# --- UpdateExpenseRequest ---------------------------------------------------------------------------------------


def test_update_request_distinguishes_absent_from_null() -> None:
    request = UpdateExpenseRequest.model_validate(
        {"merchant": None, "description": "", "amount": "1500", "category": "TRAVEL", "version": 3}
    )

    updates, removals = request.changes()

    assert updates == {"amount": Decimal("1500"), "category": "TRAVEL"}
    assert removals == ["description", "merchant"]
    assert request.version == 3
    assert "expenseDate" not in updates  # absent = unchanged
    assert "expenseDate" not in removals


def test_empty_update_changes_nothing() -> None:
    assert UpdateExpenseRequest.model_validate({}).changes() == ({}, [])


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"title": None}, {(("title",), "not_removable")}),
        ({"currency": None}, {(("currency",), "not_removable")}),
        ({"currency": ""}, {(("currency",), "not_removable")}),
        ({"version": None}, {(("version",), "not_removable")}),
        ({"version": 0}, {(("version",), "greater_than_equal")}),
        ({"title": ""}, {(("title",), "string_too_short")}),
        ({"status": "APPROVED"}, {(("status",), "extra_forbidden")}),
    ],
)
def test_update_request_validation(body: dict[str, Any], expected: set[Any]) -> None:
    with pytest.raises(ValidationError) as raised:
        UpdateExpenseRequest.model_validate(body)

    assert _errors(raised.value) == expected


# --- Other request models ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("image/jpeg", ".jpg"),
        ("image/png", ".png"),
        ("image/webp", ".webp"),
        ("image/gif", ".gif"),
        ("application/pdf", ".pdf"),
    ],
)
def test_receipt_upload_request(content_type: str, extension: str) -> None:
    request = ReceiptUploadRequest.model_validate({"contentType": content_type, "size": 1})

    assert request.extension == extension


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"contentType": "text/html", "size": 10}, {(("contentType",), "literal_error")}),
        ({"contentType": "image/png", "size": 0}, {(("size",), "greater_than")}),
        ({"contentType": "image/png", "size": 3_500_001}, {(("size",), "receipt_too_large")}),
        ({"contentType": "image/png"}, {(("size",), "missing")}),
    ],
)
def test_receipt_upload_request_validation(body: dict[str, Any], expected: set[Any]) -> None:
    with pytest.raises(ValidationError) as raised:
        ReceiptUploadRequest.model_validate(body)

    assert _errors(raised.value) == expected


def test_receipt_size_limit_follows_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_RECEIPT_BYTES", "1000")

    assert ReceiptUploadRequest.model_validate({"contentType": "image/png", "size": 1000}).size == 1000
    with pytest.raises(ValidationError):
        ReceiptUploadRequest.model_validate({"contentType": "image/png", "size": 1001})


def test_decision_request() -> None:
    approve = DecisionRequest.model_validate({"decision": "APPROVED"})
    reject = DecisionRequest.model_validate({"decision": "REJECTED", "comment": "  Missing receipt  "})

    assert (approve.decision, approve.comment) == ("APPROVED", None)
    assert (reject.decision, reject.comment) == ("REJECTED", "Missing receipt")
    assert DecisionRequest.model_validate({"decision": "APPROVED", "comment": ""}).comment is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"decision": "REJECTED"}, {((), "comment_required")}),
        ({"decision": "REJECTED", "comment": "   "}, {((), "comment_required")}),
        ({"decision": "MAYBE"}, {(("decision",), "literal_error")}),
        ({"decision": "APPROVED", "comment": "c" * 501}, {(("comment",), "string_too_long")}),
    ],
)
def test_decision_request_validation(body: dict[str, Any], expected: set[Any]) -> None:
    with pytest.raises(ValidationError) as raised:
        DecisionRequest.model_validate(body)

    assert _errors(raised.value) == expected


def test_report_request(clock: FakeClock) -> None:
    assert CreateReportRequest.model_validate({"month": "2026-10"}).month == "2026-10"
    assert CreateReportRequest.model_validate({"month": "2025-01"}).month == "2025-01"


@pytest.mark.parametrize(
    ("month", "error"),
    [("2026-11", "month_in_future"), ("2026-13", "string_pattern_mismatch"), ("2026-1", "string_pattern_mismatch")],
)
def test_report_request_validation(clock: FakeClock, month: str, error: str) -> None:
    with pytest.raises(ValidationError) as raised:
        CreateReportRequest.model_validate({"month": month})

    assert _errors(raised.value) == {(("month",), error)}


def test_report_month_uses_japan_time(clock: FakeClock) -> None:
    clock.set(datetime(2026, 10, 31, 16, 0, tzinfo=UTC))  # already November 1st in Tokyo

    assert CreateReportRequest.model_validate({"month": "2026-11"}).month == "2026-11"


# --- missing_required_fields ------------------------------------------------------------------------------------


def test_complete_draft_has_no_missing_fields() -> None:
    assert missing_required_fields(expense_item()) == []


def test_missing_fields_are_listed_in_order() -> None:
    item = expense_item(merchant=None, category=None, expenseDate=None)

    assert missing_required_fields(item) == ["merchant", "category", "expenseDate"]


@pytest.mark.parametrize("amount", [None, Decimal(0), Decimal("-1")])
def test_amount_must_be_positive(amount: Decimal | None) -> None:
    assert missing_required_fields(expense_item(amount=amount)) == ["amount"]


# --- to_api -----------------------------------------------------------------------------------------------------

_API_FIELDS = {
    "id", "ownerId", "ownerEmail", "ownerName", "title", "merchant", "amount", "currency", "category",
    "expenseDate", "description", "status", "createdAt", "updatedAt", "submittedAt", "decidedAt", "version",
    "receipt", "pendingUpload", "extraction", "approval", "workflowExecutionArn",
}  # fmt: skip


def test_to_api_of_a_fresh_draft() -> None:
    item = expense_item(id="01JA2B3C4D5E6F7G8H9JKMNPQR", amount=Decimal("1280.00"), description=None)

    body = to_api(item)

    assert set(body) == _API_FIELDS
    assert body["id"] == "01JA2B3C4D5E6F7G8H9JKMNPQR"
    assert body["amount"] == 1280
    assert type(body["amount"]) is int
    assert body["version"] == 1
    assert type(body["version"]) is int
    assert body["description"] is None
    assert body["receipt"] is None
    assert body["pendingUpload"] is False
    assert body["extraction"] is None
    assert body["approval"] is None
    assert body["workflowExecutionArn"] is None
    json.dumps(body)  # JSON-ready without a custom encoder


def test_to_api_of_a_decided_expense_never_leaks_keys_or_the_task_token() -> None:
    result = {"isReceipt": True, "merchant": "すき家", "total": 1280, "currency": "JPY"}
    item = expense_item(
        status="APPROVED",
        amount=Decimal("12.50"),
        submittedAt="2026-10-11T00:00:00.000Z",
        decidedAt="2026-10-12T00:00:00.000Z",
        pendingReceiptKey="receipts/u/e/new.png",
        pendingReceiptContentType="image/png",
        receiptKey="receipts/u/e/old.jpg",
        receiptContentType="image/jpeg",
        receiptUploadedAt="2026-10-10T02:00:00.000Z",
        extractionStatus="SUCCEEDED",
        extractionResult=json.dumps(result, ensure_ascii=False),
        extractionModel="us.anthropic.claude-opus-5-5",
        extractedAt="2026-10-10T02:00:10.000Z",
        approvalTaskToken="AQCEAAAAKgAAAAMAAAAA-secret",
        approvalRequestedAt="2026-10-11T00:00:01.000Z",
        escalated=True,
        approverId="boss",
        approverEmail="kenji@example.com",
        decisionComment="OK",
        autoApproved=False,
        workflowExecutionArn="arn:aws:states:us-east-1:123456789012:execution:ApprovalWorkflow:abc",
    )

    body = to_api(item)

    assert set(body) == _API_FIELDS
    text = json.dumps(body, ensure_ascii=False)
    assert "secret" not in text
    assert "approvalTaskToken" not in text
    assert not {keys.PK, keys.SK, keys.GSI1PK, keys.GSI1SK, keys.GSI2PK, keys.GSI2SK} & set(body)
    assert body["amount"] == 12.5
    assert body["pendingUpload"] is True
    assert body["receipt"] == {
        "key": "receipts/u/e/old.jpg",
        "contentType": "image/jpeg",
        "uploadedAt": "2026-10-10T02:00:00.000Z",
    }
    assert body["extraction"] == {
        "status": "SUCCEEDED",
        "result": result,
        "error": None,
        "model": "us.anthropic.claude-opus-5-5",
        "extractedAt": "2026-10-10T02:00:10.000Z",
    }
    assert body["approval"] == {
        "approverId": "boss",
        "approverEmail": "kenji@example.com",
        "comment": "OK",
        "autoApproved": False,
        "escalated": True,
        "requestedAt": "2026-10-11T00:00:01.000Z",
    }
    assert body["workflowExecutionArn"].endswith(":abc")


def test_to_api_shows_a_pending_approval_request() -> None:
    body = to_api(expense_item(status="PENDING_APPROVAL", approvalRequestedAt="2026-10-11T00:00:01.000Z"))

    assert body["approval"] == {
        "approverId": None,
        "approverEmail": None,
        "comment": None,
        "autoApproved": False,
        "escalated": False,
        "requestedAt": "2026-10-11T00:00:01.000Z",
    }


def test_to_api_of_a_failed_extraction() -> None:
    body = to_api(expense_item(extractionStatus="FAILED", extractionError="ModelThrottledError: slow down"))

    assert body["extraction"]["status"] == "FAILED"
    assert body["extraction"]["result"] is None
    assert body["extraction"]["error"] == "ModelThrottledError: slow down"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"total": 1280}', {"total": 1280}),
        ("not json", None),
        ("[1, 2]", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_extraction_result_is_forgiving(raw: object, expected: dict[str, Any] | None) -> None:
    assert parse_extraction_result(raw) == expected


def test_history_to_api() -> None:
    item = {
        "PK": "EXPENSE#e",
        "SK": "HISTORY#2026-10-12T00:00:00.000Z#STATUS#REJECTED",
        "entityType": "History",
        "at": "2026-10-12T00:00:00.000Z",
        "kind": "STATUS",
        "from": "PENDING_APPROVAL",
        "to": "REJECTED",
        "actorId": "boss",
        "actorEmail": "kenji@example.com",
        "comment": "Missing receipt",
    }

    assert history_to_api(item) == {
        "at": "2026-10-12T00:00:00.000Z",
        "kind": "STATUS",
        "from": "PENDING_APPROVAL",
        "to": "REJECTED",
        "actorEmail": "kenji@example.com",
        "comment": "Missing receipt",
        "extractionStatus": None,
    }


def test_total_to_api() -> None:
    item = {
        "PK": "USER#u",
        "SK": "TOTAL#2026-10#JPY",
        "approvedAmount": Decimal("12345"),
        "approvedCount": Decimal(3),
    }

    assert total_to_api(item) == {"month": "2026-10", "currency": "JPY", "approvedAmount": 12345, "approvedCount": 3}


def test_category_enum_values_are_plain_strings() -> None:
    assert f"{Category.MEALS}" == "MEALS"
    assert keys.status_gsi2pk(ExpenseStatus.APPROVED) == "STATUS#APPROVED"
