"""DynamoDB Streams consumer: audit trail, monthly totals and domain events (SPEC 7.1).

Every change to an expense's META item arrives here as a stream record with the item's OLD and NEW image. This
is the **transactional outbox** pattern without an outbox table: the API's single conditional write is the only
thing that has to succeed, and the stream guarantees each committed change is delivered here (in order per
item, retained 24 h). From it we derive:

1. a HISTORY item per status change / finished receipt extraction (plus totals on approval) - see ``history``;
2. the domain events of SPEC 4 on the custom EventBridge bus (``Expense Submitted`` starts the approval workflow).

The event source mapping only invokes us for INSERT/MODIFY of ``EXPENSE#*`` / ``META`` keys (a server-side
filter), so this function's own HISTORY and TOTAL writes never come back to it as new records - no loops.

Failures: a record that raises is reported in ``batchItemFailures`` (Powertools ``BatchProcessor``) and Lambda
retries the batch from that record on; the writes before it are idempotent and events carry an ``idempotencyKey``,
so a re-processed record double-counts nothing, and consumers drop the re-published duplicate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit, single_metric
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

from expenseflow import events, history
from expenseflow.models import ExpenseStatus, HistoryKind

logger = Logger()
tracer = Tracer()
metrics = Metrics()
processor = BatchProcessor(event_type=EventType.DynamoDBStreams)

_DECISION_METRICS: Final[Mapping[str, str]] = {
    ExpenseStatus.APPROVED: "ExpensesApproved",
    ExpenseStatus.REJECTED: "ExpensesRejected",
}


@tracer.capture_method(capture_response=False)
def record_handler(record: DynamoDBRecord) -> None:
    """Process one stream record. Powertools calls it as ``record_handler(record=...)`` for each record."""
    if record.dynamodb is None:
        return
    # Images are deserialised to Python values (numbers as Decimal); an absent image is {} - e.g. OLD on INSERT.
    new_image, old_image = record.dynamodb.new_image, record.dynamodb.old_image
    if not new_image:
        return
    entries: list[events.EventEntry] = []
    if new_image.get("status") != old_image.get("status"):  # includes INSERT: a new expense starts as DRAFT
        entries.extend(_status_changed(new_image, old_image))
    if events.receipt_processed(old_image, new_image):
        entries.append(_receipt_processed(new_image, old_image))
    # Write first, publish second: if publishing fails the record is retried, the writes are skipped as
    # duplicates and the events are published again.
    events.publish(entries)


def _status_changed(new_image: Mapping[str, Any], old_image: Mapping[str, Any]) -> list[events.EventEntry]:
    new_status, old_status = new_image["status"], old_image.get("status")
    if history.record_status_change(new_image, old_image):
        _count_transition(new_status)
    else:
        logger.info("status change already recorded", extra={"expense_id": new_image["id"], "to": new_status})
    detail_type = events.detail_type_for_transition(old_status, new_status)
    if detail_type is None:
        return []
    detail = events.build_expense_detail(new_image, old_image, HistoryKind.STATUS)
    return [events.expense_event(detail_type, detail)]


def _receipt_processed(new_image: Mapping[str, Any], old_image: Mapping[str, Any]) -> events.EventEntry:
    if not history.record_receipt_processed(new_image):
        logger.info("receipt result already recorded", extra={"expense_id": new_image["id"]})
    detail = events.build_expense_detail(new_image, old_image, HistoryKind.RECEIPT)
    return events.expense_event(events.DETAIL_TYPE_RECEIPT_PROCESSED, detail)


def _count_transition(new_status: str) -> None:
    """Business metrics, emitted only for changes recorded for the first time (replays would inflate them).

    ``StatusTransitions`` needs its own dimension value per record (``to``), which is what ``single_metric`` is
    for: the handler-wide ``metrics`` object has one dimension set shared by every metric it flushes.
    """
    with single_metric(
        name="StatusTransitions", unit=MetricUnit.Count, value=1, default_dimensions=metrics.default_dimensions
    ) as metric:
        metric.add_dimension(name="to", value=new_status)
    if decision_metric := _DECISION_METRICS.get(new_status):
        metrics.add_metric(name=decision_metric, unit=MetricUnit.Count, value=1)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    return process_partial_response(event=event, record_handler=record_handler, processor=processor, context=context)
