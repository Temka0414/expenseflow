"""HTTP API: receipt upload authorization (presigned POST policy) and download (presigned GET URL)."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from expenseflow import ids, keys
from tests.builders import EMPLOYEE_ID, MANAGER_ID, expense_item, http_event
from tests.conftest import REGION, FakeLambdaContext

type Api = Callable[[dict[str, Any]], tuple[int, Any]]

MAX_RECEIPT_BYTES = 3_500_000  # MAX_RECEIPT_BYTES in the test environment


@pytest.fixture
def api(table: Any, receipts_bucket: str, lambda_context: FakeLambdaContext) -> Api:
    """Invoke the API Lambda handler; returns ``(statusCode, parsed JSON body or None)``."""
    from handlers.api.app import handler

    def call(event: dict[str, Any]) -> tuple[int, Any]:
        response = handler(event, lambda_context)
        return response["statusCode"], json.loads(response["body"]) if response.get("body") else None

    return call


def put_expense(table: Any, **overrides: Any) -> dict[str, Any]:
    item = expense_item(**overrides)
    table.put_item(Item=item)
    return item


def upload_request(expense_id: str, payload: dict[str, Any], **event_options: Any) -> dict[str, Any]:
    return http_event("POST", f"/expenses/{expense_id}/receipt-upload", body=payload, **event_options)


# --- Presigned POST --------------------------------------------------------------------------------------------


def test_upload_returns_a_presigned_post_policy_with_strict_conditions(
    api: Api, table: Any, receipts_bucket: str
) -> None:
    expense = put_expense(table)

    status, body = api(upload_request(expense["id"], {"contentType": "image/png", "size": 120_000}))

    assert status == 200
    key = body["key"]
    assert re.fullmatch(rf"receipts/{EMPLOYEE_ID}/{expense['id']}/[0-9A-Z]{{26}}\.png", key)
    assert body["expiresIn"] == 300
    assert body["maxBytes"] == MAX_RECEIPT_BYTES
    # SigV4, virtual-hosted style, regional endpoint (not the legacy global s3.amazonaws.com).
    assert body["url"] == f"https://{receipts_bucket}.s3.{REGION}.amazonaws.com/"
    fields = body["fields"]
    assert fields["key"] == key
    assert fields["Content-Type"] == "image/png"
    assert fields["x-amz-algorithm"] == "AWS4-HMAC-SHA256"

    policy = json.loads(base64.b64decode(fields["policy"]))
    conditions = policy["conditions"]
    assert {"bucket": receipts_bucket} in conditions
    assert {"key": key} in conditions
    assert {"Content-Type": "image/png"} in conditions  # exact match, not a starts-with prefix
    assert ["content-length-range", 1, MAX_RECEIPT_BYTES] in conditions
    expiration = datetime.fromisoformat(policy["expiration"])
    assert timedelta(seconds=290) < expiration - datetime.now(UTC) <= timedelta(seconds=300)


def test_upload_records_the_pending_key_without_bumping_the_version(api: Api, table: Any) -> None:
    expense = put_expense(table)

    _, body = api(upload_request(expense["id"], {"contentType": "application/pdf", "size": 1}))

    item = table.get_item(Key=keys.expense_key(expense["id"]))["Item"]
    assert item["pendingReceiptKey"] == body["key"]
    assert item["pendingReceiptContentType"] == "application/pdf"
    assert body["key"].endswith(".pdf")
    assert item["version"] == expense["version"]
    assert item["updatedAt"] == expense["updatedAt"]


def test_each_upload_gets_a_fresh_key(api: Api, table: Any) -> None:
    expense = put_expense(table)
    request = upload_request(expense["id"], {"contentType": "image/jpeg", "size": 10})

    first, second = api(request)[1]["key"], api(request)[1]["key"]

    assert first != second
    assert table.get_item(Key=keys.expense_key(expense["id"]))["Item"]["pendingReceiptKey"] == second


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"contentType": "text/html", "size": 10}, {(("body", "contentType"), "literal_error")}),
        ({"contentType": "image/png", "size": 0}, {(("body", "size"), "greater_than")}),
        ({"contentType": "image/png", "size": MAX_RECEIPT_BYTES + 1}, {(("body", "size"), "receipt_too_large")}),
        ({"contentType": "image/png"}, {(("body", "size"), "missing")}),
    ],
)
def test_upload_validates_type_and_size(
    api: Api, table: Any, payload: dict[str, Any], expected: set[tuple[tuple[str, ...], str]]
) -> None:
    expense = put_expense(table)

    status, body = api(upload_request(expense["id"], payload))

    assert status == 422
    assert {(tuple(error["loc"]), error["type"]) for error in body["detail"]} == expected


@pytest.mark.parametrize(
    ("overrides", "user_id", "expected"),
    [
        ({}, MANAGER_ID, 403),
        ({"status": "PENDING_APPROVAL"}, EMPLOYEE_ID, 409),
        ({"status": "REJECTED"}, EMPLOYEE_ID, 409),
    ],
)
def test_upload_requires_the_owner_and_a_draft(
    api: Api, table: Any, overrides: dict[str, Any], user_id: str, expected: int
) -> None:
    expense = put_expense(table, **overrides)

    status, _ = api(upload_request(expense["id"], {"contentType": "image/png", "size": 10}, user_id=user_id))

    assert status == expected
    assert "pendingReceiptKey" not in table.get_item(Key=keys.expense_key(expense["id"]))["Item"]


def test_upload_for_an_unknown_expense_is_404(api: Api) -> None:
    status, _ = api(upload_request(ids.new_id(), {"contentType": "image/png", "size": 10}))

    assert status == 404


# --- Presigned GET ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("user_id", "groups"), [(EMPLOYEE_ID, ("employees",)), (MANAGER_ID, ("managers",))])
def test_receipt_download_is_a_presigned_inline_get(
    api: Api, table: Any, receipts_bucket: str, user_id: str, groups: tuple[str, ...]
) -> None:
    expense_id = ids.new_id()
    receipt_key = keys.receipt_object_key(EMPLOYEE_ID, expense_id, ids.new_id(), ".jpg")
    put_expense(table, id=expense_id, receiptKey=receipt_key, receiptContentType="image/jpeg")

    status, body = api(http_event("GET", f"/expenses/{expense_id}/receipt", user_id=user_id, groups=groups))

    assert status == 200
    assert body["expiresIn"] == 300
    url = urlsplit(body["url"])
    assert url.netloc == f"{receipts_bucket}.s3.{REGION}.amazonaws.com"
    assert url.path == f"/{receipt_key}"
    query = parse_qs(url.query)
    assert query["response-content-disposition"] == [f'inline; filename="{receipt_key.rsplit("/", 1)[-1]}"']
    assert query["X-Amz-Expires"] == ["300"]


def test_receipt_download_is_forbidden_for_other_employees(api: Api, table: Any) -> None:
    expense = put_expense(table, receiptKey="receipts/x/y/z.jpg")

    status, _ = api(http_event("GET", f"/expenses/{expense['id']}/receipt", user_id="someone-else"))

    assert status == 403


def test_receipt_download_without_a_receipt_is_404(api: Api, table: Any) -> None:
    expense = put_expense(table, pendingReceiptKey="receipts/x/y/pending.jpg")  # uploaded but not processed yet

    status, body = api(http_event("GET", f"/expenses/{expense['id']}/receipt"))

    assert status == 404
    assert body == {"statusCode": 404, "message": "this expense has no receipt"}
