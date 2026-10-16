"""JSON helpers that understand DynamoDB's types, and opaque pagination cursors.

boto3 returns every DynamoDB number as ``decimal.Decimal`` (binary floats cannot represent money exactly) and
string/number sets as Python ``set``. The standard ``json`` module serialises neither, so everything that turns
items into JSON (API responses, EventBridge details) goes through :func:`dumps`.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from expenseflow.errors import DomainValidationError

_MAX_CURSOR_LENGTH: Final = 2048


def to_json_number(value: Decimal | int | float) -> int | float:
    """Render a number for JSON: ``Decimal("1280")`` -> ``1280``, ``Decimal("12.50")`` -> ``12.5``."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def json_default(value: object) -> object:
    """``json.dumps(default=...)`` hook: Decimal -> int/float, set -> sorted list."""
    if isinstance(value, Decimal):
        return to_json_number(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps(value: Any) -> str:
    """Compact, Decimal-safe JSON. Non-ASCII text (Japanese merchant names) stays readable UTF-8."""
    return json.dumps(value, default=json_default, separators=(",", ":"), ensure_ascii=False)


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Convert to a finite ``Decimal`` suitable for a DynamoDB number.

    Goes through ``str`` on purpose: ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827...`` (the exact
    binary float), while ``Decimal(str(0.1))`` is ``0.1``. NaN and infinity are rejected - DynamoDB cannot store them.
    """
    if isinstance(value, bool):
        raise ValueError("booleans are not numbers")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"number must be finite: {value!r}")
    return result


def encode_cursor(last_evaluated_key: Mapping[str, Any] | None) -> str | None:
    """Turn a DynamoDB ``LastEvaluatedKey`` into an opaque, URL-safe page token (``None`` = no more pages)."""
    if not last_evaluated_key:
        return None
    raw = dumps(dict(last_evaluated_key)).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    """Inverse of :func:`encode_cursor`. Raises :class:`DomainValidationError` for anything malformed.

    The result is client-supplied data: callers must still check it is a key of the expected partition before
    passing it to DynamoDB as ``ExclusiveStartKey`` (the repository does).
    """
    if not cursor:
        return None
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise DomainValidationError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        decoded = json.loads(raw, parse_float=Decimal)
    except (binascii.Error, ValueError):
        raise DomainValidationError("invalid cursor") from None
    if not isinstance(decoded, dict) or not decoded:
        raise DomainValidationError("invalid cursor")
    return decoded
