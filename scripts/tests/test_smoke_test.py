from __future__ import annotations

from datetime import date
from email import message_from_bytes
from typing import Any

import pytest

import smoke_test
from _common import ScriptError
from add_user_to_group import email_filter
from smoke_test import Api, ApiError, missing_history, multipart_form, suggested_fields, wait_for

TODAY = date(2026, 10, 15)


def test_multipart_puts_the_signed_fields_first_and_the_file_last() -> None:
    fields = {"key": "receipts/a.pdf", "Content-Type": "application/pdf"}
    body, content_type = multipart_form(fields, "a.pdf", "application/pdf", b"%PDF-\r\n\x00data")

    # The standard library's e-mail parser understands multipart/form-data too.
    message = message_from_bytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
    parts = message.get_payload()
    names = [part.get_param("name", header="content-disposition") for part in parts]

    assert names == ["key", "Content-Type", "file"]
    assert parts[0].get_payload(decode=True) == b"receipts/a.pdf"
    assert parts[2].get_payload(decode=True) == b"%PDF-\r\n\x00data"
    assert parts[2].get_filename() == "a.pdf"


def test_suggestions_use_the_ai_reading_when_it_is_valid() -> None:
    reading = {"merchant": " Cafe Mori ", "category": "MEALS", "date": "2026-10-14"}

    assert suggested_fields(reading, TODAY) == {
        "merchant": "Cafe Mori",
        "category": "MEALS",
        "expenseDate": "2026-10-14",
    }


@pytest.mark.parametrize(
    "reading",
    [
        None,
        {},
        {"merchant": 7, "category": ["MEALS"], "date": "tomorrow"},
        {"category": "GAMBLING", "date": "2099-01-01"},
    ],
)
def test_suggestions_fall_back_to_test_values(reading: dict[str, Any] | None) -> None:
    assert suggested_fields(reading, TODAY) == {
        "merchant": "Smoke Test",
        "category": "OTHER",
        "expenseDate": "2026-10-15",
    }


def test_history_lists_what_is_still_missing() -> None:
    history = [{"kind": "STATUS", "to": "PENDING_APPROVAL"}, {"kind": "RECEIPT"}]

    assert missing_history(history) == ["STATUS -> APPROVED"]
    assert missing_history([]) == ["STATUS -> PENDING_APPROVAL", "STATUS -> APPROVED", "RECEIPT"]


def test_wait_for_polls_until_the_probe_returns_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke_test.time, "sleep", lambda _seconds: None)
    answers = iter([None, None, "done"])

    assert wait_for("something", lambda: next(answers), timeout=60) == "done"


def test_wait_for_gives_up_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke_test.time, "sleep", lambda _seconds: None)

    with pytest.raises(ScriptError, match="Timed out after 0s waiting for the thing"):
        wait_for("the thing", lambda: None, timeout=0)


def test_api_sends_the_id_token_and_raises_on_an_unexpected_status(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def fake_http(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        calls.append((method, url, body, headers))
        return 409, b'{"message": "not ready"}'

    monkeypatch.setattr(smoke_test, "http", fake_http)

    with pytest.raises(ApiError, match="HTTP 409") as error:
        Api("https://api.example/", "TOKEN").call("POST", "/x", {"a": 1})

    headers = {"Authorization": "Bearer TOKEN", "Content-Type": "application/json"}
    assert error.value.status == 409
    assert calls == [("POST", "https://api.example/x", b'{"a": 1}', headers)]


def test_email_filter_uses_cognitos_filter_syntax() -> None:
    assert email_filter("a@example.com") == 'email = "a@example.com"'


@pytest.mark.parametrize("email", ["nobody", 'a"@example.com', "a\\@example.com"])
def test_email_filter_rejects_values_that_could_break_the_filter(email: str) -> None:
    with pytest.raises(ScriptError):
        email_filter(email)
