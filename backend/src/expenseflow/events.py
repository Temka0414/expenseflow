"""Domain events on the custom EventBridge bus (SPEC section 4): names, detail builders and publishing.

EventBridge routes events by matching their ``source`` and ``detail-type`` against rules, so these strings are
a contract with the infrastructure (rules, archive) and with every consumer (Step Functions, notifier,
WebSocket push, the SPA). They are defined once, here.

Delivery is **at least once**: EventBridge, SQS and Lambda retries can all deliver an event twice. Every detail
therefore carries an ``idempotencyKey`` derived from the state change itself (not from a random id), so a
re-published copy of the same change has the same key and consumers can drop the duplicate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import batched
from typing import Any, Final

from expenseflow import aws, settings
from expenseflow.models import TERMINAL_EXTRACTION_STATUSES, ExpenseStatus, HistoryKind
from expenseflow.serialization import dumps, to_json_number

SOURCE_EXPENSES: Final = "expenseflow.expenses"
SOURCE_APPROVALS: Final = "expenseflow.approvals"
SOURCE_REPORTS: Final = "expenseflow.reports"

DETAIL_TYPE_EXPENSE_SUBMITTED: Final = "Expense Submitted"
DETAIL_TYPE_EXPENSE_APPROVED: Final = "Expense Approved"
DETAIL_TYPE_EXPENSE_REJECTED: Final = "Expense Rejected"
DETAIL_TYPE_EXPENSE_WITHDRAWN: Final = "Expense Withdrawn"
DETAIL_TYPE_RECEIPT_PROCESSED: Final = "Receipt Processed"
DETAIL_TYPE_APPROVAL_ESCALATED: Final = "Approval Escalated"
DETAIL_TYPE_REPORT_GENERATED: Final = "Report Generated"

# PutEvents accepts at most 10 entries per call (and 256 KB in total).
MAX_ENTRIES_PER_PUT: Final = 10

_DETAIL_TYPE_BY_NEW_STATUS: Final[Mapping[str, str]] = {
    ExpenseStatus.PENDING_APPROVAL: DETAIL_TYPE_EXPENSE_SUBMITTED,
    ExpenseStatus.APPROVED: DETAIL_TYPE_EXPENSE_APPROVED,
    ExpenseStatus.REJECTED: DETAIL_TYPE_EXPENSE_REJECTED,
}


class EventPublishError(RuntimeError):
    """EventBridge rejected at least one entry. Raised so the caller's retry mechanism (e.g. the stream) retries."""


@dataclass(frozen=True, slots=True)
class EventEntry:
    """One event to put on the custom bus; ``detail`` may contain ``Decimal`` values (rendered as JSON numbers)."""

    source: str
    detail_type: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_request_entry(self, event_bus_name: str) -> dict[str, str]:
        """The ``PutEvents`` request entry. ``Detail`` must be a JSON *object* serialised as a string."""
        return {
            "Source": self.source,
            "DetailType": self.detail_type,
            "Detail": dumps(self.detail),
            "EventBusName": event_bus_name,
        }


def publish(entries: Iterable[EventEntry]) -> list[str]:
    """Put ``entries`` on the custom bus (``EVENT_BUS_NAME``) in batches of 10; returns the EventBridge event ids.

    Gotcha: ``PutEvents`` does not raise when individual entries fail - it returns ``FailedEntryCount`` and an
    ``ErrorCode`` per failed entry (throttling, internal errors, malformed detail). Ignoring that silently loses
    events, so any failure raises :class:`EventPublishError`. The batch is then retried as a whole; consumers
    de-duplicate the entries that had already gone through by their ``idempotencyKey``.
    """
    bus_name = settings.event_bus_name()
    client = aws.client("events")
    event_ids: list[str] = []
    for chunk in batched(entries, MAX_ENTRIES_PER_PUT, strict=False):  # the last chunk may be shorter
        response = client.put_events(Entries=[entry.to_request_entry(bus_name) for entry in chunk])
        if response.get("FailedEntryCount", 0):
            raise EventPublishError(_describe_failures(chunk, response["Entries"]))
        event_ids.extend(result["EventId"] for result in response["Entries"])
    return event_ids


def _describe_failures(chunk: Sequence[EventEntry], results: Sequence[Mapping[str, Any]]) -> str:
    failures = [
        f"{entry.detail_type}: {result.get('ErrorCode')} {result.get('ErrorMessage', '')}".strip()
        for entry, result in zip(chunk, results, strict=True)
        if "ErrorCode" in result
    ]
    return f"PutEvents failed for {len(failures)} of {len(chunk)} entries: " + "; ".join(failures)


# --- Expense events (emitted by the stream processor from DynamoDB stream images) ---------------------------


def detail_type_for_transition(old_status: str | None, new_status: str | None) -> str | None:
    """The event a status change produces, or ``None`` when it is not a published transition.

    ``old_status`` is ``None`` for a newly inserted item. Creating a draft and re-opening a rejected expense
    (REJECTED -> DRAFT) are recorded in the history but are not events anyone subscribes to.
    """
    if new_status is None or old_status == new_status:
        return None
    if old_status == ExpenseStatus.PENDING_APPROVAL and new_status == ExpenseStatus.DRAFT:
        return DETAIL_TYPE_EXPENSE_WITHDRAWN
    return _DETAIL_TYPE_BY_NEW_STATUS.get(new_status)


def receipt_processed(old_image: Mapping[str, Any] | None, new_image: Mapping[str, Any]) -> bool:
    """True when the AI extraction just finished: ``extractionStatus`` changed to SUCCEEDED, FAILED or SKIPPED."""
    new_status = new_image.get("extractionStatus")
    old_status = (old_image or {}).get("extractionStatus")
    return new_status != old_status and new_status in TERMINAL_EXTRACTION_STATUSES


def build_expense_detail(
    new_image: Mapping[str, Any],
    old_image: Mapping[str, Any] | None,
    kind: str,
) -> dict[str, Any]:
    """The ``detail`` of an expense event (SPEC section 4) from the stream's new/old images.

    ``kind`` is ``STATUS`` for status transitions and ``RECEIPT`` for "Receipt Processed". All keys are always
    present (``None`` when unknown) so consumers never need ``.get`` guesswork. The idempotency key identifies
    the state change: ``<expenseId>:<status>:<updatedAt>`` or ``<expenseId>:RECEIPT-<extractionStatus>:<extractedAt>``.
    """
    event_kind = HistoryKind(kind)
    old = old_image or {}
    expense_id = new_image["id"]
    if event_kind is HistoryKind.STATUS:
        occurred_at = new_image.get("updatedAt")
        change = str(new_image.get("status"))
        comment = new_image.get("decisionComment")
    else:
        occurred_at = new_image.get("extractedAt")
        change = f"RECEIPT-{new_image.get('extractionStatus')}"
        comment = None
    amount = new_image.get("amount")
    auto_approved = new_image.get("autoApproved")
    return {
        "expenseId": expense_id,
        "ownerId": new_image.get("ownerId"),
        "ownerEmail": new_image.get("ownerEmail"),
        "ownerName": new_image.get("ownerName"),
        "title": new_image.get("title"),
        "amount": None if amount is None else to_json_number(amount),
        "currency": new_image.get("currency"),
        "status": new_image.get("status"),
        "previousStatus": old.get("status"),
        "actorId": new_image.get("lastActorId"),
        "actorEmail": new_image.get("lastActorEmail"),
        "comment": comment,
        "autoApproved": None if auto_approved is None else bool(auto_approved),
        "extractionStatus": new_image.get("extractionStatus"),
        "occurredAt": occurred_at,
        "idempotencyKey": f"{expense_id}:{change}:{occurred_at}",
    }


def expense_event(detail_type: str, detail: Mapping[str, Any]) -> EventEntry:
    """An :class:`EventEntry` from ``expenseflow.expenses``."""
    return EventEntry(source=SOURCE_EXPENSES, detail_type=detail_type, detail=detail)


# --- Report events ------------------------------------------------------------------------------------------


def build_report_detail(
    *,
    month: str,
    key: str,
    row_count: int,
    totals: Mapping[str, Any],
    requested_by: str,
    generated_at: str,
) -> dict[str, Any]:
    """The ``Report Generated`` detail; ``totals`` maps currency to the approved sum (``{"JPY": 12345}``)."""
    return {
        "month": month,
        "key": key,
        "rowCount": row_count,
        "totals": {currency: to_json_number(amount) for currency, amount in totals.items()},
        "requestedBy": requested_by,
        "idempotencyKey": f"{month}:REPORT:{generated_at}",
    }


def report_event(detail: Mapping[str, Any]) -> EventEntry:
    """An :class:`EventEntry` for ``Report Generated`` from ``expenseflow.reports``."""
    return EventEntry(source=SOURCE_REPORTS, detail_type=DETAIL_TYPE_REPORT_GENERATED, detail=detail)
