"""Registry of open WebSocket connections, stored in the single table (SPEC 3 and 6).

API Gateway keeps the WebSocket open, but it only tells our code about a connection when it opens (``$connect``),
when it closes (``$disconnect``) and when a message arrives. To *push* a message later we must remember who is
connected, so each connection gets one item per **audience** it should receive events for::

    PK WSCONN#{connectionId}   SK AUD#USER#{userId}        GSI1 WSAUD#USER#{userId} / {connectionId}
    PK WSCONN#{connectionId}   SK AUD#GROUP#managers       GSI1 WSAUD#GROUP#managers / {connectionId}

"Who listens to audience X?" is a GSI1 query; "forget connection C" is a query on its partition + batch delete.

Clean-up has two safety nets, because ``$disconnect`` is best effort (AWS documents it may not be delivered):
the items carry a TTL (``expiresAt``) a little beyond API Gateway's hard 2-hour connection limit, and a push that
gets ``410 Gone`` deletes the connection on the spot.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import timedelta
from typing import Any, Final

from boto3.dynamodb.conditions import Attr, Key

from expenseflow import aws, ids, keys, settings

# API Gateway closes every WebSocket after 2 hours (not adjustable); the extra minutes avoid an early expiry.
CONNECTION_LIFETIME: Final = timedelta(hours=2, minutes=5)
# Groups whose members also receive group-wide events (e.g. new expenses waiting for approval).
_BROADCAST_GROUPS: Final = (settings.GROUP_MANAGERS, settings.GROUP_ADMINS)


def audiences_for(user_id: str, groups: Iterable[str]) -> list[str]:
    """``USER#{userId}`` plus ``GROUP#managers`` / ``GROUP#admins`` for members of those groups."""
    memberships = set(groups)
    return [keys.audience_user(user_id)] + [
        keys.audience_group(group) for group in _BROADCAST_GROUPS if group in memberships
    ]


def register(connection_id: str, user_id: str, audiences: Iterable[str]) -> None:
    """Store one item per audience for a newly opened connection."""
    now = ids.utc_now()
    connected_at, expires_at = ids.to_iso(now), ids.epoch_seconds(now + CONNECTION_LIFETIME)
    with aws.table().batch_writer() as batch:
        for audience in audiences:
            batch.put_item(
                Item={
                    keys.PK: keys.wsconn_pk(connection_id),
                    keys.SK: keys.aud_sk(audience),
                    keys.GSI1PK: keys.wsaud_gsi1pk(audience),
                    keys.GSI1SK: connection_id,
                    "entityType": keys.ENTITY_WS_CONNECTION,
                    "connectionId": connection_id,
                    "userId": user_id,
                    "audience": audience,
                    "connectedAt": connected_at,
                    "expiresAt": expires_at,  # DynamoDB TTL: epoch SECONDS (milliseconds would mean "year 50000")
                }
            )


def unregister(connection_id: str) -> int:
    """Delete every item of a connection; returns how many were deleted (0 if it was already gone).

    ``batch_writer`` groups deletes into ``BatchWriteItem`` calls of at most 25 and re-sends ``UnprocessedItems``
    (batch writes may partially succeed under throttling - a classic gotcha when calling the API directly).
    """
    table = aws.table()
    response = table.query(KeyConditionExpression=Key(keys.PK).eq(keys.wsconn_pk(connection_id)))
    items = response["Items"]  # at most one item per audience: always a single page
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={keys.PK: item[keys.PK], keys.SK: item[keys.SK]})
    return len(items)


def connection_ids(audiences: Iterable[str]) -> list[str]:
    """Open connections of the given audiences, de-duplicated (a manager's connection is in several audiences).

    TTL deletion is a background process that can lag expiry by a day or more, so expired items are filtered out
    here: correctness must never depend on *when* TTL actually deletes an item.
    """
    now = ids.epoch_seconds()
    found: dict[str, None] = {}  # an insertion-ordered set
    for audience in audiences:
        for item in _query_all(
            IndexName=keys.GSI1,
            KeyConditionExpression=Key(keys.GSI1PK).eq(keys.wsaud_gsi1pk(audience)),
            FilterExpression=Attr("expiresAt").gt(now),
        ):
            found.setdefault(item[keys.GSI1SK], None)
    return list(found)


def _query_all(**request: Any) -> Iterator[dict[str, Any]]:
    table = aws.table()
    while True:
        response = table.query(**request)
        yield from response["Items"]
        if "LastEvaluatedKey" not in response:
            return
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]
