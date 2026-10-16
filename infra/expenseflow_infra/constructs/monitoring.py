"""Monitoring: CloudWatch alarms that email you when something breaks, and one dashboard for the app.

Every alarm uses `TreatMissingData.NOT_BREACHING`: AWS publishes most metrics only when there is
activity (no invocations -> no `Errors` datapoint), and "no data" must mean "fine", not "unknown".
Alarms evaluate one 5-minute period, so a single failed invocation or message in a DLQ notifies you.
Each standard alarm costs about USD 0.10 per month (the first 10 are free), each dashboard USD 3
beyond the first 3.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.nag import acknowledge

PERIOD: Final = Duration.minutes(5)
API_5XX_THRESHOLD: Final = 5
_FULL_WIDTH: Final = 24
_HALF_WIDTH: Final = 12

_BUSINESS_METRICS: Final = (
    "ExpensesApproved",
    "ExpensesRejected",
    "StatusTransitions",
    "NotificationsSkipped",
)
_CLAUDE_TOKEN_METRICS: Final = ("ExtractionInputTokens", "ExtractionOutputTokens")
_CLAUDE_QUALITY_METRICS: Final = ("ExtractionFallbackUsed",)

_DOC_LINKS: Final = (
    ("Lambda metrics", "https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics-types.html"),
    (
        "HTTP API metrics",
        "https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-metrics.html",
    ),
    (
        "Step Functions metrics",
        "https://docs.aws.amazon.com/step-functions/latest/dg/procedure-cw-metrics.html",
    ),
    (
        "SQS metrics",
        "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html",
    ),
    (
        "DynamoDB metrics",
        "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/metrics-dimensions.html",
    ),
    ("Powertools metrics", "https://docs.powertools.aws.dev/lambda/python/latest/core/metrics/"),
)


class Monitoring(Construct):
    """SNS alarm topic, alarms (API 5xx, Lambda errors, DLQ messages, failed executions) and a dashboard.

    Attributes:
        topic: SNS topic every alarm notifies (email subscription when `-c alarmEmail=` is set).
        alarms: All alarms, in dashboard order.
        dashboard: The CloudWatch dashboard `ExpenseFlow-{stage}`.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        http_api: apigwv2.IHttpApi,
        websocket_api: apigwv2.IWebSocketApi,
        functions: Mapping[str, lambda_.IFunction],
        state_machines: Mapping[str, sfn.IStateMachine],
        work_queues: Mapping[str, sqs.IQueue],
        dead_letter_queues: Mapping[str, sqs.IQueue],
        table: dynamodb.ITableV2,
    ) -> None:
        """Create topic, alarms and dashboard.

        Args:
            config: Stage settings (alarm email, dashboard name).
            http_api: The SPA's HTTP API.
            websocket_api: The realtime WebSocket API.
            functions: Lambda functions by display name; each gets an `Errors` alarm.
            state_machines: State machines by display name; each gets an `ExecutionsFailed` alarm.
            work_queues: Queues consumed by Lambda (graphed only).
            dead_letter_queues: DLQs by display name; each gets a "messages visible" alarm.
            table: The single table.
        """
        super().__init__(scope, construct_id)
        self.topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name=f"{PROJECT_NAME} {config.stage} alarms",
            # Deny publishing over plain HTTP (cdk-nag SNS3).
            enforce_ssl=True,
        )
        acknowledge(
            self.topic,
            (
                "AwsSolutions-SNS2",
                "Alarm notifications carry no sensitive data. CloudWatch alarms cannot publish to a topic "
                "encrypted with the AWS managed SNS key; SSE would need a customer managed KMS key "
                "(about USD 1 per month) with a key policy for cloudwatch.amazonaws.com.",
            ),
        )
        if config.alarm_email:
            # AWS emails a confirmation link first; no alarm mail arrives until it is clicked.
            self.topic.add_subscription(subscriptions.EmailSubscription(config.alarm_email))
        self._action = cw_actions.SnsAction(self.topic)

        api_alarm = self._alarm(
            "Api5xxAlarm",
            http_api.metric_server_error(period=PERIOD, statistic=cw.Stats.SUM),
            threshold=API_5XX_THRESHOLD,
            description=f"HTTP API returned >= {API_5XX_THRESHOLD} server errors (5xx) in 5 minutes. "
            "Check the API function's logs and the API access logs.",
        )
        function_alarms = [
            self._alarm(
                f"{name}ErrorsAlarm",
                function.metric_errors(period=PERIOD, statistic=cw.Stats.SUM),
                threshold=1,
                description=f"Lambda function {name} failed (unhandled error or timeout). Check its logs.",
            )
            for name, function in functions.items()
        ]
        dlq_alarms = [
            self._alarm(
                f"{name}MessagesAlarm",
                queue.metric_approximate_number_of_messages_visible(
                    period=PERIOD, statistic=cw.Stats.MAXIMUM
                ),
                threshold=1,
                description=f"Dead-letter queue {name} holds messages that could not be processed. "
                "Inspect them in the SQS console, fix the cause, then redrive them.",
            )
            for name, queue in dead_letter_queues.items()
        ]
        state_machine_alarms = [
            self._alarm(
                f"{name}FailedAlarm",
                machine.metric_failed(period=PERIOD, statistic=cw.Stats.SUM),
                threshold=1,
                description=f"A {name} execution failed. Open it in the Step Functions console to see "
                "which state failed and why.",
            )
            for name, machine in state_machines.items()
        ]
        self.alarms: list[cw.Alarm] = [api_alarm, *function_alarms, *dlq_alarms, *state_machine_alarms]

        self.dashboard = cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"{PROJECT_NAME}-{config.stage}",
            default_interval=Duration.hours(6),
        )
        self.dashboard.add_widgets(_header(config))
        self.dashboard.add_widgets(*_api_widgets(http_api, websocket_api))
        self.dashboard.add_widgets(*_lambda_widgets(functions))
        self.dashboard.add_widgets(*_state_machine_widgets(state_machines))
        self.dashboard.add_widgets(*_queue_widgets(work_queues, dead_letter_queues))
        self.dashboard.add_widgets(*_table_widgets(table))
        self.dashboard.add_widgets(*_business_widgets())
        self.dashboard.add_widgets(
            cw.AlarmStatusWidget(title="Alarms", alarms=self.alarms, width=_FULL_WIDTH, height=6)
        )

    def _alarm(
        self, construct_id: str, metric: cw.IMetric, *, threshold: float, description: str
    ) -> cw.Alarm:
        alarm = cw.Alarm(
            self,
            construct_id,
            metric=metric,
            threshold=threshold,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description=description,
        )
        alarm.add_alarm_action(self._action)
        return alarm


def _header(config: StageConfig) -> cw.TextWidget:
    links = " · ".join(f"[{title}]({url})" for title, url in _DOC_LINKS)
    return cw.TextWidget(
        markdown=(
            f"# {PROJECT_NAME} ({config.stage})\n"
            "Requests flow SPA → HTTP API → Lambda → DynamoDB; changes flow DynamoDB stream → EventBridge → "
            "Step Functions / SQS / WebSocket. Red alarms below link to their metric.\n\n"
            f"Docs: {links}"
        ),
        width=_FULL_WIDTH,
        height=3,
    )


def _api_widgets(http_api: apigwv2.IHttpApi, websocket_api: apigwv2.IWebSocketApi) -> list[cw.IWidget]:
    def websocket_metric(name: str) -> cw.Metric:
        return cw.Metric(
            namespace="AWS/ApiGateway",
            metric_name=name,
            dimensions_map={"ApiId": websocket_api.api_id},
            statistic=cw.Stats.SUM,
            period=PERIOD,
        )

    return [
        cw.GraphWidget(
            title="HTTP API requests and errors",
            left=[
                http_api.metric_count(period=PERIOD, statistic=cw.Stats.SUM, label="Requests"),
                http_api.metric_client_error(period=PERIOD, statistic=cw.Stats.SUM, label="4xx"),
                http_api.metric_server_error(period=PERIOD, statistic=cw.Stats.SUM, label="5xx"),
            ],
            right=[
                http_api.metric_latency(
                    period=PERIOD, statistic=cw.Stats.percentile(99), label="Latency p99 (ms)"
                ),
            ],
            width=_HALF_WIDTH,
        ),
        cw.GraphWidget(
            title="WebSocket API",
            left=[
                websocket_metric("ConnectCount").with_(label="Connections opened"),
                websocket_metric("MessageCount").with_(label="Messages"),
            ],
            right=[
                websocket_metric("ClientError").with_(label="Client errors"),
                websocket_metric("ExecutionError").with_(label="Execution errors"),
            ],
            width=_HALF_WIDTH,
        ),
    ]


def _lambda_widgets(functions: Mapping[str, lambda_.IFunction]) -> list[cw.IWidget]:
    return [
        cw.GraphWidget(
            title="Lambda errors",
            left=[
                fn.metric_errors(period=PERIOD, statistic=cw.Stats.SUM, label=name)
                for name, fn in functions.items()
            ],
            width=_HALF_WIDTH,
        ),
        cw.GraphWidget(
            title="Lambda duration p99 (ms)",
            left=[
                fn.metric_duration(period=PERIOD, statistic=cw.Stats.percentile(99), label=name)
                for name, fn in functions.items()
            ],
            width=_HALF_WIDTH,
        ),
    ]


def _state_machine_widgets(state_machines: Mapping[str, sfn.IStateMachine]) -> list[cw.IWidget]:
    return [
        cw.GraphWidget(
            title=f"{name} executions",
            left=[
                machine.metric_started(period=PERIOD, statistic=cw.Stats.SUM, label="Started"),
                machine.metric_succeeded(period=PERIOD, statistic=cw.Stats.SUM, label="Succeeded"),
                machine.metric_failed(period=PERIOD, statistic=cw.Stats.SUM, label="Failed"),
                machine.metric_timed_out(period=PERIOD, statistic=cw.Stats.SUM, label="Timed out"),
            ],
            width=_HALF_WIDTH,
        )
        for name, machine in state_machines.items()
    ]


def _queue_widgets(
    work_queues: Mapping[str, sqs.IQueue], dead_letter_queues: Mapping[str, sqs.IQueue]
) -> list[cw.IWidget]:
    return [
        cw.GraphWidget(
            title="Work queues: backlog and age of oldest message (s)",
            left=[
                queue.metric_approximate_number_of_messages_visible(
                    period=PERIOD, statistic=cw.Stats.MAXIMUM, label=f"{name} visible"
                )
                for name, queue in work_queues.items()
            ],
            right=[
                queue.metric_approximate_age_of_oldest_message(
                    period=PERIOD, statistic=cw.Stats.MAXIMUM, label=f"{name} oldest"
                )
                for name, queue in work_queues.items()
            ],
            width=_HALF_WIDTH,
        ),
        cw.GraphWidget(
            title="Dead-letter queues: messages waiting",
            left=[
                queue.metric_approximate_number_of_messages_visible(
                    period=PERIOD, statistic=cw.Stats.MAXIMUM, label=name
                )
                for name, queue in dead_letter_queues.items()
            ],
            width=_HALF_WIDTH,
        ),
    ]


def _table_widgets(table: dynamodb.ITableV2) -> list[cw.IWidget]:
    return [
        cw.GraphWidget(
            title="DynamoDB consumed capacity (on-demand: what you pay for)",
            left=[
                table.metric_consumed_read_capacity_units(
                    period=PERIOD, statistic=cw.Stats.SUM, label="Read units"
                ),
                table.metric_consumed_write_capacity_units(
                    period=PERIOD, statistic=cw.Stats.SUM, label="Write units"
                ),
            ],
            width=_HALF_WIDTH,
        ),
        cw.GraphWidget(
            title="DynamoDB conditional-check failures (idempotency guards and edit conflicts)",
            left=[
                table.metric_conditional_check_failed_requests(
                    period=PERIOD, statistic=cw.Stats.SUM, label="Condition failed"
                )
            ],
            width=_HALF_WIDTH,
        ),
    ]


def _business_widgets() -> list[cw.IWidget]:
    return [
        cw.GraphWidget(
            title="Expenses (custom metrics)", left=_searches(_BUSINESS_METRICS), width=_HALF_WIDTH
        ),
        cw.GraphWidget(
            title="Claude on Bedrock: tokens per 5 minutes (cost driver)",
            left=_searches(_CLAUDE_TOKEN_METRICS),
            right=_searches(_CLAUDE_QUALITY_METRICS),
            width=_HALF_WIDTH,
        ),
    ]


def _searches(metric_names: tuple[str, ...]) -> list[cw.IMetric]:
    """One `SEARCH` expression per custom metric emitted by Powertools.

    Powertools adds a `service` dimension, and handlers add more (`to`, `Model`, ...). A search
    graphs every dimension combination of the metric, including values only known at run time
    (such as the Bedrock model that answered).
    """
    return [
        cw.MathExpression(
            expression=f"SEARCH('{PROJECT_NAME} MetricName=\"{name}\"', 'Sum', {int(PERIOD.to_seconds())})",
            using_metrics={},
            label=name,
            period=PERIOD,
        )
        for name in metric_names
    ]
