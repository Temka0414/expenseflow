"""Realtime push: audiences per event, delivery over ``@connections`` and clean-up on 410 Gone (SPEC 6, 7.3)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from boto3.dynamodb.conditions import Key
from botocore.stub import ANY, Stubber
from moto.apigatewaymanagementapi.models import apigatewaymanagementapi_backends

from expenseflow import aws, connections, keys, settings
from handlers import realtime_push
from tests.builders import ACCOUNT_ID, ADMIN_ID, EMPLOYEE_ID, MANAGER_ID, REGION, eventbridge_event
from tests.conftest import FakeLambdaContext

EXPENSE_ID = "01JA2B3C4D5E6F7G8H9JKMNPQR"


def submitted_event() -> dict[str, Any]:
    detail = {"expenseId": EXPENSE_ID, "ownerId": EMPLOYEE_ID, "status": "PENDING_APPROVAL", "amount": 1280}
    return eventbridge_event("Expense Submitted", "expenseflow.expenses", detail, time="2026-10-13T05:01:02Z")


def connect(connection_id: str, user_id: str, *groups: str) -> None:
    connections.register(connection_id, user_id, connections.audiences_for(user_id, groups))


def received(connection_id: str) -> bytes:
    """What moto's Management API recorded as sent to a connection."""
    backend = apigatewaymanagementapi_backends[ACCOUNT_ID][REGION]
    data: bytes = backend.connections[connection_id].data if connection_id in backend.connections else b""
    return data


def stored(table: Any, connection_id: str) -> int:
    response = table.query(KeyConditionExpression=Key(keys.PK).eq(keys.wsconn_pk(connection_id)))
    return len(response["Items"])


@pytest.mark.parametrize(
    ("detail_type", "detail", "expected"),
    [
        ("Expense Submitted", {"ownerId": "u1"}, ["USER#u1", "GROUP#managers"]),
        ("Expense Withdrawn", {"ownerId": "u1"}, ["USER#u1", "GROUP#managers"]),
        ("Expense Approved", {"ownerId": "u1"}, ["USER#u1"]),
        ("Expense Rejected", {"ownerId": "u1"}, ["USER#u1"]),
        ("Receipt Processed", {"ownerId": "u1"}, ["USER#u1"]),
        ("Approval Escalated", {"ownerId": "u1"}, ["USER#u1", "GROUP#managers", "GROUP#admins"]),
        ("Report Generated", {"requestedBy": "yuki@example.com"}, ["GROUP#admins"]),
    ],
)
def test_audiences(detail_type: str, detail: dict[str, Any], expected: list[str]) -> None:
    assert realtime_push.audiences(detail_type, detail) == expected


def test_event_reaches_owner_and_managers_but_nobody_else(table: Any, lambda_context: FakeLambdaContext) -> None:
    connect("owner-conn", EMPLOYEE_ID, "employees")
    connect("manager-conn", MANAGER_ID, "employees", "managers")
    connect("bystander-conn", "someone-else", "employees")

    result = realtime_push.handler(submitted_event(), lambda_context)

    assert result == {"delivered": 2, "gone": 0, "failed": 0}
    frame = json.loads(received("owner-conn"))
    assert frame == {
        "type": "event",
        "source": "expenseflow.expenses",
        "detailType": "Expense Submitted",
        "time": "2026-10-13T05:01:02Z",
        "detail": {"expenseId": EXPENSE_ID, "ownerId": EMPLOYEE_ID, "status": "PENDING_APPROVAL", "amount": 1280},
    }
    assert json.loads(received("manager-conn")) == frame
    assert received("bystander-conn") == b""


def test_a_connection_in_several_audiences_gets_the_event_once(table: Any, lambda_context: FakeLambdaContext) -> None:
    connect("admin-conn", ADMIN_ID, "managers", "admins")
    escalated = eventbridge_event("Approval Escalated", "expenseflow.approvals", {"ownerId": EMPLOYEE_ID})

    with Stubber(aws.apigw_management(settings.ws_callback_url())) as stub:
        stub.add_response("post_to_connection", {}, {"ConnectionId": "admin-conn", "Data": ANY})
        result = realtime_push.handler(escalated, lambda_context)
        stub.assert_no_pending_responses()

    assert result == {"delivered": 1, "gone": 0, "failed": 0}


def test_gone_connections_are_deleted_and_others_still_served(table: Any, lambda_context: FakeLambdaContext) -> None:
    connect("stale-conn", MANAGER_ID, "managers")
    connect("live-conn", EMPLOYEE_ID, "employees")

    with Stubber(aws.apigw_management(settings.ws_callback_url())) as stub:
        stub.add_response("post_to_connection", {}, {"ConnectionId": "live-conn", "Data": ANY})
        stub.add_client_error(
            "post_to_connection",
            service_error_code="GoneException",
            http_status_code=410,
            expected_params={"ConnectionId": "stale-conn", "Data": ANY},
        )
        result = realtime_push.handler(submitted_event(), lambda_context)

    assert result == {"delivered": 1, "gone": 1, "failed": 0}
    assert stored(table, "stale-conn") == 0
    assert stored(table, "live-conn") == 1


def test_other_push_errors_are_skipped_without_deleting(table: Any, lambda_context: FakeLambdaContext) -> None:
    connect("busy-conn", EMPLOYEE_ID, "employees")
    connect("manager-conn", MANAGER_ID, "managers")

    with Stubber(aws.apigw_management(settings.ws_callback_url())) as stub:
        stub.add_client_error("post_to_connection", service_error_code="LimitExceededException", http_status_code=429)
        stub.add_response("post_to_connection", {}, {"ConnectionId": "manager-conn", "Data": ANY})
        result = realtime_push.handler(submitted_event(), lambda_context)

    assert result == {"delivered": 1, "gone": 0, "failed": 1}
    assert stored(table, "busy-conn") == 1


def test_nobody_connected_is_a_no_op(table: Any, lambda_context: FakeLambdaContext) -> None:
    assert realtime_push.handler(submitted_event(), lambda_context) == {"delivered": 0, "gone": 0, "failed": 0}
