"""HTTP API: expense CRUD, listing/pagination and the owner's submit/withdraw transitions (SPEC section 5)."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.stub import Stubber

from expenseflow import aws, ids, keys
from tests.builders import (
    ADMIN_ID,
    EMPLOYEE_EMAIL,
    EMPLOYEE_ID,
    MANAGER_EMAIL,
    MANAGER_ID,
    expense_item,
    http_event,
)
from tests.conftest import FakeClock, FakeLambdaContext

type Api = Callable[[dict[str, Any]], tuple[int, Any]]

OTHER_EMPLOYEE_ID = "9d4f6b8a-1c3e-4a5b-8d7f-0e2a4c6e8f04"
REJECTED_ROUND = {
    "approverId": MANAGER_ID,
    "approverEmail": MANAGER_EMAIL,
    "decisionComment": "Missing receipt",
    "decidedAt": "2026-10-11T00:00:00.000Z",
    "autoApproved": False,
}


@pytest.fixture
def api(table: Any, clock: FakeClock, lambda_context: FakeLambdaContext) -> Api:
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


def stored(table: Any, expense_id: str) -> dict[str, Any]:
    item: dict[str, Any] = table.get_item(Key=keys.expense_key(expense_id))["Item"]
    return item


def error_locations(body: dict[str, Any]) -> set[tuple[tuple[Any, ...], str]]:
    return {(tuple(error["loc"]), error["type"]) for error in body["detail"]}


# --- Create ----------------------------------------------------------------------------------------------------


def test_create_returns_201_with_the_new_draft(api: Api, table: Any) -> None:
    payload = {"title": "  Taxi  ", "amount": 1280.5, "category": "TRANSPORT", "expenseDate": "2026-10-09"}

    status, body = api(http_event("POST", "/expenses", body=payload, email=EMPLOYEE_EMAIL, name="Hanako"))

    assert status == 201
    assert body["status"] == "DRAFT"
    assert body["title"] == "Taxi"
    assert body["amount"] == 1280.5
    assert body["currency"] == "JPY"  # DEFAULT_CURRENCY
    assert body["version"] == 1
    assert (body["ownerId"], body["ownerEmail"], body["ownerName"]) == (EMPLOYEE_ID, EMPLOYEE_EMAIL, "Hanako")
    assert body["merchant"] is None
    assert body["receipt"] is None
    assert body["pendingUpload"] is False
    item = stored(table, body["id"])
    assert item["amount"] == Decimal("1280.5")
    assert "merchant" not in item  # optional attributes are absent, never null


def test_create_rejects_invalid_fields_with_422(api: Api) -> None:
    payload = {"title": "", "amount": -5, "currency": "XXX", "expenseDte": "2026-10-01"}

    status, body = api(http_event("POST", "/expenses", body=payload))

    assert status == 422
    assert body["statusCode"] == 422
    assert error_locations(body) == {
        (("body", "title"), "string_too_short"),
        (("body", "amount"), "greater_than"),
        (("body", "currency"), "literal_error"),
        (("body", "expenseDte"), "extra_forbidden"),
    }
    assert all(set(error) == {"loc", "type"} for error in body["detail"])  # no messages echoing the input


@pytest.mark.parametrize(
    ("amount", "error_type"),
    [(100_000_001, "less_than_equal"), (12.345, "decimal_max_places")],
)
def test_create_validates_amount_limits(api: Api, amount: float, error_type: str) -> None:
    status, body = api(http_event("POST", "/expenses", body={"title": "Hotel", "amount": amount}))

    assert status == 422
    assert error_locations(body) == {(("body", "amount"), error_type)}


def test_create_rejects_expense_dates_in_the_future(api: Api) -> None:
    status, body = api(http_event("POST", "/expenses", body={"title": "Hotel", "expenseDate": "2026-10-20"}))

    assert status == 422
    assert error_locations(body) == {(("body", "expenseDate"), "date_out_of_range")}


# --- List ------------------------------------------------------------------------------------------------------


def test_list_returns_my_expenses_newest_first_in_pages(api: Api, table: Any) -> None:
    created = [api(http_event("POST", "/expenses", body={"title": f"Expense {n}"}))[1]["id"] for n in range(3)]
    put_expense(table, ownerId=OTHER_EMPLOYEE_ID)

    status, first = api(http_event("GET", "/expenses", query={"limit": "2"}))

    assert status == 200
    assert [item["id"] for item in first["items"]] == [created[2], created[1]]
    assert first["nextCursor"]

    status, second = api(http_event("GET", "/expenses", query={"limit": "2", "cursor": first["nextCursor"]}))

    assert status == 200
    assert [item["id"] for item in second["items"]] == [created[0]]
    assert second["nextCursor"] is None


def test_list_filters_by_status(api: Api, table: Any) -> None:
    put_expense(table, status="DRAFT")
    pending = put_expense(table, status="PENDING_APPROVAL")

    status, body = api(http_event("GET", "/expenses", query={"status": "PENDING_APPROVAL"}))

    assert status == 200
    assert [item["id"] for item in body["items"]] == [pending["id"]]


def test_list_validates_query_parameters(api: Api) -> None:
    status, body = api(http_event("GET", "/expenses", query={"status": "PAID", "limit": "0"}))

    assert status == 422
    assert error_locations(body) == {(("query", "status"), "enum"), (("query", "limit"), "greater_than_equal")}


def test_list_rejects_a_tampered_cursor_with_400(api: Api) -> None:
    status, body = api(http_event("GET", "/expenses", query={"cursor": "bm90LWEta2V5"}))

    assert status == 400
    assert body == {"statusCode": 400, "message": "invalid cursor"}


# --- Get -------------------------------------------------------------------------------------------------------


def test_get_returns_the_expense_with_its_history(api: Api, table: Any) -> None:
    expense = put_expense(table, status="PENDING_APPROVAL", approvalTaskToken="secret-token")
    table.put_item(
        Item={
            keys.PK: keys.expense_pk(expense["id"]),
            keys.SK: keys.history_sk_status("2026-10-12T00:00:00.000Z", "PENDING_APPROVAL"),
            "kind": "STATUS",
            "from": "DRAFT",
            "to": "PENDING_APPROVAL",
            "actorId": EMPLOYEE_ID,
            "actorEmail": EMPLOYEE_EMAIL,
            "at": "2026-10-12T00:00:00.000Z",
        }
    )

    status, body = api(http_event("GET", f"/expenses/{expense['id']}"))

    assert status == 200
    assert body["expense"]["id"] == expense["id"]
    assert "approvalTaskToken" not in body["expense"]
    assert not {keys.PK, keys.SK, keys.GSI1PK, keys.GSI2PK} & body["expense"].keys()
    assert body["history"] == [
        {
            "at": "2026-10-12T00:00:00.000Z",
            "kind": "STATUS",
            "from": "DRAFT",
            "to": "PENDING_APPROVAL",
            "actorEmail": EMPLOYEE_EMAIL,
            "comment": None,
            "extractionStatus": None,
        }
    ]


@pytest.mark.parametrize(
    ("user_id", "groups", "expected"),
    [
        (MANAGER_ID, ("managers",), 200),
        (ADMIN_ID, ("admins",), 200),
        (OTHER_EMPLOYEE_ID, ("employees",), 403),
    ],
)
def test_get_is_limited_to_owner_managers_and_admins(
    api: Api, table: Any, user_id: str, groups: tuple[str, ...], expected: int
) -> None:
    expense = put_expense(table)

    status, _ = api(http_event("GET", f"/expenses/{expense['id']}", user_id=user_id, groups=groups))

    assert status == expected


def test_get_unknown_expense_is_404(api: Api, table: Any) -> None:
    expense_id = expense_item()["id"]

    status, body = api(http_event("GET", f"/expenses/{expense_id}"))

    assert status == 404
    assert body == {"statusCode": 404, "message": f"expense {expense_id} not found"}


def test_get_rejects_ids_that_are_not_ulids(api: Api) -> None:
    status, body = api(http_event("GET", "/expenses/EXPENSE%23META"))

    assert status == 422
    assert error_locations(body) == {(("path", "expense_id"), "string_pattern_mismatch")}


def test_amounts_are_json_numbers_not_strings(api: Api, table: Any) -> None:
    fractional = put_expense(table, amount=Decimal("12.50"), currency="USD")
    integral = put_expense(table, amount=Decimal("1280"))

    _, fractional_body = api(http_event("GET", f"/expenses/{fractional['id']}"))
    _, integral_body = api(http_event("GET", f"/expenses/{integral['id']}"))

    assert fractional_body["expense"]["amount"] == 12.5
    assert integral_body["expense"]["amount"] == 1280
    assert isinstance(integral_body["expense"]["amount"], int)
    assert isinstance(integral_body["expense"]["version"], int)


# --- Update ----------------------------------------------------------------------------------------------------


def test_patch_sets_and_removes_fields(api: Api, table: Any) -> None:
    expense = put_expense(table)
    patch = {"merchant": "MK Taxi", "description": None, "category": "", "version": 1}

    status, body = api(http_event("PATCH", f"/expenses/{expense['id']}", body=patch))

    assert status == 200
    assert body["merchant"] == "MK Taxi"
    assert body["description"] is None
    assert body["category"] is None
    assert body["version"] == 2
    item = stored(table, expense["id"])
    assert "description" not in item
    assert "category" not in item


def test_patch_with_a_stale_version_is_409(api: Api, table: Any) -> None:
    expense = put_expense(table, version=Decimal(3))

    status, body = api(http_event("PATCH", f"/expenses/{expense['id']}", body={"title": "New", "version": 2}))

    assert status == 409
    assert "version 3" in body["message"]
    assert stored(table, expense["id"])["title"] == expense["title"]


def test_patch_reopens_a_rejected_expense_as_draft(api: Api, table: Any) -> None:
    expense = put_expense(table, status="REJECTED", **REJECTED_ROUND)

    status, body = api(http_event("PATCH", f"/expenses/{expense['id']}", body={"amount": 1500}))

    assert status == 200
    assert body["status"] == "DRAFT"
    assert body["amount"] == 1500
    assert body["approval"] is None
    assert not REJECTED_ROUND.keys() & stored(table, expense["id"]).keys()


def test_patch_cannot_remove_the_title(api: Api, table: Any) -> None:
    expense = put_expense(table)

    status, body = api(http_event("PATCH", f"/expenses/{expense['id']}", body={"title": None}))

    assert status == 422
    assert error_locations(body) == {(("body", "title"), "not_removable")}


@pytest.mark.parametrize(
    ("overrides", "user_id", "expected"),
    [
        ({}, OTHER_EMPLOYEE_ID, 403),
        ({"status": "PENDING_APPROVAL"}, EMPLOYEE_ID, 409),
        ({"status": "APPROVED"}, EMPLOYEE_ID, 409),
    ],
)
def test_patch_requires_the_owner_and_an_editable_status(
    api: Api, table: Any, overrides: dict[str, Any], user_id: str, expected: int
) -> None:
    expense = put_expense(table, **overrides)

    status, _ = api(http_event("PATCH", f"/expenses/{expense['id']}", body={"title": "New"}, user_id=user_id))

    assert status == expected


# --- Delete ----------------------------------------------------------------------------------------------------


def test_delete_removes_the_item_collection_and_receipt_objects(
    table: Any, receipts_bucket: str, clock: FakeClock, lambda_context: FakeLambdaContext
) -> None:
    from handlers.api.app import handler

    expense_id = ids.new_id()
    receipt_key = keys.receipt_object_key(EMPLOYEE_ID, expense_id, ids.new_id(), ".pdf")
    pending_key = keys.receipt_object_key(EMPLOYEE_ID, expense_id, ids.new_id(), ".png")
    expense = put_expense(table, id=expense_id, receiptKey=receipt_key, pendingReceiptKey=pending_key)
    table.put_item(
        Item={keys.PK: keys.expense_pk(expense_id), keys.SK: keys.history_sk_receipt("2026-10-10T00:00:00.000Z")}
    )
    s3 = boto3.client("s3")
    for key in (receipt_key, pending_key, "receipts/unrelated.pdf"):
        s3.put_object(Bucket=receipts_bucket, Key=key, Body=b"%PDF-1.7")

    response = handler(http_event("DELETE", f"/expenses/{expense['id']}"), lambda_context)

    assert response["statusCode"] == 204
    assert not response.get("body")
    assert "Content-Type" not in response.get("headers", {})
    assert table.query(KeyConditionExpression=Key(keys.PK).eq(keys.expense_pk(expense_id)))["Items"] == []
    remaining = [obj["Key"] for obj in s3.list_objects_v2(Bucket=receipts_bucket)["Contents"]]
    assert remaining == ["receipts/unrelated.pdf"]


@pytest.mark.parametrize(
    ("overrides", "user_id", "expected"),
    [({}, OTHER_EMPLOYEE_ID, 403), ({"status": "PENDING_APPROVAL"}, EMPLOYEE_ID, 409)],
)
def test_delete_requires_the_owner_and_a_draft(
    api: Api, table: Any, overrides: dict[str, Any], user_id: str, expected: int
) -> None:
    expense = put_expense(table, **overrides)

    status, _ = api(http_event("DELETE", f"/expenses/{expense['id']}", user_id=user_id))

    assert status == expected
    assert stored(table, expense["id"])["id"] == expense["id"]


# --- Submit ----------------------------------------------------------------------------------------------------


def test_submit_moves_a_complete_draft_to_pending_approval(api: Api, table: Any) -> None:
    expense = put_expense(table, **REJECTED_ROUND, workflowExecutionArn="arn:aws:states:old")

    status, body = api(http_event("POST", f"/expenses/{expense['id']}/submit"))

    assert status == 200
    assert body["status"] == "PENDING_APPROVAL"
    assert body["submittedAt"] == body["updatedAt"]
    assert body["version"] == 2
    assert body["approval"] is None
    assert body["workflowExecutionArn"] is None
    assert stored(table, expense["id"])[keys.GSI2PK] == "STATUS#PENDING_APPROVAL"


def test_submit_without_required_fields_is_400(api: Api, table: Any) -> None:
    expense = put_expense(table, merchant=None, category=None)

    status, body = api(http_event("POST", f"/expenses/{expense['id']}/submit"))

    assert status == 400
    assert body == {"statusCode": 400, "message": "cannot submit: missing or invalid merchant, category"}


@pytest.mark.parametrize(
    ("overrides", "user_id", "expected"),
    [
        ({}, OTHER_EMPLOYEE_ID, 403),
        ({"status": "PENDING_APPROVAL"}, EMPLOYEE_ID, 409),
        ({"status": "REJECTED"}, EMPLOYEE_ID, 409),
    ],
)
def test_submit_requires_the_owner_and_a_draft(
    api: Api, table: Any, overrides: dict[str, Any], user_id: str, expected: int
) -> None:
    expense = put_expense(table, **overrides)

    status, _ = api(http_event("POST", f"/expenses/{expense['id']}/submit", user_id=user_id))

    assert status == expected


# --- Withdraw --------------------------------------------------------------------------------------------------


def test_withdraw_returns_to_draft_and_stops_the_workflow(api: Api, table: Any, approval_state_machine: str) -> None:
    sfn = boto3.client("stepfunctions")
    execution_arn = sfn.start_execution(stateMachineArn=approval_state_machine, input="{}")["executionArn"]
    expense = put_expense(
        table,
        status="PENDING_APPROVAL",
        workflowExecutionArn=execution_arn,
        approvalTaskToken="token-1",
        approvalRequestedAt="2026-10-12T00:00:00.000Z",
        escalated=True,
    )

    status, body = api(http_event("POST", f"/expenses/{expense['id']}/withdraw"))

    assert status == 200
    assert body["status"] == "DRAFT"
    assert body["version"] == 2
    assert body["workflowExecutionArn"] is None
    assert body["approval"] is None
    assert sfn.describe_execution(executionArn=execution_arn)["status"] == "ABORTED"
    item = stored(table, expense["id"])
    assert not {"approvalTaskToken", "workflowExecutionArn", "escalated"} & item.keys()
    assert body["updatedAt"] == item["updatedAt"]


EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:ApprovalWorkflow-test:finished-long-ago"


@pytest.mark.parametrize(("error_code", "expected"), [("ExecutionDoesNotExist", 200), ("AccessDeniedException", 500)])
def test_withdraw_ignores_only_an_execution_that_no_longer_exists(
    api: Api, table: Any, error_code: str, expected: int
) -> None:
    expense = put_expense(table, status="PENDING_APPROVAL", workflowExecutionArn=EXECUTION_ARN)

    with Stubber(aws.client("stepfunctions")) as stubber:
        stubber.add_client_error(
            "stop_execution",
            service_error_code=error_code,
            http_status_code=400,
            expected_params={
                "executionArn": EXECUTION_ARN,
                "error": "Withdrawn",
                "cause": "The owner withdrew the expense",
            },
        )
        status, _ = api(http_event("POST", f"/expenses/{expense['id']}/withdraw"))
        stubber.assert_no_pending_responses()

    assert status == expected
    assert stored(table, expense["id"])["status"] == "DRAFT"  # the withdrawal itself is already committed


def test_withdraw_before_the_workflow_claimed_the_expense_calls_no_workflow(api: Api, table: Any) -> None:
    expense = put_expense(table, status="PENDING_APPROVAL")

    with Stubber(aws.client("stepfunctions")) as stubber:  # no responses queued: any call would fail the test
        status, body = api(http_event("POST", f"/expenses/{expense['id']}/withdraw"))
        stubber.assert_no_pending_responses()

    assert status == 200
    assert body["status"] == "DRAFT"


@pytest.mark.parametrize(
    ("overrides", "user_id", "expected"),
    [({"status": "PENDING_APPROVAL"}, MANAGER_ID, 403), ({}, EMPLOYEE_ID, 409)],
)
def test_withdraw_requires_the_owner_and_a_pending_expense(
    api: Api, table: Any, overrides: dict[str, Any], user_id: str, expected: int
) -> None:
    expense = put_expense(table, **overrides)

    status, _ = api(http_event("POST", f"/expenses/{expense['id']}/withdraw", user_id=user_id))

    assert status == expected
