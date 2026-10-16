"""HTTP API: the approval queue and decisions (``SendTaskSuccess`` resumes the waiting ApprovalWorkflow).

moto accepts any task token and does nothing, so the token handling is asserted with a botocore ``Stubber`` on
the very client the API uses (``expenseflow.aws.client("stepfunctions")`` is cached).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from botocore.stub import Stubber

from expenseflow import aws, keys
from tests.builders import ADMIN_EMAIL, ADMIN_ID, EMPLOYEE_ID, MANAGER_EMAIL, MANAGER_ID, expense_item, http_event
from tests.conftest import FakeLambdaContext

type Api = Callable[[dict[str, Any]], tuple[int, Any]]

MANAGER: dict[str, Any] = {"user_id": MANAGER_ID, "email": MANAGER_EMAIL, "groups": ("managers", "employees")}
ADMIN: dict[str, Any] = {"user_id": ADMIN_ID, "email": ADMIN_EMAIL, "groups": ("admins",)}
TOKEN = "AQCEAAAAKgAAAAMAAAAAAAAAAeMbP+example-task-token"


@pytest.fixture
def api(table: Any, lambda_context: FakeLambdaContext) -> Api:
    """Invoke the API Lambda handler; returns ``(statusCode, parsed JSON body or None)``."""
    from handlers.api.app import handler

    def call(event: dict[str, Any]) -> tuple[int, Any]:
        response = handler(event, lambda_context)
        return response["statusCode"], json.loads(response["body"]) if response.get("body") else None

    return call


@pytest.fixture
def sfn_stub() -> Iterator[Stubber]:
    with Stubber(aws.client("stepfunctions")) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def put_expense(table: Any, **overrides: Any) -> dict[str, Any]:
    item = expense_item(**overrides)
    table.put_item(Item=item)
    return item


def put_pending(table: Any, **overrides: Any) -> dict[str, Any]:
    """A submitted expense whose workflow is waiting for a decision (task token stored)."""
    return put_expense(table, status="PENDING_APPROVAL", approvalTaskToken=TOKEN, **overrides)


def decision_request(expense_id: str, payload: dict[str, Any], caller: dict[str, Any] = MANAGER) -> dict[str, Any]:
    return http_event("POST", f"/approvals/{expense_id}/decision", body=payload, **caller)


def task_output(decision: str, comment: str | None, approver: dict[str, Any] = MANAGER) -> str:
    output = {"decision": decision, "approverId": approver["user_id"], "approverEmail": approver["email"]}
    return json.dumps({**output, "comment": comment}, separators=(",", ":"))


# --- Queue -----------------------------------------------------------------------------------------------------


def test_queue_lists_pending_expenses_oldest_first_without_my_own(api: Api, table: Any) -> None:
    newer = put_pending(table, updatedAt="2026-10-12T00:00:00.000Z")
    older = put_pending(table, updatedAt="2026-10-11T00:00:00.000Z")
    put_pending(table, ownerId=MANAGER_ID)  # the manager's own expense
    put_expense(table, status="DRAFT")

    status, body = api(http_event("GET", "/approvals", **MANAGER))

    assert status == 200
    assert [item["id"] for item in body["items"]] == [older["id"], newer["id"]]
    assert body["nextCursor"] is None
    assert all("approvalTaskToken" not in item for item in body["items"])


def test_queue_pages_with_a_cursor(api: Api, table: Any) -> None:
    first = put_pending(table, updatedAt="2026-10-11T00:00:00.000Z")
    second = put_pending(table, updatedAt="2026-10-12T00:00:00.000Z")

    _, page_one = api(http_event("GET", "/approvals", query={"limit": "1"}, **ADMIN))
    _, page_two = api(http_event("GET", "/approvals", query={"limit": "1", "cursor": page_one["nextCursor"]}, **ADMIN))

    assert [item["id"] for item in page_one["items"]] == [first["id"]]
    assert [item["id"] for item in page_two["items"]] == [second["id"]]


def test_queue_is_forbidden_for_employees(api: Api) -> None:
    status, body = api(http_event("GET", "/approvals", groups=("employees",)))

    assert status == 403
    assert body == {"statusCode": 403, "message": "only managers and admins can do this"}


def test_queue_rejects_a_cursor_from_another_listing(api: Api, table: Any) -> None:
    for _ in range(2):
        put_expense(table)
    _, my_expenses = api(http_event("GET", "/expenses", query={"limit": "1"}))

    status, _ = api(http_event("GET", "/approvals", query={"cursor": my_expenses["nextCursor"]}, **MANAGER))

    assert status == 400


# --- Decision --------------------------------------------------------------------------------------------------


def test_approval_resumes_the_workflow_and_answers_202(api: Api, table: Any, sfn_stub: Stubber) -> None:
    expense = put_pending(table)
    sfn_stub.add_response("send_task_success", {}, {"taskToken": TOKEN, "output": task_output("APPROVED", None)})

    status, body = api(decision_request(expense["id"], {"decision": "APPROVED"}))

    assert status == 202
    assert body == {"accepted": True}


def test_rejection_by_an_admin_carries_the_comment(api: Api, table: Any, sfn_stub: Stubber) -> None:
    expense = put_pending(table)
    output = task_output("REJECTED", "Please attach the receipt", approver=ADMIN)
    sfn_stub.add_response("send_task_success", {}, {"taskToken": TOKEN, "output": output})

    payload = {"decision": "REJECTED", "comment": "  Please attach the receipt  "}
    status, _ = api(decision_request(expense["id"], payload, caller=ADMIN))

    assert status == 202


def test_decision_leaves_the_status_to_the_workflow(api: Api, table: Any) -> None:
    expense = put_pending(table)

    api(decision_request(expense["id"], {"decision": "APPROVED"}))  # moto accepts any token

    assert table.get_item(Key=keys.expense_key(expense["id"]))["Item"]["status"] == "PENDING_APPROVAL"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"decision": "REJECTED"}, {(("body",), "comment_required")}),
        ({"decision": "REJECTED", "comment": "   "}, {(("body",), "comment_required")}),
        ({"decision": "MAYBE"}, {(("body", "decision"), "literal_error")}),
        ({"decision": "APPROVED", "comment": "x" * 501}, {(("body", "comment"), "string_too_long")}),
    ],
)
def test_decision_body_is_validated(
    api: Api, table: Any, payload: dict[str, Any], expected: set[tuple[tuple[str, ...], str]]
) -> None:
    expense = put_pending(table)

    status, body = api(decision_request(expense["id"], payload))

    assert status == 422
    assert {(tuple(error["loc"]), error["type"]) for error in body["detail"]} == expected


def test_employees_cannot_decide(api: Api, table: Any) -> None:
    expense = put_pending(table)

    status, _ = api(decision_request(expense["id"], {"decision": "APPROVED"}, caller={"user_id": "someone"}))

    assert status == 403


def test_nobody_decides_on_their_own_expense(api: Api, table: Any, sfn_stub: Stubber) -> None:
    expense = put_pending(table, ownerId=MANAGER_ID)

    status, body = api(decision_request(expense["id"], {"decision": "APPROVED"}))

    assert status == 403
    assert body["message"] == "you cannot decide on your own expense"


def test_decision_on_an_unknown_expense_is_404(api: Api, table: Any) -> None:
    status, _ = api(decision_request(expense_item()["id"], {"decision": "APPROVED"}))

    assert status == 404


@pytest.mark.parametrize("status_value", ["DRAFT", "APPROVED", "REJECTED"])
def test_decision_requires_a_pending_expense(api: Api, table: Any, sfn_stub: Stubber, status_value: str) -> None:
    expense = put_expense(table, status=status_value, approvalTaskToken=TOKEN)

    status, body = api(decision_request(expense["id"], {"decision": "APPROVED"}))

    assert status == 409
    assert body["message"] == f"expense is {status_value}, not PENDING_APPROVAL"


def test_decision_before_the_task_token_is_stored_is_409(api: Api, table: Any, sfn_stub: Stubber) -> None:
    expense = put_expense(table, status="PENDING_APPROVAL")

    status, body = api(decision_request(expense["id"], {"decision": "APPROVED"}))

    assert status == 409
    assert body == {"statusCode": 409, "message": "approval request not ready yet"}


@pytest.mark.parametrize("error_code", ["TaskTimedOut", "InvalidToken", "TaskDoesNotExist"])
def test_an_inactive_task_token_is_409(api: Api, table: Any, sfn_stub: Stubber, error_code: str) -> None:
    expense = put_pending(table)
    sfn_stub.add_client_error("send_task_success", service_error_code=error_code, http_status_code=400)

    status, body = api(decision_request(expense["id"], {"decision": "APPROVED"}))

    assert status == 409
    assert body["message"] == "this approval request is no longer active; reload the expense"


def test_other_step_functions_errors_are_500(api: Api, table: Any, sfn_stub: Stubber) -> None:
    expense = put_pending(table)
    sfn_stub.add_client_error("send_task_success", service_error_code="AccessDeniedException", http_status_code=400)

    status, body = api(decision_request(expense["id"], {"decision": "APPROVED"}))

    assert status == 500
    assert body == {"statusCode": 500, "message": "Internal server error"}


def test_owner_of_the_expense_is_identified_by_sub_not_email(api: Api, table: Any) -> None:
    expense = put_pending(table, ownerId=EMPLOYEE_ID, ownerEmail=MANAGER_EMAIL)

    status, _ = api(decision_request(expense["id"], {"decision": "APPROVED"}))

    assert status == 202
