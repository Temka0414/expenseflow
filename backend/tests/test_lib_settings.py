from __future__ import annotations

from collections.abc import Callable

import pytest

from expenseflow import settings
from expenseflow.settings import ConfigurationError


@pytest.mark.parametrize(
    ("accessor", "variable", "value"),
    [
        (settings.stage, "STAGE", "test"),
        (settings.table_name, "TABLE_NAME", "expenseflow-test-table"),
        (settings.idempotency_table_name, "IDEMPOTENCY_TABLE_NAME", "expenseflow-test-idempotency"),
        (settings.receipts_bucket, "RECEIPTS_BUCKET", "expenseflow-test-receipts"),
        (settings.reports_bucket, "REPORTS_BUCKET", "expenseflow-test-reports"),
        (settings.event_bus_name, "EVENT_BUS_NAME", "expenseflow-test-bus"),
        (settings.report_function_name, "REPORT_FUNCTION_NAME", "expenseflow-test-report-generator"),
        (settings.user_pool_id, "USER_POOL_ID", "us-east-1_TestPool1"),
        (settings.user_pool_client_id, "USER_POOL_CLIENT_ID", "4testclientid0000000000000"),
        (settings.extraction_model_id, "EXTRACTION_MODEL_ID", "us.anthropic.claude-opus-5-5"),
    ],
)
def test_required_values_are_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch, accessor: Callable[[], str], variable: str, value: str
) -> None:
    assert accessor() == value

    monkeypatch.setenv(variable, "changed")
    assert accessor() == "changed"

    monkeypatch.delenv(variable)
    with pytest.raises(ConfigurationError, match=variable):
        accessor()


def test_urls_lose_their_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_CALLBACK_URL", "https://abc.execute-api.us-east-1.amazonaws.com/live/")
    monkeypatch.setenv("APP_URL", "https://d1234abcd.cloudfront.net/")

    assert settings.ws_callback_url() == "https://abc.execute-api.us-east-1.amazonaws.com/live"
    assert settings.app_url() == "https://d1234abcd.cloudfront.net"


def test_defaults_apply_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("MAX_RECEIPT_BYTES", "DEFAULT_CURRENCY", "SENDER_EMAIL"):
        monkeypatch.delenv(variable)

    assert settings.max_receipt_bytes() == 3_500_000
    assert settings.default_currency() == "JPY"
    assert settings.sender_email() is None


def test_sender_email_empty_string_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.sender_email() is None

    monkeypatch.setenv("SENDER_EMAIL", "noreply@example.com")
    assert settings.sender_email() == "noreply@example.com"


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5"])
def test_max_receipt_bytes_must_be_a_positive_integer(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("MAX_RECEIPT_BYTES", raw)

    with pytest.raises(ConfigurationError, match="MAX_RECEIPT_BYTES"):
        settings.max_receipt_bytes()


@pytest.mark.parametrize(("configured", "expected"), [(None, "ap-northeast-1"), ("eu-west-1", "eu-west-1")])
def test_bedrock_region_defaults_to_the_function_region(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, expected: str
) -> None:
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    if configured is None:
        monkeypatch.delenv("BEDROCK_REGION")
    else:
        monkeypatch.setenv("BEDROCK_REGION", configured)

    assert settings.bedrock_region() == expected


@pytest.mark.parametrize(("raw", "expected"), [("", None), ("none", None), ("NONE", None), ("jp.x", "jp.x")])
def test_fallback_model_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str | None) -> None:
    monkeypatch.setenv("EXTRACTION_FALLBACK_MODEL_ID", raw)

    assert settings.extraction_fallback_model_id() == expected


def test_groups() -> None:
    assert settings.GROUPS == ("employees", "managers", "admins")
