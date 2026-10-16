"""Push every domain event to the browsers that should see it, over the WebSocket API (SPEC 6, 7.3).

An EventBridge rule (source prefix ``expenseflow.``) invokes this function **asynchronously**: EventBridge does not
wait for the result, Lambda retries a failed invocation (2 retries) and then hands the event to the target's DLQ.

Sending uses the API Gateway **Management API** (``POST @connections/{connectionId}``) on the stage's callback URL.
A connection that is closed answers ``410 GoneException``; that is routine (laptop lid closed, tab gone) and we
delete it. Any other per-connection error is logged and skipped, so one bad connection never blocks the others
and never makes EventBridge retry the event for everyone (which would deliver duplicates to the healthy ones).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.data_classes import EventBridgeEvent, event_source
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from expenseflow import aws, connections, events, keys, settings
from expenseflow.serialization import dumps

logger = Logger()
tracer = Tracer()

_MANAGERS: Final = keys.audience_group(settings.GROUP_MANAGERS)
_ADMINS: Final = keys.audience_group(settings.GROUP_ADMINS)
# Group audiences per event, in addition to the expense owner (SPEC 6).
_GROUP_AUDIENCES: Final[Mapping[str, tuple[str, ...]]] = {
    events.DETAIL_TYPE_EXPENSE_SUBMITTED: (_MANAGERS,),  # the approval queue changed
    events.DETAIL_TYPE_EXPENSE_WITHDRAWN: (_MANAGERS,),
    events.DETAIL_TYPE_APPROVAL_ESCALATED: (_MANAGERS, _ADMINS),
    events.DETAIL_TYPE_REPORT_GENERATED: (_ADMINS,),
}


def audiences(detail_type: str, detail: Mapping[str, Any]) -> list[str]:
    """Who receives an event: always its owner (when it has one), plus the groups of ``_GROUP_AUDIENCES``."""
    owner_id = detail.get("ownerId")
    owner = [keys.audience_user(owner_id)] if owner_id else []
    return owner + list(_GROUP_AUDIENCES.get(detail_type, ()))


def message(event: EventBridgeEvent) -> bytes:
    """The server -> client frame: ``{"type": "event", "source", "detailType", "time", "detail"}`` as UTF-8 JSON."""
    return dumps(
        {
            "type": "event",
            "source": event.source,
            "detailType": event.detail_type,
            "time": event.time,
            "detail": event.detail,
        }
    ).encode()


@tracer.capture_method(capture_response=False)
def push(connection_ids: list[str], data: bytes) -> dict[str, int]:
    """Post ``data`` to every connection; returns counts of ``delivered``, ``gone`` and ``failed`` connections."""
    client = aws.apigw_management(settings.ws_callback_url())
    counts = {"delivered": 0, "gone": 0, "failed": 0}
    for connection_id in connection_ids:
        try:
            client.post_to_connection(ConnectionId=connection_id, Data=data)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code == "GoneException":
                connections.unregister(connection_id)
                counts["gone"] += 1
            else:
                logger.warning("push failed", extra={"connection_id": connection_id, "error_code": code})
                counts["failed"] += 1
        else:
            counts["delivered"] += 1
    return counts


@logger.inject_lambda_context(correlation_id_path=correlation_paths.EVENT_BRIDGE, log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@event_source(data_class=EventBridgeEvent)
def handler(event: EventBridgeEvent, context: LambdaContext) -> dict[str, int]:
    targets = audiences(event.detail_type, event.detail)
    counts = push(connections.connection_ids(targets), message(event))
    logger.info("event pushed", extra={"detail_type": event.detail_type, "audiences": targets, **counts})
    return counts
