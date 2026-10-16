from __future__ import annotations

import base64
import json
from decimal import Decimal

import pytest

from expenseflow.errors import DomainValidationError
from expenseflow.serialization import (
    decode_cursor,
    dumps,
    encode_cursor,
    json_default,
    to_decimal,
    to_json_number,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1280"), 1280),
        (Decimal("1280.00"), 1280),
        (Decimal("12.50"), 12.5),
        (Decimal("0.1"), 0.1),
        (7, 7),
        (2.5, 2.5),
    ],
)
def test_to_json_number(value: Decimal | int | float, expected: float) -> None:
    result = to_json_number(value)

    assert result == expected
    assert type(result) is type(expected)


def test_dumps_renders_decimals_as_numbers_and_sets_as_sorted_lists() -> None:
    text = dumps({"amount": Decimal("1280"), "tax": Decimal("116.5"), "tags": {"b", "a"}, "merchant": "すき家"})

    assert text == '{"amount":1280,"tax":116.5,"tags":["a","b"],"merchant":"すき家"}'


def test_json_default_rejects_other_types() -> None:
    with pytest.raises(TypeError, match="object is not JSON serializable"):
        json.dumps({"x": object()}, default=json_default)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.1, Decimal("0.1")), (1280, Decimal("1280")), ("12.50", Decimal("12.50")), (Decimal("3"), Decimal("3"))],
)
def test_to_decimal_goes_through_str(value: Decimal | float | str, expected: Decimal) -> None:
    result = to_decimal(value)

    assert result == expected
    assert str(result) == str(expected)


@pytest.mark.parametrize("value", ["abc", "", "NaN", "Infinity", float("inf"), float("nan"), True])
def test_to_decimal_rejects_non_finite_and_non_numbers(value: float | str) -> None:
    with pytest.raises(ValueError, match=r"number|finite"):
        to_decimal(value)


def test_cursor_round_trip() -> None:
    key = {"PK": "EXPENSE#01J", "SK": "META", "GSI1PK": "OWNER#u-1", "GSI1SK": "2026-10-13T05:01:02.345Z#01J"}

    cursor = encode_cursor(key)

    assert cursor is not None
    assert not set(cursor) & {"=", "+", "/"}  # URL-safe without escaping
    assert decode_cursor(cursor) == key


def test_cursor_handles_decimals() -> None:
    cursor = encode_cursor({"PK": "X", "n": Decimal("12.5")})

    assert decode_cursor(cursor) == {"PK": "X", "n": Decimal("12.5")}


@pytest.mark.parametrize("value", [None, {}])
def test_no_cursor_means_no_more_pages(value: dict[str, str] | None) -> None:
    assert encode_cursor(value) is None


@pytest.mark.parametrize("cursor", [None, ""])
def test_decode_empty_cursor(cursor: str | None) -> None:
    assert decode_cursor(cursor) is None


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b"[1, 2]").decode(),
        base64.urlsafe_b64encode(b"{}").decode(),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        "A" * 3000,
    ],
)
def test_decode_cursor_rejects_garbage(cursor: str) -> None:
    with pytest.raises(DomainValidationError, match="invalid cursor"):
        decode_cursor(cursor)
