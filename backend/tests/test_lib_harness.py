"""The shared test harness itself.

Fixtures must create resources exactly as SPEC section 3 defines them, and the event builders must produce payloads
that Powertools' event source data classes parse like the real ones - otherwise handler tests would pass against
a fiction. These tests pin both down.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import boto3
from aws_lambda_powertools.utilities import parameters
from aws_lambda_powertools.utilities.data_classes import (
    APIGatewayProxyEventV2,
    APIGatewayWebSocketEvent,
    DynamoDBStreamEvent,
    EventBridgeEvent,
    SQSEvent,
)
from aws_lambda_powertools.utilities.data_classes.api_gateway_authorizer_event import APIGatewayAuthorizerRequestEvent
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecordEventName

from expenseflow import aws, ids, keys, settings
from expenseflow.auth import caller_from_claims
from expenseflow.models import missing_required_fields
from tests.builders import (
    EMPLOYEE_ID,
    MANAGER_ID,
    ddb_stream_event,
    eventbridge_event,
    expense_item,
    http_event,
    sqs_event,
    sqs_message_id,
    stream_sequence_number,
    ws_authorizer_event,
    ws_event,
    ws_method_arn,
)
from tests.conftest import FakeClock, FakeLambdaContext, UserPool

# --- Resource fixtures ------------------------------------------------------------------------------------------


def test_table_matches_the_single_table_design(table: Any) -> None:
    client = boto3.client("dynamodb")
    description = client.describe_table(TableName=settings.table_name())["Table"]

    assert table is aws.table()  # the app's own cached resource, so tests can wrap it in a Stubber
    assert description["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    indexes = {index["IndexName"]: index for index in description["GlobalSecondaryIndexes"]}
    assert set(indexes) == {"GSI1", "GSI2"}
    for name, index in indexes.items():
        assert [part["AttributeName"] for part in index["KeySchema"]] == [f"{name}PK", f"{name}SK"]
        assert index["Projection"]["ProjectionType"] == "ALL"
    assert description["StreamSpecification"] == {"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"}
    ttl = client.describe_time_to_live(TableName=settings.table_name())["TimeToLiveDescription"]
    assert ttl["AttributeName"] == "expiresAt"


def test_idempotency_table_has_the_powertools_layout(idempotency_table: str) -> None:
    client = boto3.client("dynamodb")

    assert idempotency_table == settings.idempotency_table_name()
    key_schema = client.describe_table(TableName=idempotency_table)["Table"]["KeySchema"]
    assert key_schema == [{"AttributeName": "id", "KeyType": "HASH"}]
    ttl = client.describe_time_to_live(TableName=idempotency_table)["TimeToLiveDescription"]
    assert ttl["AttributeName"] == "expiration"


def test_buckets_exist_under_the_configured_names(receipts_bucket: str, reports_bucket: str) -> None:
    names = {bucket["Name"] for bucket in boto3.client("s3").list_buckets()["Buckets"]}

    assert {receipts_bucket, reports_bucket} == names
    assert (receipts_bucket, reports_bucket) == (settings.receipts_bucket(), settings.reports_bucket())


def test_auto_approve_param_is_readable_through_powertools(auto_approve_param: str) -> None:
    assert auto_approve_param == f"/expenseflow/{settings.stage()}/auto-approve-limit"
    assert parameters.get_parameter(auto_approve_param) == "5000"


def test_approval_state_machine_supports_start_and_stop(approval_state_machine: str) -> None:
    sfn = aws.client("stepfunctions")
    execution = sfn.start_execution(stateMachineArn=approval_state_machine, input="{}")["executionArn"]

    sfn.stop_execution(executionArn=execution, error="Withdrawn")

    assert sfn.describe_execution(executionArn=execution)["status"] == "ABORTED"


def test_report_function_accepts_async_invocations(report_function: str) -> None:
    response = aws.client("lambda").invoke(FunctionName=report_function, InvocationType="Event", Payload=b"{}")

    assert response["StatusCode"] == 202


# --- Cognito ----------------------------------------------------------------------------------------------------


def test_user_pool_issues_real_tokens(user_pool: UserPool) -> None:
    user = user_pool.create_user("kenji@example.com", groups=("managers", "employees"), name="Kenji Tanaka")

    assert (settings.user_pool_id(), settings.user_pool_client_id()) == (user_pool.pool_id, user_pool.client_id)
    assert user.id_claims["sub"] == user.sub
    assert user.id_claims["email"] == "kenji@example.com"
    assert set(user.id_claims["cognito:groups"]) == {"managers", "employees"}
    assert user.access_claims["token_use"] == "access"
    assert user.access_claims["client_id"] == settings.user_pool_client_id()
    assert user.access_claims["iss"].endswith(f"/{settings.user_pool_id()}")
    # GetUser is authorised by the access token alone: the unsigned client the WebSocket authorizer uses works.
    profile = aws.cognito_unsigned().get_user(AccessToken=user.access_token)
    attributes = {attribute["Name"]: attribute["Value"] for attribute in profile["UserAttributes"]}
    assert attributes["sub"] == user.sub


def test_http_event_flattens_moto_token_claims_like_api_gateway(user_pool: UserPool) -> None:
    user = user_pool.create_user("kenji@example.com", groups=("managers", "employees"), name="Kenji Tanaka")

    event = APIGatewayProxyEventV2(http_event("GET", "/me", claims=user.id_claims))

    claims = event.request_context.authorizer.jwt_claim
    assert isinstance(claims["cognito:groups"], str)
    assert claims["cognito:groups"].startswith("[")
    caller = caller_from_claims(claims)
    assert caller.user_id == user.sub
    assert set(caller.groups) == {"managers", "employees"}
    assert caller.is_manager


# --- Event builders ---------------------------------------------------------------------------------------------


def test_http_event_parses_as_payload_v2() -> None:
    expense_id = ids.new_id()
    raw = http_event(
        "patch",
        f"/expenses/{expense_id}",
        body={"title": "Taxi", "amount": 1280},
        user_id=MANAGER_ID,
        groups=("managers", "employees"),
        path_params={"id": expense_id},
        query={"limit": "5"},
        route_key="PATCH /expenses/{id}",
    )

    event = APIGatewayProxyEventV2(raw)

    assert (event.http_method, event.path, event.route_key) == ("PATCH", f"/expenses/{expense_id}", raw["routeKey"])
    assert event.json_body == {"title": "Taxi", "amount": 1280}
    assert event.path_parameters == {"id": expense_id}
    assert event.query_string_parameters == {"limit": "5"}
    assert event.raw_query_string == "limit=5"
    authorizer = event.request_context.authorizer
    assert authorizer.jwt_claim["sub"] == MANAGER_ID
    assert authorizer.jwt_claim["cognito:groups"] == "[managers employees]"
    assert authorizer.jwt_scopes is None


def test_http_event_without_groups_omits_the_claim() -> None:
    claims = APIGatewayProxyEventV2(http_event("GET", "/me", groups=())).request_context.authorizer.jwt_claim

    assert "cognito:groups" not in claims
    assert caller_from_claims(claims).groups == ()


def test_ddb_stream_event_round_trips_images() -> None:
    old = expense_item()
    new = expense_item(id=old["id"], status="PENDING_APPROVAL", version=Decimal(2), amount=Decimal("12.50"))
    inserted = expense_item()

    records = list(DynamoDBStreamEvent(ddb_stream_event([("MODIFY", old, new), ("INSERT", None, inserted)])).records)

    modify, insert = records
    assert modify.event_name is DynamoDBRecordEventName.MODIFY
    assert modify.dynamodb is not None
    assert modify.dynamodb.new_image == new  # Decimal numbers, like boto3 reads
    assert modify.dynamodb.old_image == old
    assert modify.dynamodb.keys == {keys.PK: old[keys.PK], keys.SK: keys.META}
    assert modify.dynamodb.sequence_number == stream_sequence_number(1)
    assert insert.event_name is DynamoDBRecordEventName.INSERT
    assert insert.dynamodb is not None
    assert insert.dynamodb.old_image == {}


def test_sqs_event_carries_eventbridge_envelopes() -> None:
    envelope = eventbridge_event("Expense Submitted", "expenseflow.expenses", {"amount": Decimal("12.5")})

    records = list(SQSEvent(sqs_event([envelope, "not json"])).records)

    assert [record.message_id for record in records] == [sqs_message_id(1), sqs_message_id(2)]
    delivered = EventBridgeEvent(records[0].json_body)
    assert (delivered.source, delivered.detail_type) == ("expenseflow.expenses", "Expense Submitted")
    assert delivered.detail == {"amount": 12.5}  # Decimals become JSON numbers, as EventBridge delivers them
    assert records[1].body == "not json"


def test_ws_event_for_connect_carries_the_authorizer_context() -> None:
    context = {"userId": EMPLOYEE_ID, "email": "hanako@example.com", "groups": "employees"}
    raw = ws_event("$connect", "conn-1=", authorizer_context=context, query={"token": "t"})

    event = APIGatewayWebSocketEvent(raw)

    assert (event.request_context.route_key, event.request_context.connection_id) == ("$connect", "conn-1=")
    assert event.request_context.event_type == "CONNECT"
    assert raw["requestContext"]["authorizer"]["principalId"] == EMPLOYEE_ID
    assert raw["requestContext"]["authorizer"]["groups"] == "employees"


def test_ws_event_for_messages_has_a_body() -> None:
    event = APIGatewayWebSocketEvent(ws_event("$default", body={"action": "ping"}))

    assert event.request_context.event_type == "MESSAGE"
    assert event.json_body == {"action": "ping"}


def test_ws_authorizer_event() -> None:
    event = APIGatewayAuthorizerRequestEvent(ws_authorizer_event("token-123"))

    assert event.method_arn == ws_method_arn("$connect")
    assert event.query_string_parameters == {"token": "token-123"}
    assert "queryStringParameters" not in ws_authorizer_event(None)


def test_expense_item_is_a_complete_draft_with_derived_keys() -> None:
    item = expense_item(status="REJECTED", description=None)

    assert missing_required_fields(item) == []
    assert "description" not in item  # None removes the attribute: optional attributes are absent, never null
    assert item[keys.PK] == keys.expense_pk(item["id"])
    assert item[keys.GSI1SK] == keys.owner_gsi1sk(item["createdAt"], item["id"])
    assert item[keys.GSI2PK] == "STATUS#REJECTED"


# --- Clock and Lambda context -----------------------------------------------------------------------------------


def test_clock_freezes_time_and_advances_per_reading(clock: FakeClock) -> None:
    assert ids.now_iso() == "2026-10-13T05:01:02.345Z"
    assert ids.now_iso() == "2026-10-13T05:01:02.346Z"


def test_lambda_context(lambda_context: FakeLambdaContext) -> None:
    assert lambda_context.get_remaining_time_in_millis() == 30_000
    assert lambda_context.invoked_function_arn.endswith(f":function:{lambda_context.function_name}")
