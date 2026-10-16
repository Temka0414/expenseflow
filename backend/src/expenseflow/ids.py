"""Identifiers, timestamps and calendar helpers.

**IDs are ULIDs**: 48-bit millisecond timestamp + 80 random bits, written as 26 Crockford base32 characters
(``01JA2B3C4D5E6F7G8H9JKMNPQR``). Compared with UUIDv4 they sort by creation time - handy as the tail of
DynamoDB sort keys such as ``{createdAt}#{id}`` - and they are URL- and S3-key-safe (no escaping needed).

**Timestamps** are ISO-8601 UTC with milliseconds and a ``Z`` suffix (``2026-10-13T05:01:02.345Z``). This is the
exact format of Step Functions' ``$states.context.State.EnteredTime``, so values written by Lambdas and by the
workflows compare and sort correctly as plain strings (DynamoDB sort keys compare byte by byte).

**Japan time**: Asia/Tokyo has had no daylight saving time since 1951, so a fixed UTC+9 offset is exact. (We avoid
``zoneinfo`` because Windows has no system tz database unless the ``tzdata`` package is installed.)
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

ID_LENGTH: Final = 26
# First character is 0-7: 26 base32 characters hold 130 bits, a ULID uses 128.
ID_PATTERN: Final = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
MONTH_PATTERN: Final = r"^\d{4}-(0[1-9]|1[0-2])$"
JST: Final = timezone(timedelta(hours=9), "JST")

_CROCKFORD32: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # no I, L, O, U: unambiguous when read aloud
_TIMESTAMP_BITS: Final = 48
_RANDOM_BITS: Final = 80


def utc_now() -> datetime:
    """Current time (timezone-aware UTC). Tests freeze time by monkeypatching this function."""
    return datetime.now(UTC)


def new_id(*, timestamp_ms: int | None = None) -> str:
    """Return a new ULID; ``timestamp_ms`` (Unix epoch milliseconds) defaults to now.

    Randomness comes from ``secrets`` (the OS CSPRNG), so IDs are unguessable as well as unique. ULIDs created in
    the same millisecond are not ordered among themselves; nothing in ExpenseFlow needs that.
    """
    millis = int(utc_now().timestamp() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= millis < 1 << _TIMESTAMP_BITS:
        raise ValueError(f"timestamp out of ULID range: {millis}")
    value = (millis << _RANDOM_BITS) | secrets.randbits(_RANDOM_BITS)
    chars = []
    for _ in range(ID_LENGTH):
        value, index = divmod(value, 32)
        chars.append(_CROCKFORD32[index])
    return "".join(reversed(chars))


def to_iso(moment: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (UTC, millisecond precision)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; pass a timezone-aware datetime")
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    return to_iso(utc_now())


def epoch_seconds(moment: datetime | None = None) -> int:
    """Unix time in whole seconds - the unit DynamoDB TTL attributes (``expiresAt``) require."""
    return int((moment or utc_now()).timestamp())


def month_of(timestamp: str) -> str:
    """``YYYY-MM`` of an ISO timestamp or date (UTC month, e.g. the month an expense was decided)."""
    return timestamp[:7]


def current_month_jst(now: datetime | None = None) -> str:
    """The current calendar month in Japan as ``YYYY-MM``."""
    local = (now or utc_now()).astimezone(JST)
    return f"{local:%Y-%m}"


def previous_month_jst(now: datetime | None = None) -> str:
    """The previous calendar month in Japan as ``YYYY-MM`` (what the monthly report covers)."""
    first_of_month = (now or utc_now()).astimezone(JST).replace(day=1)
    return f"{first_of_month - timedelta(days=1):%Y-%m}"
