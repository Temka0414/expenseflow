"""Notifier: recipients per event, SES v2 sending, sandbox tolerance and Powertools idempotency (SPEC 7.2)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from moto.ses.models import ses_backends

from expenseflow import aws, notify
from handlers import notifier
from tests.builders import ACCOUNT_ID, MANAGER_EMAIL, MANAGER_ID, REGION, eventbridge_event, sqs_event, sqs_message_id
from tests.conftest import CognitoUser, FakeLambdaContext, UserPool

SENDER = "noreply@example.com"
APP_URL = "https://d1234abcd.cloudfront.net"  # APP_URL in the test environment
EXPENSE_ID = "01JA2B3C4D5E6F7G8H9JKMNPQR"
SUBMITTED_AT = "2026-10-11T00:00:00.000Z"
DECIDED_AT = "2026-10-12T03:04:05.678Z"
# The harness disables idempotency by default (opt in below), and Powertools warns about that on every call.
pytestmark = pytest.mark.filterwarnings("ignore:Disabling idempotency:UserWarning")


@dataclass(frozen=True)
class People:
    owner: CognitoUser  # an employee who is also a manager: never asked to approve their own expense
    manager: CognitoUser
    admin: CognitoUser


@pytest.fixture(autouse=True)
def _fresh_idempotency_layer() -> Iterator[None]:
    """The persistence layer is built once per execution environment; give every test its own."""
    notifier._idempotent_notify.cache_clear()
    yield
    notifier._idempotent_notify.cache_clear()


@pytest.fixture
def people(user_pool: UserPool) -> People:
    owner = user_pool.create_user("hanako@example.com", groups=("employees", "managers"), name="Hanako Sato")
    manager = user_pool.create_user("kenji@example.com", groups=("employees", "managers"))
    admin = user_pool.create_user("yuki@example.com", groups=("employees", "admins"))
    user_pool.create_user("jiro@example.com", groups=("managers",))
    user_pool.idp.admin_disable_user(UserPoolId=user_pool.pool_id, Username="jiro@example.com")  # left the company
    return People(owner=owner, manager=manager, admin=admin)


@pytest.fixture
def verified_sender(monkeypatch: pytest.MonkeyPatch) -> str:
    """E-mail switched on with a verified SES identity (as CDK's EmailIdentity creates it)."""
    monkeypatch.setenv("SENDER_EMAIL", SENDER)
    boto3.client("sesv2").create_email_identity(EmailIdentity=SENDER)
    return SENDER


def expense_detail(owner: CognitoUser, status: str = "PENDING_APPROVAL", **overrides: Any) -> dict[str, Any]:
    occurred_at = SUBMITTED_AT if status == "PENDING_APPROVAL" else DECIDED_AT
    detail = {
        "expenseId": EXPENSE_ID,
        "ownerId": owner.sub,
        "ownerEmail": owner.email,
        "ownerName": owner.name,
        "title": "Taxi to client meeting",
        "amount": 1280,
        "currency": "JPY",
        "status": status,
        "previousStatus": "PENDING_APPROVAL" if status != "PENDING_APPROVAL" else "DRAFT",
        "actorId": owner.sub,
        "actorEmail": owner.email,
        "comment": None,
        "autoApproved": None,
        "extractionStatus": None,
        "occurredAt": occurred_at,
        "idempotencyKey": f"{EXPENSE_ID}:{status}:{occurred_at}",
    }
    return {**detail, **overrides}


def decision_detail(owner: CognitoUser, status: str, comment: str) -> dict[str, Any]:
    return expense_detail(owner, status, actorId=MANAGER_ID, actorEmail=MANAGER_EMAIL, comment=comment)


def escalated_detail(owner: CognitoUser) -> dict[str, Any]:
    return {
        "expenseId": EXPENSE_ID,
        "ownerId": owner.sub,
        "title": "Taxi to client meeting",
        "amount": 1280,
        "currency": "JPY",
        "requestedAt": SUBMITTED_AT,
        "idempotencyKey": f"{EXPENSE_ID}:ESCALATED:exec-1",
    }


def report_detail() -> dict[str, Any]:
    return {
        "month": "2026-09",
        "key": "reports/2026-09.csv",
        "rowCount": 2,
        "totals": {"JPY": 12345, "USD": 12.5},
        "requestedBy": "scheduler",
        "idempotencyKey": "2026-09:REPORT:2026-10-01T00:00:03.000Z",
    }


def envelope(detail_type: str, detail: Mapping[str, Any], source: str = "expenseflow.expenses") -> dict[str, Any]:
    return eventbridge_event(detail_type, source, detail)


def run(envelopes: Sequence[Mapping[str, Any] | str], lambda_context: FakeLambdaContext) -> dict[str, Any]:
    return dict(notifier.handler(sqs_event(envelopes), lambda_context))


def sent() -> list[tuple[list[str], str, str]]:
    """``(to, subject, body)`` of every e-mail moto's SES accepted."""
    messages = ses_backends[ACCOUNT_ID][REGION].sent_messages
    return [(message.destinations["ToAddresses"], message.subject, message.body) for message in messages]


# --- Recipients -----------------------------------------------------------------------------------------------------


def test_submitted_goes_to_enabled_managers_except_the_owner(people: People) -> None:
    detail = expense_detail(people.owner)

    assert notify.recipients("Expense Submitted", detail) == [people.manager.email]


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED"])
def test_decisions_go_to_the_owner(status: str, people: People) -> None:
    detail = decision_detail(people.owner, status, "OK")

    assert notify.recipients(f"Expense {status.title()}", detail) == [people.owner.email]


def test_escalations_and_reports_go_to_admins(people: People) -> None:
    assert notify.recipients("Approval Escalated", escalated_detail(people.owner)) == [people.admin.email]
    assert notify.recipients("Report Generated", report_detail()) == [people.admin.email]


@pytest.mark.parametrize("detail_type", ["Expense Withdrawn", "Receipt Processed", "Something Else"])
def test_other_events_notify_nobody(detail_type: str, people: People) -> None:
    assert notify.recipients(detail_type, expense_detail(people.owner)) == []


# --- Sending --------------------------------------------------------------------------------------------------------


def test_without_a_sender_the_email_is_only_logged(
    people: People, lambda_context: FakeLambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = io.StringIO()
    handler = notifier.logger.registered_handler
    assert isinstance(handler, logging.StreamHandler)
    monkeypatch.setattr(handler, "stream", log)

    assert run([envelope("Expense Submitted", expense_detail(people.owner))], lambda_context) == {
        "batchItemFailures": []
    }

    assert sent() == []
    [would_send] = [
        entry for entry in map(json.loads, log.getvalue().splitlines()) if "would have sent" in entry["message"]
    ]
    assert would_send["to"] == [people.manager.email]
    assert would_send["subject"] == "Approval needed: Taxi to client meeting (1,280 JPY)"


def test_submitted_emails_each_manager_a_review_link(
    people: People, verified_sender: str, lambda_context: FakeLambdaContext
) -> None:
    run([envelope("Expense Submitted", expense_detail(people.owner))], lambda_context)

    [(to, subject, body)] = sent()
    assert to == [people.manager.email]
    assert subject == "Approval needed: Taxi to client meeting (1,280 JPY)"
    assert "Hanako Sato submitted an expense for approval." in body
    assert f"{APP_URL}/expenses/{EXPENSE_ID}" in body


@pytest.mark.parametrize(
    ("status", "subject", "phrase"),
    [
        ("APPROVED", "Expense approved: Taxi to client meeting", f"was approved by {MANAGER_EMAIL}"),
        ("REJECTED", "Expense rejected: Taxi to client meeting", "was rejected"),
    ],
)
def test_decisions_email_the_owner_with_the_comment(
    status: str,
    subject: str,
    phrase: str,
    people: People,
    verified_sender: str,
    lambda_context: FakeLambdaContext,
) -> None:
    detail = decision_detail(people.owner, status, "Please attach the receipt next time")

    run([envelope(f"Expense {status.title()}", detail)], lambda_context)

    [(to, actual_subject, body)] = sent()
    assert (to, actual_subject) == ([people.owner.email], subject)
    assert phrase in body
    assert "Comment: Please attach the receipt next time" in body
    assert f"{APP_URL}/expenses/{EXPENSE_ID}" in body


def test_escalation_emails_admins_a_review_link(
    people: People, verified_sender: str, lambda_context: FakeLambdaContext
) -> None:
    run(
        [envelope("Approval Escalated", escalated_detail(people.owner), source="expenseflow.approvals")], lambda_context
    )

    [(to, subject, body)] = sent()
    assert (to, subject) == ([people.admin.email], "Escalated: Taxi to client meeting is still waiting for approval")
    assert f"(1,280 JPY) since {SUBMITTED_AT}" in body
    assert f"{APP_URL}/expenses/{EXPENSE_ID}" in body


def test_report_generated_emails_admins_a_download_link(
    people: People, verified_sender: str, lambda_context: FakeLambdaContext
) -> None:
    run([envelope("Report Generated", report_detail(), source="expenseflow.reports")], lambda_context)

    [(to, subject, body)] = sent()
    assert (to, subject) == ([people.admin.email], "Expense report 2026-09 is ready")
    assert "Totals: 12,345 JPY, 12.5 USD" in body
    assert f"{APP_URL}/reports" in body


def test_ses_sandbox_rejection_is_not_a_failure(
    people: People, lambda_context: FakeLambdaContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENDER_EMAIL", "unverified@example.com")  # SES answers MessageRejected

    response = run([envelope("Expense Approved", decision_detail(people.owner, "APPROVED", "OK"))], lambda_context)

    assert response == {"batchItemFailures": []}
    assert sent() == []


def test_throttling_fails_only_that_message(
    people: People, verified_sender: str, lambda_context: FakeLambdaContext
) -> None:
    approved = envelope("Expense Approved", decision_detail(people.owner, "APPROVED", "OK"))
    withdrawn = envelope("Expense Withdrawn", expense_detail(people.owner, "DRAFT"))

    with Stubber(aws.client("sesv2")) as stub:
        stub.add_client_error("send_email", service_error_code="TooManyRequestsException", http_status_code=429)
        response = run([approved, withdrawn], lambda_context)

    assert response == {"batchItemFailures": [{"itemIdentifier": sqs_message_id(1)}]}


# --- Idempotency (real persistence layer on the moto idempotency table) ----------------------------------------------


@pytest.fixture
def idempotency_on(idempotency_table: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED")
    return idempotency_table


def test_a_duplicate_event_is_not_emailed_twice(
    people: People, verified_sender: str, idempotency_on: str, lambda_context: FakeLambdaContext
) -> None:
    detail = decision_detail(people.owner, "APPROVED", "OK")
    original = envelope("Expense Approved", detail)
    republished = envelope("Expense Approved", detail)  # new EventBridge id, same change -> same idempotencyKey
    other_change = envelope("Expense Rejected", decision_detail(people.owner, "REJECTED", "No"))

    assert run([original], lambda_context) == {"batchItemFailures": []}
    assert run([other_change, republished], lambda_context) == {"batchItemFailures": []}
    assert run([original], lambda_context) == {"batchItemFailures": []}  # SQS redelivery of the same message

    assert [subject for _, subject, _ in sent()] == [
        "Expense approved: Taxi to client meeting",
        "Expense rejected: Taxi to client meeting",
    ]
    records = boto3.client("dynamodb").scan(TableName=idempotency_on)["Items"]
    assert sorted(record["status"]["S"] for record in records) == ["COMPLETED", "COMPLETED"]


def test_a_message_without_idempotency_key_is_failed(
    people: People, verified_sender: str, idempotency_on: str, lambda_context: FakeLambdaContext
) -> None:
    keyless = expense_detail(people.owner, "APPROVED")
    del keyless["idempotencyKey"]
    valid = decision_detail(people.owner, "APPROVED", "OK")

    response = run([envelope("Expense Approved", keyless), envelope("Expense Approved", valid)], lambda_context)

    assert response == {"batchItemFailures": [{"itemIdentifier": sqs_message_id(1)}]}
    assert len(sent()) == 1
