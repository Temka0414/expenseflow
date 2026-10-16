"""Expense routes: the owner's CRUD, receipt upload/download, and the submit/withdraw status transitions.

Business rules live in the repository's conditional writes (owner only, allowed statuses, optimistic locking);
a failed rule surfaces as an :mod:`expenseflow.errors` exception that ``app.py`` turns into 400/403/404/409.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.event_handler.openapi.params import Query
from aws_lambda_powertools.event_handler.router import APIGatewayHttpRouter
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from expenseflow import aws, ids, keys, settings
from expenseflow.auth import ensure_can_view
from expenseflow.errors import NotFoundError
from expenseflow.models import (
    CreateExpenseRequest,
    ExpenseStatus,
    ReceiptUploadRequest,
    UpdateExpenseRequest,
    history_to_api,
    to_api,
)
from expenseflow.repository import DEFAULT_PAGE_SIZE, ExpenseRepository
from handlers.api.deps import PRESIGNED_URL_TTL_SECONDS, CurrentCaller, ExpenseIdPath, PageCursor, PageLimit

router = APIGatewayHttpRouter()
repository = ExpenseRepository()
logger = Logger(child=True)
# Every Metrics() instance shares one metric set; the handler's `log_metrics` decorator (app.py) flushes it.
metrics = Metrics()


@router.get("/expenses")
def list_expenses(
    caller: CurrentCaller,
    status: Annotated[ExpenseStatus | None, Query()] = None,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
    cursor: PageCursor = None,
) -> dict[str, Any]:
    """The caller's expenses, newest first (GSI1), one page at a time."""
    page = repository.list_by_owner(caller.user_id, status=status, limit=limit, cursor=cursor)
    return {"items": [to_api(item) for item in page.items], "nextCursor": page.next_cursor}


@router.post("/expenses")
def create_expense(caller: CurrentCaller, body: CreateExpenseRequest) -> tuple[dict[str, Any], int]:
    """Create a DRAFT. Only the title is required; the receipt's AI extraction can suggest the rest."""
    item = repository.create(caller, body.attributes())
    metrics.add_metric(name="ExpenseCreated", unit=MetricUnit.Count, value=1)
    return to_api(item), HTTPStatus.CREATED


@router.get("/expenses/<expense_id>")
def get_expense(expense_id: ExpenseIdPath, caller: CurrentCaller) -> dict[str, Any]:
    """The expense and its timeline (HISTORY items), read together with one Query on the item collection."""
    expense, history = repository.get_with_history(expense_id)
    ensure_can_view(caller, expense["ownerId"])
    return {"expense": to_api(expense), "history": [history_to_api(entry) for entry in history]}


@router.patch("/expenses/<expense_id>")
def update_expense(expense_id: ExpenseIdPath, caller: CurrentCaller, body: UpdateExpenseRequest) -> dict[str, Any]:
    """Partial update (absent = unchanged, ``null``/``""`` = remove); a REJECTED expense re-opens as DRAFT.

    Sending ``version`` (from the last read) turns on optimistic locking: a concurrent change answers 409.
    """
    updates, removals = body.changes()
    item = repository.update_fields(
        expense_id, caller, updates=updates, removals=removals, expected_version=body.version
    )
    return to_api(item)


@router.delete("/expenses/<expense_id>")
def delete_expense(expense_id: ExpenseIdPath, caller: CurrentCaller) -> Response[None]:
    """Delete a DRAFT with its history and receipt files. 204 carries no body, so no content type either."""
    receipt_keys = repository.delete_draft(expense_id, caller)
    if receipt_keys:
        _delete_receipt_objects(receipt_keys)
    return Response(status_code=HTTPStatus.NO_CONTENT)


def _delete_receipt_objects(object_keys: list[str]) -> None:
    """One DeleteObjects call removes up to 1,000 objects.

    It answers 200 even when single objects fail; those are listed in ``Errors`` (``Quiet`` omits the successes).
    The expense itself is already deleted, so leftovers are only logged - they cannot be reached through the API.
    """
    response = aws.client("s3").delete_objects(
        Bucket=settings.receipts_bucket(),
        Delete={"Objects": [{"Key": key} for key in object_keys], "Quiet": True},
    )
    if errors := response.get("Errors"):
        logger.warning("Some receipt objects could not be deleted", extra={"errors": errors})


@router.post("/expenses/<expense_id>/receipt-upload")
def create_receipt_upload(
    expense_id: ExpenseIdPath, caller: CurrentCaller, body: ReceiptUploadRequest
) -> dict[str, Any]:
    """Authorize ONE direct browser-to-S3 upload with a presigned POST policy.

    The file never passes through API Gateway or Lambda (payload limits, duration cost). The policy is a signed,
    short-lived permission that S3 itself enforces: exactly this key, exactly this ``Content-Type`` and a size
    between 1 byte and the limit. A presigned PUT URL could not enforce the size; ``content-length-range`` can.

    The key is recorded as ``pendingReceiptKey`` first; the ReceiptPipeline ignores any upload whose key does not
    match it, so a stale or replayed policy cannot attach a file. That conditional write also proves the caller
    owns the DRAFT, which is why the owner segment of the key can come from the caller.
    """
    max_bytes = settings.max_receipt_bytes()
    key = keys.receipt_object_key(caller.user_id, expense_id, ids.new_id(), body.extension)
    repository.set_pending_receipt(expense_id, caller, key=key, content_type=body.content_type)
    post = aws.s3_presigner().generate_presigned_post(
        Bucket=settings.receipts_bucket(),
        Key=key,
        Fields={"Content-Type": body.content_type},
        # Every form field the browser sends must be covered by a condition; boto3 adds `bucket` and `key` itself.
        Conditions=[{"Content-Type": body.content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
    )
    metrics.add_metric(name="ReceiptUploadRequested", unit=MetricUnit.Count, value=1)
    return {
        "url": post["url"],
        "fields": post["fields"],
        "key": key,
        "expiresIn": PRESIGNED_URL_TTL_SECONDS,
        "maxBytes": max_bytes,
    }


@router.get("/expenses/<expense_id>/receipt")
def get_receipt(expense_id: ExpenseIdPath, caller: CurrentCaller) -> dict[str, Any]:
    """A presigned GET URL for the receipt: the bucket stays private, the browser downloads directly from S3.

    ``ResponseContentDisposition`` is signed into the URL and makes S3 send ``Content-Disposition: inline``, so
    the browser displays the image/PDF instead of downloading it.
    """
    expense = repository.get(expense_id)
    ensure_can_view(caller, expense["ownerId"])
    key = expense.get("receiptKey")
    if key is None:
        raise NotFoundError("this expense has no receipt")
    filename = key.rsplit("/", 1)[-1]
    url = aws.s3_presigner().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.receipts_bucket(),
            "Key": key,
            "ResponseContentDisposition": f'inline; filename="{filename}"',
        },
        ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
    )
    return {"url": url, "expiresIn": PRESIGNED_URL_TTL_SECONDS}


@router.post("/expenses/<expense_id>/submit")
def submit_expense(expense_id: ExpenseIdPath, caller: CurrentCaller) -> dict[str, Any]:
    """DRAFT -> PENDING_APPROVAL. Nothing else happens here.

    The DynamoDB stream delivers the status change to the stream processor, which publishes ``Expense Submitted``;
    an EventBridge rule then starts the ApprovalWorkflow. Because the event is derived from the committed write
    (a transactional outbox), there is no "saved but the event was lost" window.
    """
    item = repository.submit(expense_id, caller)
    metrics.add_metric(name="ExpenseSubmitted", unit=MetricUnit.Count, value=1)
    return to_api(item)


@router.post("/expenses/<expense_id>/withdraw")
def withdraw_expense(expense_id: ExpenseIdPath, caller: CurrentCaller) -> dict[str, Any]:
    """PENDING_APPROVAL -> DRAFT, then stop the approval workflow execution that waits for a decision.

    Status first, workflow second: once the expense is DRAFT every workflow write (conditioned on
    PENDING_APPROVAL) fails into ``AlreadyHandled``, so even a workflow that is not stopped can never approve a
    withdrawn expense. A workflow that has not claimed the expense yet has no execution ARN stored and will find
    the expense no longer pending.
    """
    withdrawal = repository.withdraw(expense_id, caller)
    if withdrawal.workflow_execution_arn:
        _stop_approval_workflow(withdrawal.workflow_execution_arn)
    return to_api(withdrawal.expense)


def _stop_approval_workflow(execution_arn: str) -> None:
    try:
        aws.client("stepfunctions").stop_execution(
            executionArn=execution_arn, error="Withdrawn", cause="The owner withdrew the expense"
        )
    except ClientError as error:
        # The execution may be gone (finished, or past the execution history retention); nothing left to stop.
        if error.response["Error"]["Code"] != "ExecutionDoesNotExist":
            raise
        logger.info("Approval workflow execution no longer exists", extra={"execution_arn": execution_arn})
