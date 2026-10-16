"""E-mail notifications: SQS ``notifications`` queue -> SES (SPEC 7.2).

Why a queue between EventBridge and this function: the queue absorbs bursts, retries failed messages with a
visibility timeout, and parks poison messages in a DLQ after 3 receives - and the event source mapping hands us
up to 10 messages per invocation. Each SQS body is the EventBridge envelope (no input transformer).

**Partial batch failures.** ``process_partial_response`` reports only the failed message ids
(``batchItemFailures``); SQS deletes the rest. Without it, one failing message would redeliver all ten.

**Idempotency.** SQS is at-least-once and the stream processor may publish an event twice, so the same
notification can arrive more than once. Powertools ``idempotent_function`` stores a record per
``detail.idempotencyKey`` in the idempotency table (24 h): a duplicate returns the stored result instead of
e-mailing people again. The key comes from the business change, not from the SQS message id, so a re-published
copy of the same change is caught too.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Any, Final

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes import EventBridgeEvent
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

from expenseflow import aws, notify, settings

logger = Logger()
tracer = Tracer()
metrics = Metrics()
processor = BatchProcessor(event_type=EventType.SQS)

IDEMPOTENCY_TTL_SECONDS: Final = 24 * 60 * 60
idempotency_config = IdempotencyConfig(
    # JMESPath into the record: `powertools_json()` parses the SQS body (a JSON string) so we can reach the detail.
    event_key_jmespath="powertools_json(body).detail.idempotencyKey",
    expires_after_seconds=IDEMPOTENCY_TTL_SECONDS,
    raise_on_no_idempotency_key=True,  # a message without a key is malformed: fail it (-> DLQ), don't guess
)


@tracer.capture_method(capture_response=False)
def notify_record(record: SQSRecord) -> dict[str, Any]:
    """E-mail the recipients of one event. The return value is what an idempotent replay returns (JSON only)."""
    event = EventBridgeEvent(record.json_body)
    detail_type, detail = event.detail_type, event.detail
    recipients = notify.recipients(detail_type, detail)
    outcome: dict[str, Any] = {"detailType": detail_type, "recipients": len(recipients), "sent": 0}
    if not recipients:
        return outcome

    email = notify.compose(detail_type, detail, settings.app_url())
    sender = settings.sender_email()
    if sender is None:
        logger.info(
            "e-mail disabled (no SENDER_EMAIL): would have sent",
            extra={"detail_type": detail_type, "to": recipients, "subject": email.subject, "text": email.text},
        )
        metrics.add_metric(name="NotificationsSkipped", unit=MetricUnit.Count, value=len(recipients))
        return outcome

    for recipient in recipients:
        if notify.send(sender, recipient, email):
            outcome["sent"] += 1
        else:
            logger.warning(
                "SES rejected the e-mail; in the SES sandbox every recipient must be a verified identity",
                extra={"detail_type": detail_type, "to": recipient},
            )
    metrics.add_metric(name="NotificationsSent", unit=MetricUnit.Count, value=outcome["sent"])
    return outcome


@cache
def _idempotent_notify() -> Callable[..., dict[str, Any]]:
    """``notify_record`` wrapped with Powertools idempotency, built on first use.

    Built lazily rather than with a module-level decorator because the persistence layer needs the table name
    (an environment variable) and a boto3 client, and neither belongs in import-time code. ``@cache`` keeps ONE
    persistence layer for the life of the execution environment - one instance per ``IdempotencyConfig`` (an
    instance silently keeps the first config it was used with).
    """
    persistence = DynamoDBPersistenceLayer(
        table_name=settings.idempotency_table_name(), boto3_client=aws.client("dynamodb")
    )
    wrapped: Callable[..., dict[str, Any]] = idempotent_function(
        data_keyword_argument="record", persistence_store=persistence, config=idempotency_config
    )(notify_record)
    return wrapped


def record_handler(record: SQSRecord) -> dict[str, Any]:
    # The idempotent wrapper finds its data by keyword, so `record=` must be passed by name.
    return _idempotent_notify()(record=record)


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    # Lets idempotency expire an IN_PROGRESS record when this invocation times out, so a retry is not blocked.
    idempotency_config.register_lambda_context(context)
    return process_partial_response(event=event, record_handler=record_handler, processor=processor, context=context)
