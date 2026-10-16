"""Backend stack: all compute and integration - Lambdas, APIs, events, queues, workflows, monitoring.

Data flows through the app like this:

    SPA --HTTP API--> ApiFunction --> DynamoDB --stream--> StreamProcessorFunction --> EventBus
    EventBus --rules--> ApprovalWorkflow (Step Functions)
                    --> NotificationsQueue (SQS) --> NotifierFunction --> SES email
                    --> RealtimePushFunction --> WebSocket API --> SPA
    S3 receipt upload --default bus--> ReceiptPipeline (Step Functions) --> ExtractReceiptFunction --> Bedrock
    Scheduler (1st of the month) --> ReportGeneratorFunction --> reports bucket

The API never publishes events itself: it only writes DynamoDB, and the stream turns each committed
change into an event ("transactional outbox"), so a write and its event can never disagree.

Every asynchronous hop has somewhere for failures to go (a dead-letter queue or a failed execution),
and each of those raises an alarm (see the Monitoring construct).

Permissions follow least privilege: each function has its own role and gets only the `grant_*`
calls it needs. Where a grant still contains a wildcard (an index of the table, the objects under a
prefix, executions created at run time), the cdk-nag finding is acknowledged next to the grant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from aws_cdk import CfnOutput, Duration, Stack, TimeZone
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_ses as ses
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from expenseflow_infra.config import LOCAL_DEV_ORIGIN, PROJECT_NAME, StageConfig, foundation_model_id
from expenseflow_infra.constructs.approval_workflow import ApprovalWorkflow
from expenseflow_infra.constructs.dependencies_layer import DependenciesLayer
from expenseflow_infra.constructs.http_api import ExpenseHttpApi
from expenseflow_infra.constructs.monitoring import Monitoring
from expenseflow_infra.constructs.python_function import PythonFunction
from expenseflow_infra.constructs.realtime_api import RealtimeApi
from expenseflow_infra.constructs.receipt_pipeline import RECEIPTS_PREFIX, ReceiptPipeline
from expenseflow_infra.nag import acknowledge, acknowledge_wildcards

EVENT_SOURCE_PREFIX: Final = "expenseflow."
EXPENSES_SOURCE: Final = "expenseflow.expenses"
EVENT_ARCHIVE_RETENTION: Final = Duration.days(30)
DLQ_RETENTION: Final = Duration.days(14)
"""The SQS maximum: time to notice the alarm, fix the bug and redrive the messages."""
NOTIFICATIONS_VISIBILITY_TIMEOUT: Final = Duration.seconds(180)
"""AWS recommends >= 6 x the consumer's timeout (30 s), so a slow batch is not redelivered mid-flight."""
NOTIFICATIONS_MAX_RECEIVES: Final = 3
REPORTS_PREFIX: Final = "reports/"

_RECEIPT_OBJECTS: Final = f"{RECEIPTS_PREFIX}*"
_REPORT_OBJECTS: Final = f"{REPORTS_PREFIX}*"
_SCHEDULER_ROLE_PREFIX: Final = "SchedulerRoleForTarget-"
"""Construct id prefix of the role CDK creates (once per stack) for Scheduler's Lambda targets."""

_TABLE_INDEXES: Final = (
    "The table grant includes its global secondary indexes (`<table>/index/*`); the access "
    "patterns query GSI1 (by owner, by WebSocket audience) and GSI2 (by status)."
)
_RECEIPT_READ: Final = (
    "CDK's S3 read grant uses the GetObject*/GetBucket*/List* action groups; objects are limited to "
    "the receipts/ prefix of the receipts bucket."
)
_FUNCTION_VERSIONS: Final = (
    "lambda:InvokeFunction on one function and its versions/aliases (`<arn>:*`), as CDK's grant_invoke "
    "adds it; no other function can be invoked."
)


@dataclass(frozen=True)
class _Queues:
    notifications: sqs.Queue
    notifications_dlq: sqs.Queue
    stream_dlq: sqs.Queue
    realtime_dlq: sqs.Queue
    approval_rule_dlq: sqs.Queue
    receipt_rule_dlq: sqs.Queue
    scheduler_dlq: sqs.Queue

    def dead_letter_queues(self) -> dict[str, sqs.IQueue]:
        return {
            "NotificationsDlq": self.notifications_dlq,
            "StreamDlq": self.stream_dlq,
            "RealtimeDlq": self.realtime_dlq,
            "ApprovalRuleDlq": self.approval_rule_dlq,
            "ReceiptRuleDlq": self.receipt_rule_dlq,
            "SchedulerDlq": self.scheduler_dlq,
        }


class BackendStack(Stack):
    """Lambdas, HTTP + WebSocket APIs, event bus and rules, queues, workflows, schedule, monitoring.

    Attributes:
        api_url: HTTP API base URL without a trailing slash.
        websocket_url: `wss://.../live` URL of the WebSocket API.
        http_api: The HTTP API.
        websocket_api: The WebSocket API.
        event_bus: Custom bus carrying the domain events (`expenseflow.*`).
        approval_state_machine: The ApprovalWorkflow state machine.
        receipt_state_machine: The ReceiptPipeline state machine.
        dashboard_name: Name of the CloudWatch dashboard.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        table: dynamodb.ITableV2,
        idempotency_table: dynamodb.ITableV2,
        receipts_bucket: s3.IBucket,
        reports_bucket: s3.IBucket,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        site_url: str,
    ) -> None:
        """Create the stack.

        Args:
            config: Stage settings.
            table: The single table (DataStack), with its stream.
            idempotency_table: Powertools idempotency records (DataStack).
            receipts_bucket: Receipt uploads (DataStack); emits S3 events to the default bus.
            reports_bucket: Monthly CSV reports (DataStack).
            user_pool: Cognito user pool (AuthStack).
            user_pool_client: The SPA's app client (AuthStack).
            site_url: Public URL of the SPA (EdgeStack): CORS origin and link target in emails.
        """
        super().__init__(
            scope,
            construct_id,
            env=config.env,
            description=f"{PROJECT_NAME} ({config.stage}): Lambdas, APIs, events, workflows and monitoring",
        )
        self._config = config
        self._layer = DependenciesLayer(self, "DependenciesLayer")
        queues = self._queues()
        self.event_bus = self._event_bus()

        report_generator = self._report_generator(table, reports_bucket, queues.scheduler_dlq)
        api = self._api_function(table, receipts_bucket, reports_bucket, report_generator)
        functions: dict[str, lambda_.Function] = {
            "Api": api,
            "StreamProcessor": self._stream_processor(table, queues.stream_dlq),
            "Notifier": self._notifier(idempotency_table, user_pool, queues.notifications, site_url),
            "ReportGenerator": report_generator,
        }

        realtime = self._realtime_api(table, user_pool, user_pool_client, functions)
        realtime_push = self._realtime_push(table, realtime)
        functions["RealtimePush"] = realtime_push

        approval_request = self._approval_request(table)
        functions["ApprovalRequest"] = approval_request
        approval = ApprovalWorkflow(
            self,
            "ApprovalWorkflow",
            config=config,
            table=table,
            request_function=approval_request,
            event_bus=self.event_bus,
            policy_parameter=self._auto_approve_limit_parameter(),
        )
        self._grant_workflow_control(api, approval)

        extract_receipt = self._extract_receipt(receipts_bucket) if config.extraction_enabled else None
        if extract_receipt is not None:
            functions["ExtractReceipt"] = extract_receipt
        receipt_pipeline = ReceiptPipeline(
            self,
            "ReceiptPipeline",
            config=config,
            table=table,
            receipts_bucket=receipts_bucket,
            extract_function=extract_receipt,
        )

        http_api = ExpenseHttpApi(
            self,
            "HttpApi",
            config=config,
            handler=api,
            user_pool=user_pool,
            user_pool_client=user_pool_client,
            allowed_origins=[site_url, LOCAL_DEV_ORIGIN],
        )
        self._route_events(
            queues,
            approval_workflow=approval,
            receipt_pipeline=receipt_pipeline,
            realtime_push=realtime_push,
            receipts_bucket=receipts_bucket,
        )
        monitoring = Monitoring(
            self,
            "Monitoring",
            config=config,
            http_api=http_api.api,
            websocket_api=realtime.api,
            functions=functions,
            state_machines={
                "ApprovalWorkflow": approval.state_machine,
                "ReceiptPipeline": receipt_pipeline.state_machine,
            },
            work_queues={"Notifications": queues.notifications},
            dead_letter_queues=queues.dead_letter_queues(),
            table=table,
        )

        self.http_api = http_api.api
        self.websocket_api = realtime.api
        self.api_url = http_api.url
        self.websocket_url = realtime.url
        self.approval_state_machine = approval.state_machine
        self.receipt_state_machine = receipt_pipeline.state_machine
        self.dashboard_name = monitoring.dashboard.dashboard_name
        self._outputs()

    # --- Building blocks -----------------------------------------------------------------------
    def _function(
        self,
        construct_id: str,
        *,
        handler: str,
        service: str,
        description: str,
        timeout: Duration,
        memory_size: int,
        environment: Mapping[str, str],
    ) -> lambda_.Function:
        """A Powertools function with the shared dependencies layer."""
        return PythonFunction(
            self,
            construct_id,
            config=self._config,
            handler=handler,
            service=service,
            description=description,
            timeout=timeout,
            memory_size=memory_size,
            layer=self._layer,
            environment=environment,
        ).function

    def _queues(self) -> _Queues:
        notifications_dlq = _queue(self, "NotificationsDlq", retention_period=DLQ_RETENTION)
        return _Queues(
            notifications=_queue(
                self,
                "NotificationsQueue",
                visibility_timeout=NOTIFICATIONS_VISIBILITY_TIMEOUT,
                # After 3 failed receives SQS moves a message aside instead of retrying it forever.
                dead_letter_queue=sqs.DeadLetterQueue(
                    max_receive_count=NOTIFICATIONS_MAX_RECEIVES, queue=notifications_dlq
                ),
            ),
            notifications_dlq=notifications_dlq,
            stream_dlq=self._dead_letter_queue("StreamDlq", "stream records stream_processor gave up on"),
            realtime_dlq=self._dead_letter_queue(
                "RealtimeDlq", "events EventBridge could not deliver to realtime_push"
            ),
            approval_rule_dlq=self._dead_letter_queue(
                "ApprovalRuleDlq", "Expense Submitted events that could not start the ApprovalWorkflow"
            ),
            receipt_rule_dlq=self._dead_letter_queue(
                "ReceiptRuleDlq", "S3 upload events that could not start the ReceiptPipeline"
            ),
            scheduler_dlq=self._dead_letter_queue(
                "SchedulerDlq", "monthly report invocations Scheduler could not deliver"
            ),
        )

    def _dead_letter_queue(self, construct_id: str, holds: str) -> sqs.Queue:
        """A queue that an AWS service (not an SQS redrive policy) sends failures to."""
        queue = _queue(self, construct_id, retention_period=DLQ_RETENTION)
        acknowledge(
            queue,
            (
                "AwsSolutions-SQS3",
                f"This queue IS a dead-letter queue: it holds {holds}. The service sends to it directly "
                "(an on-failure destination, not a redrive policy), and an alarm fires on its first message.",
            ),
        )
        return queue

    def _event_bus(self) -> events.EventBus:
        """Custom bus for ExpenseFlow's domain events, with an archive for replays.

        A custom bus keeps the app's events apart from the AWS service events on the default bus.
        The archive stores every domain event for 30 days so that past events can be *replayed*
        into the bus, e.g. after fixing a bug in a consumer.
        """
        bus = events.EventBus(
            self, "EventBus", description=f"{PROJECT_NAME} ({self._config.stage}) domain events"
        )
        bus.archive(
            "EventArchive",
            description="Every ExpenseFlow domain event, replayable for 30 days",
            event_pattern=_all_domain_events(),
            retention=EVENT_ARCHIVE_RETENTION,
        )
        return bus

    def _auto_approve_limit_parameter(self) -> ssm.StringParameter:
        config = self._config
        return ssm.StringParameter(
            self,
            "AutoApproveLimitParameter",
            parameter_name=f"/expenseflow/{config.stage}/auto-approve-limit",
            string_value=str(config.auto_approve_limit),
            description=(
                f"Expenses up to this amount ({config.default_currency}, inclusive) whose receipt total was "
                "verified by AI are approved automatically. Read by every ApprovalWorkflow execution."
            ),
        )

    # --- Functions -----------------------------------------------------------------------------
    def _report_generator(
        self, table: dynamodb.ITableV2, reports_bucket: s3.IBucket, scheduler_dlq: sqs.IQueue
    ) -> lambda_.Function:
        function = self._function(
            "ReportGeneratorFunction",
            handler="handlers.report_generator.handler",
            service="report-generator",
            description="Writes the monthly CSV report of approved expenses",
            timeout=Duration.seconds(300),
            memory_size=1024,
            environment={
                "TABLE_NAME": table.table_name,
                "REPORTS_BUCKET": reports_bucket.bucket_name,
                "EVENT_BUS_NAME": self.event_bus.event_bus_name,
            },
        )
        _acknowledge_grant(function, table.grant_read_data(function), _TABLE_INDEXES)
        _acknowledge_grant(
            function,
            reports_bucket.grant_put(function, _REPORT_OBJECTS),
            "CDK's S3 put grant (PutObject* and Abort* for multipart uploads) on the reports/ prefix only; "
            "the report key depends on the month.",
        )
        self.event_bus.grant_put_events_to(function)

        scheduler.Schedule(
            self,
            "MonthlyReportSchedule",
            description="Previous month's report at 09:00 Tokyo time on the 1st of every month",
            # Scheduler evaluates cron in the given time zone (EventBridge *rules* only know UTC).
            schedule=scheduler.ScheduleExpression.cron(
                minute="0", hour="9", day="1", time_zone=TimeZone.ASIA_TOKYO
            ),
            target=scheduler_targets.LambdaInvoke(
                function,
                input=scheduler.ScheduleTargetInput.from_object(
                    {"month": "previous", "requestedBy": "scheduler"}
                ),
                retry_attempts=2,
                dead_letter_queue=scheduler_dlq,
            ),
        )
        scheduler_role = next(
            child for child in self.node.children if child.node.id.startswith(_SCHEDULER_ROLE_PREFIX)
        )
        acknowledge_wildcards(scheduler_role, _FUNCTION_VERSIONS, resources=[f"{function.function_arn}:*"])
        return function

    def _api_function(
        self,
        table: dynamodb.ITableV2,
        receipts_bucket: s3.IBucket,
        reports_bucket: s3.IBucket,
        report_generator: lambda_.IFunction,
    ) -> lambda_.Function:
        config = self._config
        function = self._function(
            "ApiFunction",
            handler="handlers.api.app.handler",
            service="api",
            description="HTTP API: expenses, receipts, approvals, stats and reports",
            timeout=Duration.seconds(15),
            memory_size=512,
            environment={
                "TABLE_NAME": table.table_name,
                "RECEIPTS_BUCKET": receipts_bucket.bucket_name,
                "REPORTS_BUCKET": reports_bucket.bucket_name,
                "REPORT_FUNCTION_NAME": report_generator.function_name,
                "MAX_RECEIPT_BYTES": str(config.max_receipt_bytes),
                "DEFAULT_CURRENCY": config.default_currency,
            },
        )
        _acknowledge_grant(function, table.grant_read_write_data(function), _TABLE_INDEXES)
        # A presigned URL carries the *signer's* permissions: browsers can upload or view a receipt
        # only because this role may. Each grant is limited to one prefix of one bucket.
        _acknowledge_grant(function, receipts_bucket.grant_read(function, _RECEIPT_OBJECTS), _RECEIPT_READ)
        _acknowledge_grant(
            function,
            receipts_bucket.grant_put(function, _RECEIPT_OBJECTS),
            "CDK's S3 put grant (PutObject* and Abort*) on the receipts/ prefix: the API presigns POST "
            "uploads whose keys are generated per upload.",
        )
        _acknowledge_grant(
            function,
            receipts_bucket.grant_delete(function, _RECEIPT_OBJECTS),
            "CDK's S3 delete grant (DeleteObject*) on the receipts/ prefix: deleting a draft deletes "
            "its receipts.",
        )
        _acknowledge_grant(
            function,
            reports_bucket.grant_read(function, _REPORT_OBJECTS),
            "CDK's S3 read grant (GetObject*/GetBucket*/List*) on the reports/ prefix: the API lists the "
            "reports and presigns downloads.",
        )
        _acknowledge_grant(function, report_generator.grant_invoke(function), _FUNCTION_VERSIONS)
        return function

    def _stream_processor(self, table: dynamodb.ITableV2, dlq: sqs.IQueue) -> lambda_.Function:
        function = self._function(
            "StreamProcessorFunction",
            handler="handlers.stream_processor.handler",
            service="stream-processor",
            description="DynamoDB stream -> history, monthly totals and domain events",
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"TABLE_NAME": table.table_name, "EVENT_BUS_NAME": self.event_bus.event_bus_name},
        )
        _acknowledge_grant(function, table.grant_read_write_data(function), _TABLE_INDEXES)
        self.event_bus.grant_put_events_to(function)
        function.add_event_source(_stream_source(table, dlq))
        return function

    def _notifier(
        self,
        idempotency_table: dynamodb.ITableV2,
        user_pool: cognito.IUserPool,
        queue: sqs.IQueue,
        site_url: str,
    ) -> lambda_.Function:
        sender_email = self._config.sender_email
        function = self._function(
            "NotifierFunction",
            handler="handlers.notifier.handler",
            service="notifier",
            description="Emails people about domain events (SES), idempotently",
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "IDEMPOTENCY_TABLE_NAME": idempotency_table.table_name,
                "USER_POOL_ID": user_pool.user_pool_id,
                "SENDER_EMAIL": sender_email or "",
                "APP_URL": site_url,
            },
        )
        idempotency_table.grant_read_write_data(function)
        user_pool.grant(function, "cognito-idp:ListUsersInGroup")
        function.add_event_source(
            event_sources.SqsEventSource(
                queue,
                batch_size=10,
                max_batching_window=Duration.seconds(5),
                # Only the failed messages of a batch return to the queue, not the whole batch.
                report_batch_item_failures=True,
            )
        )
        if sender_email:
            # Gotcha: a new SES account is in the *sandbox*: it sends only to verified addresses until
            # production access is granted. AWS emails a verification link to the sender after deployment.
            sender = ses.EmailIdentity(self, "SenderIdentity", identity=ses.Identity.email(sender_email))
            sender.grant_send_email(function)
        return function

    def _realtime_api(
        self,
        table: dynamodb.ITableV2,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        functions: dict[str, lambda_.Function],
    ) -> RealtimeApi:
        """The WebSocket API with its two handler functions (added to `functions`)."""
        authorizer = self._function(
            "WsAuthorizerFunction",
            handler="handlers.ws_authorizer.handler",
            service="ws-authorizer",
            description="WebSocket $connect authorizer: validates the Cognito access token",
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "USER_POOL_ID": user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": user_pool_client.user_pool_client_id,
            },
        )
        connections = self._function(
            "WsConnectionsFunction",
            handler="handlers.ws_connections.handler",
            service="ws-connections",
            description="WebSocket $connect/$disconnect/$default: stores connections per audience",
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={"TABLE_NAME": table.table_name},
        )
        _acknowledge_grant(connections, table.grant_read_write_data(connections), _TABLE_INDEXES)
        functions["WsAuthorizer"] = authorizer
        functions["WsConnections"] = connections
        return RealtimeApi(
            self,
            "RealtimeApi",
            config=self._config,
            authorizer_function=authorizer,
            connections_function=connections,
        )

    def _realtime_push(self, table: dynamodb.ITableV2, realtime: RealtimeApi) -> lambda_.Function:
        function = self._function(
            "RealtimePushFunction",
            handler="handlers.realtime_push.handler",
            service="realtime-push",
            description="Pushes domain events to the browsers connected over WebSocket",
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"TABLE_NAME": table.table_name, "WS_CALLBACK_URL": realtime.callback_url},
        )
        _acknowledge_grant(function, table.grant_read_write_data(function), _TABLE_INDEXES)
        _acknowledge_grant(
            function,
            realtime.stage.grant_management_api_access(function),
            "execute-api:ManageConnections on the `live` stage of this API only; connection ids are "
            "created at run time.",
        )
        return function

    def _approval_request(self, table: dynamodb.ITableV2) -> lambda_.Function:
        function = self._function(
            "ApprovalRequestFunction",
            handler="handlers.approval_request.handler",
            service="approval-request",
            description="ApprovalWorkflow callback task: stores the task token on the expense",
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={"TABLE_NAME": table.table_name},
        )
        _acknowledge_grant(function, table.grant_read_write_data(function), _TABLE_INDEXES)
        return function

    def _extract_receipt(self, receipts_bucket: s3.IBucket) -> lambda_.Function:
        config = self._config
        bedrock_region = config.bedrock_region or self.region
        function = self._function(
            "ExtractReceiptFunction",
            handler="handlers.extract_receipt.handler",
            service="extract-receipt",
            description="ReceiptPipeline task: Claude on Amazon Bedrock extracts the receipt fields",
            # Reading a receipt takes Claude seconds to tens of seconds; retries live in Step Functions.
            timeout=Duration.seconds(180),
            memory_size=512,
            environment={
                "RECEIPTS_BUCKET": receipts_bucket.bucket_name,
                "BEDROCK_REGION": bedrock_region,
                "EXTRACTION_MODEL_ID": config.extraction_model_id,
                "EXTRACTION_FALLBACK_MODEL_ID": config.extraction_fallback_model_id or "",
                "MAX_RECEIPT_BYTES": str(config.max_receipt_bytes),
            },
        )
        _acknowledge_grant(function, receipts_bucket.grant_read(function, _RECEIPT_OBJECTS), _RECEIPT_READ)
        statement = self._bedrock_statement(bedrock_region)
        function.add_to_role_policy(statement)
        _acknowledge_statements(
            function,
            [statement],
            "A cross-Region inference profile sends each request to the model in a Region that Bedrock picks "
            "at run time, so IAM must allow these foundation models in every Region.",
        )
        return function

    def _model_ids(self) -> list[str]:
        fallback = self._config.extraction_fallback_model_id
        return [self._config.extraction_model_id, *([fallback] if fallback else [])]

    def _bedrock_statement(self, bedrock_region: str) -> iam.PolicyStatement:
        """`bedrock:InvokeModel` for the configured models through bedrock-runtime.

        A cross-Region *inference profile* (`jp.anthropic...`) is a router: IAM checks the call
        against the profile ARN AND against the foundation model in whichever Region serves it
        (Tokyo or Osaka for `jp.`; any Region for `global.`). Hence the foundation-model ARNs with
        a wildcard Region, plus the Region-less form that global profiles use.
        """
        resources: list[str] = []
        for model_id in self._model_ids():
            base_model = foundation_model_id(model_id)
            if base_model != model_id:
                resources.append(
                    self.format_arn(
                        service="bedrock",
                        region=bedrock_region,
                        resource="inference-profile",
                        resource_name=model_id,
                    )
                )
            resources.extend(
                self.format_arn(
                    service="bedrock",
                    region=region,
                    account="",
                    resource="foundation-model",
                    resource_name=base_model,
                )
                for region in ("*", "")
            )
        return iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=resources)

    # --- Workflows and event routing -----------------------------------------------------------
    @staticmethod
    def _grant_workflow_control(api: lambda_.Function, approval: ApprovalWorkflow) -> None:
        """The decision endpoint completes the waiting task; withdrawing stops the execution."""
        approval.state_machine.grant_task_response(api)
        _acknowledge_grant(
            api,
            approval.state_machine.grant_execution(api, "states:StopExecution"),
            "Execution ARNs are created at run time; StopExecution is limited to executions of the "
            "ApprovalWorkflow state machine.",
        )

    def _route_events(
        self,
        queues: _Queues,
        *,
        approval_workflow: ApprovalWorkflow,
        receipt_pipeline: ReceiptPipeline,
        realtime_push: lambda_.IFunction,
        receipts_bucket: s3.IBucket,
    ) -> None:
        """EventBridge rules: who reacts to which event.

        Each target retries on its own and sends what it finally cannot deliver to a DLQ.
        """
        events.Rule(
            self,
            "ExpenseSubmittedRule",
            event_bus=self.event_bus,
            description="Start the ApprovalWorkflow for every submitted expense",
            event_pattern=events.EventPattern(source=[EXPENSES_SOURCE], detail_type=["Expense Submitted"]),
            targets=[
                targets.SfnStateMachine(
                    approval_workflow.state_machine,
                    dead_letter_queue=queues.approval_rule_dlq,
                    retry_attempts=4,
                    max_event_age=Duration.hours(2),
                )
            ],
        )
        events.Rule(
            self,
            "NotificationsRule",
            event_bus=self.event_bus,
            description="Queue every domain event for the notifier",
            event_pattern=_all_domain_events(),
            targets=[targets.SqsQueue(queues.notifications)],
        )
        events.Rule(
            self,
            "RealtimeRule",
            event_bus=self.event_bus,
            description="Push every domain event to the connected browsers",
            event_pattern=_all_domain_events(),
            targets=[
                targets.LambdaFunction(
                    realtime_push,
                    retry_attempts=2,
                    max_event_age=Duration.hours(1),
                    dead_letter_queue=queues.realtime_dlq,
                )
            ],
        )
        # S3 publishes to the account's *default* bus (enabled on the bucket in the Data stack).
        events.Rule(
            self,
            "ReceiptUploadedRule",
            description="Start the ReceiptPipeline for every receipt uploaded to the receipts bucket",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [receipts_bucket.bucket_name]},
                    "object": {"key": events.Match.prefix(RECEIPTS_PREFIX)},
                },
            ),
            targets=[
                targets.SfnStateMachine(
                    receipt_pipeline.state_machine, dead_letter_queue=queues.receipt_rule_dlq
                )
            ],
        )

    def _outputs(self) -> None:
        CfnOutput(self, "ApiUrl", value=self.api_url)
        CfnOutput(self, "WebSocketUrl", value=self.websocket_url)
        CfnOutput(self, "EventBusName", value=self.event_bus.event_bus_name)
        CfnOutput(self, "ApprovalStateMachineArn", value=self.approval_state_machine.state_machine_arn)
        CfnOutput(self, "ReceiptStateMachineArn", value=self.receipt_state_machine.state_machine_arn)
        CfnOutput(self, "DashboardName", value=self.dashboard_name)
        CfnOutput(self, "HttpApiId", value=self.http_api.api_id)
        CfnOutput(self, "WebSocketApiId", value=self.websocket_api.api_id)


def _queue(
    scope: Construct,
    construct_id: str,
    *,
    retention_period: Duration | None = None,
    visibility_timeout: Duration | None = None,
    dead_letter_queue: sqs.DeadLetterQueue | None = None,
) -> sqs.Queue:
    """An SQS queue encrypted at rest (SSE-SQS, free) that refuses unencrypted (HTTP) requests."""
    return sqs.Queue(
        scope,
        construct_id,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=retention_period,
        visibility_timeout=visibility_timeout,
        dead_letter_queue=dead_letter_queue,
    )


def _all_domain_events() -> events.EventPattern:
    return events.EventPattern(source=events.Match.prefix(EVENT_SOURCE_PREFIX))


def _stream_source(table: dynamodb.ITableV2, dlq: sqs.IQueue) -> event_sources.DynamoEventSource:
    """The table's change stream as an event source for stream_processor.

    Lambda evaluates the filter *before* invoking the function (filtered records are not billed):
    only expense items (`EXPENSE#...` / `META`) pass. The processor's own writes (HISTORY and
    TOTAL items) are filtered out, which also keeps it from triggering itself in a loop.
    A failing record is retried, the batch bisected to isolate it, and after 5 attempts or 1 hour
    its location goes to the DLQ so that the shard keeps moving.
    """
    return event_sources.DynamoEventSource(
        table,
        starting_position=lambda_.StartingPosition.LATEST,
        batch_size=25,
        retry_attempts=5,
        bisect_batch_on_error=True,
        report_batch_item_failures=True,
        max_record_age=Duration.hours(1),
        on_failure=event_sources.SqsDlq(dlq),
        filters=[
            lambda_.FilterCriteria.filter(
                {
                    "eventName": lambda_.FilterRule.or_("INSERT", "MODIFY"),
                    "dynamodb": {
                        "Keys": {
                            "PK": {"S": lambda_.FilterRule.begins_with("EXPENSE#")},
                            "SK": {"S": lambda_.FilterRule.is_equal("META")},
                        }
                    },
                }
            )
        ],
    )


def _acknowledge_grant(function: lambda_.Function, grant: iam.Grant, reason: str) -> None:
    """Acknowledge the wildcards that `grant` added to `function`'s role, with `reason`."""
    _acknowledge_statements(function, grant.principal_statements, reason)


def _acknowledge_statements(
    function: lambda_.Function, statements: list[iam.PolicyStatement], reason: str
) -> None:
    """Acknowledge each wildcard action/resource of `statements` on the function's role.

    Statements without wildcards produce no cdk-nag finding and need no acknowledgment.
    """
    role = function.role
    if role is None:
        raise ValueError(f"{function.node.path} has no execution role")
    acknowledge_wildcards(
        role,
        reason,
        actions=[action for statement in statements for action in statement.actions],
        resources=[resource for statement in statements for resource in statement.resources],
    )
