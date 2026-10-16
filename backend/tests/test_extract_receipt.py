"""Tests for the ``extract_receipt`` Lambda: moto S3 for the receipt, a mocked Bedrock transport for Claude."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from anthropic import AnthropicBedrock

from expenseflow import aws, receipts
from tests.conftest import FakeLambdaContext
from tests.test_receipts import (
    PDF,
    RECEIPT_INPUT,
    Recorder,
    err,
    install_network_guard,
    make_client,
    message,
    ok,
    tool_use,
)

KEY = "receipts/user-1/01JA0000000000000000000000/01JA0000000000000000000001.pdf"
EXPENSE_ID = "01JA0000000000000000000000"


@dataclass
class FakeBedrock:
    """Stands in for ``receipts.get_client``: records the configuration it is asked for, serves a mocked client."""

    recorder: Recorder = field(default_factory=Recorder)
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def get_client(self, region: str, fallback_model_id: str | None) -> AnthropicBedrock:
        self.calls.append((region, fallback_model_id))
        config = receipts.ExtractionConfig(region=region, model_id="unused", fallback_model_id=fallback_model_id)
        return make_client(self.recorder, config)


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    install_network_guard(monkeypatch)


@pytest.fixture
def bedrock(monkeypatch: pytest.MonkeyPatch) -> FakeBedrock:
    fake = FakeBedrock()
    monkeypatch.setattr(receipts, "get_client", fake.get_client)
    return fake


def upload(bucket: str, body: bytes = PDF, content_type: str = "application/pdf") -> None:
    boto3.client("s3").put_object(Bucket=bucket, Key=KEY, Body=body, ContentType=content_type)


def invoke(bucket: str, context: FakeLambdaContext) -> dict[str, Any]:
    from handlers.extract_receipt import handler

    event = {"bucket": bucket, "key": KEY, "contentType": "application/pdf", "expenseId": EXPENSE_ID}
    return handler(event, context)


def test_extracts_the_receipt_from_s3(
    receipts_bucket: str, bedrock: FakeBedrock, lambda_context: FakeLambdaContext
) -> None:
    upload(receipts_bucket)
    bedrock.recorder.responses.append(ok(message([tool_use(RECEIPT_INPUT)])))
    lambda_context.remaining_time_in_millis = 180_000

    output = invoke(receipts_bucket, lambda_context)

    # conftest configures bedrock-runtime in us-east-1 with the "us" geo inference profiles.
    assert bedrock.calls == [("us-east-1", "us.anthropic.claude-opus-4-8")]
    request = bedrock.recorder.requests[0]
    assert request.url.path == "/model/us.anthropic.claude-opus-5-5/invoke"
    assert request.extensions["timeout"]["read"] == 41.25  # (180 s - 15 s) / (2 attempts x 2 models)
    assert bedrock.recorder.body(0)["messages"][0]["content"][0]["type"] == "document"
    assert output["isReceipt"] is True
    assert output["total"] == 1080
    assert output["category"] == "MEALS"
    assert output["lineItems"] == [{"description": "おにぎり", "quantity": 2, "unitPrice": 150, "amount": 300}]
    assert output["model"] == output["requestedModel"] == "us.anthropic.claude-opus-5-5"
    assert output["fallbackUsed"] is False
    assert (output["inputTokens"], output["outputTokens"]) == (2900, 640)
    assert json.loads(json.dumps(output)) == output


@pytest.mark.parametrize("disabled_fallback", ["", "none"])
def test_configuration_from_the_environment(
    receipts_bucket: str,
    bedrock: FakeBedrock,
    lambda_context: FakeLambdaContext,
    monkeypatch: pytest.MonkeyPatch,
    disabled_fallback: str,
) -> None:
    monkeypatch.delenv("BEDROCK_REGION")  # defaults to the function's own region
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("EXTRACTION_MODEL_ID", "jp.anthropic.claude-opus-5-5")
    monkeypatch.setenv("EXTRACTION_FALLBACK_MODEL_ID", disabled_fallback)
    upload(receipts_bucket)
    bedrock.recorder.responses.append(ok(message([tool_use(RECEIPT_INPUT)])))

    output = invoke(receipts_bucket, lambda_context)

    assert bedrock.calls == [("ap-northeast-1", None)]
    assert str(bedrock.recorder.requests[0].url).startswith("https://bedrock-runtime.ap-northeast-1.amazonaws.com/")
    # 30 s left without a fallback: (30 - 15) / 2 attempts = 7.5 s, raised to the 10 s floor.
    assert bedrock.recorder.requests[0].extensions["timeout"]["read"] == receipts.MIN_TIMEOUT_S
    assert output["model"] == "jp.anthropic.claude-opus-5-5"


def test_oversized_receipt_is_rejected_before_download(
    receipts_bucket: str,
    bedrock: FakeBedrock,
    lambda_context: FakeLambdaContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_RECEIPT_BYTES", "64")
    upload(receipts_bucket, PDF + b"0" * 64)

    def no_download(**kwargs: Any) -> None:
        raise AssertionError("GetObject must not be called for an oversized receipt")

    monkeypatch.setattr(aws.client("s3"), "get_object", no_download)  # the same cached client the handler uses

    with pytest.raises(receipts.UnsupportedInputError, match="limit is 64 bytes"):
        invoke(receipts_bucket, lambda_context)
    assert bedrock.recorder.requests == []


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (err(429), receipts.ModelThrottledError),
        (err(503), receipts.ModelUnavailableError),
        (err(403), receipts.ModelRequestError),
        (ok(message([{"type": "text", "text": "Sorry"}], stop_reason="end_turn")), receipts.ExtractionInvalidError),
    ],
)
def test_named_errors_propagate_to_step_functions(
    receipts_bucket: str,
    bedrock: FakeBedrock,
    lambda_context: FakeLambdaContext,
    response: Any,
    expected: type[receipts.ExtractionError],
) -> None:
    upload(receipts_bucket)
    # The SDK retries 429/5xx once (DEFAULT_MAX_RETRIES), so serve the same answer twice.
    bedrock.recorder.responses.extend([response, response])

    with pytest.raises(expected):
        invoke(receipts_bucket, lambda_context)


def test_unrecognised_file_is_unsupported(
    receipts_bucket: str, bedrock: FakeBedrock, lambda_context: FakeLambdaContext
) -> None:
    upload(receipts_bucket, b"not a receipt", "application/pdf")
    with pytest.raises(receipts.UnsupportedInputError):
        invoke(receipts_bucket, lambda_context)
    assert bedrock.recorder.requests == []


def test_emits_token_and_latency_metrics_per_model(
    receipts_bucket: str,
    bedrock: FakeBedrock,
    lambda_context: FakeLambdaContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("POWERTOOLS_METRICS_DISABLED", "false")
    upload(receipts_bucket)
    bedrock.recorder.responses.append(ok(message([tool_use(RECEIPT_INPUT)])))

    invoke(receipts_bucket, lambda_context)

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    (emf,) = [line for line in lines if "_aws" in line]  # Embedded Metric Format: one log line, many metrics
    definition = emf["_aws"]["CloudWatchMetrics"][0]
    assert definition["Namespace"] == "ExpenseFlow"
    assert ["Model", "service"] in definition["Dimensions"] or ["service", "Model"] in definition["Dimensions"]
    assert {metric["Name"] for metric in definition["Metrics"]} == {
        "ExtractionLatencyMs",
        "ExtractionInputTokens",
        "ExtractionOutputTokens",
        "ExtractionFallbackUsed",
    }
    assert emf["Model"] == "us.anthropic.claude-opus-5-5"
    assert emf["ExtractionInputTokens"] == [2900.0]
    assert emf["ExtractionFallbackUsed"] == [0.0]
