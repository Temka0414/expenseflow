"""HTTP API: resolver wiring - caller identity, error handling, observability - plus ``GET /me`` and ``GET /stats``."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from expenseflow import keys
from tests.builders import (
    ADMIN_ID,
    EMPLOYEE_EMAIL,
    EMPLOYEE_ID,
    MANAGER_ID,
    expense_item,
    http_event,
    id_token_claims,
)
from tests.conftest import FakeClock, FakeLambdaContext, UserPool

type Api = Callable[[dict[str, Any]], tuple[int, Any]]
type Records = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def api(lambda_context: FakeLambdaContext) -> Api:
    """Invoke the API Lambda handler; returns ``(statusCode, parsed JSON body or None)``."""
    from handlers.api.app import handler

    def call(event: dict[str, Any]) -> tuple[int, Any]:
        response = handler(event, lambda_context)
        return response["statusCode"], json.loads(response["body"]) if response.get("body") else None

    return call


@pytest.fixture
def log_records(monkeypatch: pytest.MonkeyPatch) -> Records:
    """The structured log records the function wrote so far.

    The Powertools Logger's stream handler keeps the ``sys.stdout`` object of the moment it was created (module
    import), so pytest's ``capsys`` cannot see it; the handler's stream is swapped for this test instead.
    """
    from handlers.api.app import logger

    stream = io.StringIO()
    monkeypatch.setattr(logger.registered_handler, "stream", stream)
    return lambda: [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture
def metric_documents(capsys: pytest.CaptureFixture[str]) -> Records:
    """The EMF documents printed so far (``log_metrics`` prints them to the current ``sys.stdout``)."""
    return lambda: [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]


@pytest.fixture
def metrics_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish EMF metrics to stdout for this test (conftest starts every test with an empty metric set)."""
    monkeypatch.setenv("POWERTOOLS_METRICS_DISABLED", "false")


def emitted_metrics(documents: list[dict[str, Any]]) -> list[tuple[str, dict[str, str]]]:
    """``(metric name, dimensions)`` of every metric in the EMF ``documents``."""
    found: list[tuple[str, dict[str, str]]] = []
    for document in documents:
        for directive in document["_aws"]["CloudWatchMetrics"]:
            assert directive["Namespace"] == "ExpenseFlow"
            dimensions = {name: document[name] for group in directive["Dimensions"] for name in group}
            found.extend((metric["Name"], dimensions) for metric in directive["Metrics"])
    return found


# --- GET /me: identity from the JWT claims ----------------------------------------------------------------------


def test_me_describes_the_caller_from_flattened_claims(api: Api) -> None:
    event = http_event("GET", "/me", email=EMPLOYEE_EMAIL, name="Hanako Sato", groups=("employees",))
    assert event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] == "[employees]"

    status, body = api(event)

    assert status == 200
    assert body == {
        "userId": EMPLOYEE_ID,
        "email": EMPLOYEE_EMAIL,
        "name": "Hanako Sato",
        "groups": ["employees"],
        "isManager": False,
        "isAdmin": False,
    }


def test_me_reports_manager_and_admin_roles(api: Api) -> None:
    status, body = api(http_event("GET", "/me", user_id=ADMIN_ID, groups=("managers", "admins")))

    assert status == 200
    assert (body["groups"], body["isManager"], body["isAdmin"]) == (["managers", "admins"], True, True)


def test_me_works_with_real_cognito_id_token_claims(api: Api, user_pool: UserPool) -> None:
    user = user_pool.create_user("aiko@example.com", groups=("employees", "managers"), name="Aiko Ito")

    status, body = api(http_event("GET", "/me", claims=user.id_claims))

    assert status == 200
    assert body["userId"] == user.sub
    assert body["email"] == "aiko@example.com"
    assert sorted(body["groups"]) == ["employees", "managers"]
    assert body["isManager"] is True


def test_claims_without_a_subject_are_401(api: Api) -> None:
    claims = {name: value for name, value in id_token_claims().items() if name != "sub"}

    status, body = api(http_event("GET", "/me", claims=claims))

    assert status == 401
    assert body == {"statusCode": 401, "message": "token has no subject (sub) claim"}


# --- Error handling ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/unknown"), ("PUT", "/expenses"), ("GET", "/expenses/")],
)
def test_unknown_routes_are_404_json(api: Api, method: str, path: str) -> None:
    status, body = api(http_event(method, path))

    assert status == 404
    assert body == {"statusCode": 404, "message": "Not found"}


def test_unexpected_exceptions_are_logged_and_answered_with_a_500_json(api: Api, log_records: Records) -> None:
    # No `table` fixture: DynamoDB answers ResourceNotFoundException, which no route handles.
    status, body = api(http_event("GET", "/expenses"))

    assert status == 500
    assert body == {"statusCode": 500, "message": "Internal server error"}
    errors = [record for record in log_records() if record["level"] == "ERROR"]
    assert [record["message"] for record in errors] == ["Unhandled error"]
    assert "ResourceNotFoundException" in errors[0]["exception"]


def test_invalid_json_is_422(api: Api) -> None:
    status, body = api(http_event("POST", "/expenses", body='{"title": "Taxi",'))

    assert status == 422
    assert body["statusCode"] == 422
    assert [error["type"] for error in body["detail"]] == ["json_invalid"]


def test_missing_body_is_422(api: Api) -> None:
    status, body = api(http_event("POST", "/expenses"))

    assert status == 422
    assert body == {"statusCode": 422, "detail": [{"loc": ["body"], "type": "missing"}]}


def test_non_json_content_type_is_415(api: Api) -> None:
    event = http_event("POST", "/expenses", body="title=Taxi", headers={"content-type": "text/plain"})

    status, body = api(event)

    assert status == 415
    assert body == {"statusCode": 415, "detail": [{"loc": ["body"], "type": "unsupported_content_type"}]}


def test_permission_errors_from_dependencies_keep_their_status(api: Api) -> None:
    status, body = api(
        http_event("POST", "/reports", body={"month": "2026-01"}, user_id=MANAGER_ID, groups=("managers",))
    )

    assert status == 403
    assert body == {"statusCode": 403, "message": "only admins can do this"}


# --- Observability ----------------------------------------------------------------------------------------------


def test_logs_carry_the_caller_and_the_request_id(api: Api, table: Any, log_records: Records) -> None:
    event = http_event("GET", f"/expenses/{expense_item()['id']}")

    status, _ = api(event)

    assert status == 404
    [record] = [record for record in log_records() if record["message"] == "Request rejected"]
    assert record["user_id"] == EMPLOYEE_ID
    assert record["correlation_id"] == event["requestContext"]["requestId"]
    assert record["status_code"] == 404


def test_the_previous_caller_is_not_logged_by_the_next_invocation(api: Api, log_records: Records) -> None:
    api(http_event("GET", "/me", user_id=MANAGER_ID, groups=("managers",)))
    anonymous = {name: value for name, value in id_token_claims().items() if name != "sub"}

    api(http_event("GET", "/me", claims=anonymous))

    [record] = [record for record in log_records() if record["message"] == "Request rejected"]
    assert "user_id" not in record


@pytest.mark.usefixtures("metrics_enabled")
def test_business_metrics_are_published_per_request(
    api: Api, table: Any, receipts_bucket: str, metric_documents: Records
) -> None:
    _, created = api(
        http_event(
            "POST",
            "/expenses",
            body={
                "title": "Taxi",
                "merchant": "Nihon Kotsu",
                "amount": 1280,
                "category": "TRANSPORT",
                "expenseDate": "2026-10-09",
            },
        )
    )
    api(http_event("POST", f"/expenses/{created['id']}/receipt-upload", body={"contentType": "image/png", "size": 5}))
    api(http_event("POST", f"/expenses/{created['id']}/submit"))
    table.update_item(
        Key=keys.expense_key(created["id"]),
        UpdateExpression="SET approvalTaskToken = :token",
        ExpressionAttributeValues={":token": "task-token"},
    )
    api(
        http_event(
            "POST",
            f"/approvals/{created['id']}/decision",
            body={"decision": "APPROVED"},
            user_id=MANAGER_ID,
            groups=("managers",),
        )
    )

    service = {"service": "expenseflow-test"}
    assert emitted_metrics(metric_documents()) == [
        ("ExpenseCreated", service),
        ("ReceiptUploadRequested", service),
        ("ExpenseSubmitted", service),
        ("ApprovalDecision", {"decision": "APPROVED", **service}),
    ]


# --- GET /stats -------------------------------------------------------------------------------------------------


def put_total(table: Any, partition: str, month: str, currency: str, amount: str, count: int) -> None:
    table.put_item(
        Item={
            keys.PK: partition,
            keys.SK: keys.total_sk(month, currency),
            "entityType": keys.ENTITY_MONTHLY_TOTAL,
            "approvedAmount": Decimal(amount),
            "approvedCount": Decimal(count),
        }
    )


@pytest.fixture
def totals(table: Any) -> None:
    put_total(table, keys.user_total_pk(EMPLOYEE_ID), "2026-09", "JPY", "12345", 3)
    put_total(table, keys.user_total_pk(EMPLOYEE_ID), "2026-10", "USD", "12.50", 1)
    put_total(table, keys.user_total_pk(EMPLOYEE_ID), "2025-12", "JPY", "999", 1)
    put_total(table, keys.ORG_PK, "2026-09", "JPY", "50000", 7)


@pytest.mark.usefixtures("totals")
def test_stats_for_an_employee_cover_only_their_own_totals(api: Api, clock: FakeClock) -> None:
    status, body = api(http_event("GET", "/stats"))

    assert status == 200
    assert body == {
        "year": 2026,  # the current year in Japan
        "mine": [
            {"month": "2026-09", "currency": "JPY", "approvedAmount": 12345, "approvedCount": 3},
            {"month": "2026-10", "currency": "USD", "approvedAmount": 12.5, "approvedCount": 1},
        ],
        "org": None,
    }


@pytest.mark.usefixtures("totals")
def test_stats_for_managers_include_the_organisation(api: Api) -> None:
    status, body = api(http_event("GET", "/stats", query={"year": "2026"}, user_id=MANAGER_ID, groups=("managers",)))

    assert status == 200
    assert body["mine"] == []
    assert body["org"] == [{"month": "2026-09", "currency": "JPY", "approvedAmount": 50000, "approvedCount": 7}]


@pytest.mark.usefixtures("totals")
def test_stats_for_another_year(api: Api) -> None:
    status, body = api(http_event("GET", "/stats", query={"year": "2025"}))

    assert status == 200
    assert body["mine"] == [{"month": "2025-12", "currency": "JPY", "approvedAmount": 999, "approvedCount": 1}]


def test_stats_validates_the_year(api: Api) -> None:
    status, body = api(http_event("GET", "/stats", query={"year": "26"}))

    assert status == 422
    assert body["detail"] == [{"loc": ["query", "year"], "type": "greater_than_equal"}]
