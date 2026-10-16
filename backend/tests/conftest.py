"""Shared pytest configuration: test environment, moto, AWS resource fixtures and a fake Lambda context.

ORDER MATTERS in this file:

1. Environment variables are set at import time, before anything reads them (Powertools reads some at import).
2. moto is imported BEFORE any boto3 Session exists. Importing moto registers its request interceptor in
   botocore's built-in handlers; only sessions created afterwards pick it up. A client from an earlier session
   would talk to real AWS even inside ``mock_aws``.
3. Application modules are imported lazily inside fixtures, never at the top of this file.

Every test runs inside a fresh, empty moto account (autouse ``aws`` fixture). Resource fixtures each create one
resource from SPEC section 3/4 in that account and compose freely::

    def test_submit_publishes(table, events_sink): ...

Fixtures: ``aws`` (autouse), ``lambda_context``, ``clock``, ``table``, ``idempotency_table``, ``receipts_bucket``,
``reports_bucket``, ``event_bus``, ``events_sink``, ``user_pool``, ``auto_approve_param``,
``approval_state_machine``, ``report_function``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import uuid
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------------------------------------------
# 1. Environment
# ---------------------------------------------------------------------------------------------------------------

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"  # moto's default account (moto.core.DEFAULT_ACCOUNT_ID)
STAGE = "test"

TEST_ENV: dict[str, str] = {
    # AWS SDK: fake credentials and a fixed region; never read ~/.aws or the instance metadata service.
    "AWS_DEFAULT_REGION": REGION,
    "AWS_REGION": REGION,
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
    # Powertools: no X-Ray, no metric flush to stdout, idempotency bypassed unless a test opts in
    # (monkeypatch.delenv("POWERTOOLS_IDEMPOTENCY_DISABLED") together with the `idempotency_table` fixture).
    "POWERTOOLS_SERVICE_NAME": "expenseflow-test",
    "POWERTOOLS_METRICS_NAMESPACE": "ExpenseFlow",
    "POWERTOOLS_TRACE_DISABLED": "true",
    "POWERTOOLS_METRICS_DISABLED": "true",
    "POWERTOOLS_IDEMPOTENCY_DISABLED": "true",
    "POWERTOOLS_LOG_LEVEL": "INFO",
    # Application configuration: every variable of SPEC section 7, with the names CDK injects.
    "STAGE": STAGE,
    "TABLE_NAME": "expenseflow-test-table",
    "IDEMPOTENCY_TABLE_NAME": "expenseflow-test-idempotency",
    "RECEIPTS_BUCKET": "expenseflow-test-receipts",
    "REPORTS_BUCKET": "expenseflow-test-reports",
    "EVENT_BUS_NAME": "expenseflow-test-bus",
    "USER_POOL_ID": f"{REGION}_TestPool1",  # replaced by the `user_pool` fixture with moto's generated id
    "USER_POOL_CLIENT_ID": "4testclientid0000000000000",
    "REPORT_FUNCTION_NAME": "expenseflow-test-report-generator",
    "WS_CALLBACK_URL": f"https://w5s3k7c9d1.execute-api.{REGION}.amazonaws.com/live",
    "SENDER_EMAIL": "",  # e-mail off, as in a default deployment; tests of the SES path set a sender
    "APP_URL": "https://d1234abcd.cloudfront.net",
    "MAX_RECEIPT_BYTES": "3500000",
    "DEFAULT_CURRENCY": "JPY",
    "BEDROCK_REGION": REGION,
    "EXTRACTION_MODEL_ID": "us.anthropic.claude-opus-5-5",
    "EXTRACTION_FALLBACK_MODEL_ID": "us.anthropic.claude-opus-4-8",
    "DEFAULT_GROUP": "employees",
}

# Anything that could redirect credentials or endpoints must go: moto only intercepts the default
# *.amazonaws.com endpoints, so e.g. AWS_ENDPOINT_URL would send real traffic past it.
for _name in list(os.environ):
    if _name in {
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_LAMBDA_FUNCTION_NAME",
        "AWS_LAMBDA_TASK_ROOT",
    } or _name.startswith(("AWS_ENDPOINT_URL", "AWS_CONTAINER_")):
        del os.environ[_name]
os.environ.update(TEST_ENV)

# ---------------------------------------------------------------------------------------------------------------
# 2. moto before any boto3 Session
# ---------------------------------------------------------------------------------------------------------------

import boto3  # noqa: E402  (importing the module is harmless; creating a Session before moto is not)
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

boto3.DEFAULT_SESSION = None  # drop a default session a plugin may have created before moto was imported

# ---------------------------------------------------------------------------------------------------------------
# 3. Core fixtures
# ---------------------------------------------------------------------------------------------------------------


def _reset_app_caches() -> None:
    from aws_lambda_powertools.utilities import parameters

    from expenseflow import aws as app_aws

    app_aws.reset_clients()
    parameters.clear_caches()  # Powertools caches SSM values at module level, across tests


@pytest.fixture(autouse=True)
def aws() -> Iterator[None]:
    """Run the test in a fresh, empty moto account; the app's cached clients are rebuilt inside it.

    ``use_docker: False`` selects moto's simple Lambda backend: ``invoke`` echoes instead of running a container.
    """
    _reset_app_caches()
    with mock_aws(config={"lambda": {"use_docker": False}}):
        yield
    _reset_app_caches()


@pytest.fixture(autouse=True)
def _no_cold_start_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ColdStart metric is printed with ``single_metric``, which ignores ``POWERTOOLS_METRICS_DISABLED``."""
    from aws_lambda_powertools.metrics.provider import cold_start

    monkeypatch.setattr(cold_start, "is_cold_start", False)


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    """Start every test with an empty metric set.

    Powertools keeps metrics in a class-level set that ``log_metrics`` clears only when it flushes, and with
    ``POWERTOOLS_METRICS_DISABLED`` it never flushes, so metrics would pile up across tests.
    """
    from aws_lambda_powertools import Metrics

    Metrics().clear_metrics()


@dataclass
class FakeLambdaContext:
    """The attributes of the Lambda context object that Powertools and the handlers read."""

    function_name: str = "expenseflow-test-function"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:expenseflow-test-function"
    aws_request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    log_group_name: str = "/aws/lambda/expenseflow-test-function"
    log_stream_name: str = "2026/10/13/[$LATEST]0123456789abcdef"
    remaining_time_in_millis: int = 30_000

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_time_in_millis


@pytest.fixture
def lambda_context() -> FakeLambdaContext:
    return FakeLambdaContext()


class FakeClock:
    """Deterministic stand-in for ``expenseflow.ids.utc_now``.

    Each reading returns the current time and then moves it forward by ``step`` (1 ms by default), so items
    created one after another get distinct, increasing timestamps - sort orders become deterministic.
    """

    def __init__(self, start: datetime, step: timedelta = timedelta(milliseconds=1)) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> datetime:
        current = self.now
        self.now += self.step
        return current

    def set(self, moment: datetime) -> None:
        self.now = moment

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Freeze ``expenseflow.ids`` time at 2026-10-13T05:01:02.345Z (14:01 in Tokyo), advancing 1 ms per reading."""
    from expenseflow import ids

    fake = FakeClock(datetime(2026, 10, 13, 5, 1, 2, 345_000, tzinfo=UTC))
    monkeypatch.setattr(ids, "utc_now", fake)
    return fake


# ---------------------------------------------------------------------------------------------------------------
# 4. AWS resources (SPEC sections 3 and 4). Names come from the environment above, as in Lambda.
# ---------------------------------------------------------------------------------------------------------------


def _gsi(name: str) -> dict[str, Any]:
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": f"{name}PK", "KeyType": "HASH"},
            {"AttributeName": f"{name}SK", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


@pytest.fixture
def table(aws: None) -> Any:
    """The single table exactly as SPEC section 3 defines it; returns the app's own cached ``Table`` resource.

    Being the same object the code under test uses, ``table.meta.client`` can be wrapped in a botocore Stubber.
    """
    from expenseflow import aws as app_aws
    from expenseflow import settings

    client = boto3.client("dynamodb")
    name = settings.table_name()
    client.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": attribute, "AttributeType": "S"}
            for attribute in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")
        ],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[_gsi("GSI1"), _gsi("GSI2")],
        StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
    )
    # moto records the TTL setting but never expires items.
    client.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"})
    return app_aws.table()


@pytest.fixture
def idempotency_table(aws: None) -> str:
    """The Powertools idempotency table (partition key ``id``, TTL ``expiration``); returns its name."""
    from expenseflow import settings

    client = boto3.client("dynamodb")
    name = settings.idempotency_table_name()
    client.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    client.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiration"})
    return name


def _create_bucket(name: str) -> str:
    boto3.client("s3").create_bucket(Bucket=name)  # us-east-1 takes no LocationConstraint
    return name


@pytest.fixture
def receipts_bucket(aws: None) -> str:
    """The receipts bucket; returns its name. (moto does not enforce presigned POST policies: decode them.)"""
    from expenseflow import settings

    return _create_bucket(settings.receipts_bucket())


@pytest.fixture
def reports_bucket(aws: None) -> str:
    from expenseflow import settings

    return _create_bucket(settings.reports_bucket())


@pytest.fixture
def event_bus(aws: None) -> str:
    """The custom EventBridge bus; returns its name."""
    from expenseflow import settings

    name = settings.event_bus_name()
    boto3.client("events").create_event_bus(Name=name)
    return name


type EventsSink = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def events_sink(event_bus: str) -> EventsSink:
    """Capture everything published on the custom bus: a catch-all rule targets an SQS queue.

    Returns a function that drains the queue and returns the received EventBridge envelopes
    (``source``, ``detail-type``, ``detail`` as a dict, ...). The order is not guaranteed.
    """
    events, sqs = boto3.client("events"), boto3.client("sqs")
    queue_url = sqs.create_queue(QueueName="events-sink")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    events.put_rule(Name="events-sink", EventBusName=event_bus, EventPattern=json.dumps({"account": [ACCOUNT_ID]}))
    events.put_targets(Rule="events-sink", EventBusName=event_bus, Targets=[{"Id": "sink", "Arn": queue_arn}])

    def received() -> list[dict[str, Any]]:
        envelopes: list[dict[str, Any]] = []
        while messages := sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", []):
            for message in messages:
                envelopes.append(json.loads(message["Body"]))
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        return envelopes

    return received


@dataclass(frozen=True)
class CognitoUser:
    """A confirmed user of the moto user pool with real (moto-signed) tokens."""

    sub: str
    email: str
    name: str
    groups: tuple[str, ...]
    id_token: str = field(repr=False)
    access_token: str = field(repr=False)

    @property
    def id_claims(self) -> dict[str, Any]:
        """Decoded ID-token payload (what the HTTP API JWT authorizer forwards, before string flattening)."""
        return _jwt_payload(self.id_token)

    @property
    def access_claims(self) -> dict[str, Any]:
        return _jwt_payload(self.access_token)


def _jwt_payload(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return decoded


@dataclass
class UserPool:
    pool_id: str
    client_id: str
    idp: Any = field(repr=False)

    def create_user(
        self,
        email: str,
        *,
        groups: Sequence[str] = ("employees",),
        name: str | None = None,
        password: str = "Correct-Horse-Battery-9",
    ) -> CognitoUser:
        """Create a CONFIRMED user in ``groups`` and sign in (``ADMIN_USER_PASSWORD_AUTH``) to obtain tokens."""
        display_name = name or email.split("@")[0].title()
        self.idp.admin_create_user(
            UserPoolId=self.pool_id,
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": display_name},
            ],
        )
        self.idp.admin_set_user_password(UserPoolId=self.pool_id, Username=email, Password=password, Permanent=True)
        for group in groups:
            self.idp.admin_add_user_to_group(UserPoolId=self.pool_id, Username=email, GroupName=group)
        tokens = self.idp.admin_initiate_auth(
            UserPoolId=self.pool_id,
            ClientId=self.client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )["AuthenticationResult"]
        sub = _jwt_payload(tokens["IdToken"])["sub"]
        return CognitoUser(
            sub=sub,
            email=email,
            name=display_name,
            groups=tuple(groups),
            id_token=tokens["IdToken"],
            access_token=tokens["AccessToken"],
        )


@pytest.fixture
def user_pool(aws: None, monkeypatch: pytest.MonkeyPatch) -> UserPool:
    """A user pool + app client (admin password auth enabled) + the three groups; sets USER_POOL_ID/CLIENT_ID."""
    from expenseflow import settings

    idp = boto3.client("cognito-idp")
    pool_id = idp.create_user_pool(PoolName="expenseflow-test", UsernameAttributes=["email"])["UserPool"]["Id"]
    client_id = idp.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="web",
        GenerateSecret=False,
        ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )["UserPoolClient"]["ClientId"]
    for group in settings.GROUPS:
        idp.create_group(UserPoolId=pool_id, GroupName=group)
    monkeypatch.setenv("USER_POOL_ID", pool_id)  # moto generates the ids, so they are set at runtime
    monkeypatch.setenv("USER_POOL_CLIENT_ID", client_id)
    return UserPool(pool_id=pool_id, client_id=client_id, idp=idp)


@pytest.fixture
def auto_approve_param(aws: None) -> str:
    """SSM parameter ``/expenseflow/{stage}/auto-approve-limit`` = ``5000``; returns its name."""
    name = f"/expenseflow/{STAGE}/auto-approve-limit"
    boto3.client("ssm").put_parameter(Name=name, Value="5000", Type="String")
    return name


def _service_role(name: str, service: str) -> str:
    """An IAM role ``service`` may assume (moto requires an existing role for functions and state machines)."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}],
    }
    role = boto3.client("iam").create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
    return str(role["Role"]["Arn"])


@pytest.fixture
def approval_state_machine(aws: None) -> str:
    """A registered (never executed) state machine standing in for the ApprovalWorkflow; returns its ARN.

    moto supports start/describe/stop of executions; ``send_task_*`` accept any token and do nothing, so error
    paths (``TaskTimedOut``, ``InvalidToken``) need a botocore Stubber on ``expenseflow.aws.client("stepfunctions")``.
    """
    definition = {"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}}
    return str(
        boto3.client("stepfunctions").create_state_machine(
            name="ApprovalWorkflow-test",
            definition=json.dumps(definition),
            roleArn=_service_role("approval-workflow-role", "states.amazonaws.com"),
        )["stateMachineArn"]
    )


@pytest.fixture
def report_function(aws: None) -> str:
    """A Lambda function named ``REPORT_FUNCTION_NAME`` so ``invoke(InvocationType="Event")`` succeeds."""
    from expenseflow import settings

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("index.py", "def handler(event, context):\n    return event\n")
    name = settings.report_function_name()
    boto3.client("lambda").create_function(
        FunctionName=name,
        Runtime="python3.13",
        Role=_service_role("report-generator-role", "lambda.amazonaws.com"),
        Handler="index.handler",
        Code={"ZipFile": archive.getvalue()},
    )
    return name
