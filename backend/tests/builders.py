"""Builders for realistic Lambda event payloads and stored items.

Each builder returns what the AWS service really sends to the function (shapes from the AWS documentation), so a
handler test runs the same parsing code as production. They are plain functions, not fixtures: call them inline
and override what the test is about.

Users: three personas with Cognito-like ``sub`` values. Their e-mail addresses use the reserved ``example.com``
domain, so nothing here can ever reach a real mailbox.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final
from urllib.parse import urlencode

from boto3.dynamodb.types import TypeSerializer

from expenseflow import ids, keys
from expenseflow.serialization import json_default

REGION: Final = "us-east-1"
ACCOUNT_ID: Final = "123456789012"  # moto's default account
HTTP_API_ID: Final = "h7t7p9a1b2"
WS_API_ID: Final = "w5s3k7c9d1"
WS_STAGE: Final = "live"
USER_POOL_ID: Final = f"{REGION}_TestPool1"
USER_POOL_CLIENT_ID: Final = "4testclientid0000000000000"

EMPLOYEE_ID: Final = "7a1e5f3c-2b4d-4e8f-9a6b-1c3d5e7f9a01"
EMPLOYEE_EMAIL: Final = "hanako@example.com"
EMPLOYEE_NAME: Final = "Hanako Sato"
MANAGER_ID: Final = "3b9d7f1e-4c6a-4b2d-8e0f-2a4c6e8a0b02"
MANAGER_EMAIL: Final = "kenji@example.com"
MANAGER_NAME: Final = "Kenji Tanaka"
ADMIN_ID: Final = "5c2e8a4f-6d1b-4f3a-9c7e-3b5d7f9b1c03"
ADMIN_EMAIL: Final = "yuki@example.com"
ADMIN_NAME: Final = "Yuki Suzuki"

_EPOCH_2026_10_13: Final = 1_791_867_662  # 2026-10-13T05:01:02Z, a fixed "now" for payload timestamps
_serializer: Final = TypeSerializer()


# --- API Gateway HTTP API (payload format 2.0) + Cognito JWT authorizer ----------------------------------------


def id_token_claims(
    user_id: str = EMPLOYEE_ID,
    *,
    email: str | None = None,
    name: str | None = None,
    groups: Sequence[str] = ("employees",),
) -> dict[str, Any]:
    """Claims of a Cognito ID token, as decoded from the JWT (``cognito:groups`` is a real list here)."""
    claims: dict[str, Any] = {
        "sub": user_id,
        "email": email if email is not None else f"{user_id[:8]}@example.com",
        "email_verified": True,
        "cognito:username": user_id,
        "aud": USER_POOL_CLIENT_ID,
        "iss": f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}",
        "token_use": "id",
        "auth_time": _EPOCH_2026_10_13,
        "iat": _EPOCH_2026_10_13,
        "exp": _EPOCH_2026_10_13 + 3600,
    }
    if name is not None:
        claims["name"] = name
    if groups:  # Cognito omits the claim entirely for a user in no group
        claims["cognito:groups"] = list(groups)
    return claims


def flatten_claims(claims: Mapping[str, Any]) -> dict[str, str]:
    """Claims the way the HTTP API JWT authorizer forwards them: every value is a string.

    Arrays become ``"[a b]"`` (brackets, space separated, no quotes) - the gotcha ``auth.parse_groups`` handles.
    """
    flat: dict[str, str] = {}
    for name, value in claims.items():
        if isinstance(value, list | tuple):
            flat[name] = "[" + " ".join(str(item) for item in value) + "]"
        elif isinstance(value, bool):
            flat[name] = "true" if value else "false"
        else:
            flat[name] = str(value)
    return flat


def http_event(
    method: str,
    path: str,
    *,
    body: Any = None,
    user_id: str = EMPLOYEE_ID,
    email: str | None = None,
    name: str | None = None,
    groups: Sequence[str] = ("employees",),
    claims: Mapping[str, Any] | None = None,
    path_params: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    route_key: str | None = None,
) -> dict[str, Any]:
    """An HTTP API (payload 2.0) request that passed the Cognito JWT authorizer.

    ``body``: a ``str`` is sent verbatim (e.g. to test invalid JSON), anything else is JSON-encoded.
    ``claims`` replaces the generated ID-token claims (e.g. ``user.id_claims`` of a moto Cognito user); list
    values are flattened exactly as API Gateway does. ``route_key`` defaults to ``"{METHOD} {path}"``; pass the
    template (``"GET /expenses/{id}"``) when a test cares.
    """
    method = method.upper()
    token_claims = claims if claims is not None else id_token_claims(user_id, email=email, name=name, groups=groups)
    all_headers = {
        "accept": "application/json",
        "authorization": "Bearer eyJraWQiOiJ0ZXN0In0.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl",
        "host": f"{HTTP_API_ID}.execute-api.{REGION}.amazonaws.com",
        "origin": "http://localhost:5173",
        "user-agent": "pytest",
        "x-forwarded-for": "203.0.113.10",
        "x-forwarded-proto": "https",
    }
    encoded_body: str | None = None
    if body is not None:
        encoded_body = body if isinstance(body, str) else json.dumps(body)
        all_headers["content-type"] = "application/json"
    all_headers.update({name.lower(): value for name, value in (headers or {}).items()})
    route = route_key or f"{method} {path}"
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": route,
        "rawPath": path,
        "rawQueryString": urlencode(query or {}),
        "headers": all_headers,
        "requestContext": {
            "accountId": ACCOUNT_ID,
            "apiId": HTTP_API_ID,
            "authorizer": {"jwt": {"claims": flatten_claims(token_claims), "scopes": None}},
            "domainName": f"{HTTP_API_ID}.execute-api.{REGION}.amazonaws.com",
            "domainPrefix": HTTP_API_ID,
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.10",
                "userAgent": "pytest",
            },
            "requestId": _request_id(),
            "routeKey": route,
            "stage": "$default",
            "time": "13/Oct/2026:05:01:02 +0000",
            "timeEpoch": _EPOCH_2026_10_13 * 1000,
        },
        "isBase64Encoded": False,
    }
    # API Gateway omits these keys (rather than sending null/empty) when there is nothing to send.
    if query:
        event["queryStringParameters"] = dict(query)
    if path_params:
        event["pathParameters"] = dict(path_params)
    if encoded_body is not None:
        event["body"] = encoded_body
    return event


# --- SQS and EventBridge ----------------------------------------------------------------------------------------


def eventbridge_event(
    detail_type: str,
    source: str,
    detail: Mapping[str, Any],
    *,
    event_id: str | None = None,
    time: str = "2026-10-13T05:01:02Z",
    resources: Sequence[str] = (),
) -> dict[str, Any]:
    """An EventBridge event envelope (what a rule delivers to Lambda, SQS or Step Functions)."""
    return {
        "version": "0",
        "id": event_id or str(uuid.uuid4()),
        "detail-type": detail_type,
        "source": source,
        "account": ACCOUNT_ID,
        "time": time,
        "region": REGION,
        "resources": list(resources),
        "detail": json.loads(json.dumps(detail, default=json_default)),
    }


def sqs_event(bodies: Iterable[str | Mapping[str, Any]], *, queue_name: str = "NotificationsQueue") -> dict[str, Any]:
    """An SQS batch for a Lambda event source mapping; mappings (e.g. EventBridge envelopes) are JSON-encoded.

    Message ids are deterministic (``...-000000000001``) so tests can assert ``batchItemFailures``.
    """
    records = []
    for index, body in enumerate(bodies, start=1):
        text = body if isinstance(body, str) else json.dumps(body, default=json_default)
        records.append(
            {
                "messageId": sqs_message_id(index),
                "receiptHandle": f"AQEB{index:08d}handle",
                "body": text,
                "attributes": {
                    "ApproximateReceiveCount": "1",
                    "SentTimestamp": str(_EPOCH_2026_10_13 * 1000),
                    "SenderId": "AIDAEVENTBRIDGE",
                    "ApproximateFirstReceiveTimestamp": str(_EPOCH_2026_10_13 * 1000),
                },
                "messageAttributes": {},
                "md5OfBody": "0" * 32,
                "eventSource": "aws:sqs",
                "eventSourceARN": f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:{queue_name}",
                "awsRegion": REGION,
            }
        )
    return {"Records": records}


def sqs_message_id(index: int) -> str:
    """The ``messageId`` :func:`sqs_event` gives its ``index``-th record (1-based)."""
    return f"00000000-0000-4000-8000-{index:012d}"


# --- DynamoDB Streams --------------------------------------------------------------------------------------------

type StreamRecordSpec = tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None]


def ddb_stream_event(records: Iterable[StreamRecordSpec], *, table_name: str = "ExpenseFlowTable") -> dict[str, Any]:
    """A DynamoDB Streams batch (``NEW_AND_OLD_IMAGES``) from ``(eventName, old_image, new_image)`` tuples.

    Images are plain Python items (``Decimal`` numbers, like boto3 returns them) and are serialised into the
    stream's wire format (``{"S": ...}``, ``{"N": ...}``). INSERT has no ``OldImage``, REMOVE no ``NewImage``.
    Sequence numbers are deterministic: record ``i`` (1-based) gets ``stream_sequence_number(i)``.
    """
    stream_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{table_name}/stream/2026-10-01T00:00:00.000"
    out = []
    for index, (event_name, old_image, new_image) in enumerate(records, start=1):
        image = new_image if new_image is not None else old_image
        if image is None:
            raise ValueError("a stream record needs an old or a new image")
        dynamodb: dict[str, Any] = {
            "ApproximateCreationDateTime": _EPOCH_2026_10_13,
            "Keys": _serialize({keys.PK: image[keys.PK], keys.SK: image[keys.SK]}),
            "SequenceNumber": stream_sequence_number(index),
            "SizeBytes": 512,
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        }
        if new_image is not None:
            dynamodb["NewImage"] = _serialize(new_image)
        if old_image is not None:
            dynamodb["OldImage"] = _serialize(old_image)
        out.append(
            {
                "eventID": uuid.uuid4().hex,
                "eventName": event_name,
                "eventVersion": "1.1",
                "eventSource": "aws:dynamodb",
                "awsRegion": REGION,
                "dynamodb": dynamodb,
                "eventSourceARN": stream_arn,
            }
        )
    return {"Records": out}


def stream_sequence_number(index: int) -> str:
    """The ``SequenceNumber`` :func:`ddb_stream_event` gives its ``index``-th record (the batch item identifier)."""
    return str(111_000_000_000_000_000_000 + index)


# --- API Gateway WebSocket API --------------------------------------------------------------------------------------

_WS_EVENT_TYPES: Final = {"$connect": "CONNECT", "$disconnect": "DISCONNECT"}


def ws_event(
    route_key: str,
    connection_id: str = "Ab1Cd2Ef3GhIJ=",
    *,
    authorizer_context: Mapping[str, Any] | None = None,
    body: Any = None,
    query: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A WebSocket route invocation (``$connect``, ``$disconnect``, ``$default`` or a custom route).

    ``authorizer_context`` is the ``context`` the REQUEST authorizer returned; API Gateway delivers it at
    ``requestContext.authorizer`` together with ``principalId`` (taken from ``userId`` here).
    """
    event_type = _WS_EVENT_TYPES.get(route_key, "MESSAGE")
    request_context: dict[str, Any] = {
        "routeKey": route_key,
        "eventType": event_type,
        "messageDirection": "IN",
        "connectionId": connection_id,
        "connectedAt": _EPOCH_2026_10_13 * 1000,
        "requestTimeEpoch": _EPOCH_2026_10_13 * 1000,
        "requestTime": "13/Oct/2026:05:01:02 +0000",
        "requestId": _request_id(),
        "extendedRequestId": _request_id(),
        "domainName": f"{WS_API_ID}.execute-api.{REGION}.amazonaws.com",
        "apiId": WS_API_ID,
        "stage": WS_STAGE,
        "identity": {"sourceIp": "203.0.113.10", "userAgent": "pytest"},
    }
    if event_type == "MESSAGE":
        request_context["messageId"] = f"{connection_id}msg"
    if event_type == "DISCONNECT":
        request_context["disconnectStatusCode"] = 1001
        request_context["disconnectReason"] = "Going away"
    if authorizer_context is not None:
        request_context["authorizer"] = {
            "principalId": authorizer_context.get("userId", "unknown"),
            **authorizer_context,
            "integrationLatency": 12,
        }
    event: dict[str, Any] = {"requestContext": request_context, "isBase64Encoded": False}
    if event_type == "CONNECT":
        event["headers"] = {"Host": request_context["domainName"], "Sec-WebSocket-Version": "13"}
        if query:
            event["queryStringParameters"] = dict(query)
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def ws_method_arn(route_key: str = "$connect") -> str:
    """``methodArn`` of a WebSocket route: ``arn:aws:execute-api:{region}:{account}:{apiId}/{stage}/{route}``."""
    return f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{WS_API_ID}/{WS_STAGE}/{route_key}"


def ws_authorizer_event(token: str | None, method_arn: str | None = None) -> dict[str, Any]:
    """The REQUEST authorizer event for ``$connect`` (identity source ``route.request.querystring.token``).

    ``token=None`` builds a request without the query string. (In AWS, a missing identity source makes API
    Gateway answer 401 without calling the authorizer; the handler should still reject it.)
    """
    connection_id = "Ab1Cd2Ef3GhIJ="
    event: dict[str, Any] = {
        "type": "REQUEST",
        "methodArn": method_arn or ws_method_arn(),
        "headers": {"Host": f"{WS_API_ID}.execute-api.{REGION}.amazonaws.com", "Sec-WebSocket-Version": "13"},
        "multiValueHeaders": {
            "Host": [f"{WS_API_ID}.execute-api.{REGION}.amazonaws.com"],
            "Sec-WebSocket-Version": ["13"],
        },
        "stageVariables": {},
        "requestContext": {
            "routeKey": "$connect",
            "eventType": "CONNECT",
            "messageDirection": "IN",
            "connectionId": connection_id,
            "connectedAt": _EPOCH_2026_10_13 * 1000,
            "requestTimeEpoch": _EPOCH_2026_10_13 * 1000,
            "requestTime": "13/Oct/2026:05:01:02 +0000",
            "requestId": _request_id(),
            "extendedRequestId": _request_id(),
            "domainName": f"{WS_API_ID}.execute-api.{REGION}.amazonaws.com",
            "apiId": WS_API_ID,
            "stage": WS_STAGE,
            "identity": {"sourceIp": "203.0.113.10", "userAgent": "pytest"},
        },
    }
    if token is not None:
        event["queryStringParameters"] = {"token": token}
        event["multiValueQueryStringParameters"] = {"token": [token]}
    return event


# --- Stored items -----------------------------------------------------------------------------------------------


def expense_item(**overrides: Any) -> dict[str, Any]:
    """A stored expense META item, as the repository writes it and the stream delivers it (numbers as ``Decimal``).

    Overrides replace attributes; an override of ``None`` removes the attribute (optional attributes are absent,
    never null). Key attributes are derived from ``id``, ``ownerId``, ``createdAt``, ``status`` and ``updatedAt``
    unless overridden explicitly. The default is a complete DRAFT that is ready to submit.
    """
    attributes: dict[str, Any] = {
        "entityType": keys.ENTITY_EXPENSE,
        "id": ids.new_id(),
        "ownerId": EMPLOYEE_ID,
        "ownerEmail": EMPLOYEE_EMAIL,
        "ownerName": EMPLOYEE_NAME,
        "title": "Taxi to client meeting",
        "merchant": "Nihon Kotsu",
        "amount": Decimal("1280"),
        "currency": "JPY",
        "category": "TRANSPORT",
        "expenseDate": "2026-10-09",
        "description": "Shinagawa to Otemachi",
        "status": "DRAFT",
        "createdAt": "2026-10-10T01:02:03.456Z",
        "updatedAt": "2026-10-10T01:02:03.456Z",
        "version": Decimal(1),
        "lastActorId": EMPLOYEE_ID,
        "lastActorEmail": EMPLOYEE_EMAIL,
    }
    attributes.update(overrides)
    expense_id = attributes["id"]
    derived = {
        **keys.expense_key(expense_id),
        keys.GSI1PK: keys.owner_gsi1pk(attributes["ownerId"]),
        keys.GSI1SK: keys.owner_gsi1sk(attributes["createdAt"], expense_id),
        keys.GSI2PK: keys.status_gsi2pk(attributes["status"]),
        keys.GSI2SK: keys.status_gsi2sk(attributes["updatedAt"], expense_id),
    }
    return {name: value for name, value in {**derived, **attributes}.items() if value is not None}


# --- Internals --------------------------------------------------------------------------------------------------


def _serialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _serializer.serialize(value) for name, value in item.items()}


def _request_id() -> str:
    return str(uuid.uuid4())
