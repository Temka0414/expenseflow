from __future__ import annotations

import base64
import json

from botocore import UNSIGNED

from expenseflow import aws


def test_clients_are_cached_per_service_and_endpoint() -> None:
    assert aws.client("events") is aws.client("events")
    assert aws.client("events") is aws.client(service="events")
    assert aws.client("events") is not aws.client("sqs")
    assert aws.apigw_management("https://a.example.com/live") is aws.apigw_management("https://a.example.com/live")
    assert aws.apigw_management("https://a.example.com/live") is not aws.apigw_management("https://b.example.com/live")


def test_reset_clients_forgets_everything() -> None:
    before = (aws.session(), aws.client("events"), aws.s3_presigner(), aws.cognito_unsigned())

    aws.reset_clients()

    after = (aws.session(), aws.client("events"), aws.s3_presigner(), aws.cognito_unsigned())
    assert all(old is not new for old, new in zip(before, after, strict=True))


def test_clients_use_standard_retries() -> None:
    config = aws.client("dynamodb").meta.config

    assert config.retries["mode"] == "standard"
    assert config.retries["total_max_attempts"] == 3  # first try + 2 retries


def test_table_uses_the_configured_name() -> None:
    assert aws.table().name == "expenseflow-test-table"
    assert aws.table() is aws.table()


def test_apigw_management_targets_the_callback_url() -> None:
    client = aws.apigw_management("https://w5s3k7c9d1.execute-api.us-east-1.amazonaws.com/live")

    assert client.meta.endpoint_url == "https://w5s3k7c9d1.execute-api.us-east-1.amazonaws.com/live"


def test_cognito_unsigned_sends_no_signature() -> None:
    assert aws.cognito_unsigned().meta.config.signature_version is UNSIGNED


def test_presigned_post_is_sigv4_on_the_regional_virtual_hosted_endpoint() -> None:
    post = aws.s3_presigner().generate_presigned_post(
        Bucket="expenseflow-test-receipts",
        Key="receipts/u/e/r.png",
        Fields={"Content-Type": "image/png"},
        Conditions=[{"Content-Type": "image/png"}, ["content-length-range", 1, 100]],
        ExpiresIn=300,
    )

    assert post["url"] == "https://expenseflow-test-receipts.s3.us-east-1.amazonaws.com/"
    assert post["fields"]["x-amz-algorithm"] == "AWS4-HMAC-SHA256"
    assert "AWSAccessKeyId" not in post["fields"]  # that would be the legacy SigV2 form
    policy = json.loads(base64.b64decode(post["fields"]["policy"]))
    assert ["content-length-range", 1, 100] in policy["conditions"]
