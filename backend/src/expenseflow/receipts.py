"""Receipt extraction with Claude on Amazon Bedrock, through the official Anthropic SDK.

How it works (SPEC section 7.4):

* **bedrock-runtime with a geo inference profile.** ``anthropic.AnthropicBedrock`` calls bedrock-runtime
  ``InvokeModel`` with a *geo inference profile* id such as ``jp.anthropic.claude-opus-5-5`` (the profile routes the
  request to a region inside that geography). It signs every request with SigV4 using boto3's default credential
  chain (in Lambda: the execution role's temporary credentials).
* **Tool use instead of structured outputs.** Bedrock rejects ``output_config.format`` (structured outputs), so the
  model is given one tool, ``record_receipt``, whose ``input_schema`` is the JSON schema of :class:`ReceiptData`; the
  tool input is then validated with pydantic. ``tool_choice`` must stay ``auto``: forcing a tool (``any``/``tool``)
  returns HTTP 400 on Claude Opus 5.5, so the system prompt asks for the call instead.
* **Refusal fallback.** A safety refusal is an HTTP 200 with ``stop_reason == "refusal"``. Bedrock has no server-side
  ``fallbacks`` parameter, so the SDK's client-side ``BetaRefusalFallbackMiddleware`` re-sends the same request to a
  fallback model. It only acts on ``client.beta.messages`` calls.
* **Errors Step Functions can act on.** Every failure becomes one of six exception classes. Lambda reports an
  unhandled exception's *class name* as the error type, so the ReceiptPipeline state machine can ``Retry`` the
  retryable ones (``ModelThrottledError``, ``ModelUnavailableError``) and ``Catch`` the rest.

Nothing here reads environment variables: the handler builds an :class:`ExtractionConfig`, and tests inject a client
whose HTTP transport is mocked.
"""

from __future__ import annotations

import base64
import functools
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Final, Literal

import anthropic
from anthropic import AnthropicBedrock, BetaFallbackState, BetaRefusalFallbackMiddleware
from anthropic.types.beta import BetaImageBlockParam, BetaRequestDocumentBlockParam, BetaToolParam
from pydantic import BaseModel, Field, ValidationError, WithJsonSchema

from expenseflow.serialization import to_json_number

log = logging.getLogger(__name__)

type Effort = Literal["low", "medium", "high", "xhigh", "max"]

PRIMARY_BASE_MODEL_ID: Final = "anthropic.claude-opus-5-5"
FALLBACK_BASE_MODEL_ID: Final = "anthropic.claude-opus-4-8"

TOOL_NAME: Final = "record_receipt"
DEFAULT_EFFORT: Final[Effort] = "low"  # Opus 5.5 defaults to "medium"; reading a receipt needs little thinking
# Thinking tokens and the tool input share this cap. Keep it well below ~21k: above that the SDK insists on
# streaming, and Bedrock's input-tokens-per-minute quota check counts max_tokens as if it were input.
DEFAULT_MAX_TOKENS: Final = 8192
DEFAULT_TIMEOUT_S: Final = 120.0  # per HTTP attempt; the handler lowers it to fit the Lambda's remaining time
MIN_TIMEOUT_S: Final = 10.0
CONNECT_TIMEOUT_S: Final = 5.0
DEFAULT_MAX_RETRIES: Final = 1  # SDK-level retry for 429/5xx/connection errors; Step Functions retries the rest
LAMBDA_SAFETY_MARGIN_S: Final = 15.0  # S3 reads, SDK retry back-off and returning the result

# Bedrock limits: 5 MB per image *base64-encoded* (about 3.75 MB of raw bytes) and 20 MB per request.
MAX_IMAGE_B64_CHARS: Final = 5_000_000
MAX_REQUEST_B64_CHARS: Final = 19_000_000  # headroom under 20 MB for the JSON envelope, prompt and tool schema

# Retryable HTTP statuses besides 5xx: 408 request timeout, 409 conflict (the SDK retries both) and 424, which
# bedrock-runtime uses for ModelErrorException ("an error occurred while processing the model").
_THROTTLED_STATUSES: Final = frozenset({429, 529})
_TRANSIENT_STATUSES: Final = frozenset({408, 409, 424})

# Geo inference profile prefixes by region (SPEC 7.4). A geo profile keeps inference inside that geography,
# e.g. Tokyo/Osaka for "jp"; "global" may route anywhere.
_GEO_PREFIXES: Final = (
    (("ap-northeast-1", "ap-northeast-3"), "jp"),
    (("ap-southeast-2", "ap-southeast-4"), "au"),
)


# --- Model ids ----------------------------------------------------------------------------------------------


def inference_profile_geo(region: str) -> str:
    """Geography prefix of the bedrock-runtime inference profile for ``region`` (``jp``, ``au``, ``us``, ...)."""
    for regions, geo in _GEO_PREFIXES:
        if region in regions:
            return geo
    for prefix in ("us", "eu"):
        if region.startswith(f"{prefix}-"):
            return prefix
    return "global"


def model_id_for(region: str, base_model_id: str = PRIMARY_BASE_MODEL_ID) -> str:
    """The geo inference profile id to invoke, e.g. ``jp.anthropic.claude-opus-5-5`` in Tokyo."""
    return f"{inference_profile_geo(region)}.{base_model_id}"


# --- Extracted data -----------------------------------------------------------------------------------------

# The JSON schema says "number"; pydantic turns the parsed JSON number into an exact Decimal (money must never be
# a binary float, and DynamoDB stores Decimal).
Number = Annotated[Decimal, WithJsonSchema({"type": "number"})]

ReceiptCategory = Literal["TRAVEL", "MEALS", "LODGING", "TRANSPORT", "SUPPLIES", "SOFTWARE", "TRAINING", "OTHER"]


class LineItem(BaseModel):
    description: str = Field(description="Item name exactly as printed (keep the original language).")
    quantity: Number | None = Field(description="Quantity, or null if not printed.")
    unit_price: Number | None = Field(description="Unit price in major currency units, or null.")
    amount: Number | None = Field(description="Line total in major currency units, or null.")


class ReceiptData(BaseModel):
    """The ``record_receipt`` tool input. Field descriptions are part of the prompt: the model reads them."""

    is_receipt: bool = Field(description="False if the document is not a receipt or invoice.")
    merchant_name: str | None = Field(description="Store or merchant name as printed, or null if unreadable.")
    transaction_date: str | None = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$", description="Purchase date as YYYY-MM-DD, or null."
    )
    currency: str | None = Field(pattern=r"^[A-Z]{3}$", description="ISO 4217 code such as JPY or USD, or null.")
    subtotal: Number | None = Field(description="Pre-tax subtotal, or null if not printed.")
    tax: Number | None = Field(description="Total tax amount (e.g. Japanese consumption tax), or null.")
    total: Number | None = Field(description="Grand total actually paid, or null if unreadable.")
    payment_method: str | None = Field(description="For example cash, credit card or IC card; or null.")
    category: ReceiptCategory | None = Field(
        description=(
            "Best-fitting expense category: MEALS (restaurants, cafes), TRANSPORT (taxi, train, bus), "
            "LODGING (hotels), TRAVEL (flights, long-distance travel), SUPPLIES (office goods), "
            "SOFTWARE (licenses, subscriptions), TRAINING (courses, books, conferences), OTHER; null if unclear."
        )
    )
    line_items: list[LineItem] = Field(description="Purchased items in printed order; [] if none are listed.")
    confidence: Literal["high", "medium", "low"] = Field(description="How legible and certain the values are.")


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int  # includes thinking tokens
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    thinking_tokens: int | None = None


class ExtractionResult(BaseModel):
    receipt: ReceiptData
    model: str  # the Bedrock model id that produced the answer (the fallback after a refusal)
    requested_model: str
    fallback_used: bool
    stop_reason: str | None
    request_id: str | None
    usage: TokenUsage

    def to_output(self) -> dict[str, Any]:
        """The Lambda result (SPEC 7.4): camelCase keys and JSON numbers, ready for the Step Functions state."""
        receipt = self.receipt
        return {
            "isReceipt": receipt.is_receipt,
            "merchant": receipt.merchant_name,
            "date": receipt.transaction_date,
            "currency": receipt.currency,
            "total": _json_number(receipt.total),
            "subtotal": _json_number(receipt.subtotal),
            "tax": _json_number(receipt.tax),
            "paymentMethod": receipt.payment_method,
            "category": receipt.category,
            "confidence": receipt.confidence,
            "lineItems": [
                {
                    "description": item.description,
                    "quantity": _json_number(item.quantity),
                    "unitPrice": _json_number(item.unit_price),
                    "amount": _json_number(item.amount),
                }
                for item in receipt.line_items
            ],
            "model": self.model,
            "requestedModel": self.requested_model,
            "fallbackUsed": self.fallback_used,
            "inputTokens": self.usage.input_tokens,
            "outputTokens": self.usage.output_tokens,
        }


def _json_number(value: Decimal | None) -> int | float | None:
    return None if value is None else to_json_number(value)


# --- Errors (the class names are Step Functions error names) ------------------------------------------------


class ExtractionError(Exception):
    """Base class. ``retryable`` says whether trying the same receipt again can help."""

    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, status_code: int | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id  # quote it to AWS Support when asking about a failed call


class ModelThrottledError(ExtractionError):
    """429/529 after the SDK's own retry: quota or capacity exhausted. Retry later with back-off."""

    retryable = True


class ModelUnavailableError(ExtractionError):
    """5xx, 408/409/424, timeout or connection failure after the SDK's own retry. Retry with back-off."""

    retryable = True


class ModelRequestError(ExtractionError):
    """400/401/403/404/413/422: invalid request, missing IAM permission or model access, wrong model id/region."""


class UnsupportedInputError(ExtractionError):
    """The file is not a supported image/PDF or exceeds Bedrock's size limits."""


class ExtractionRefusedError(ExtractionError):
    """The primary model and every fallback declined the request (``stop_reason == "refusal"``)."""


class ExtractionInvalidError(ExtractionError):
    """No schema-valid ``record_receipt`` call (plain-text answer, ``max_tokens`` reached, invalid values)."""


def classify_api_error(error: anthropic.APIError) -> ExtractionError:
    """Map an SDK error to the retryable/non-retryable exception the state machine understands.

    Mapping by HTTP status rather than SDK exception class: the bedrock-runtime client reports 529 as a plain
    ``InternalServerError`` and 413 as a generic ``APIStatusError``.
    """
    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        # The Anthropic "request-id" header, else the AWS request id (bedrock-runtime returns x-amzn-RequestId).
        request_id = error.request_id or error.response.headers.get("x-amzn-requestid")
        # bedrock-runtime names the AWS error in a header, e.g. "AccessDeniedException:http://internal.amazon.com/...".
        aws_error = error.response.headers.get("x-amzn-errortype", "").split(":")[0]
        message = f"HTTP {status}{f' {aws_error}' if aws_error else ''}: {error.message}"
        if status in _THROTTLED_STATUSES:
            return ModelThrottledError(message, status_code=status, request_id=request_id)
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return ModelUnavailableError(message, status_code=status, request_id=request_id)
        return ModelRequestError(message, status_code=status, request_id=request_id)
    if isinstance(error, anthropic.APIConnectionError):  # includes APITimeoutError
        return ModelUnavailableError(f"{type(error).__name__}: {error}")
    return ExtractionInvalidError(f"{type(error).__name__}: {error}")  # the response did not match the API schema


# --- Client -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionConfig:
    region: str  # region of the Bedrock endpoint; may differ from the Lambda's own region
    model_id: str
    fallback_model_id: str | None  # None disables the refusal fallback


@functools.cache
def get_client(region: str, fallback_model_id: str | None) -> AnthropicBedrock:
    """One client per configuration, reused across warm invocations (it owns the HTTP connection pool).

    Per-call settings go on the request (``timeout=``) rather than a per-request ``client.with_options(...)`` copy.

    Gotcha: if ``AWS_BEARER_TOKEN_BEDROCK`` is set, the SDK silently switches from SigV4 to bearer-token auth.
    Never set it in Lambda.
    """
    middleware = [BetaRefusalFallbackMiddleware([{"model": fallback_model_id}])] if fallback_model_id else None
    timeout = anthropic.Timeout(DEFAULT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    return AnthropicBedrock(aws_region=region, middleware=middleware, timeout=timeout, max_retries=DEFAULT_MAX_RETRIES)


def request_timeout_s(remaining_ms: int, *, fallback_enabled: bool) -> float:
    """Per-attempt HTTP timeout that keeps the worst case inside the Lambda's remaining time.

    Worst case: every SDK attempt on the primary model, then (after a refusal) every attempt on the fallback model
    times out. Failing with ``ModelUnavailableError`` before Lambda kills the function lets Step Functions retry it.
    """
    attempts = (DEFAULT_MAX_RETRIES + 1) * (2 if fallback_enabled else 1)
    budget_s = remaining_ms / 1000 - LAMBDA_SAFETY_MARGIN_S
    return max(MIN_TIMEOUT_S, min(DEFAULT_TIMEOUT_S, budget_s / attempts))


# --- Input --------------------------------------------------------------------------------------------------

type ImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]


def sniff_media_type(data: bytes) -> ImageMediaType | Literal["application/pdf"] | None:
    """Detect the file type from its magic bytes (the declared Content-Type is only the uploader's claim)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def build_source_block(data: bytes, declared_type: str) -> BetaImageBlockParam | BetaRequestDocumentBlockParam:
    """An ``image`` or ``document`` content block with a base64 source (the only source kind Bedrock accepts)."""
    media_type = sniff_media_type(data)
    if media_type is None:
        raise UnsupportedInputError(f"unrecognised file content (declared {declared_type!r})")
    if media_type != declared_type.split(";")[0].strip().lower():
        # The API answers 400 when media_type does not match the bytes, so the bytes win.
        log.warning("declared media type %r does not match the content; sending %s", declared_type, media_type)

    encoded = base64.standard_b64encode(data).decode("ascii")  # standard alphabet, no line breaks
    if media_type == "application/pdf":
        if len(encoded) > MAX_REQUEST_B64_CHARS:
            raise UnsupportedInputError(f"PDF is {len(data)} bytes; a Bedrock request is limited to 20 MB")
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": encoded}}
    if len(encoded) > MAX_IMAGE_B64_CHARS:
        raise UnsupportedInputError(
            f"image is {len(data)} bytes; Bedrock accepts 5 MB of base64 per image (about 3.75 MB raw)"
        )
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}


# --- Extraction ---------------------------------------------------------------------------------------------

# No "reasoning"/"explanation" fields or instructions: asking for the model's reasoning can trigger refusals.
SYSTEM_PROMPT: Final = f"""You extract structured data from photos and scans of purchase receipts.
Always respond by calling the {TOOL_NAME} tool exactly once; do not answer in plain text.
Rules:
- Copy text as printed (keep Japanese or other original-language text as-is).
- Amounts are plain numbers in major currency units (for example 1080 for "¥1,080", 12.5 for "$12.50").
- If a value is missing or unreadable, use null instead of guessing. Never invent line items.
- total is the amount actually paid (tax included where the receipt shows a tax-inclusive total).
- If the document is not a receipt or invoice, set is_receipt to false and leave the other fields null or empty."""

TOOL: Final[BetaToolParam] = {
    "name": TOOL_NAME,
    "description": "Record the fields extracted from one receipt.",
    "input_schema": ReceiptData.model_json_schema(),
}


def extract_receipt(
    data: bytes,
    media_type: str,
    config: ExtractionConfig,
    *,
    timeout_s: float | None = None,
    client: AnthropicBedrock | None = None,
) -> ExtractionResult:
    """Extract the fields of one receipt image/PDF. Raises an :class:`ExtractionError` subclass on failure.

    ``timeout_s`` overrides the per-attempt HTTP timeout for this call (see :func:`request_timeout_s`). ``client``
    is for tests; production code leaves it ``None`` and gets the cached client for ``config``.
    """
    source_block = build_source_block(data, media_type)
    bedrock = client or get_client(config.region, config.fallback_model_id)
    timeout = anthropic.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S) if timeout_s is not None else anthropic.not_given

    try:
        # One state per receipt: it records which fallback (if any) answered, and pins nothing across receipts.
        # `.beta`: the refusal middleware only handles beta requests. The ignore works around anthropic 1.13.0's
        # typing, which annotates AnthropicBedrock's beta `create` with the first-party `self` type.
        with BetaFallbackState() as fallback_state:
            response = bedrock.beta.messages.create(  # type: ignore[misc]
                model=config.model_id,
                max_tokens=DEFAULT_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=[TOOL],
                tool_choice={"type": "auto"},
                output_config={"effort": DEFAULT_EFFORT},  # no `thinking` parameter: Opus 5.5 always thinks
                messages=[
                    {
                        "role": "user",
                        # The image/document goes BEFORE the instruction: Claude reads it first.
                        "content": [source_block, {"type": "text", "text": "Extract this receipt."}],
                    }
                ],
                timeout=timeout,
            )
    except anthropic.APIError as error:
        raise classify_api_error(error) from error

    request_id = response._request_id
    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise ExtractionRefusedError(f"model declined the request (category={category})", request_id=request_id)

    # Select by type: the content may start with (empty) thinking blocks or a fallback marker block.
    tool_use = next((b for b in response.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
    if tool_use is None:
        raise ExtractionInvalidError(f"no {TOOL_NAME} call (stop_reason={response.stop_reason})", request_id=request_id)
    try:
        receipt = ReceiptData.model_validate(tool_use.input)
    except ValidationError as error:
        raise ExtractionInvalidError(f"tool input failed validation: {error}", request_id=request_id) from error

    # bedrock-runtime reports Anthropic's model name in `response.model`, not the inference profile id we invoked,
    # so the serving model is derived from the fallback state.
    fallback_used = fallback_state.index is not None
    served_model = (config.fallback_model_id or config.model_id) if fallback_used else config.model_id
    usage = response.usage
    return ExtractionResult(
        receipt=receipt,
        model=served_model,
        requested_model=config.model_id,
        fallback_used=fallback_used,
        stop_reason=response.stop_reason,
        request_id=request_id,
        usage=TokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            thinking_tokens=usage.output_tokens_details.thinking_tokens if usage.output_tokens_details else None,
        ),
    )
