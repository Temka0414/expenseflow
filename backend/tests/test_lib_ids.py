from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from expenseflow import ids
from tests.conftest import FakeClock

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_new_id_is_a_26_char_crockford_ulid() -> None:
    value = ids.new_id()

    assert len(value) == ids.ID_LENGTH == 26
    assert re.fullmatch(ids.ID_PATTERN, value)
    assert not set(value) & set("ILOU")


def test_new_ids_are_unique() -> None:
    assert len({ids.new_id() for _ in range(2000)}) == 2000


def _timestamp_of(ulid: str) -> int:
    """Decode the first 10 characters of a ULID: the 48-bit millisecond timestamp."""
    value = 0
    for char in ulid[:10]:
        value = value * 32 + _CROCKFORD.index(char)
    return value


def test_new_id_encodes_the_timestamp_so_ids_sort_by_time() -> None:
    earlier = ids.new_id(timestamp_ms=1_791_867_662_000)
    later = ids.new_id(timestamp_ms=1_791_867_662_001)

    assert earlier < later
    assert _timestamp_of(earlier) == 1_791_867_662_000


def test_new_id_uses_the_clock(clock: FakeClock) -> None:
    expected_ms = int(clock.now.timestamp() * 1000)

    assert _timestamp_of(ids.new_id()) == expected_ms


@pytest.mark.parametrize("timestamp_ms", [-1, 1 << 48])
def test_new_id_rejects_timestamps_outside_48_bits(timestamp_ms: int) -> None:
    with pytest.raises(ValueError, match="out of ULID range"):
        ids.new_id(timestamp_ms=timestamp_ms)


def test_to_iso_is_utc_with_milliseconds_and_z() -> None:
    tokyo = timezone(timedelta(hours=9))
    moment = datetime(2026, 10, 13, 14, 1, 2, 345_678, tzinfo=tokyo)

    assert ids.to_iso(moment) == "2026-10-13T05:01:02.345Z"


def test_to_iso_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="naive"):
        ids.to_iso(datetime(2026, 10, 13, 5, 1, 2))


def test_now_iso_matches_the_step_functions_format(clock: FakeClock) -> None:
    value = ids.now_iso()

    assert value == "2026-10-13T05:01:02.345Z"
    assert _ISO_PATTERN.match(value)


def test_now_iso_without_a_fake_clock_has_the_same_format() -> None:
    assert _ISO_PATTERN.match(ids.now_iso())


def test_epoch_seconds() -> None:
    assert ids.epoch_seconds(datetime(2026, 10, 13, 5, 1, 2, 900_000, tzinfo=UTC)) == 1_791_867_662


def test_epoch_seconds_defaults_to_now(clock: FakeClock) -> None:
    assert ids.epoch_seconds() == 1_791_867_662


def test_month_of() -> None:
    assert ids.month_of("2026-10-13T05:01:02.345Z") == "2026-10"
    assert ids.month_of("2026-01-31") == "2026-01"


@pytest.mark.parametrize(
    ("utc_moment", "current", "previous"),
    [
        (datetime(2026, 10, 13, 5, 0, tzinfo=UTC), "2026-10", "2026-09"),
        # 15:30 UTC on Oct 31 is already Nov 1 in Tokyo: the scheduler fires at 09:00 JST on the 1st.
        (datetime(2026, 10, 31, 15, 30, tzinfo=UTC), "2026-11", "2026-10"),
        (datetime(2026, 1, 1, 0, 0, tzinfo=UTC), "2026-01", "2025-12"),
        (datetime(2025, 12, 31, 15, 0, tzinfo=UTC), "2026-01", "2025-12"),
        (datetime(2024, 3, 1, 0, 0, tzinfo=UTC), "2024-03", "2024-02"),
    ],
)
def test_month_helpers_use_japan_time(utc_moment: datetime, current: str, previous: str) -> None:
    assert ids.current_month_jst(utc_moment) == current
    assert ids.previous_month_jst(utc_moment) == previous


def test_previous_month_defaults_to_now(clock: FakeClock) -> None:
    assert ids.previous_month_jst() == "2026-09"


def test_jst_is_a_fixed_utc_plus_9_offset() -> None:
    assert ids.JST.utcoffset(None) == timedelta(hours=9)


@pytest.mark.parametrize(
    ("value", "valid"), [("2026-10", True), ("2026-13", False), ("2026-1", False), ("26-10", False)]
)
def test_month_pattern(value: str, valid: bool) -> None:
    assert bool(re.fullmatch(ids.MONTH_PATTERN, value)) is valid
