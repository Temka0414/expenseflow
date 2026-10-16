"""ReceiptPipeline task ``ExtractReceipt``: read the uploaded receipt from S3 and let Claude extract its fields.

Input (from the state machine): ``{"bucket", "key", "contentType", "expenseId"}``. Step Functions passes the S3
location, never the file itself: a state's payload is limited to 256 KB, a receipt can be several MB.

Output: the SPEC 7.4 extraction JSON, which the next state (``SaveExtraction``) stores on the expense.

Failures are NOT caught here. An unhandled exception makes the Lambda invocation fail with the exception's class
name as the error type (``ModelThrottledError``, ``UnsupportedInputError``, ...), and the state machine decides:
it retries the two retryable errors with back-off and routes everything else to ``MarkFailed``.
"""

from __future__ import annotations

import time
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from expenseflow import aws, receipts, settings

logger = Logger()
tracer = Tracer()
metrics = Metrics()


def _config() -> receipts.ExtractionConfig:
    return receipts.ExtractionConfig(
        region=settings.bedrock_region(),
        model_id=settings.extraction_model_id(),
        fallback_model_id=settings.extraction_fallback_model_id(),
    )


# capture_response=False: by default the tracer attaches a method's return value to the X-Ray segment as metadata.
# That must never be the receipt's bytes (segments are limited to 64 KB) nor the extracted data (personal data).
@tracer.capture_method(capture_response=False)
def _read_receipt(bucket: str, key: str) -> tuple[bytes, str | None]:
    """Download the receipt, refusing oversized objects before any byte enters the function's memory.

    The presigned POST policy already caps uploads at MAX_RECEIPT_BYTES; HeadObject (metadata only, cheap) re-checks
    it so a wrongly uploaded object can never make the function load an arbitrarily large file.
    """
    s3 = aws.client("s3")
    size = int(s3.head_object(Bucket=bucket, Key=key)["ContentLength"])
    limit = settings.max_receipt_bytes()
    if size > limit:
        raise receipts.UnsupportedInputError(f"receipt is {size} bytes; the limit is {limit} bytes")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read(), obj.get("ContentType")


@tracer.capture_method(capture_response=False)
def _extract(data: bytes, media_type: str, context: LambdaContext) -> receipts.ExtractionResult:
    config = _config()
    # Bound every HTTP attempt by the time this invocation has left, so a slow model call fails with a retryable
    # ModelUnavailableError instead of Lambda killing the function mid-request.
    timeout_s = receipts.request_timeout_s(
        context.get_remaining_time_in_millis(), fallback_enabled=config.fallback_model_id is not None
    )
    started = time.perf_counter()
    try:
        result = receipts.extract_receipt(data, media_type, config, timeout_s=timeout_s)
    except receipts.ExtractionError as error:
        # The Bedrock request id is what AWS Support asks for; the failure itself propagates to Step Functions.
        logger.warning(
            "receipt extraction failed",
            extra={
                "error_type": type(error).__name__,
                "retryable": error.retryable,
                "status_code": error.status_code,
                "bedrock_request_id": error.request_id,
                "model": config.model_id,
            },
        )
        raise
    _record_metrics(result, latency_ms=(time.perf_counter() - started) * 1000)
    return result


def _record_metrics(result: receipts.ExtractionResult, *, latency_ms: float) -> None:
    """CloudWatch metrics via EMF: a structured log line that CloudWatch turns into metrics (no PutMetricData call).

    Token counts are what Bedrock bills, so the dashboard can chart cost per model.
    """
    metrics.add_dimension(name="Model", value=result.model)
    metrics.add_metric(name="ExtractionLatencyMs", unit=MetricUnit.Milliseconds, value=latency_ms)
    metrics.add_metric(name="ExtractionInputTokens", unit=MetricUnit.Count, value=result.usage.input_tokens)
    metrics.add_metric(name="ExtractionOutputTokens", unit=MetricUnit.Count, value=result.usage.output_tokens)
    metrics.add_metric(name="ExtractionFallbackUsed", unit=MetricUnit.Count, value=int(result.fallback_used))


@logger.inject_lambda_context(log_event=False, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    bucket, key = event["bucket"], event["key"]
    expense_id = event.get("expenseId")
    logger.append_keys(expense_id=expense_id, receipt_key=key)
    if expense_id:
        tracer.put_annotation(key="expense_id", value=expense_id)

    data, stored_type = _read_receipt(bucket, key)
    # The declared type is only a hint: receipts.build_source_block trusts the file's magic bytes.
    media_type = event.get("contentType") or stored_type or "application/octet-stream"
    result = _extract(data, media_type, context)

    logger.info(
        "receipt extracted",
        extra={
            "model": result.model,
            "fallback_used": result.fallback_used,
            "bedrock_request_id": result.request_id,
            "usage": result.usage.model_dump(),
            "confidence": result.receipt.confidence,
        },
    )
    return result.to_output()
