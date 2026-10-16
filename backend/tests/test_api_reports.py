"""HTTP API: monthly CSV reports (admins only) - list, generate asynchronously, download."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.stub import Stubber

from expenseflow import aws, settings
from tests.builders import ADMIN_EMAIL, ADMIN_ID, MANAGER_ID, http_event
from tests.conftest import FakeClock, FakeLambdaContext

type Api = Callable[[dict[str, Any]], tuple[int, Any]]

ADMIN: dict[str, Any] = {"user_id": ADMIN_ID, "email": ADMIN_EMAIL, "groups": ("admins", "employees")}
MANAGER: dict[str, Any] = {"user_id": MANAGER_ID, "groups": ("managers",)}


@pytest.fixture
def api(reports_bucket: str, clock: FakeClock, lambda_context: FakeLambdaContext) -> Api:
    """Invoke the API Lambda handler; returns ``(statusCode, parsed JSON body or None)``."""
    from handlers.api.app import handler

    def call(event: dict[str, Any]) -> tuple[int, Any]:
        response = handler(event, lambda_context)
        return response["statusCode"], json.loads(response["body"]) if response.get("body") else None

    return call


CSV_HEADER_ONLY = "\ufeffexpense_id\n".encode()  # UTF-8 with BOM, as the report generator writes it


def put_report(bucket: str, key: str, body: bytes = CSV_HEADER_ONLY) -> None:
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/csv")


# --- List ------------------------------------------------------------------------------------------------------


def test_list_returns_monthly_reports_newest_first(api: Api, reports_bucket: str) -> None:
    put_report(reports_bucket, "reports/2026-08.csv")
    put_report(reports_bucket, "reports/2026-09.csv", CSV_HEADER_ONLY + b"row\n")
    put_report(reports_bucket, "reports/drafts/2026-10.csv")  # not a monthly report key
    put_report(reports_bucket, "exports/2026-07.csv")

    status, body = api(http_event("GET", "/reports", **ADMIN))

    assert status == 200
    assert [(report["month"], report["key"], report["size"]) for report in body] == [
        ("2026-09", "reports/2026-09.csv", len(CSV_HEADER_ONLY) + 4),
        ("2026-08", "reports/2026-08.csv", len(CSV_HEADER_ONLY)),
    ]
    assert all(report["lastModified"].endswith("Z") for report in body)


def test_list_is_empty_without_reports(api: Api) -> None:
    status, body = api(http_event("GET", "/reports", **ADMIN))

    assert status == 200
    assert body == []


@pytest.mark.parametrize(("method", "path"), [("GET", "/reports"), ("GET", "/reports/2026-09")])
def test_reports_are_for_admins_only(api: Api, method: str, path: str) -> None:
    status, body = api(http_event(method, path, **MANAGER))

    assert status == 403
    assert body == {"statusCode": 403, "message": "only admins can do this"}


# --- Generate --------------------------------------------------------------------------------------------------


def test_generate_invokes_the_report_function_asynchronously(api: Api) -> None:
    expected_payload = json.dumps({"month": "2026-09", "requestedBy": ADMIN_EMAIL}, separators=(",", ":"))

    with Stubber(aws.client("lambda")) as stubber:
        stubber.add_response(
            "invoke",
            {"StatusCode": 202},
            {
                "FunctionName": settings.report_function_name(),
                "InvocationType": "Event",
                "Payload": expected_payload.encode(),
            },
        )
        status, body = api(http_event("POST", "/reports", body={"month": "2026-09"}, **ADMIN))
        stubber.assert_no_pending_responses()

    assert status == 202
    assert body == {"accepted": True, "month": "2026-09"}


def test_generate_against_a_real_function_answers_202(api: Api, report_function: str) -> None:
    status, body = api(http_event("POST", "/reports", body={"month": "2026-10"}, **ADMIN))  # current month

    assert status == 202
    assert body == {"accepted": True, "month": "2026-10"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"month": "2026-11"}, {(("body", "month"), "month_in_future")}),
        ({"month": "2026-9"}, {(("body", "month"), "string_pattern_mismatch")}),
        ({}, {(("body", "month"), "missing")}),
    ],
)
def test_generate_validates_the_month(
    api: Api, payload: dict[str, Any], expected: set[tuple[tuple[str, ...], str]]
) -> None:
    status, body = api(http_event("POST", "/reports", body=payload, **ADMIN))

    assert status == 422
    assert {(tuple(error["loc"]), error["type"]) for error in body["detail"]} == expected


def test_generate_is_for_admins_only(api: Api) -> None:
    status, _ = api(http_event("POST", "/reports", body={"month": "2026-09"}, **MANAGER))

    assert status == 403


# --- Download --------------------------------------------------------------------------------------------------


def test_download_is_a_presigned_attachment_url(api: Api, reports_bucket: str) -> None:
    put_report(reports_bucket, "reports/2026-09.csv")

    status, body = api(http_event("GET", "/reports/2026-09", **ADMIN))

    assert status == 200
    assert body["expiresIn"] == 300
    url = urlsplit(body["url"])
    assert url.path == "/reports/2026-09.csv"
    assert url.netloc.startswith(f"{reports_bucket}.s3.")
    assert parse_qs(url.query)["response-content-disposition"] == ['attachment; filename="expenseflow-2026-09.csv"']


def test_download_of_a_missing_report_is_404(api: Api) -> None:
    status, body = api(http_event("GET", "/reports/2026-01", **ADMIN))

    assert status == 404
    assert body == {"statusCode": 404, "message": "there is no report for 2026-01 yet"}


def test_download_rejects_malformed_months(api: Api) -> None:
    status, body = api(http_event("GET", "/reports/2026-13", **ADMIN))

    assert status == 422
    assert [(error["loc"], error["type"]) for error in body["detail"]] == [
        (["path", "month"], "string_pattern_mismatch")
    ]
