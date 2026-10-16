"""Approval routes for managers and admins: the queue and the decision that resumes the ApprovalWorkflow.

How a decision travels (Step Functions "callback" pattern): the workflow's ``RequestApproval`` state invoked the
``approval_request`` Lambda with ``.waitForTaskToken``; that Lambda stored the task token on the expense and the
execution paused. ``SendTaskSuccess`` with that token resumes the execution, and its ``RecordDecision`` state
writes the outcome. The API therefore never changes the status itself and answers 202 Accepted.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Final

from aws_lambda_powertools import Metrics
from aws_lambda_powertools.event_handler.router import APIGatewayHttpRouter
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from expenseflow import aws
from expenseflow.errors import ConflictError, ForbiddenError
from expenseflow.models import DecisionRequest, ExpenseStatus, to_api
from expenseflow.repository import DEFAULT_PAGE_SIZE, ExpenseRepository
from expenseflow.serialization import dumps
from handlers.api.deps import Approver, ExpenseIdPath, PageCursor, PageLimit

router = APIGatewayHttpRouter()
repository = ExpenseRepository()
metrics = Metrics()

# SendTaskSuccess errors meaning "this token no longer resumes anything": the wait timed out (and the workflow
# escalated or expired the request), the task already completed (another decision won the race), or the token is
# unknown. All are conflicts with the current state, not server errors.
_INACTIVE_TASK_ERRORS: Final = frozenset({"TaskTimedOut", "InvalidToken", "TaskDoesNotExist"})


@router.get("/approvals")
def list_approvals(
    caller: Approver,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
    cursor: PageCursor = None,
) -> dict[str, Any]:
    """Expenses waiting for approval, oldest first (GSI2 ``STATUS#PENDING_APPROVAL``), minus the caller's own."""
    page = repository.list_pending_approval(exclude_owner_id=caller.user_id, limit=limit, cursor=cursor)
    return {"items": [to_api(item) for item in page.items], "nextCursor": page.next_cursor}


@router.post("/approvals/<expense_id>/decision")
def decide(expense_id: ExpenseIdPath, caller: Approver, body: DecisionRequest) -> tuple[dict[str, Any], int]:
    """Approve or reject a pending expense by completing the workflow's waiting task.

    No conditional write is needed here: a task token is single-use, so Step Functions itself guarantees that
    only the first of two concurrent decisions (two managers, a double click) is accepted.
    """
    expense = repository.get(expense_id)
    if expense["ownerId"] == caller.user_id:
        raise ForbiddenError("you cannot decide on your own expense")
    if expense["status"] != ExpenseStatus.PENDING_APPROVAL:
        raise ConflictError(f"expense is {expense['status']}, not {ExpenseStatus.PENDING_APPROVAL}")
    # Between submit and the workflow storing its token (usually about a second) there is nothing to resume yet.
    token = expense.get("approvalTaskToken")
    if token is None:
        raise ConflictError("approval request not ready yet")

    output = {
        "decision": body.decision,
        "approverId": caller.user_id,
        "approverEmail": caller.email,
        "comment": body.comment,
    }
    try:
        aws.client("stepfunctions").send_task_success(taskToken=token, output=dumps(output))
    except ClientError as error:
        if error.response["Error"]["Code"] not in _INACTIVE_TASK_ERRORS:
            raise
        raise ConflictError("this approval request is no longer active; reload the expense") from None

    # Dimensions apply to every metric of this invocation's EMF document; this route emits exactly one.
    metrics.add_dimension(name="decision", value=body.decision)
    metrics.add_metric(name="ApprovalDecision", unit=MetricUnit.Count, value=1)
    return {"accepted": True}, HTTPStatus.ACCEPTED
