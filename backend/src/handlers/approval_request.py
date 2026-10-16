"""ApprovalWorkflow task ``RequestApproval`` / ``RequestEscalatedApproval`` (SPEC 7.5, 8.2).

The state machine invokes this function with the **wait-for-task-token** pattern (``.waitForTaskToken``): Step
Functions generates a task token, passes it in the payload, and then *pauses the execution* - for up to the
task's timeout (72 h by default), at no cost while waiting - until someone calls ``SendTaskSuccess`` or
``SendTaskFailure`` with that token. Our only job is to park the token where that "someone" can find it: on the
expense item. When a manager decides, the API reads ``approvalTaskToken`` and calls ``SendTaskSuccess``.

Input ``{"taskToken", "expenseId", "escalated"}`` -> output ``{"stored": true}`` (the output of this Lambda is
ignored by a ``.waitForTaskToken`` task; the state's result is what ``SendTaskSuccess`` sends later).

The write is conditional on the expense still being PENDING_APPROVAL: if the owner withdrew it in the meantime,
storing a token would be pointless, so we fail with :class:`ExpenseNotPending`. A Python Lambda's error type is
the exception's class name, which is how the state machine's ``Catch`` on ``ExpenseNotPending`` recognises it.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from expenseflow import aws, ids, keys
from expenseflow.models import ExpenseStatus

logger = Logger()
tracer = Tracer()


class ExpenseNotPending(Exception):
    """The expense is no longer PENDING_APPROVAL (withdrawn, already decided, or deleted).

    Keep the name: it is the Lambda error type the workflow's ``Catch`` matches (no ``Error`` suffix, SPEC 8.2).
    """


def store_task_token(expense_id: str, task_token: str, *, escalated: bool) -> None:
    """Save the task token on the expense, only while it waits for approval."""
    try:
        aws.table().update_item(
            Key=keys.expense_key(expense_id),
            UpdateExpression="SET approvalTaskToken = :token, approvalRequestedAt = :now, escalated = :escalated",
            ConditionExpression="#status = :pending",  # `status` is a DynamoDB reserved word
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":token": task_token,
                ":now": ids.now_iso(),
                ":escalated": escalated,
                ":pending": ExpenseStatus.PENDING_APPROVAL.value,
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ExpenseNotPending(f"expense {expense_id} is not pending approval") from None
        raise


@logger.inject_lambda_context(log_event=False, clear_state=True)  # never log the event: it contains the token
@tracer.capture_lambda_handler(capture_response=False)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, bool]:
    expense_id = event["expenseId"]
    escalated = bool(event.get("escalated", False))
    logger.append_keys(expense_id=expense_id, escalated=escalated)
    # The task token is a bearer credential for completing this workflow step: store it, never log it.
    store_task_token(expense_id, event["taskToken"], escalated=escalated)
    logger.info("approval requested; waiting for a decision")
    return {"stored": True}
