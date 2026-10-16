"""Domain vocabulary, HTTP request models and the expense API representation.

Request bodies are pydantic v2 models. The HTTP API resolver runs with Powertools ``enable_validation=True``:
a route that declares e.g. ``body: CreateExpenseRequest`` gets a validated instance, and invalid input is
answered with ``422 {"statusCode": 422, "detail": [{"loc": ["body", "amount"], "type": "greater_than"}]}``
before our code runs. JSON uses camelCase (``expenseDate``) - an alias generator maps it to snake_case fields.

Responses go the other way through :func:`to_api`, which decides exactly which stored attributes the client
may see (never key attributes, never the Step Functions task token).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

from expenseflow import ids, keys, settings
from expenseflow.serialization import to_json_number

# --- Vocabulary -------------------------------------------------------------------------------------------


class ExpenseStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ExtractionStatus(StrEnum):
    """State of the AI receipt extraction (written by the ReceiptPipeline state machine)."""

    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Category(StrEnum):
    TRAVEL = "TRAVEL"
    MEALS = "MEALS"
    LODGING = "LODGING"
    TRANSPORT = "TRANSPORT"
    SUPPLIES = "SUPPLIES"
    SOFTWARE = "SOFTWARE"
    TRAINING = "TRAINING"
    OTHER = "OTHER"


class HistoryKind(StrEnum):
    """What a HISTORY item (and the matching domain event) records."""

    STATUS = "STATUS"
    RECEIPT = "RECEIPT"


Currency = Literal[
    "JPY", "USD", "EUR", "GBP", "CNY", "KRW", "TWD", "HKD", "SGD", "AUD", "CAD", "CHF", "THB", "INR", "MNT"
]
Decision = Literal["APPROVED", "REJECTED"]
ReceiptContentType = Literal["image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"]

CURRENCIES: Final[tuple[str, ...]] = get_args(Currency)
CATEGORIES: Final[tuple[str, ...]] = tuple(Category)
TERMINAL_EXTRACTION_STATUSES: Final = frozenset(
    {ExtractionStatus.SUCCEEDED, ExtractionStatus.FAILED, ExtractionStatus.SKIPPED}
)
# File extension used in the S3 key of an uploaded receipt, per accepted content type.
RECEIPT_EXTENSIONS: Final[Mapping[str, str]] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}

MAX_AMOUNT: Final = Decimal(100_000_000)
EARLIEST_EXPENSE_DATE: Final = date(2000, 1, 1)
# Attributes the owner edits through the API (DynamoDB attribute names = JSON field names).
EDITABLE_ATTRIBUTES: Final = ("title", "merchant", "amount", "currency", "category", "expenseDate", "description")
# Every expense has a title and a currency; the other editable attributes may be removed (absent = unknown).
REMOVABLE_ATTRIBUTES: Final = tuple(name for name in EDITABLE_ATTRIBUTES if name not in {"title", "currency"})
# Attributes an expense needs before it can be submitted for approval (amount must also be > 0).
REQUIRED_FOR_SUBMIT: Final = ("title", "merchant", "amount", "currency", "category", "expenseDate")
# Approval outcome attributes, cleared whenever an expense goes back to DRAFT or is (re)submitted.
DECISION_ATTRIBUTES: Final = (
    "approverId",
    "approverEmail",
    "decisionComment",
    "decidedAt",
    "autoApproved",
    "escalated",
)

# --- Request models ---------------------------------------------------------------------------------------

Title = Annotated[str, Field(min_length=1, max_length=120)]
Merchant = Annotated[str, Field(min_length=1, max_length=120)]
Description = Annotated[str, Field(min_length=1, max_length=1000)]
Comment = Annotated[str, Field(min_length=1, max_length=500)]
# Money is a Decimal end to end (DynamoDB stores it exactly); JSON 12.5 becomes Decimal("12.5"), never a float.
Amount = Annotated[Decimal, Field(gt=0, le=MAX_AMOUNT, decimal_places=2)]
# A plain string with a strict pattern: pydantic's `date` type would also accept timestamps and datetimes.
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Month = Annotated[str, Field(pattern=ids.MONTH_PATTERN)]


class RequestModel(BaseModel):
    """Base for request bodies: camelCase JSON, unknown fields rejected, surrounding whitespace trimmed, immutable.

    ``extra="forbid"`` turns a misspelt field (``expense_date`` instead of ``expenseDate`` from a buggy client) into
    a 422 instead of silently ignoring it. Python code may also use the snake_case names (``validate_by_name``).
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class _ExpenseFields(RequestModel):
    """Optional expense fields shared by create and update. A blank string means "no value"."""

    merchant: Merchant | None = None
    amount: Amount | None = None
    currency: Currency | None = None
    category: Category | None = None
    expense_date: IsoDate | None = None
    description: Description | None = None

    @field_validator("merchant", "amount", "currency", "category", "expense_date", "description", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("expense_date")
    @classmethod
    def _expense_date_in_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise PydanticCustomError("date_parsing", "expenseDate must be a real calendar date") from None
        # +1 day of slack: "today" in Japan (UTC+9) can already be tomorrow in UTC.
        latest = ids.utc_now().date() + timedelta(days=1)
        if not EARLIEST_EXPENSE_DATE <= parsed <= latest:
            raise PydanticCustomError(
                "date_out_of_range",
                "expenseDate must be between {earliest} and {latest}",
                {"earliest": EARLIEST_EXPENSE_DATE.isoformat(), "latest": latest.isoformat()},
            )
        return value


class CreateExpenseRequest(_ExpenseFields):
    """``POST /expenses``. Only the title is required; the rest can be filled in later (or by the AI)."""

    title: Title

    def attributes(self) -> dict[str, Any]:
        """The provided values keyed by DynamoDB attribute name. Missing values stay absent from the item."""
        provided = self.model_dump(by_alias=True, exclude_none=True)
        return {name: _attribute_value(value) for name, value in provided.items()}


class UpdateExpenseRequest(_ExpenseFields):
    """``PATCH /expenses/{id}``: a partial update.

    JSON merge-patch semantics: a field that is *absent* stays unchanged, a field sent as ``null`` (or ``""``)
    is removed. pydantic records which fields the client actually sent in ``model_fields_set``, which is how
    "absent" and "explicitly null" are told apart. ``version`` enables optimistic locking: when sent, the update
    only succeeds if the stored version still matches.
    """

    title: Title | None = None
    version: int | None = Field(default=None, ge=1)

    @field_validator("title", "currency", "version")
    @classmethod
    def _cannot_be_removed(cls, value: object) -> object:
        # Runs only for values the client sent (defaults are not validated), so `None` here is an explicit null.
        if value is None:
            raise PydanticCustomError("not_removable", "this field cannot be removed")
        return value

    def changes(self) -> tuple[dict[str, Any], list[str]]:
        """Split the patch into ``(attributes to SET, attributes to REMOVE)``, keyed by DynamoDB attribute name."""
        sent = self.model_dump(by_alias=True, exclude_unset=True, exclude={"version"})
        updates = {name: _attribute_value(value) for name, value in sent.items() if value is not None}
        removals = sorted(name for name, value in sent.items() if value is None)
        return updates, removals


class ReceiptUploadRequest(RequestModel):
    """``POST /expenses/{id}/receipt-upload``: what the browser is about to upload."""

    content_type: ReceiptContentType
    size: int = Field(gt=0)

    @field_validator("size")
    @classmethod
    def _within_limit(cls, value: int) -> int:
        limit = settings.max_receipt_bytes()
        if value > limit:
            raise PydanticCustomError("receipt_too_large", "receipt must be at most {limit} bytes", {"limit": limit})
        return value

    @property
    def extension(self) -> str:
        return RECEIPT_EXTENSIONS[self.content_type]


class DecisionRequest(RequestModel):
    """``POST /approvals/{id}/decision``. Rejections must explain themselves."""

    decision: Decision
    comment: Comment | None = None

    @field_validator("comment", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _comment_required_for_rejection(self) -> Self:
        if self.decision == "REJECTED" and self.comment is None:
            raise PydanticCustomError("comment_required", "a comment is required when rejecting an expense")
        return self


class CreateReportRequest(RequestModel):
    """``POST /reports``: generate the CSV report of one month (current month or earlier, Japan time)."""

    month: Month

    @field_validator("month")
    @classmethod
    def _not_in_future(cls, value: str) -> str:
        if value > ids.current_month_jst():  # "YYYY-MM" strings compare chronologically
            raise PydanticCustomError("month_in_future", "month must not be in the future")
        return value


def _attribute_value(value: object) -> object:
    """Plain DynamoDB-ready value (enum members become their string value)."""
    return value.value if isinstance(value, StrEnum) else value


# --- Business rules on stored items -----------------------------------------------------------------------


def missing_required_fields(item: Mapping[str, Any]) -> list[str]:
    """Attributes that block submission, in display order (``amount`` also counts as missing when not > 0)."""
    missing = [name for name in REQUIRED_FOR_SUBMIT if item.get(name) in (None, "")]
    if "amount" not in missing and not Decimal(item["amount"]) > 0:
        missing.append("amount")
    return missing


# --- API representation -----------------------------------------------------------------------------------


def to_api(item: Mapping[str, Any]) -> dict[str, Any]:
    """The expense as the HTTP API and the SPA see it (SPEC section 5).

    Built field by field on purpose: an allow-list cannot leak attributes added later (keys, ``approvalTaskToken``).
    Numbers become JSON-ready ``int``/``float``.
    """
    amount = item.get("amount")
    return {
        "id": item["id"],
        "ownerId": item.get("ownerId"),
        "ownerEmail": item.get("ownerEmail"),
        "ownerName": item.get("ownerName"),
        "title": item.get("title"),
        "merchant": item.get("merchant"),
        "amount": None if amount is None else to_json_number(amount),
        "currency": item.get("currency"),
        "category": item.get("category"),
        "expenseDate": item.get("expenseDate"),
        "description": item.get("description"),
        "status": item.get("status"),
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
        "submittedAt": item.get("submittedAt"),
        "decidedAt": item.get("decidedAt"),
        "version": int(item.get("version", 0)),
        "receipt": _receipt(item),
        "pendingUpload": "pendingReceiptKey" in item,
        "extraction": _extraction(item),
        "approval": _approval(item),
        "workflowExecutionArn": item.get("workflowExecutionArn"),
    }


def _receipt(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if "receiptKey" not in item:
        return None
    return {
        "key": item["receiptKey"],
        "contentType": item.get("receiptContentType"),
        "uploadedAt": item.get("receiptUploadedAt"),
    }


def _extraction(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if "extractionStatus" not in item:
        return None
    return {
        "status": item["extractionStatus"],
        "result": parse_extraction_result(item.get("extractionResult")),
        "error": item.get("extractionError"),
        "model": item.get("extractionModel"),
        "extractedAt": item.get("extractedAt"),
    }


_APPROVAL_ATTRIBUTES: Final = (*DECISION_ATTRIBUTES, "approvalRequestedAt")


def _approval(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not any(name in item for name in _APPROVAL_ATTRIBUTES):
        return None
    return {
        "approverId": item.get("approverId"),
        "approverEmail": item.get("approverEmail"),
        "comment": item.get("decisionComment"),
        "autoApproved": bool(item.get("autoApproved", False)),
        "escalated": bool(item.get("escalated", False)),
        "requestedAt": item.get("approvalRequestedAt"),
    }


def parse_extraction_result(raw: object) -> dict[str, Any] | None:
    """Decode the ``extractionResult`` JSON string; anything unexpected yields ``None`` instead of an error.

    The value is written by the ReceiptPipeline (``$string(...)`` in JSONata), so a malformed value must never
    break reading the expense itself.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def history_to_api(item: Mapping[str, Any]) -> dict[str, Any]:
    """One HISTORY item (written by the stream processor) as shown in the expense timeline."""
    return {
        "at": item.get("at"),
        "kind": item.get("kind"),
        "from": item.get("from"),
        "to": item.get("to"),
        "actorEmail": item.get("actorEmail"),
        "comment": item.get("comment"),
        "extractionStatus": item.get("extractionStatus"),
    }


def total_to_api(item: Mapping[str, Any]) -> dict[str, Any]:
    """One monthly-total item (``TOTAL#{yyyy-mm}#{currency}``, maintained by the stream processor) for ``GET /stats``.

    Month and currency are read from the sort key, the one attribute every total item is guaranteed to have.
    """
    month, currency = keys.parse_total_sk(item[keys.SK])
    return {
        "month": month,
        "currency": currency,
        "approvedAmount": to_json_number(item.get("approvedAmount", 0)),
        "approvedCount": int(item.get("approvedCount", 0)),
    }
