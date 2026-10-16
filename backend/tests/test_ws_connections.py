"""WebSocket route integration: ``$connect`` / ``$disconnect`` / ``$default`` and the connection registry (SPEC 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from boto3.dynamodb.conditions import Key

from expenseflow import connections, keys
from handlers import ws_connections
from tests.builders import ADMIN_ID, EMPLOYEE_EMAIL, EMPLOYEE_ID, MANAGER_ID, ws_event
from tests.conftest import FakeClock, FakeLambdaContext

CONNECTION_ID = "Ab1Cd2Ef3GhIJ="
NOW = datetime(2026, 10, 13, 5, 1, 2, 345_000, tzinfo=UTC)  # the `clock` fixture's start


def connection_items(table: Any, connection_id: str = CONNECTION_ID) -> list[dict[str, Any]]:
    return list(table.query(KeyConditionExpression=Key(keys.PK).eq(keys.wsconn_pk(connection_id)))["Items"])


def connect(user_id: str, groups: str, lambda_context: FakeLambdaContext) -> dict[str, Any]:
    context = {"userId": user_id, "email": EMPLOYEE_EMAIL, "groups": groups}
    return dict(ws_connections.handler(ws_event("$connect", CONNECTION_ID, authorizer_context=context), lambda_context))


@pytest.mark.parametrize(
    ("user_id", "groups", "audiences"),
    [
        (EMPLOYEE_ID, "employees", {f"USER#{EMPLOYEE_ID}"}),
        (MANAGER_ID, "employees,managers", {f"USER#{MANAGER_ID}", "GROUP#managers"}),
        (ADMIN_ID, "admins,managers", {f"USER#{ADMIN_ID}", "GROUP#managers", "GROUP#admins"}),
    ],
)
def test_connect_stores_one_item_per_audience(
    user_id: str,
    groups: str,
    audiences: set[str],
    table: Any,
    clock: FakeClock,
    lambda_context: FakeLambdaContext,
) -> None:
    assert connect(user_id, groups, lambda_context) == {"statusCode": 200}

    items = connection_items(table)
    assert {item["audience"] for item in items} == audiences
    expires_at = int(NOW.timestamp()) + 2 * 3600 + 5 * 60
    for item in items:
        assert item == {
            keys.PK: f"WSCONN#{CONNECTION_ID}",
            keys.SK: f"AUD#{item['audience']}",
            keys.GSI1PK: f"WSAUD#{item['audience']}",
            keys.GSI1SK: CONNECTION_ID,
            "entityType": "WsConnection",
            "connectionId": CONNECTION_ID,
            "userId": user_id,
            "audience": item["audience"],
            "connectedAt": "2026-10-13T05:01:02.345Z",
            "expiresAt": expires_at,
        }


def test_connect_without_authorizer_context_is_refused(table: Any, lambda_context: FakeLambdaContext) -> None:
    with pytest.raises(RuntimeError, match="authorizer"):
        ws_connections.handler(ws_event("$connect", CONNECTION_ID), lambda_context)

    assert connection_items(table) == []


def test_disconnect_deletes_every_item_of_the_connection(table: Any, lambda_context: FakeLambdaContext) -> None:
    connect(MANAGER_ID, "managers", lambda_context)
    connections.register("Other1Connection=", EMPLOYEE_ID, [keys.audience_user(EMPLOYEE_ID)])

    assert ws_connections.handler(ws_event("$disconnect", CONNECTION_ID), lambda_context) == {"statusCode": 200}

    assert connection_items(table) == []
    assert len(connection_items(table, "Other1Connection=")) == 1
    # A repeated (or late) $disconnect is harmless.
    assert ws_connections.handler(ws_event("$disconnect", CONNECTION_ID), lambda_context) == {"statusCode": 200}


def test_default_route_accepts_keep_alive_pings(table: Any, lambda_context: FakeLambdaContext) -> None:
    event = ws_event("$default", CONNECTION_ID, body={"action": "ping"})

    assert ws_connections.handler(event, lambda_context) == {"statusCode": 200}
    assert connection_items(table) == []


def test_connection_ids_are_deduplicated_across_audiences(table: Any) -> None:
    connections.register("manager-conn", MANAGER_ID, connections.audiences_for(MANAGER_ID, ["managers"]))
    connections.register("owner-conn", EMPLOYEE_ID, connections.audiences_for(EMPLOYEE_ID, ["employees"]))

    found = connections.connection_ids([keys.audience_user(MANAGER_ID), keys.audience_group("managers")])

    assert found == ["manager-conn"]


def test_expired_connections_are_not_returned_even_before_ttl_deletes_them(table: Any, clock: FakeClock) -> None:
    connections.register("old-conn", EMPLOYEE_ID, [keys.audience_user(EMPLOYEE_ID)])
    clock.set(datetime(2026, 10, 13, 7, 30, tzinfo=UTC))  # 2 h 29 min later: past the 2 h 5 min lifetime

    assert connections.connection_ids([keys.audience_user(EMPLOYEE_ID)]) == []
    assert len(connection_items(table, "old-conn")) == 1  # still stored: moto, like DynamoDB, expires lazily
