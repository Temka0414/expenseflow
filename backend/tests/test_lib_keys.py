from __future__ import annotations

import re

import pytest

from expenseflow import keys

EXPENSE_ID = "01JA2B3C4D5E6F7G8H9JKMNPQR"


def test_expense_keys() -> None:
    assert keys.expense_key(EXPENSE_ID) == {"PK": f"EXPENSE#{EXPENSE_ID}", "SK": "META"}
    assert keys.owner_gsi1pk("u-1") == "OWNER#u-1"
    assert keys.owner_gsi1sk("2026-10-13T05:01:02.345Z", EXPENSE_ID) == f"2026-10-13T05:01:02.345Z#{EXPENSE_ID}"
    assert keys.status_gsi2pk("PENDING_APPROVAL") == "STATUS#PENDING_APPROVAL"
    assert keys.status_gsi2sk("2026-10-13T05:01:02.345Z", EXPENSE_ID) == f"2026-10-13T05:01:02.345Z#{EXPENSE_ID}"


def test_month_prefix_does_not_match_other_months() -> None:
    prefix = keys.status_gsi2sk_month_prefix("2026-01")

    assert prefix == "2026-01-"
    assert keys.status_gsi2sk("2026-01-31T23:59:59.999Z", EXPENSE_ID).startswith(prefix)
    assert not keys.status_gsi2sk("2026-10-01T00:00:00.000Z", EXPENSE_ID).startswith(prefix)


def test_history_keys() -> None:
    at = "2026-10-13T05:01:02.345Z"

    assert keys.history_sk_status(at, "APPROVED") == f"HISTORY#{at}#STATUS#APPROVED"
    assert keys.history_sk_receipt(at) == f"HISTORY#{at}#RECEIPT"


def test_total_keys_round_trip() -> None:
    sk = keys.total_sk("2026-10", "JPY")

    assert keys.user_total_pk("u-1") == "USER#u-1"
    assert keys.ORG_PK == "ORG"
    assert sk == "TOTAL#2026-10#JPY"
    assert keys.parse_total_sk(sk) == ("2026-10", "JPY")
    assert sk.startswith(keys.total_sk_year_prefix(2026))
    assert keys.total_sk_year_prefix(2026) == "TOTAL#2026-"


@pytest.mark.parametrize("sk", ["TOTAL#2026-10", "HISTORY#2026-10#JPY", "TOTAL##JPY", "TOTAL#2026-10#JPY#X"])
def test_parse_total_sk_rejects_other_keys(sk: str) -> None:
    with pytest.raises(ValueError, match="not a total"):
        keys.parse_total_sk(sk)


def test_websocket_keys() -> None:
    audience = keys.audience_user("u-1")

    assert audience == "USER#u-1"
    assert keys.audience_group("managers") == "GROUP#managers"
    assert keys.wsconn_pk("Ab1=") == "WSCONN#Ab1="
    assert keys.aud_sk(audience) == "AUD#USER#u-1"
    assert keys.wsaud_gsi1pk(audience) == "WSAUD#USER#u-1"


def test_receipt_object_keys_are_url_safe_and_splittable() -> None:
    key = keys.receipt_object_key("owner-1", EXPENSE_ID, "01JA2B3C4D5E6F7G8H9JKMNPQS", ".jpg")

    assert key == f"receipts/owner-1/{EXPENSE_ID}/01JA2B3C4D5E6F7G8H9JKMNPQS.jpg"
    assert key.startswith(keys.receipt_prefix("owner-1", EXPENSE_ID))
    assert re.fullmatch(r"[A-Za-z0-9/_.-]+", key)
    # The ReceiptPipeline recovers owner and expense ids by splitting on "/".
    assert key.split("/")[1:3] == ["owner-1", EXPENSE_ID]


def test_report_keys_round_trip() -> None:
    assert keys.report_object_key("2026-09") == "reports/2026-09.csv"
    assert keys.month_from_report_key("reports/2026-09.csv") == "2026-09"


@pytest.mark.parametrize("key", ["reports/2026-09.txt", "receipts/2026-09.csv", "reports/old/2026-09.csv"])
def test_month_from_report_key_ignores_other_objects(key: str) -> None:
    assert keys.month_from_report_key(key) is None
