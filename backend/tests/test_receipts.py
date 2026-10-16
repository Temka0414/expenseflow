"""Tests for ``expenseflow.receipts``: Claude on Amazon Bedrock, fully offline.

Claude is never called. Every client gets an ``httpx2.MockTransport`` that records the signed HTTP requests and
replays canned responses, and an autouse guard fails any request that escapes the mock (moto only intercepts
botocore, not the Anthropic SDK's own HTTP stack).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, cast, get_args

import httpx2
import pytest
from anthropic import AnthropicBedrock, BetaRefusalFallbackMiddleware

from expenseflow import receipts
from expenseflow.models import CATEGORIES
from expenseflow.receipts import ExtractionConfig

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64

TOKYO = ExtractionConfig(
    region="ap-northeast-1",
    model_id="jp.anthropic.claude-opus-5-5",
    fallback_model_id="jp.anthropic.claude-opus-4-8",
)

RECEIPT_INPUT: dict[str, Any] = {
    "is_receipt": True,
    "merchant_name": "ローソン 渋谷店",
    "transaction_date": "2026-10-12",
    "currency": "JPY",
    "subtotal": 1000,
    "tax": 80,
    "total": 1080,
    "payment_method": "IC card",
    "category": "MEALS",
    "line_items": [{"description": "おにぎり", "quantity": 2, "unit_price": 150, "amount": 300}],
    "confidence": "high",
}

THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}
REFUSAL = {"type": "refusal", "category": "general_harms", "explanation": "declined", "fallback_credit_token": "fct_1"}


# --- Fakes (also used by test_extract_receipt.py) -----------------------------------------------------------


def message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "tool_use",
    model: str = "claude-opus-5-5",
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Messages API response body (bedrock-runtime reports Anthropic's model name, not the profile id)."""
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {
            "input_tokens": 2900,
            "output_tokens": 640,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens_details": {"thinking_tokens": 300},
        },
    }


def tool_use(tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": "toolu_01", "name": receipts.TOOL_NAME, "input": tool_input}


def ok(body: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(200, json=body, headers={"request-id": "req_ok"})


def err(status: int, **headers: str) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={"type": "error", "error": {"type": "x", "message": f"status {status}"}},
        headers={"x-amzn-requestid": "aws-req-1", "retry-after-ms": "1", **headers},
    )


class Recorder:
    """MockTransport handler: records each request and answers with the next canned response (or raises it)."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int) -> dict[str, Any]:
        return dict(json.loads(self.requests[index].content))


def make_client(
    recorder: Recorder,
    config: ExtractionConfig = TOKYO,
    *,
    fallback: bool = True,
    max_retries: int = 0,
) -> AnthropicBedrock:
    """A real SDK client for ``config`` whose HTTP transport is the recorder."""
    middleware = (
        [BetaRefusalFallbackMiddleware([{"model": config.fallback_model_id}])]
        if fallback and config.fallback_model_id
        else None
    )
    http_client = httpx2.Client(transport=httpx2.MockTransport(recorder))
    return AnthropicBedrock(
        aws_region=config.region, max_retries=max_retries, middleware=middleware, http_client=http_client
    )


def install_network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guard: any request that escapes a MockTransport fails the test instead of reaching AWS."""

    def blocked(self: httpx2.HTTPTransport, request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"real network call attempted: {request.url}")

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", blocked)


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    install_network_guard(monkeypatch)


@pytest.fixture
def fresh_client_cache() -> Iterator[None]:
    receipts.get_client.cache_clear()
    yield
    receipts.get_client.cache_clear()


def extract(data: bytes, recorder: Recorder, config: ExtractionConfig = TOKYO, **kwargs: Any) -> Any:
    client_options = {name: kwargs.pop(name) for name in ("fallback", "max_retries") if name in kwargs}
    client = make_client(recorder, config, **client_options)
    return receipts.extract_receipt(data, "image/jpeg", config, client=client, **kwargs)


# --- Request shape -----------------------------------------------------------------------------------------


def test_request_targets_the_inference_profile_with_sigv4_service_bedrock() -> None:
    recorder = Recorder(ok(message([THINKING, tool_use(RECEIPT_INPUT)])))
    result = extract(JPEG, recorder)

    request = recorder.requests[0]
    assert (
        str(request.url)
        == "https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/jp.anthropic.claude-opus-5-5/invoke"
    )
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
    assert "/ap-northeast-1/bedrock/aws4_request" in request.headers["authorization"]
    assert request.headers["x-amz-security-token"]

    body = recorder.body(0)
    assert "model" not in body  # InvokeModel carries the model id in the URL
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "fallback-credit-2026-07-01" in body["anthropic_beta"]  # added by the refusal middleware, sent in the body
    assert body["max_tokens"] == receipts.DEFAULT_MAX_TOKENS
    assert body["tool_choice"] == {"type": "auto"}  # forced tool choice is a 400 on Opus 5.5
    assert body["output_config"] == {"effort": "low"}  # no "format": structured outputs are unsupported
    assert "thinking" not in body
    assert body["tools"][0]["name"] == receipts.TOOL_NAME
    assert "strict" not in body["tools"][0]
    blocks = body["messages"][0]["content"]
    assert blocks[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(JPEG).decode()},
    }
    assert blocks[1]["type"] == "text"

    assert result.receipt.total == Decimal("1080")
    assert result.receipt.line_items[0].unit_price == Decimal("150")
    assert result.receipt.merchant_name == "ローソン 渋谷店"
    assert result.receipt.category == "MEALS"
    # The response names Anthropic's model ("claude-opus-5-5"); the result reports the profile id that was invoked.
    assert result.model == result.requested_model == "jp.anthropic.claude-opus-5-5"
    assert result.fallback_used is False
    assert result.usage.input_tokens == 2900
    assert result.usage.thinking_tokens == 300
    assert result.request_id == "req_ok"


def test_to_output_matches_the_step_functions_contract() -> None:
    receipt = {
        **RECEIPT_INPUT,
        "total": 12.5,
        "subtotal": None,
        "line_items": [{"description": "Latte", "quantity": 1, "unit_price": 12.5, "amount": 12.5}],
    }
    result = extract(JPEG, Recorder(ok(message([tool_use(receipt)]))))

    output = result.to_output()
    assert output == {
        "isReceipt": True,
        "merchant": "ローソン 渋谷店",
        "date": "2026-10-12",
        "currency": "JPY",
        "total": 12.5,
        "subtotal": None,
        "tax": 80,
        "paymentMethod": "IC card",
        "category": "MEALS",
        "confidence": "high",
        "lineItems": [{"description": "Latte", "quantity": 1, "unitPrice": 12.5, "amount": 12.5}],
        "model": "jp.anthropic.claude-opus-5-5",
        "requestedModel": "jp.anthropic.claude-opus-5-5",
        "fallbackUsed": False,
        "inputTokens": 2900,
        "outputTokens": 640,
    }
    assert isinstance(output["tax"], int)
    assert json.loads(json.dumps(output)) == output  # plain JSON: what Lambda returns to Step Functions


# --- Input handling -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (b"GIF89a" + b"\x00" * 8, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (PDF, "application/pdf"),
        (b"hello world", None),
        (b"", None),
    ],
)
def test_sniff_media_type(data: bytes, expected: str | None) -> None:
    assert receipts.sniff_media_type(data) == expected


def test_pdf_becomes_a_document_block_and_bytes_win_over_the_declared_type() -> None:
    recorder = Recorder(ok(message([tool_use(RECEIPT_INPUT)])))
    receipts.extract_receipt(PDF, "application/octet-stream", TOKYO, client=make_client(recorder))
    assert recorder.body(0)["messages"][0]["content"][0]["type"] == "document"
    assert recorder.body(0)["messages"][0]["content"][0]["source"]["media_type"] == "application/pdf"

    recorder = Recorder(ok(message([tool_use(RECEIPT_INPUT)])))
    receipts.extract_receipt(PNG, "image/jpeg", TOKYO, client=make_client(recorder))
    assert recorder.body(0)["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_unsupported_inputs_fail_before_any_call() -> None:
    recorder = Recorder()
    client = make_client(recorder)
    with pytest.raises(receipts.UnsupportedInputError):
        receipts.extract_receipt(b"hello world", "text/plain", TOKYO, client=client)
    with pytest.raises(receipts.UnsupportedInputError):  # 3.8 MB raw -> more than 5 MB of base64
        receipts.extract_receipt(b"\xff\xd8\xff" + b"0" * 3_800_000, "image/jpeg", TOKYO, client=client)
    assert recorder.requests == []


# --- Refusals and fallback ----------------------------------------------------------------------------------


def test_refusal_falls_back_to_the_fallback_inference_profile() -> None:
    recorder = Recorder(
        ok(message([], stop_reason="refusal", stop_details=REFUSAL)),
        ok(message([tool_use(RECEIPT_INPUT)], model="claude-opus-4-8")),
    )
    result = extract(JPEG, recorder)

    assert [request.url.path for request in recorder.requests] == [
        "/model/jp.anthropic.claude-opus-5-5/invoke",
        "/model/jp.anthropic.claude-opus-4-8/invoke",
    ]
    assert recorder.body(1)["fallback_credit_token"] == {"token": "fct_1", "mode": "best_effort"}
    assert recorder.body(1)["messages"] == recorder.body(0)["messages"]
    assert recorder.requests[1].headers["authorization"] != recorder.requests[0].headers["authorization"]  # re-signed
    assert result.fallback_used is True
    assert result.model == "jp.anthropic.claude-opus-4-8"
    assert result.requested_model == "jp.anthropic.claude-opus-5-5"


def test_refusal_everywhere_is_not_retryable() -> None:
    refusal = ok(message([], stop_reason="refusal", stop_details=REFUSAL))
    recorder = Recorder(refusal, ok(message([], stop_reason="refusal", stop_details=REFUSAL)))
    with pytest.raises(receipts.ExtractionRefusedError) as raised:
        extract(JPEG, recorder)
    assert raised.value.retryable is False
    assert "general_harms" in str(raised.value)
    assert len(recorder.requests) == 2


def test_refusal_without_fallback_middleware() -> None:
    recorder = Recorder(ok(message([], stop_reason="refusal", stop_details=REFUSAL)))
    with pytest.raises(receipts.ExtractionRefusedError):
        extract(JPEG, recorder, fallback=False)
    assert len(recorder.requests) == 1
    assert "anthropic_beta" not in recorder.body(0)


# --- Errors -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, receipts.ModelThrottledError),
        (529, receipts.ModelThrottledError),
        (500, receipts.ModelUnavailableError),
        (503, receipts.ModelUnavailableError),
        (408, receipts.ModelUnavailableError),
        (424, receipts.ModelUnavailableError),
        (400, receipts.ModelRequestError),
        (403, receipts.ModelRequestError),
        (404, receipts.ModelRequestError),
        (413, receipts.ModelRequestError),
    ],
)
def test_http_errors_map_to_step_functions_error_names(status: int, expected: type[receipts.ExtractionError]) -> None:
    with pytest.raises(expected) as raised:
        extract(JPEG, Recorder(err(status)))
    error = raised.value
    assert type(error) is expected  # Lambda reports the exact class name as the error type
    assert error.retryable is (expected in (receipts.ModelThrottledError, receipts.ModelUnavailableError))
    assert error.status_code == status
    assert error.request_id == "aws-req-1"


def test_aws_error_type_is_part_of_the_message() -> None:
    response = err(403, **{"x-amzn-ErrorType": "AccessDeniedException:http://internal.amazon.com/coral/"})
    with pytest.raises(receipts.ModelRequestError, match=r"HTTP 403 AccessDeniedException: "):
        extract(JPEG, Recorder(response))


@pytest.mark.parametrize("failure", [httpx2.ReadTimeout("slow"), httpx2.ConnectError("refused")])
def test_transport_failures_are_retryable(failure: Exception) -> None:
    with pytest.raises(receipts.ModelUnavailableError):
        extract(JPEG, Recorder(failure))


def test_sdk_retries_a_429_once_then_succeeds() -> None:
    recorder = Recorder(err(429), ok(message([tool_use(RECEIPT_INPUT)])))
    result = extract(JPEG, recorder, max_retries=receipts.DEFAULT_MAX_RETRIES)
    assert len(recorder.requests) == 2
    assert result.receipt.total == Decimal("1080")


@pytest.mark.parametrize(
    "response",
    [
        message([{"type": "text", "text": "Here is the receipt..."}], stop_reason="end_turn"),
        message([THINKING], stop_reason="max_tokens"),
        message([tool_use({**RECEIPT_INPUT, "total": "not a number"})]),
        message([tool_use({**RECEIPT_INPUT, "transaction_date": "2026/10/12"})]),
        message([tool_use({**RECEIPT_INPUT, "category": "FOOD"})]),
        message([tool_use({"merchant_name": "x"})]),
    ],
    ids=["text-answer", "max-tokens", "bad-number", "bad-date", "unknown-category", "missing-fields"],
)
def test_invalid_model_output_is_not_retryable(response: dict[str, Any]) -> None:
    with pytest.raises(receipts.ExtractionInvalidError) as raised:
        extract(JPEG, Recorder(ok(response)))
    assert raised.value.retryable is False


# --- Client, timeouts, model ids ----------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_client_cache")
def test_get_client_builds_one_cached_client_per_configuration() -> None:
    client = receipts.get_client("ap-northeast-1", "jp.anthropic.claude-opus-4-8")
    assert isinstance(client, AnthropicBedrock)
    assert str(client.base_url) == "https://bedrock-runtime.ap-northeast-1.amazonaws.com"
    assert client.max_retries == receipts.DEFAULT_MAX_RETRIES
    assert isinstance(client.timeout, httpx2.Timeout)
    assert client.timeout.read == receipts.DEFAULT_TIMEOUT_S
    assert client.timeout.connect == receipts.CONNECT_TIMEOUT_S
    assert [type(m) for m in client.middleware] == [BetaRefusalFallbackMiddleware]
    assert receipts.get_client("ap-northeast-1", "jp.anthropic.claude-opus-4-8") is client

    assert receipts.get_client("us-east-1", None).middleware == ()  # no fallback model: no middleware


def test_per_request_timeout_keeps_the_injected_transport() -> None:
    recorder = Recorder(ok(message([tool_use(RECEIPT_INPUT)])))
    result = extract(JPEG, recorder, timeout_s=42.0)
    assert result.receipt.total == Decimal("1080")
    assert recorder.requests[0].extensions["timeout"]["read"] == 42.0
    assert recorder.requests[0].extensions["timeout"]["connect"] == receipts.CONNECT_TIMEOUT_S


@pytest.mark.parametrize(
    ("remaining_ms", "fallback_enabled", "expected"),
    [
        (180_000, True, 41.25),  # (180 s - 15 s margin) / (2 attempts x 2 models)
        (180_000, False, 82.5),
        (900_000, False, receipts.DEFAULT_TIMEOUT_S),
        (20_000, True, receipts.MIN_TIMEOUT_S),
    ],
)
def test_request_timeout_fits_the_remaining_lambda_time(
    remaining_ms: int, fallback_enabled: bool, expected: float
) -> None:
    assert receipts.request_timeout_s(remaining_ms, fallback_enabled=fallback_enabled) == expected


@pytest.mark.parametrize(
    ("region", "geo"),
    [
        ("ap-northeast-1", "jp"),
        ("ap-northeast-3", "jp"),
        ("ap-southeast-2", "au"),
        ("ap-southeast-4", "au"),
        ("us-east-1", "us"),
        ("us-west-2", "us"),
        ("eu-central-1", "eu"),
        ("ap-south-1", "global"),
        ("ca-central-1", "global"),
    ],
)
def test_model_ids_use_the_geo_inference_profile(region: str, geo: str) -> None:
    assert receipts.inference_profile_geo(region) == geo
    assert receipts.model_id_for(region) == f"{geo}.anthropic.claude-opus-5-5"
    assert receipts.model_id_for(region, receipts.FALLBACK_BASE_MODEL_ID) == f"{geo}.anthropic.claude-opus-4-8"


def test_tool_schema_offers_the_expense_categories() -> None:
    assert get_args(receipts.ReceiptCategory) == CATEGORIES
    schema = cast("dict[str, Any]", receipts.TOOL["input_schema"])
    category_types = schema["properties"]["category"]["anyOf"]
    assert {"enum": list(CATEGORIES), "type": "string"} in category_types
    assert schema["properties"]["transaction_date"]["anyOf"][0]["pattern"] == r"^\d{4}-\d{2}-\d{2}$"
    assert {"type": "number"} in schema["properties"]["total"]["anyOf"]
