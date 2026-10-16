"""Monthly CSV report routes (admins only): list, generate on demand, download.

Reports are written by the ``report_generator`` Lambda to ``reports/{yyyy-mm}.csv`` in the private reports bucket,
on a schedule (EventBridge Scheduler, 1st of the month 09:00 Tokyo) or on demand through ``POST /reports``.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any, Final

from aws_lambda_powertools.event_handler.openapi.params import Path
from aws_lambda_powertools.event_handler.router import APIGatewayHttpRouter
from botocore.exceptions import ClientError

from expenseflow import aws, ids, keys, settings
from expenseflow.errors import NotFoundError
from expenseflow.models import CreateReportRequest
from expenseflow.serialization import dumps
from handlers.api.deps import PRESIGNED_URL_TTL_SECONDS, Admin

router = APIGatewayHttpRouter()

# HeadObject on a missing key answers a bare HTTP 404 (no error body, so no "NoSuchKey" code). Without
# s3:ListBucket permission S3 would answer 403 instead, to avoid revealing which keys exist - the API role has it.
_MISSING_OBJECT_ERRORS: Final = frozenset({"404", "NoSuchKey", "NotFound"})


@router.get("/reports")
def list_reports(caller: Admin) -> list[dict[str, Any]]:
    """Every monthly report, newest month first.

    ListObjectsV2 returns at most 1,000 keys per call; the paginator follows ``ContinuationToken`` for us.
    """
    paginator = aws.client("s3").get_paginator("list_objects_v2")
    reports = []
    for page in paginator.paginate(Bucket=settings.reports_bucket(), Prefix=keys.REPORTS_PREFIX):
        for summary in page.get("Contents", []):  # an empty listing has no "Contents" key at all
            month = keys.month_from_report_key(summary["Key"])
            if month is not None:
                reports.append(
                    {
                        "month": month,
                        "key": summary["Key"],
                        "size": summary["Size"],
                        "lastModified": ids.to_iso(summary["LastModified"]),
                    }
                )
    return sorted(reports, key=lambda report: report["month"], reverse=True)


@router.post("/reports")
def create_report(caller: Admin, body: CreateReportRequest) -> tuple[dict[str, Any], int]:
    """Start generating one month's report and return immediately.

    ``InvocationType="Event"`` is an asynchronous invoke: Lambda queues the event, answers 202 at once and runs the
    function in the background (retrying twice on failure). Generating a report can take longer than an HTTP
    request may wait, and the admin learns about the result from the ``Report Generated`` event (WebSocket).
    """
    payload = {"month": body.month, "requestedBy": caller.email}
    aws.client("lambda").invoke(
        FunctionName=settings.report_function_name(),
        InvocationType="Event",
        Payload=dumps(payload).encode(),
    )
    return {"accepted": True, "month": body.month}, HTTPStatus.ACCEPTED


@router.get("/reports/<month>")
def get_report(month: Annotated[str, Path(pattern=ids.MONTH_PATTERN)], caller: Admin) -> dict[str, Any]:
    """A presigned GET URL that downloads the CSV (``attachment``), or 404 when the month has no report yet.

    A presigned URL is signed locally and would be issued for any key, so HeadObject checks existence first.
    """
    bucket = settings.reports_bucket()
    key = keys.report_object_key(month)
    try:
        aws.client("s3").head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] not in _MISSING_OBJECT_ERRORS:
            raise
        raise NotFoundError(f"there is no report for {month} yet") from None
    url = aws.s3_presigner().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="expenseflow-{month}.csv"',
        },
        ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
    )
    return {"url": url, "expiresIn": PRESIGNED_URL_TTL_SECONDS}
