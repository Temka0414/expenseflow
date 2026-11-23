"""Backend stack: functions, event wiring, APIs, both state machines, monitoring and cdk-nag."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from aws_cdk import App
from aws_cdk.assertions import Match, Template

from expenseflow_infra.config import StageConfig
from expenseflow_infra.stacks.auth_stack import AuthStack
from expenseflow_infra.stacks.backend_stack import BackendStack
from expenseflow_infra.stacks.data_stack import DataStack
from expenseflow_infra.stacks.edge_stack import EdgeStack
from tests.conftest import TEST_ACCOUNT, TEST_REGION, AppFactory, NagCheck

type Resources = Mapping[str, Mapping[str, Any]]

COMMON_ENV = {"STAGE", "POWERTOOLS_SERVICE_NAME", "POWERTOOLS_METRICS_NAMESPACE"}
FUNCTION_ENV = {
    "handlers.api.app.handler": {
        "TABLE_NAME",
        "RECEIPTS_BUCKET",
        "REPORTS_BUCKET",
        "REPORT_FUNCTION_NAME",
        "MAX_RECEIPT_BYTES",
        "DEFAULT_CURRENCY",
    },
    "handlers.stream_processor.handler": {"TABLE_NAME", "EVENT_BUS_NAME"},
    "handlers.notifier.handler": {"IDEMPOTENCY_TABLE_NAME", "USER_POOL_ID", "SENDER_EMAIL", "APP_URL"},
    "handlers.realtime_push.handler": {"TABLE_NAME", "WS_CALLBACK_URL"},
    "handlers.ws_authorizer.handler": {"USER_POOL_ID", "USER_POOL_CLIENT_ID"},
    "handlers.ws_connections.handler": {"TABLE_NAME"},
    "handlers.extract_receipt.handler": {
        "RECEIPTS_BUCKET",
        "BEDROCK_REGION",
        "EXTRACTION_MODEL_ID",
        "EXTRACTION_FALLBACK_MODEL_ID",
        "MAX_RECEIPT_BYTES",
    },
    "handlers.approval_request.handler": {"TABLE_NAME"},
    "handlers.report_generator.handler": {"TABLE_NAME", "REPORTS_BUCKET", "EVENT_BUS_NAME"},
}
HTTP_ROUTES = {
    "GET /me",
    "GET /expenses",
    "POST /expenses",
    "GET /expenses/{id}",
    "PATCH /expenses/{id}",
    "DELETE /expenses/{id}",
    "POST /expenses/{id}/receipt-upload",
    "GET /expenses/{id}/receipt",
    "POST /expenses/{id}/submit",
    "POST /expenses/{id}/withdraw",
    "GET /approvals",
    "POST /approvals/{id}/decision",
    "GET /stats",
    "GET /reports",
    "POST /reports",
    "GET /reports/{month}",
}
APPROVAL_STATES = {
    "Init": "Pass",
    "ClaimExpense": "Task",
    "LoadPolicy": "Task",
    "CanAutoApprove": "Choice",
    "AutoApprove": "Task",
    "RequestApproval": "Task",
    "Escalate": "Task",
    "RequestEscalatedApproval": "Task",
    "ExpireRequest": "Task",
    "RecordDecision": "Task",
    "AlreadyHandled": "Succeed",
    "Approved": "Succeed",
    "Expired": "Succeed",
    "Decided": "Succeed",
}
RECEIPT_STATES = {
    "ParseKey": "Pass",
    "MarkProcessing": "Task",
    "HasOldReceipt": "Choice",
    "DeleteOldReceipt": "Task",
    "ExtractReceipt": "Task",
    "SaveExtraction": "Task",
    "MarkFailed": "Task",
    "IgnoreUpload": "Succeed",
    "Superseded": "Succeed",
    "Done": "Succeed",
    "ExtractionFailed": "Succeed",
}
TASK_TOKEN = "{% $states.context.Task.Token %}"
CCF = "DynamoDB.ConditionalCheckFailedException"


def _backend(app: App) -> BackendStack:
    """The Backend stack wired to its producer stacks exactly as in app.py."""
    config = StageConfig.from_app(app)
    edge = EdgeStack(app, config.stack_id("Edge"), config=config)
    data = DataStack(app, config.stack_id("Data"), config=config, site_url=edge.site_url)
    auth = AuthStack(app, config.stack_id("Auth"), config=config)
    return BackendStack(
        app,
        config.stack_id("Backend"),
        config=config,
        table=data.table,
        idempotency_table=data.idempotency_table,
        receipts_bucket=data.receipts_bucket,
        reports_bucket=data.reports_bucket,
        user_pool=auth.user_pool,
        user_pool_client=auth.user_pool_client,
        site_url=edge.site_url,
    )


@pytest.fixture(scope="module")
def stack(cdk_context: dict[str, Any]) -> Iterator[BackendStack]:
    """One default-configuration Backend stack shared by the read-only tests of this module."""
    outdir = tempfile.mkdtemp(prefix="cdk")
    with pytest.MonkeyPatch.context() as env:
        env.setenv("CDK_DEFAULT_ACCOUNT", TEST_ACCOUNT)
        env.setenv("CDK_DEFAULT_REGION", TEST_REGION)
        yield _backend(App(context={**cdk_context, "aws:cdk:bundling-stacks": []}, outdir=outdir))
    shutil.rmtree(outdir, ignore_errors=True)


@pytest.fixture(scope="module")
def template(stack: BackendStack) -> Template:
    return Template.from_stack(stack)


def _functions(template: Template) -> dict[str, dict[str, Any]]:
    """Application functions (handler -> properties)."""
    return {
        props["Properties"]["Handler"]: props["Properties"]
        for props in template.find_resources("AWS::Lambda::Function").values()
        if props["Properties"]["Handler"].startswith("handlers.")
    }


def _logical_id(resources: Resources, prefix: str) -> str:
    matches = [logical_id for logical_id in resources if logical_id.startswith(prefix)]
    assert len(matches) == 1, f"expected one resource starting with {prefix}, got {matches}"
    return matches[0]


def _asl(template: Template, prefix: str) -> dict[str, Any]:
    """The state machine definition, with CloudFormation tokens replaced by a placeholder string.

    `DefinitionString` is an `Fn::Join` of literal JSON fragments and tokens (ARNs, names); tokens
    always sit inside JSON strings, so substituting text keeps the document valid JSON.
    """
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    definition = machines[_logical_id(machines, prefix)]["Properties"]["DefinitionString"]
    parts = definition["Fn::Join"][1]
    asl: dict[str, Any] = json.loads("".join(part if isinstance(part, str) else "TOKEN" for part in parts))
    return asl


def _policy_statements(template: Template, role_prefix: str) -> list[dict[str, Any]]:
    policies = template.find_resources("AWS::IAM::Policy")
    return [
        statement
        for logical_id, policy in policies.items()
        if logical_id.startswith(role_prefix)
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _actions(statements: list[dict[str, Any]]) -> set[str]:
    return {
        action
        for statement in statements
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    }


# --- Functions --------------------------------------------------------------------------------
def test_all_functions_share_the_powertools_defaults(template: Template) -> None:
    functions = _functions(template)

    assert set(functions) == set(FUNCTION_ENV)
    for handler, props in functions.items():
        assert props["Runtime"] == "python3.13", handler
        assert props["Architectures"] == ["arm64"], handler
        assert props["TracingConfig"] == {"Mode": "Active"}, handler
        assert props["LoggingConfig"]["LogFormat"] == "JSON", handler
        assert props["LoggingConfig"]["ApplicationLogLevel"] == "INFO", handler
        assert props["LoggingConfig"]["SystemLogLevel"] == "WARN", handler
        assert len(props["Layers"]) == 1, handler
        assert props["MemorySize"] <= 1024, handler
        assert "ReservedConcurrentExecutions" not in props, handler


@pytest.mark.parametrize("handler", sorted(FUNCTION_ENV))
def test_function_environment(template: Template, handler: str) -> None:
    variables = _functions(template)[handler]["Environment"]["Variables"]

    assert set(variables) == FUNCTION_ENV[handler] | COMMON_ENV
    assert variables["STAGE"] == "dev"
    assert variables["POWERTOOLS_METRICS_NAMESPACE"] == "ExpenseFlow"


def test_function_settings_from_config(template: Template) -> None:
    functions = _functions(template)
    api = functions["handlers.api.app.handler"]
    extract = functions["handlers.extract_receipt.handler"]
    notifier = functions["handlers.notifier.handler"]

    assert api["Environment"]["Variables"]["MAX_RECEIPT_BYTES"] == "3500000"
    assert api["Environment"]["Variables"]["DEFAULT_CURRENCY"] == "JPY"
    assert (api["Timeout"], api["MemorySize"]) == (15, 512)
    assert notifier["Environment"]["Variables"]["SENDER_EMAIL"] == ""
    assert extract["Environment"]["Variables"]["BEDROCK_REGION"] == TEST_REGION
    assert extract["Environment"]["Variables"]["EXTRACTION_MODEL_ID"] == "jp.anthropic.claude-opus-5-5"
    assert (
        extract["Environment"]["Variables"]["EXTRACTION_FALLBACK_MODEL_ID"] == "jp.anthropic.claude-opus-4-8"
    )
    assert (extract["Timeout"], extract["MemorySize"]) == (180, 512)
    assert functions["handlers.report_generator.handler"]["MemorySize"] == 1024


def test_stream_processor_reads_only_expense_changes(template: Template) -> None:
    mappings = [
        props["Properties"]
        for props in template.find_resources("AWS::Lambda::EventSourceMapping").values()
        if "FilterCriteria" in props["Properties"]
    ]

    assert len(mappings) == 1
    mapping = mappings[0]
    assert json.loads(mapping["FilterCriteria"]["Filters"][0]["Pattern"]) == {
        "eventName": ["INSERT", "MODIFY"],
        "dynamodb": {"Keys": {"PK": {"S": [{"prefix": "EXPENSE#"}]}, "SK": {"S": ["META"]}}},
    }
    assert mapping["StartingPosition"] == "LATEST"
    assert mapping["BatchSize"] == 25
    assert mapping["MaximumRetryAttempts"] == 5
    assert mapping["BisectBatchOnFunctionError"] is True
    assert mapping["MaximumRecordAgeInSeconds"] == 3600
    assert mapping["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert "OnFailure" in mapping["DestinationConfig"]


def test_notifier_consumes_the_notifications_queue(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {
            "BatchSize": 10,
            "MaximumBatchingWindowInSeconds": 5,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "EventSourceArn": {"Fn::GetAtt": [Match.string_like_regexp("^NotificationsQueue"), "Arn"]},
        },
    )
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"VisibilityTimeout": 180, "RedrivePolicy": Match.object_like({"maxReceiveCount": 3})},
    )


def test_queues_are_encrypted_and_reject_plain_http(template: Template) -> None:
    queues = template.find_resources("AWS::SQS::Queue")
    names = {logical_id.rstrip("0123456789ABCDEF") for logical_id in queues}

    assert names == {
        "NotificationsQueue",
        "NotificationsDlq",
        "StreamDlq",
        "RealtimeDlq",
        "ApprovalRuleDlq",
        "ReceiptRuleDlq",
        "SchedulerDlq",
    }
    assert all(queue["Properties"]["SqsManagedSseEnabled"] is True for queue in queues.values())
    policies = template.find_resources("AWS::SQS::QueuePolicy")
    insecure_denied = [
        policy
        for policy in policies.values()
        if any(
            statement.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        )
    ]
    assert len(insecure_denied) == len(queues)


def test_api_function_permissions(template: Template) -> None:
    actions = _actions(_policy_statements(template, "ApiFunctionServiceRole"))

    assert {
        "states:SendTaskSuccess",
        "states:SendTaskFailure",
        "states:SendTaskHeartbeat",
        "states:StopExecution",
        "lambda:InvokeFunction",
        "s3:PutObject",
        "s3:DeleteObject*",
        "dynamodb:UpdateItem",
    } <= actions
    assert not any(action.endswith(":*") for action in actions)


def test_extraction_uses_bedrock_runtime_with_inference_profiles(template: Template) -> None:
    statements = [
        statement
        for statement in _policy_statements(template, "ExtractReceiptFunctionServiceRole")
        if statement["Action"] == "bedrock:InvokeModel"
    ]

    assert len(statements) == 1
    assert set(statements[0]["Resource"]) == {
        f"arn:aws:bedrock:{TEST_REGION}:{TEST_ACCOUNT}:inference-profile/jp.anthropic.claude-opus-5-5",
        f"arn:aws:bedrock:{TEST_REGION}:{TEST_ACCOUNT}:inference-profile/jp.anthropic.claude-opus-4-8",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-opus-5-5",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-8",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-opus-4-8",
    }


# --- Events, schedule, configuration ---------------------------------------------------------
def test_event_bus_archive_keeps_domain_events_for_30_days(template: Template) -> None:
    template.resource_count_is("AWS::Events::EventBus", 1)
    template.has_resource_properties(
        "AWS::Events::Archive",
        {"EventPattern": {"source": [{"prefix": "expenseflow."}]}, "RetentionDays": 30},
    )


def test_submitted_expenses_start_the_approval_workflow(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventBusName": {"Ref": Match.string_like_regexp("^EventBus")},
            "EventPattern": {"source": ["expenseflow.expenses"], "detail-type": ["Expense Submitted"]},
            "Targets": [
                Match.object_like(
                    {
                        "Arn": {"Ref": Match.string_like_regexp("^ApprovalWorkflowStateMachine")},
                        "DeadLetterConfig": {
                            "Arn": {"Fn::GetAtt": [Match.string_like_regexp("^ApprovalRuleDlq"), "Arn"]}
                        },
                        "RetryPolicy": {"MaximumEventAgeInSeconds": 7200, "MaximumRetryAttempts": 4},
                    }
                )
            ],
        },
    )


def test_domain_events_fan_out_to_notifications_and_realtime(template: Template) -> None:
    all_domain_events = {"source": [{"prefix": "expenseflow."}]}
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": all_domain_events,
            "Targets": [
                Match.object_like(
                    {"Arn": {"Fn::GetAtt": [Match.string_like_regexp("^NotificationsQueue"), "Arn"]}}
                )
            ],
        },
    )
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": all_domain_events,
            "Targets": [
                Match.object_like(
                    {
                        "Arn": {"Fn::GetAtt": [Match.string_like_regexp("^RealtimePushFunction"), "Arn"]},
                        "DeadLetterConfig": {
                            "Arn": {"Fn::GetAtt": [Match.string_like_regexp("^RealtimeDlq"), "Arn"]}
                        },
                        "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 2},
                    }
                )
            ],
        },
    )


def test_receipt_uploads_on_the_default_bus_start_the_pipeline(template: Template) -> None:
    rules = template.find_resources(
        "AWS::Events::Rule", {"Properties": {"EventPattern": {"source": ["aws.s3"]}}}
    )

    assert len(rules) == 1
    rule = next(iter(rules.values()))["Properties"]
    assert "EventBusName" not in rule  # the account's default bus, where S3 publishes
    pattern = rule["EventPattern"]
    assert pattern["detail-type"] == ["Object Created"]
    assert pattern["detail"]["object"] == {"key": [{"prefix": "receipts/"}]}
    assert len(pattern["detail"]["bucket"]["name"]) == 1
    target = rule["Targets"][0]
    assert target["Arn"] == {
        "Ref": _logical_id(template.find_resources("AWS::StepFunctions::StateMachine"), "ReceiptPipeline")
    }
    assert "DeadLetterConfig" in target


def test_monthly_report_schedule(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "cron(0 9 1 * ? *)",
            "ScheduleExpressionTimezone": "Asia/Tokyo",
            "State": "ENABLED",
            "Target": Match.object_like(
                {
                    "Arn": {"Fn::GetAtt": [Match.string_like_regexp("^ReportGeneratorFunction"), "Arn"]},
                    "Input": json.dumps(
                        {"month": "previous", "requestedBy": "scheduler"}, separators=(",", ":")
                    ),
                    "RetryPolicy": Match.object_like({"MaximumRetryAttempts": 2}),
                    "DeadLetterConfig": {
                        "Arn": {"Fn::GetAtt": [Match.string_like_regexp("^SchedulerDlq"), "Arn"]}
                    },
                }
            ),
        },
    )


def test_auto_approve_limit_parameter(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": "/expenseflow/dev/auto-approve-limit", "Type": "String", "Value": "5000"},
    )


# --- State machines -----------------------------------------------------------------------------
@pytest.mark.parametrize("prefix", ["ApprovalWorkflow", "ReceiptPipeline"])
def test_state_machines_are_standard_jsonata_traced_and_logged(template: Template, prefix: str) -> None:
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    props = machines[_logical_id(machines, prefix)]["Properties"]

    assert props["StateMachineType"] == "STANDARD"
    assert props["TracingConfiguration"] == {"Enabled": True}
    assert props["LoggingConfiguration"]["Level"] == "ALL"
    assert props["LoggingConfiguration"]["IncludeExecutionData"] is True
    assert _asl(template, prefix)["QueryLanguage"] == "JSONata"
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": f"/aws/vendedlogs/states/ExpenseFlow-dev-{prefix}", "RetentionInDays": 30},
    )


def test_approval_workflow_states(template: Template) -> None:
    asl = _asl(template, "ApprovalWorkflow")
    states = asl["States"]

    assert {name: state["Type"] for name, state in states.items()} == APPROVAL_STATES
    assert asl["StartAt"] == "Init"
    assert asl["TimeoutSeconds"] == 30 * 24 * 3600
    assert states["Init"]["Assign"] == {
        "expenseId": "{% $states.input.detail.expenseId %}",
        "ownerId": "{% $states.input.detail.ownerId %}",
        "now": "{% $states.context.State.EnteredTime %}",
    }
    claim = states["ClaimExpense"]
    assert claim["Arguments"]["ConditionExpression"] == (
        "#status = :pending AND attribute_not_exists(workflowExecutionArn)"
    )
    assert claim["Arguments"]["ReturnValues"] == "ALL_NEW"
    assert claim["Catch"] == [{"ErrorEquals": [CCF], "Next": "AlreadyHandled"}]
    assert states["LoadPolicy"]["Resource"].endswith(":states:::aws-sdk:ssm:getParameter")
    assert states["CanAutoApprove"]["Choices"][0]["Next"] == "AutoApprove"
    assert states["CanAutoApprove"]["Default"] == "RequestApproval"
    assert "$expense.currency.S = 'JPY'" in states["CanAutoApprove"]["Choices"][0]["Condition"]
    assert states["AutoApprove"]["Next"] == "Approved"
    assert states["RecordDecision"]["Next"] == "Decided"
    assert states["RecordDecision"]["Catch"] == [
        {"ErrorEquals": ["DynamoDb.ConditionalCheckFailedException"], "Next": "AlreadyHandled"}
    ]
    assert states["ExpireRequest"]["Next"] == "Expired"
    assert states["Escalate"]["Arguments"]["Entries"][0]["Source"] == "expenseflow.approvals"
    assert states["Escalate"]["Arguments"]["Entries"][0]["DetailType"] == "Approval Escalated"
    assert states["Escalate"]["Next"] == "RequestEscalatedApproval"


@pytest.mark.parametrize(
    ("state_name", "escalated", "on_timeout"),
    [("RequestApproval", False, "Escalate"), ("RequestEscalatedApproval", True, "ExpireRequest")],
)
def test_approval_requests_wait_for_the_task_token(
    template: Template, state_name: str, escalated: bool, on_timeout: str
) -> None:
    state = _asl(template, "ApprovalWorkflow")["States"][state_name]

    assert state["Resource"].endswith(":states:::lambda:invoke.waitForTaskToken")
    assert state["Arguments"]["Payload"] == {
        "taskToken": TASK_TOKEN,
        "expenseId": "{% $expenseId %}",
        "escalated": escalated,
    }
    assert state["TimeoutSeconds"] == 4320 * 60
    assert {"ErrorEquals": ["States.Timeout"], "Next": on_timeout} in state["Catch"]
    assert {"ErrorEquals": ["ExpenseNotPending"], "Next": "AlreadyHandled"} in state["Catch"]
    assert state["Next"] == "RecordDecision"


def test_task_token_is_a_jsonata_expression_not_jsonpath(template: Template) -> None:
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    definition = json.dumps(
        machines[_logical_id(machines, "ApprovalWorkflow")]["Properties"]["DefinitionString"]
    )

    assert "$$.Task.Token" not in definition
    assert definition.count(TASK_TOKEN) == 2


def test_condition_failures_are_never_retried(template: Template) -> None:
    for prefix in ("ApprovalWorkflow", "ReceiptPipeline"):
        for name, state in _asl(template, prefix)["States"].items():
            retried = {error for retrier in state.get("Retry", []) for error in retrier["ErrorEquals"]}
            assert not any("ConditionalCheckFailed" in error for error in retried), name


def test_receipt_pipeline_states(template: Template) -> None:
    asl = _asl(template, "ReceiptPipeline")
    states = asl["States"]

    assert {name: state["Type"] for name, state in states.items()} == RECEIPT_STATES
    assert asl["StartAt"] == "ParseKey"
    assert asl["TimeoutSeconds"] == 3600
    mark = states["MarkProcessing"]
    assert mark["Arguments"]["ReturnValues"] == "UPDATED_OLD"
    assert mark["Arguments"]["ConditionExpression"] == "pendingReceiptKey = :key AND ownerId = :owner"
    assert mark["Catch"] == [{"ErrorEquals": [CCF], "Next": "IgnoreUpload"}]
    assert states["HasOldReceipt"]["Choices"][0]["Next"] == "DeleteOldReceipt"
    assert states["DeleteOldReceipt"]["Resource"].endswith(":states:::aws-sdk:s3:deleteObject")

    extract = states["ExtractReceipt"]
    retried = [retrier["ErrorEquals"] for retrier in extract["Retry"]]
    assert ["ModelThrottledError"] in retried
    assert ["ModelUnavailableError"] in retried
    assert "Lambda.ServiceException" in retried[0]  # CDK's default Lambda retrier comes first
    throttled = next(r for r in extract["Retry"] if r["ErrorEquals"] == ["ModelThrottledError"])
    assert (throttled["IntervalSeconds"], throttled["MaxAttempts"], throttled["MaxDelaySeconds"]) == (
        10,
        5,
        120,
    )
    assert extract["Catch"] == [{"ErrorEquals": ["States.ALL"], "Next": "MarkFailed"}]
    assert extract["Next"] == "SaveExtraction"
    assert states["SaveExtraction"]["Next"] == "Done"
    assert states["MarkFailed"]["Next"] == "ExtractionFailed"
    for name in ("SaveExtraction", "MarkFailed"):
        assert states[name]["Arguments"]["ConditionExpression"] == "receiptKey = :key"
        assert states[name]["Catch"] == [{"ErrorEquals": [CCF], "Next": "Superseded"}]


def test_extraction_can_be_disabled(make_app: AppFactory, assert_nag_clean: NagCheck) -> None:
    stack = _backend(make_app(extractionModelId="none"))
    template = Template.from_stack(stack)

    assert "handlers.extract_receipt.handler" not in _functions(template)
    states = _asl(template, "ReceiptPipeline")["States"]
    assert "ExtractReceipt" not in states
    assert states["MarkSkipped"]["Next"] == "Done"
    assert states["HasOldReceipt"]["Default"] == "MarkSkipped"
    assert not any("bedrock" in action for action in _actions(_policy_statements(template, "")))
    template.resource_count_is("AWS::CloudWatch::Alarm", 17)
    assert_nag_clean(stack)


# --- APIs -------------------------------------------------------------------------------------
def test_http_api_has_an_explicit_jwt_protected_route_per_endpoint(template: Template) -> None:
    routes = [
        props["Properties"]
        for props in template.find_resources("AWS::ApiGatewayV2::Route").values()
        if " /" in props["Properties"]["RouteKey"]
    ]

    assert {route["RouteKey"] for route in routes} == HTTP_ROUTES
    assert len(routes) == len(HTTP_ROUTES)
    assert all(route["AuthorizationType"] == "JWT" for route in routes)
    assert len({json.dumps(route["Target"]) for route in routes}) == 1  # one Lambda integration
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "IdentitySource": ["$request.header.Authorization"],
            "JwtConfiguration": {"Audience": [Match.any_value()], "Issuer": Match.any_value()},
        },
    )
    # One invoke permission for the whole API instead of one per route.
    permissions = template.find_resources(
        "AWS::Lambda::Permission",
        {"Properties": {"FunctionName": {"Fn::GetAtt": [Match.string_like_regexp("^ApiFunction"), "Arn"]}}},
    )
    assert len(permissions) == 1


def test_http_api_cors(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {
            "ProtocolType": "HTTP",
            "CorsConfiguration": {
                "AllowOrigins": [Match.any_value(), "http://localhost:5173"],
                "AllowHeaders": ["authorization", "content-type"],
                "AllowMethods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                "ExposeHeaders": ["apigw-requestid"],
                "MaxAge": 86400,
            },
        },
    )


def test_http_api_stage_throttles_and_writes_json_access_logs(template: Template) -> None:
    stages = template.find_resources("AWS::ApiGatewayV2::Stage", {"Properties": {"StageName": "$default"}})

    assert len(stages) == 1
    stage = next(iter(stages.values()))["Properties"]
    assert stage["AutoDeploy"] is True
    assert stage["DefaultRouteSettings"] == {"ThrottlingBurstLimit": 50, "ThrottlingRateLimit": 100}
    log_format = json.loads(stage["AccessLogSettings"]["Format"])
    assert log_format["requestId"] == "$context.requestId"
    assert log_format["status"] == "$context.status"
    assert stage["AccessLogSettings"]["DestinationArn"]["Fn::GetAtt"][0].startswith("HttpApiAccessLogs")


def test_websocket_api(template: Template) -> None:
    routes = {
        props["Properties"]["RouteKey"]: props["Properties"]
        for props in template.find_resources("AWS::ApiGatewayV2::Route").values()
        if props["Properties"]["RouteKey"].startswith("$")
    }

    assert set(routes) == {"$connect", "$disconnect", "$default"}
    assert routes["$connect"]["AuthorizationType"] == "CUSTOM"
    assert routes["$disconnect"]["AuthorizationType"] == "NONE"
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {"AuthorizerType": "REQUEST", "IdentitySource": ["route.request.querystring.token"]},
    )
    stages = template.find_resources("AWS::ApiGatewayV2::Stage", {"Properties": {"StageName": "live"}})
    assert len(stages) == 1
    stage = next(iter(stages.values()))
    assert stage["Properties"]["AutoDeploy"] is True
    assert stage["Properties"]["DefaultRouteSettings"] == {
        "ThrottlingBurstLimit": 100,
        "ThrottlingRateLimit": 50,
    }
    assert (
        json.loads(stage["Properties"]["AccessLogSettings"]["Format"])["connectionId"]
        == "$context.connectionId"
    )
    # WebSocket logging needs the account-level CloudWatch role, set before the stage is created.
    accounts = template.find_resources("AWS::ApiGateway::Account")
    assert len(accounts) == 1
    assert next(iter(accounts)) in stage["DependsOn"]


def test_realtime_push_can_manage_connections_of_the_live_stage(template: Template) -> None:
    statements = _policy_statements(template, "RealtimePushFunctionServiceRole")
    manage = [statement for statement in statements if statement["Action"] == "execute-api:ManageConnections"]

    assert len(manage) == 1
    resource = json.dumps(manage[0]["Resource"])
    assert "/live/*/@connections/*" in resource
    callback_url = _functions(template)["handlers.realtime_push.handler"]["Environment"]["Variables"][
        "WS_CALLBACK_URL"
    ]
    assert callback_url["Fn::Join"][1][0] == "https://"
    assert callback_url["Fn::Join"][1][-1].endswith("/live")


# --- Monitoring, outputs, nag, cycles -----------------------------------------------------------
def test_alarms_notify_the_alarm_topic(template: Template) -> None:
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    topic = _logical_id(template.find_resources("AWS::SNS::Topic"), "MonitoringAlarmTopic")

    # API 5xx + 9 functions + 6 DLQs + 2 state machines
    assert len(alarms) == 18
    for alarm in alarms.values():
        assert alarm["Properties"]["TreatMissingData"] == "notBreaching"
        assert alarm["Properties"]["AlarmActions"] == [{"Ref": topic}]
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "AWS/ApiGateway",
            "MetricName": "5xx",
            "Statistic": "Sum",
            "Period": 300,
            "Threshold": 5,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        },
    )
    template.has_resource_properties("AWS::CloudWatch::Dashboard", {"DashboardName": "ExpenseFlow-dev"})
    template.resource_count_is("AWS::SNS::Subscription", 0)


def test_cloudwatch_alarms_may_publish_to_the_alarm_topic(template: Template) -> None:
    # The TLS-only topic policy replaces SNS's default policy, so CloudWatch needs an explicit Allow:
    # without it every alarm action fails with "not authorized to perform: SNS:Publish"
    # (found by testing an alarm in a real account).
    topic = _logical_id(template.find_resources("AWS::SNS::Topic"), "MonitoringAlarmTopic")
    policies = [
        policy
        for policy in template.find_resources("AWS::SNS::TopicPolicy").values()
        if policy["Properties"]["Topics"] == [{"Ref": topic}]
    ]
    assert len(policies) == 1
    statements = {s["Sid"]: s for s in policies[0]["Properties"]["PolicyDocument"]["Statement"]}

    assert statements["AllowPublishThroughSSLOnly"]["Effect"] == "Deny"
    allow = statements["AllowCloudWatchAlarmsToPublish"]
    assert allow["Effect"] == "Allow"
    assert allow["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert allow["Action"] == "sns:Publish"
    assert allow["Resource"] == {"Ref": topic}
    # Only alarms in this account and Region (confused-deputy protection).
    assert allow["Condition"]["StringEquals"] == {"aws:SourceAccount": TEST_ACCOUNT}
    assert allow["Condition"]["ArnLike"] == {
        "aws:SourceArn": f"arn:aws:cloudwatch:{TEST_REGION}:{TEST_ACCOUNT}:alarm:*"
    }


def test_optional_emails(make_app: AppFactory, assert_nag_clean: NagCheck) -> None:
    stack = _backend(make_app(alarmEmail="ops@example.com", senderEmail="noreply@example.com"))
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"}
    )
    # The address must not leak into the logical ID: `cdk deploy` prints logical IDs in its event lines,
    # and GitHub masks a secret only when it appears exactly, not with its punctuation removed.
    (subscription_id,) = template.find_resources("AWS::SNS::Subscription")
    assert "ops" not in subscription_id.lower()
    assert subscription_id.startswith("MonitoringAlarmEmailSubscription")
    template.has_resource_properties("AWS::SES::EmailIdentity", {"EmailIdentity": "noreply@example.com"})
    notifier = _functions(template)["handlers.notifier.handler"]
    assert notifier["Environment"]["Variables"]["SENDER_EMAIL"] == "noreply@example.com"
    assert "ses:SendEmail" in _actions(_policy_statements(template, "NotifierFunctionServiceRole"))
    assert_nag_clean(stack)


def test_outputs(template: Template) -> None:
    assert {
        "ApiUrl",
        "WebSocketUrl",
        "EventBusName",
        "ApprovalStateMachineArn",
        "ReceiptStateMachineArn",
        "DashboardName",
        "HttpApiId",
        "WebSocketApiId",
    } <= set(template.find_outputs("*"))


def test_cdk_nag_passes(stack: BackendStack, template: Template, assert_nag_clean: NagCheck) -> None:
    # Check after synthesis, as `cdk synth` does: only then do references to other stacks (in IAM
    # resources such as `<table ARN>/index/*`) have the final form that the finding ids contain.
    assert template is not None
    assert_nag_clean(stack)


def test_cdk_nag_passes_without_an_account(
    make_app: AppFactory, assert_nag_clean: NagCheck, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cdk synth` without credentials leaves the account unresolved, so more ARNs contain tokens."""
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT")
    stack = _backend(make_app())

    Template.from_stack(stack)
    assert_nag_clean(stack)


def test_synthesizes_without_cycles(stack: BackendStack, template: Template) -> None:
    """A dependency cycle between resources or stacks would fail synthesis (`Template.from_stack`)."""
    assert template is not None
    dependencies = {dependency.node.id for dependency in stack.dependencies}
    assert dependencies == {"ExpenseFlow-Dev-Edge", "ExpenseFlow-Dev-Data", "ExpenseFlow-Dev-Auth"}
