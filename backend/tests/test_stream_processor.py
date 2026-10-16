"""Stream processor: HISTORY items, exactly-once monthly totals and the domain events of SPEC 4 (SPEC 7.1)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from aws_lambda_powertools.utilities.batch.exceptions import BatchProcessingError
from boto3.dynamodb.conditions import Key
from botocore.stub import ANY, Stubber

from expenseflow import aws, keys
from handlers import stream_processor
from tests.builders import (
    EMPLOYEE_EMAIL,
    EMPLOYEE_ID,
    EMPLOYEE_NAME,
    MANAGER_EMAIL,
    MANAGER_ID,
    StreamRecordSpec,
    ddb_stream_event,
    expense_item,
    stream_sequence_number,
)
from tests.conftest import EventsSink, FakeLambdaContext

SUBMITTED_AT = "2026-10-11T00:00:00.000Z"
DECIDED_AT = "2026-10-12T03:04:05.678Z"
EXTRACTED_AT = "2026-10-10T02:00:00.000Z"
SEPTEMBER = "2026-09-30T23:59:59.999Z"
# SPEC 4: every expense event detail has exactly these keys (null when unknown).
EXPENSE_DETAIL_KEYS = {
    "expenseId",
    "ownerId",
    "ownerEmail",
    "ownerName",
    "title",
    "amount",
    "currency",
    "status",
    "previousStatus",
    "actorId",
    "actorEmail",
    "comment",
    "autoApproved",
    "extractionStatus",
    "occurredAt",
    "idempotencyKey",
}
_KEY_ATTRIBUTES = {keys.PK, keys.SK, keys.GSI1PK, keys.GSI1SK, keys.GSI2PK, keys.GSI2SK}


def then(before: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The item after an update of ``before`` (keys re-derived; a change to ``None`` removes the attribute)."""
    attributes = {name: value for name, value in before.items() if name not in _KEY_ATTRIBUTES}
    return expense_item(**{**attributes, **changes})


def pending(**overrides: Any) -> dict[str, Any]:
    draft = expense_item(**overrides)
    return then(draft, status="PENDING_APPROVAL", updatedAt=SUBMITTED_AT, submittedAt=SUBMITTED_AT, version=Decimal(2))


def approved(before: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """``before`` after a manager's approval (RecordDecision); ``changes`` override the decision attributes."""
    decision: dict[str, Any] = {
        "status": "APPROVED",
        "updatedAt": DECIDED_AT,
        "decidedAt": DECIDED_AT,
        "approverId": MANAGER_ID,
        "approverEmail": MANAGER_EMAIL,
        "decisionComment": "OK",
        "autoApproved": False,
        "lastActorId": MANAGER_ID,
        "lastActorEmail": MANAGER_EMAIL,
        "version": Decimal(3),
    }
    return then(before, **{**decision, **changes})


def run(records: Sequence[StreamRecordSpec], context: FakeLambdaContext) -> dict[str, Any]:
    return dict(stream_processor.handler(ddb_stream_event(records), context))


def history_of(table: Any, expense_id: str) -> list[dict[str, Any]]:
    condition = Key(keys.PK).eq(keys.expense_pk(expense_id)) & Key(keys.SK).begins_with(keys.HISTORY_PREFIX)
    return list(table.query(KeyConditionExpression=condition)["Items"])


def total(table: Any, pk: str, month: str = "2026-10", currency: str = "JPY") -> dict[str, Any] | None:
    item: dict[str, Any] | None = table.get_item(Key={keys.PK: pk, keys.SK: keys.total_sk(month, currency)}).get("Item")
    return item


def counted(table: Any, pk: str, month: str = "2026-10", currency: str = "JPY") -> tuple[Decimal, Decimal] | None:
    """``(approvedAmount, approvedCount)`` of a monthly total, ``None`` when nothing was approved."""
    item = total(table, pk, month, currency)
    return None if item is None else (item["approvedAmount"], item["approvedCount"])


def by_type(envelopes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for envelope in envelopes:
        grouped.setdefault(envelope["detail-type"], []).append(envelope)
    return grouped


# --- Status transitions -------------------------------------------------------------------------------------------


def test_insert_records_creation_without_publishing(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    draft = expense_item()

    assert run([("INSERT", None, draft)], lambda_context) == {"batchItemFailures": []}

    [entry] = history_of(table, draft["id"])
    assert entry == {
        keys.PK: keys.expense_pk(draft["id"]),
        keys.SK: f"HISTORY#{draft['updatedAt']}#STATUS#DRAFT",
        "entityType": "History",
        "expenseId": draft["id"],
        "kind": "STATUS",
        "at": draft["updatedAt"],
        "to": "DRAFT",
        "actorId": EMPLOYEE_ID,
        "actorEmail": EMPLOYEE_EMAIL,
    }
    assert events_sink() == []


def test_submit_records_history_and_publishes_expense_submitted(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    draft = expense_item()
    submitted = then(draft, status="PENDING_APPROVAL", updatedAt=SUBMITTED_AT, submittedAt=SUBMITTED_AT)

    run([("MODIFY", draft, submitted)], lambda_context)

    [entry] = history_of(table, draft["id"])
    assert entry["SK"] == f"HISTORY#{SUBMITTED_AT}#STATUS#PENDING_APPROVAL"
    assert (entry["from"], entry["to"], entry["at"]) == ("DRAFT", "PENDING_APPROVAL", SUBMITTED_AT)
    assert "comment" not in entry
    [envelope] = events_sink()
    assert (envelope["source"], envelope["detail-type"]) == ("expenseflow.expenses", "Expense Submitted")
    assert envelope["detail"] == {
        "expenseId": draft["id"],
        "ownerId": EMPLOYEE_ID,
        "ownerEmail": EMPLOYEE_EMAIL,
        "ownerName": EMPLOYEE_NAME,
        "title": "Taxi to client meeting",
        "amount": 1280,
        "currency": "JPY",
        "status": "PENDING_APPROVAL",
        "previousStatus": "DRAFT",
        "actorId": EMPLOYEE_ID,
        "actorEmail": EMPLOYEE_EMAIL,
        "comment": None,
        "autoApproved": None,
        "extractionStatus": None,
        "occurredAt": SUBMITTED_AT,
        "idempotencyKey": f"{draft['id']}:PENDING_APPROVAL:{SUBMITTED_AT}",
    }


def test_approval_records_history_and_both_monthly_totals(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    before = pending()
    after = approved(before)

    run([("MODIFY", before, after)], lambda_context)

    [entry] = history_of(table, before["id"])
    assert entry["SK"] == f"HISTORY#{DECIDED_AT}#STATUS#APPROVED"
    assert (entry["from"], entry["to"], entry["comment"]) == ("PENDING_APPROVAL", "APPROVED", "OK")
    assert (entry["actorId"], entry["actorEmail"]) == (MANAGER_ID, MANAGER_EMAIL)
    expected_total = {
        "entityType": "MonthlyTotal",
        "month": "2026-10",
        "currency": "JPY",
        "approvedAmount": Decimal(1280),
        "approvedCount": Decimal(1),
    }
    user_total = total(table, keys.user_total_pk(EMPLOYEE_ID))
    org_total = total(table, keys.ORG_PK)
    assert user_total == {keys.PK: f"USER#{EMPLOYEE_ID}", keys.SK: "TOTAL#2026-10#JPY", **expected_total}
    assert org_total == {keys.PK: "ORG", keys.SK: "TOTAL#2026-10#JPY", **expected_total}
    [envelope] = events_sink()
    assert envelope["detail-type"] == "Expense Approved"
    assert set(envelope["detail"]) == EXPENSE_DETAIL_KEYS
    assert envelope["detail"]["comment"] == "OK"
    assert envelope["detail"]["autoApproved"] is False
    assert envelope["detail"]["actorEmail"] == MANAGER_EMAIL
    assert envelope["detail"]["idempotencyKey"] == f"{before['id']}:APPROVED:{DECIDED_AT}"


def test_totals_accumulate_per_owner_month_and_currency(
    table: Any, event_bus: str, lambda_context: FakeLambdaContext
) -> None:
    first, second = pending(amount=Decimal("1000.50")), pending(amount=Decimal("250.25"))
    colleague = pending(ownerId=MANAGER_ID, amount=Decimal(4000))
    in_dollars = pending(amount=Decimal("12.5"), currency="USD")
    last_month = pending(amount=Decimal(99))

    run(
        [
            ("MODIFY", first, approved(first)),
            ("MODIFY", second, approved(second)),
            ("MODIFY", colleague, approved(colleague)),
            ("MODIFY", in_dollars, approved(in_dollars)),
            ("MODIFY", last_month, approved(last_month, decidedAt=SEPTEMBER, updatedAt=SEPTEMBER)),
        ],
        lambda_context,
    )

    assert counted(table, keys.user_total_pk(EMPLOYEE_ID)) == (Decimal("1250.75"), 2)
    assert counted(table, keys.ORG_PK) == (Decimal("5250.75"), 3)
    assert counted(table, keys.user_total_pk(EMPLOYEE_ID), currency="USD") == (Decimal("12.5"), 1)
    assert counted(table, keys.ORG_PK, month="2026-09") == (Decimal(99), 1)


def test_rejection_records_the_comment_without_totals(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    before = pending()
    after = then(
        before,
        status="REJECTED",
        updatedAt=DECIDED_AT,
        decidedAt=DECIDED_AT,
        decisionComment="Receipt is unreadable",
        lastActorId=MANAGER_ID,
        lastActorEmail=MANAGER_EMAIL,
    )

    run([("MODIFY", before, after)], lambda_context)

    [entry] = history_of(table, before["id"])
    assert (entry["to"], entry["comment"]) == ("REJECTED", "Receipt is unreadable")
    assert total(table, keys.ORG_PK) is None
    [envelope] = events_sink()
    assert envelope["detail-type"] == "Expense Rejected"
    assert envelope["detail"]["comment"] == "Receipt is unreadable"


@pytest.mark.parametrize(
    ("status", "detail_type", "auto_approved", "comment"),
    [
        ("APPROVED", "Expense Approved", True, "Auto-approved by policy: ≤ 5000 JPY and receipt total verified by AI"),
        ("REJECTED", "Expense Rejected", False, "Automatically rejected: no decision within 4320 minutes"),
    ],
)
def test_workflow_decisions_are_attributed_to_the_system(
    status: str,
    detail_type: str,
    auto_approved: bool,
    comment: str,
    table: Any,
    events_sink: EventsSink,
    lambda_context: FakeLambdaContext,
) -> None:
    """AutoApprove and ExpireRequest (ApprovalWorkflow) write the decision with ``system`` as the actor."""
    before = pending()
    after = then(
        before,
        status=status,
        updatedAt=DECIDED_AT,
        decidedAt=DECIDED_AT,
        decisionComment=comment,
        autoApproved=auto_approved,
        approverEmail="auto-approval" if auto_approved else None,
        lastActorId="system",
        lastActorEmail="system",
        version=Decimal(3),
    )

    run([("MODIFY", before, after)], lambda_context)

    [entry] = history_of(table, before["id"])
    assert (entry["actorId"], entry["actorEmail"], entry["comment"]) == ("system", "system", comment)
    assert (counted(table, keys.ORG_PK) == (Decimal(1280), 1)) is auto_approved
    [envelope] = events_sink()
    detail = envelope["detail"]
    assert envelope["detail-type"] == detail_type
    assert (detail["actorId"], detail["actorEmail"]) == ("system", "system")
    assert (detail["autoApproved"], detail["comment"]) == (auto_approved, comment)


def test_withdrawal_publishes_expense_withdrawn(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    before = pending()
    after = then(before, status="DRAFT", updatedAt="2026-10-11T08:00:00.000Z", decisionComment=None)

    run([("MODIFY", before, after)], lambda_context)

    [entry] = history_of(table, before["id"])
    assert (entry["from"], entry["to"]) == ("PENDING_APPROVAL", "DRAFT")
    [envelope] = events_sink()
    assert envelope["detail-type"] == "Expense Withdrawn"
    assert (envelope["detail"]["status"], envelope["detail"]["previousStatus"]) == ("DRAFT", "PENDING_APPROVAL")


def test_reopening_a_rejected_expense_is_history_only(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    rejected = expense_item(status="REJECTED", decisionComment="Too expensive")
    reopened = then(rejected, status="DRAFT", updatedAt="2026-10-14T00:00:00.000Z", decisionComment=None)

    run([("MODIFY", rejected, reopened)], lambda_context)

    [entry] = history_of(table, rejected["id"])
    assert (entry["from"], entry["to"]) == ("REJECTED", "DRAFT")
    assert events_sink() == []


def test_edits_without_a_status_change_are_ignored(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    draft = expense_item()
    edited = then(draft, title="Taxi (corrected)", updatedAt="2026-10-10T05:00:00.000Z", version=Decimal(2))

    run([("MODIFY", draft, edited)], lambda_context)

    assert history_of(table, draft["id"]) == []
    assert events_sink() == []


# --- Receipt extraction ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extraction", "comment"),
    [
        ({"extractionStatus": "SUCCEEDED", "extractionResult": '{"total": 1280}'}, None),
        (
            {"extractionStatus": "FAILED", "extractionError": "ModelThrottledError: slow down"},
            "ModelThrottledError: slow down",
        ),
        ({"extractionStatus": "SKIPPED"}, None),
    ],
)
def test_finished_extraction_records_history_and_publishes_receipt_processed(
    extraction: dict[str, Any],
    comment: str | None,
    table: Any,
    events_sink: EventsSink,
    lambda_context: FakeLambdaContext,
) -> None:
    processing = expense_item(extractionStatus="PROCESSING", receiptKey="receipts/u/e/r.jpg")
    finished = then(processing, extractedAt=EXTRACTED_AT, **extraction)

    run([("MODIFY", processing, finished)], lambda_context)

    status = extraction["extractionStatus"]
    [entry] = history_of(table, processing["id"])
    assert entry["SK"] == f"HISTORY#{EXTRACTED_AT}#RECEIPT"
    assert (entry["kind"], entry["extractionStatus"], entry["at"]) == ("RECEIPT", status, EXTRACTED_AT)
    assert (entry["actorId"], entry["actorEmail"]) == ("system", "system")
    assert entry.get("comment") == comment
    [envelope] = events_sink()
    assert (envelope["source"], envelope["detail-type"]) == ("expenseflow.expenses", "Receipt Processed")
    assert set(envelope["detail"]) == EXPENSE_DETAIL_KEYS
    assert envelope["detail"]["extractionStatus"] == status
    assert envelope["detail"]["occurredAt"] == EXTRACTED_AT
    assert envelope["detail"]["idempotencyKey"] == f"{processing['id']}:RECEIPT-{status}:{EXTRACTED_AT}"


def test_extraction_in_progress_is_ignored(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    draft = expense_item()
    processing = then(draft, extractionStatus="PROCESSING", receiptKey="receipts/u/e/r.jpg")

    run([("MODIFY", draft, processing)], lambda_context)

    assert history_of(table, draft["id"]) == []
    assert events_sink() == []


# --- At-least-once delivery ---------------------------------------------------------------------------------------


def test_replaying_a_batch_counts_nothing_twice(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    first, second = pending(), pending(amount=Decimal(720))
    processing = expense_item(extractionStatus="PROCESSING")
    batch: list[StreamRecordSpec] = [
        ("MODIFY", first, approved(first)),
        ("MODIFY", second, approved(second)),
        ("MODIFY", processing, then(processing, extractionStatus="SKIPPED", extractedAt=EXTRACTED_AT)),
    ]

    assert run(batch, lambda_context) == {"batchItemFailures": []}
    published = events_sink()
    assert run(batch, lambda_context) == {"batchItemFailures": []}
    republished = events_sink()

    assert counted(table, keys.ORG_PK) == (Decimal(2000), 2)
    assert len(history_of(table, first["id"])) == len(history_of(table, processing["id"])) == 1
    # Events are published again (at-least-once) with the SAME idempotency keys, so consumers drop the copies.
    keys_of = sorted(envelope["detail"]["idempotencyKey"] for envelope in published)
    assert len(keys_of) == 3
    assert sorted(envelope["detail"]["idempotencyKey"] for envelope in republished) == keys_of


def test_a_failing_record_is_reported_and_the_others_are_processed(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    good_before, poison_before, later_before = pending(), pending(), pending()
    poison = approved(poison_before, currency=None)  # an approval without a currency cannot be totalled

    response = run(
        [
            ("MODIFY", good_before, approved(good_before)),
            ("MODIFY", poison_before, poison),
            ("MODIFY", later_before, approved(later_before)),
        ],
        lambda_context,
    )

    assert response == {"batchItemFailures": [{"itemIdentifier": stream_sequence_number(2)}]}
    assert counted(table, keys.ORG_PK) == (Decimal(2560), 2)
    assert len(by_type(events_sink())["Expense Approved"]) == 2


def test_a_failed_publish_fails_the_record_and_its_retry_publishes_once(
    table: Any, events_sink: EventsSink, lambda_context: FakeLambdaContext
) -> None:
    submitted_before, withdrawn_before = expense_item(), pending()
    submitted = then(submitted_before, status="PENDING_APPROVAL", updatedAt=SUBMITTED_AT)
    withdrawn = then(withdrawn_before, status="DRAFT", updatedAt="2026-10-11T09:00:00.000Z")
    failed_entry = {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure", "ErrorMessage": "try again"}]}

    with Stubber(aws.client("events")) as stub:
        stub.add_response("put_events", failed_entry, {"Entries": ANY})
        stub.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "e-1"}]}, {"Entries": ANY})
        response = run(
            [("MODIFY", submitted_before, submitted), ("MODIFY", withdrawn_before, withdrawn)], lambda_context
        )

    assert response == {"batchItemFailures": [{"itemIdentifier": stream_sequence_number(1)}]}
    assert len(history_of(table, submitted["id"])) == 1  # written before the publish failed

    assert run([("MODIFY", submitted_before, submitted)], lambda_context) == {"batchItemFailures": []}
    [envelope] = events_sink()
    assert envelope["detail-type"] == "Expense Submitted"
    assert len(history_of(table, submitted["id"])) == 1


def test_a_transaction_cancelled_for_another_reason_is_retried(table: Any, lambda_context: FakeLambdaContext) -> None:
    before = pending()
    conflict = [{"Code": "None"}, {"Code": "TransactionConflict"}, {"Code": "None"}]

    with Stubber(table.meta.client) as stub:
        stub.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            http_status_code=400,
            modeled_fields={"CancellationReasons": conflict},
        )
        with pytest.raises(BatchProcessingError):  # the only record failed: Powertools fails the whole batch
            run([("MODIFY", before, approved(before))], lambda_context)

    assert total(table, keys.ORG_PK) is None


# --- Metrics ------------------------------------------------------------------------------------------------------


def test_metrics_count_first_time_transitions_only(
    table: Any,
    event_bus: str,
    lambda_context: FakeLambdaContext,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERTOOLS_METRICS_DISABLED", "false")
    before = pending()
    batch: list[StreamRecordSpec] = [("MODIFY", before, approved(before))]

    run(batch, lambda_context)
    first = _emitted_metrics(capsys.readouterr().out)
    run(batch, lambda_context)
    replay = _emitted_metrics(capsys.readouterr().out)

    assert ("StatusTransitions", "APPROVED") in first
    assert ("ExpensesApproved", None) in first
    assert replay == []


def _emitted_metrics(stdout: str) -> list[tuple[str, str | None]]:
    """(metric name, value of the ``to`` dimension) for every EMF line printed to stdout."""
    emitted: list[tuple[str, str | None]] = []
    for line in stdout.splitlines():
        if not line.startswith("{") or "_aws" not in line:
            continue
        blob = json.loads(line)
        for directive in blob["_aws"]["CloudWatchMetrics"]:
            emitted.extend((metric["Name"], blob.get("to")) for metric in directive["Metrics"])
    return emitted
