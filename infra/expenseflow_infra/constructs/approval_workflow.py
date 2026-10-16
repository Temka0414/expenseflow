"""ApprovalWorkflow: the Step Functions state machine that decides every submitted expense.

Started by the EventBridge rule for `Expense Submitted`, one execution per submission:

    Init -> ClaimExpense -> LoadPolicy -> CanAutoApprove? --yes--> AutoApprove -> Approved
                                                         `--no--> RequestApproval (wait for a manager)
    RequestApproval --decision--> RecordDecision -> Decided
                    --timeout---> Escalate -> RequestEscalatedApproval --decision--> RecordDecision
                                                                       --timeout---> ExpireRequest -> Expired
    Any "the expense is no longer PENDING_APPROVAL" condition failure ---------------> AlreadyHandled

Concepts this file demonstrates:

* **JSONata state machines** (`query_language=JSONATA`): states read data with `{% ... %}` expressions
  (`$states.input`, `$states.result`, `$states.context`) and keep values in *variables* (`Assign`), so
  no `InputPath`/`ResultPath` plumbing is needed. Only the `.jsonata(...)` factories are used: the
  JSONPath helpers (`sfn.JsonPath.*`) render JSONPath syntax that a JSONata state does not evaluate.
* **Callback pattern** (`.waitForTaskToken`): Step Functions invokes `approval_request` with a task
  token and then *waits*, for free, until the API calls `SendTaskSuccess(token, decision)`.
* **Conditional writes as a state guard**: every update requires `status = PENDING_APPROVAL`. If the
  owner withdrew the expense, or a duplicate event started a second execution, the condition fails
  and the execution ends in `AlreadyHandled` instead of overwriting newer data (idempotency).
* **Direct service integrations**: DynamoDB, SSM and EventBridge are called by Step Functions itself;
  no Lambda function sits in between, so there is no code to maintain and nothing to cold-start.

The module also holds the small helpers shared with the ReceiptPipeline workflow and the Backend stack
(JSONata building blocks, the logged state machine factory, `acknowledge_lambda_invoke`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.nag import acknowledge, acknowledge_wildcards

# --- Error names --------------------------------------------------------------------------------
# Step Functions prefixes service errors with the integration's service name. The *optimized*
# DynamoDB integration (`arn:aws:states:::dynamodb:updateItem`) uses `DynamoDB.`, while the generic
# *AWS SDK* integration (`arn:aws:states:::aws-sdk:dynamodb:updateItem`) uses the SDK's spelling
# `DynamoDb.` - a classic reason why a Catch silently never matches.
CONDITIONAL_CHECK_FAILED: Final = "DynamoDB.ConditionalCheckFailedException"
SDK_CONDITIONAL_CHECK_FAILED: Final = "DynamoDb.ConditionalCheckFailedException"
TRANSIENT_DYNAMODB_ERRORS: Final = (
    "DynamoDB.ProvisionedThroughputExceededException",
    "DynamoDB.ThrottlingException",
    "DynamoDB.InternalServerErrorException",
)
TRANSIENT_SDK_DYNAMODB_ERRORS: Final = (
    "DynamoDb.ProvisionedThroughputExceededException",
    "DynamoDb.ThrottlingException",
    "DynamoDb.InternalServerErrorException",
)
_TRANSIENT_SSM_ERRORS: Final = ("Ssm.ThrottlingException", "Ssm.InternalServerErrorException")
_TRANSIENT_EVENTBRIDGE_ERRORS: Final = ("EventBridge.InternalException", "EventBridge.ThrottlingException")
_EXPENSE_NOT_PENDING: Final = "ExpenseNotPending"
"""Error raised by the approval_request Lambda when the expense is no longer pending."""

# --- Domain values ------------------------------------------------------------------------------
PENDING_APPROVAL: Final = "PENDING_APPROVAL"
APPROVED: Final = "APPROVED"
REJECTED: Final = "REJECTED"
SYSTEM_ACTOR: Final = "system"
ESCALATION_SOURCE: Final = "expenseflow.approvals"
ESCALATION_DETAIL_TYPE: Final = "Approval Escalated"

APPROVAL_EXECUTION_TIMEOUT: Final = Duration.days(30)
"""Longer than two approval waits of the maximum configurable length (2 x 14 days)."""

STATE_ENTERED_TIME: Final = "$states.context.State.EnteredTime"
"""ISO-8601 UTC timestamp with milliseconds, the same format as the backend's `ids.now_iso()`."""


# --- Helpers shared with the ReceiptPipeline workflow and the Backend stack ---------------------------
def jsonata(expression: str) -> str:
    """Wrap `expression` in `{% %}`: Step Functions evaluates such strings as JSONata at run time."""
    return "{% " + expression + " %}"


def or_null(path: str) -> str:
    """JSONata for "`path` if it exists, else null".

    DynamoDB omits absent attributes, and a JSONata path to a missing field yields *undefined*.
    Normalising to null keeps variables and event payloads well-defined.
    """
    return f"$exists({path}) ? {path} : null"


def string_value(expression: str) -> tasks.DynamoAttributeValue:
    """A DynamoDB string (`{"S": ...}`) computed by a JSONata expression."""
    return tasks.DynamoAttributeValue.from_string(jsonata(expression))


def expense_key() -> dict[str, tasks.DynamoAttributeValue]:
    """Key of the expense item (`EXPENSE#{id}` / `META`), using the workflow variable `$expenseId`."""
    return {
        "PK": string_value("'EXPENSE#' & $expenseId"),
        "SK": tasks.DynamoAttributeValue.from_string("META"),
    }


def retry_transient(task: sfn.TaskStateBase, errors: Sequence[str]) -> None:
    """Retry throttling and 5xx errors: 1 s, 2 s, 4 s with FULL jitter (randomised waits).

    Jitter spreads retries of many parallel executions over time instead of having them all hit
    a throttled service again at the same instant. Condition failures are never retried: they are
    a decision ("someone else already changed this item"), not a glitch.
    """
    task.add_retry(
        errors=list(errors),
        interval=Duration.seconds(1),
        max_attempts=3,
        backoff_rate=2,
        jitter_strategy=sfn.JitterType.FULL,
    )


def update_expense(
    scope: Construct,
    state_name: str,
    *,
    table: dynamodb.ITableV2,
    comment: str,
    update_expression: str,
    condition_expression: str,
    values: Mapping[str, tasks.DynamoAttributeValue],
    names: Mapping[str, str] | None = None,
    on_condition_failed: sfn.IChainable,
    return_values: tasks.DynamoReturnValues | None = None,
    assign: Mapping[str, str] | None = None,
) -> tasks.DynamoUpdateItem:
    """A conditional `UpdateItem` of the expense item through the optimized DynamoDB integration.

    The condition makes the state idempotent and race-free: when it fails, DynamoDB changes
    nothing and the execution continues at `on_condition_failed`.
    """
    task = tasks.DynamoUpdateItem.jsonata(
        scope,
        state_name,
        comment=comment,
        table=table,
        key=expense_key(),
        update_expression=update_expression,
        condition_expression=condition_expression,
        expression_attribute_names=names,
        expression_attribute_values=values,
        return_values=return_values,
        assign=assign,
    )
    retry_transient(task, TRANSIENT_DYNAMODB_ERRORS)
    task.add_catch(on_condition_failed, errors=[CONDITIONAL_CHECK_FAILED])
    return task


def succeed(scope: Construct, state_name: str, *, comment: str) -> sfn.Succeed:
    """Terminal state whose output names the outcome (shown on the execution in the console)."""
    return sfn.Succeed.jsonata(
        scope,
        state_name,
        comment=comment,
        outputs={"expenseId": jsonata("$expenseId"), "outcome": state_name},
    )


def acknowledge_lambda_invoke(state_machine: sfn.StateMachine, function: lambda_.IFunction) -> None:
    """Acknowledge the `<function ARN>:*` resource that CDK grants when a state invokes `function`."""
    acknowledge_wildcards(
        state_machine,
        "lambda:InvokeFunction on one function and its versions/aliases (`<arn>:*`), as granted by "
        "the LambdaInvoke task; the workflow invokes only this function.",
        resources=[f"{function.function_arn}:*"],
    )


def jsonata_state_machine(
    scope: Construct,
    construct_id: str,
    *,
    config: StageConfig,
    definition: sfn.IChainable,
    timeout: Duration,
    comment: str,
) -> sfn.StateMachine:
    """A STANDARD JSONata state machine with X-Ray tracing and full execution logging.

    STANDARD (not EXPRESS) because executions wait for humans for days, need exactly-once state
    transitions and keep a visual execution history for 90 days. Logging level ALL with execution
    data records every state's input and output: invaluable while learning, but it stores expense
    data in CloudWatch Logs, so the log group's retention is limited like every other log group.
    """
    log_group = logs.LogGroup(
        scope,
        f"{construct_id}Logs",
        # The `/aws/vendedlogs/` prefix matters: for other names CloudWatch Logs adds a statement
        # to the account's log *resource policy* for every state machine, and that policy has a
        # 5,120-character limit that larger accounts hit sooner than expected.
        log_group_name=f"/aws/vendedlogs/states/{PROJECT_NAME}-{config.stage}-{scope.node.id}",
        retention=config.log_retention,
        removal_policy=config.removal_policy,
    )
    state_machine = sfn.StateMachine(
        scope,
        construct_id,
        comment=comment,
        definition_body=sfn.DefinitionBody.from_chainable(definition),
        query_language=sfn.QueryLanguage.JSONATA,
        state_machine_type=sfn.StateMachineType.STANDARD,
        timeout=timeout,
        tracing_enabled=True,
        logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
    )
    acknowledge(
        state_machine,
        (
            "AwsSolutions-IAM5[Resource::*]",
            "Step Functions log delivery (logs:CreateLogDelivery and related actions) and X-Ray "
            "tracing do not support resource-level permissions, so AWS requires Resource '*'.",
        ),
    )
    return state_machine


# --- The approval workflow ----------------------------------------------------------------------
class ApprovalWorkflow(Construct):
    """Auto-approves small, AI-verified expenses; otherwise waits for a manager, escalating once.

    Attributes:
        state_machine: The state machine; its input is the whole `Expense Submitted` event.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        table: dynamodb.ITableV2,
        request_function: lambda_.IFunction,
        event_bus: events.IEventBus,
        policy_parameter: ssm.IStringParameter,
    ) -> None:
        """Create the workflow.

        Args:
            config: Stage settings (default currency, approval timeout, logs).
            table: The single table holding the expense items.
            request_function: `approval_request` Lambda; stores the task token on the expense.
            event_bus: Custom bus receiving `Approval Escalated`.
            policy_parameter: SSM parameter with the auto-approval limit (read on every execution,
                so the limit can be changed without a deployment).
        """
        super().__init__(scope, construct_id)
        self._config = config
        self._table = table
        self.state_machine = jsonata_state_machine(
            self,
            "StateMachine",
            config=config,
            definition=self._definition(request_function, event_bus, policy_parameter),
            timeout=APPROVAL_EXECUTION_TIMEOUT,
            comment="ExpenseFlow approval: auto-approve by policy or wait for a manager's decision",
        )
        acknowledge_lambda_invoke(self.state_machine, request_function)

    def _definition(
        self,
        request_function: lambda_.IFunction,
        event_bus: events.IEventBus,
        policy_parameter: ssm.IStringParameter,
    ) -> sfn.IChainable:
        already_handled = succeed(
            self,
            "AlreadyHandled",
            comment="The expense was withdrawn, decided or claimed by another execution: nothing to do.",
        )
        record_decision = self._record_decision(already_handled)
        record_decision.next(succeed(self, "Decided", comment="A manager approved or rejected the expense."))

        expire = self._expire_request(already_handled)
        expire.next(succeed(self, "Expired", comment="Nobody decided in time: automatically rejected."))

        request_escalated = self._request_approval(
            "RequestEscalatedApproval", request_function, escalated=True, already_handled=already_handled
        )
        request_escalated.add_catch(expire, errors=[sfn.Errors.TIMEOUT])
        request_escalated.next(record_decision)

        escalate = self._escalate(event_bus)
        # The escalation event only triggers notifications; failing to publish it must not cancel
        # the approval itself, so any error still proceeds to the second wait.
        escalate.add_catch(request_escalated, errors=[sfn.Errors.ALL])
        escalate.next(request_escalated)

        request = self._request_approval(
            "RequestApproval", request_function, escalated=False, already_handled=already_handled
        )
        request.add_catch(escalate, errors=[sfn.Errors.TIMEOUT])
        request.next(record_decision)

        auto_approve = self._auto_approve(already_handled)
        auto_approve.next(succeed(self, "Approved", comment="Approved automatically by policy."))

        can_auto_approve = (
            sfn.Choice.jsonata(
                self,
                "CanAutoApprove",
                comment="Small amount in the default currency AND the receipt total read by Claude matches.",
            )
            .when(sfn.Condition.jsonata(jsonata(self._auto_approval_rule())), auto_approve)
            .otherwise(request)
        )

        return (
            self._init()
            .next(self._claim_expense(already_handled))
            .next(self._load_policy(policy_parameter))
            .next(can_auto_approve)
        )

    def _init(self) -> sfn.Pass:
        return sfn.Pass.jsonata(
            self,
            "Init",
            comment="Keep the ids from the EventBridge event in workflow variables.",
            assign={
                "expenseId": jsonata("$states.input.detail.expenseId"),
                "ownerId": jsonata("$states.input.detail.ownerId"),
                "now": jsonata(STATE_ENTERED_TIME),
            },
        )

    def _claim_expense(self, already_handled: sfn.IChainable) -> tasks.DynamoUpdateItem:
        return update_expense(
            self,
            "ClaimExpense",
            table=self._table,
            comment=(
                "Record this execution on the expense. EventBridge delivers at least once: a duplicate "
                "event finds workflowExecutionArn already set and stops here."
            ),
            update_expression="SET workflowExecutionArn = :arn",
            condition_expression="#status = :pending AND attribute_not_exists(workflowExecutionArn)",
            names={"#status": "status"},
            values={
                ":arn": string_value("$states.context.Execution.Id"),
                ":pending": tasks.DynamoAttributeValue.from_string(PENDING_APPROVAL),
            },
            return_values=tasks.DynamoReturnValues.ALL_NEW,
            # The full item in DynamoDB JSON, e.g. {"amount": {"N": "1280"}, "title": {"S": "..."}}.
            assign={"expense": jsonata("$states.result.Attributes")},
            on_condition_failed=already_handled,
        )

    def _load_policy(self, policy_parameter: ssm.IStringParameter) -> tasks.CallAwsService:
        task = tasks.CallAwsService.jsonata(
            self,
            "LoadPolicy",
            comment="Read the auto-approval limit from SSM Parameter Store (change it without deploying).",
            service="ssm",
            action="getParameter",
            parameters={"Name": policy_parameter.parameter_name},
            iam_resources=[policy_parameter.parameter_arn],
            assign={"autoApproveLimit": jsonata("$number($states.result.Parameter.Value)")},
        )
        retry_transient(task, _TRANSIENT_SSM_ERRORS)
        return task

    def _auto_approval_rule(self) -> str:
        """JSONata: amount <= limit in the default currency AND |AI total - amount| <= max(1, 1%).

        Every attribute is optional on the item, so each is checked with `$exists` before use, and
        the arithmetic only runs (ternary) once all inputs are known to be numbers.
        """
        currency = self._config.default_currency
        return " ".join(
            [
                "(",
                "$amount := $exists($expense.amount.N) ? $number($expense.amount.N) : null;",
                "$verified := $expense.extractionStatus.S = 'SUCCEEDED'",
                "and $exists($expense.extractionResult.S);",
                "$total := $verified ? $parse($expense.extractionResult.S).total : null;",
                f"($amount != null and $type($total) = 'number' and $expense.currency.S = '{currency}'",
                "and $amount <= $autoApproveLimit)",
                "? $abs($total - $amount) <= $max([1, $amount * 0.01])",
                ": false",
                ")",
            ]
        )

    def _auto_approve(self, already_handled: sfn.IChainable) -> tasks.DynamoUpdateItem:
        return update_expense(
            self,
            "AutoApprove",
            table=self._table,
            comment="Approve without a human; the comment records which policy allowed it.",
            update_expression=(
                "SET #status = :approved, autoApproved = :true, approverEmail = :auto, "
                "decisionComment = :comment, decidedAt = :now, updatedAt = :now, "
                "GSI2PK = :gsi2pk, GSI2SK = :gsi2sk, lastActorId = :system, lastActorEmail = :system, "
                "#version = #version + :one"
            ),
            condition_expression="#status = :pending",
            names={"#status": "status", "#version": "version"},
            values={
                ":approved": tasks.DynamoAttributeValue.from_string(APPROVED),
                ":true": tasks.DynamoAttributeValue.from_boolean(True),
                ":auto": tasks.DynamoAttributeValue.from_string("auto-approval"),
                ":comment": string_value(
                    "'Auto-approved by policy: ≤ ' & $string($autoApproveLimit) & ' ' & "
                    "$expense.currency.S & ' and receipt total verified by AI'"
                ),
                ":now": string_value("$now"),
                ":gsi2pk": tasks.DynamoAttributeValue.from_string(f"STATUS#{APPROVED}"),
                ":gsi2sk": string_value("$now & '#' & $expenseId"),
                ":system": tasks.DynamoAttributeValue.from_string(SYSTEM_ACTOR),
                ":one": tasks.DynamoAttributeValue.number_from_string("1"),
                ":pending": tasks.DynamoAttributeValue.from_string(PENDING_APPROVAL),
            },
            on_condition_failed=already_handled,
        )

    def _request_approval(
        self,
        state_name: str,
        request_function: lambda_.IFunction,
        *,
        escalated: bool,
        already_handled: sfn.IChainable,
    ) -> tasks.LambdaInvoke:
        """Invoke `approval_request` with a task token, then pause until the API reports a decision.

        The token must come from `$states.context.Task.Token` inside a JSONata expression. (The
        JSONPath helper `sfn.JsonPath.task_token` renders the literal text "$$.Task.Token" here.)
        The default Lambda retrier (service errors, throttling) stays enabled.
        """
        task = tasks.LambdaInvoke.jsonata(
            self,
            state_name,
            comment=(
                "Store the task token on the expense and wait for POST /approvals/{id}/decision "
                "(SendTaskSuccess). Waiting costs nothing: Standard workflows bill per transition."
            ),
            lambda_function=request_function,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object(
                {
                    "taskToken": jsonata("$states.context.Task.Token"),
                    "expenseId": jsonata("$expenseId"),
                    "escalated": escalated,
                }
            ),
            task_timeout=sfn.Timeout.duration(Duration.minutes(self._config.approval_timeout_minutes)),
            # `$states.result` is the JSON the API passed to SendTaskSuccess.
            assign={
                "decision": jsonata("$states.result.decision"),
                "approverId": jsonata("$states.result.approverId"),
                "approverEmail": jsonata("$states.result.approverEmail"),
                "comment": jsonata(or_null("$states.result.comment")),
            },
        )
        task.add_catch(already_handled, errors=[_EXPENSE_NOT_PENDING])
        return task

    def _escalate(self, event_bus: events.IEventBus) -> tasks.EventBridgePutEvents:
        task = tasks.EventBridgePutEvents.jsonata(
            self,
            "Escalate",
            comment="No decision in time: tell managers and admins, then wait once more.",
            entries=[
                tasks.EventBridgePutEventsEntry(
                    event_bus=event_bus,
                    source=ESCALATION_SOURCE,
                    detail_type=ESCALATION_DETAIL_TYPE,
                    detail=sfn.TaskInput.from_object(
                        {
                            "expenseId": jsonata("$expenseId"),
                            "ownerId": jsonata("$ownerId"),
                            "title": jsonata("$expense.title.S"),
                            "amount": jsonata("$number($expense.amount.N)"),
                            "currency": jsonata("$expense.currency.S"),
                            "requestedAt": jsonata("$now"),
                            "idempotencyKey": jsonata(
                                "$expenseId & ':ESCALATED:' & $states.context.Execution.Id"
                            ),
                        }
                    ),
                )
            ],
        )
        retry_transient(task, _TRANSIENT_EVENTBRIDGE_ERRORS)
        return task

    def _expire_request(self, already_handled: sfn.IChainable) -> tasks.DynamoUpdateItem:
        total_minutes = 2 * self._config.approval_timeout_minutes
        return update_expense(
            self,
            "ExpireRequest",
            table=self._table,
            comment="Both waits timed out: reject automatically so that nothing stays pending forever.",
            update_expression=(
                "SET #status = :rejected, autoApproved = :false, decisionComment = :comment, "
                "decidedAt = :now, updatedAt = :now, GSI2PK = :gsi2pk, GSI2SK = :gsi2sk, "
                "lastActorId = :system, lastActorEmail = :system, #version = #version + :one "
                "REMOVE approvalTaskToken"
            ),
            condition_expression="#status = :pending",
            names={"#status": "status", "#version": "version"},
            values={
                ":rejected": tasks.DynamoAttributeValue.from_string(REJECTED),
                ":false": tasks.DynamoAttributeValue.from_boolean(False),
                ":comment": tasks.DynamoAttributeValue.from_string(
                    f"Automatically rejected: no decision within {total_minutes} minutes (after escalation)"
                ),
                ":now": string_value(STATE_ENTERED_TIME),
                ":gsi2pk": tasks.DynamoAttributeValue.from_string(f"STATUS#{REJECTED}"),
                ":gsi2sk": string_value(f"{STATE_ENTERED_TIME} & '#' & $expenseId"),
                ":system": tasks.DynamoAttributeValue.from_string(SYSTEM_ACTOR),
                ":one": tasks.DynamoAttributeValue.number_from_string("1"),
                ":pending": tasks.DynamoAttributeValue.from_string(PENDING_APPROVAL),
            },
            on_condition_failed=already_handled,
        )

    def _record_decision(self, already_handled: sfn.IChainable) -> tasks.CallAwsService:
        """Write the manager's decision; `decisionComment` only when the manager wrote one.

        Optional attributes stay *absent* rather than empty, so the update expression and its value
        map depend on the data. DynamoDB rejects values the expression does not use, and the typed
        `DynamoUpdateItem` task has a fixed value map; the AWS SDK integration (`CallAwsService`)
        accepts any parameters, so JSONata builds both at run time. Its errors use the SDK's
        `DynamoDb.` prefix.
        """
        now = STATE_ENTERED_TIME
        values = ", ".join(
            [
                "':decision': {'S': $decision}",
                "':approverId': {'S': $approverId}",
                "':approverEmail': {'S': $approverEmail}",
                "':false': {'BOOL': false}",
                f"':now': {{'S': {now}}}",
                "':gsi2pk': {'S': 'STATUS#' & $decision}",
                f"':gsi2sk': {{'S': {now} & '#' & $expenseId}}",
                "':one': {'N': '1'}",
                f"':pending': {{'S': '{PENDING_APPROVAL}'}}",
            ]
        )
        task = tasks.CallAwsService.jsonata(
            self,
            "RecordDecision",
            comment="Store the manager's decision (status, approver, comment) and drop the used task token.",
            service="dynamodb",
            action="updateItem",
            iam_action="dynamodb:UpdateItem",
            iam_resources=[self._table.table_arn],
            parameters={
                "TableName": self._table.table_name,
                "Key": {"PK": {"S": jsonata("'EXPENSE#' & $expenseId")}, "SK": {"S": "META"}},
                "UpdateExpression": jsonata(
                    "'SET #status = :decision, approverId = :approverId, approverEmail = :approverEmail, "
                    "autoApproved = :false, decidedAt = :now, updatedAt = :now, GSI2PK = :gsi2pk, "
                    "GSI2SK = :gsi2sk, lastActorId = :approverId, lastActorEmail = :approverEmail, "
                    "#version = #version + :one' "
                    "& ($comment ? ', decisionComment = :comment' : '') & ' REMOVE approvalTaskToken'"
                ),
                "ConditionExpression": "#status = :pending",
                "ExpressionAttributeNames": {"#status": "status", "#version": "version"},
                "ExpressionAttributeValues": jsonata(
                    f"$merge([{{{values}}}, $comment ? {{':comment': {{'S': $comment}}}} : {{}}])"
                ),
            },
        )
        retry_transient(task, TRANSIENT_SDK_DYNAMODB_ERRORS)
        task.add_catch(already_handled, errors=[SDK_CONDITIONAL_CHECK_FAILED])
        return task
