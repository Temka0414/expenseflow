"""Shared, lazily-created boto3 clients.

Why a module like this:

* **Reuse across invocations.** Lambda keeps the execution environment (and module globals) warm between
  invocations. Creating a client costs tens of milliseconds and a TLS handshake, so we create each one once,
  on first use, and cache it with ``functools.cache``.
* **Never at import time.** A client built during import would read configuration too early, slow down
  cold starts of code paths that never use it, and - in tests - be created before moto is active.
  ``reset_clients()`` drops every cached object so each test starts clean.
* **One instance per service.** A test can wrap the exact client a handler uses in a botocore ``Stubber``
  (e.g. to simulate ``GoneException``), because both get the same cached object.

All clients share the "standard" retry mode: exponential backoff with jitter on throttling and transient
errors, plus short connect/read timeouts so a slow dependency fails fast instead of eating the Lambda timeout.
"""

from __future__ import annotations

from functools import cache
from typing import Any, Final

import boto3
from botocore import UNSIGNED
from botocore.config import Config

from expenseflow import settings

# `total_max_attempts` counts the first try (3 = 1 try + 2 retries). Beware botocore's other spelling,
# `max_attempts`, which counts retries only: `max_attempts=3` would mean 4 calls.
_BASE_CONFIG: Final = Config(
    retries={"mode": "standard", "total_max_attempts": 3},
    connect_timeout=3,
    read_timeout=10,
)

# Presigned S3 URLs/POST policies: boto3's default still signs with SigV2 against the *global* endpoint in
# us-east-1 and ap-northeast-1. SigV4 + virtual-hosted + regional gives `https://<bucket>.s3.<region>.amazonaws.com`.
_S3_PRESIGN_CONFIG: Final = _BASE_CONFIG.merge(
    Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
    )
)

# Cognito's token APIs (GetUser with an access token) are authorised by the token itself, not by IAM.
# An unsigned client needs no IAM permission at all - least privilege for the WebSocket authorizer.
_UNSIGNED_CONFIG: Final = _BASE_CONFIG.merge(Config(signature_version=UNSIGNED))


@cache
def session() -> boto3.session.Session:
    """The boto3 session every client is built from (credentials come from the Lambda role)."""
    return boto3.session.Session()


@cache
def _client(service: str, endpoint_url: str | None) -> Any:
    return session().client(service, config=_BASE_CONFIG, endpoint_url=endpoint_url)


def client(service: str, endpoint_url: str | None = None) -> Any:
    """Cached low-level client, e.g. ``client("events")`` or ``client("stepfunctions")``.

    Arguments are normalised before hitting the cache, so ``client("s3")`` and ``client(service="s3")`` return
    the same instance.
    """
    return _client(service, endpoint_url)


@cache
def _dynamodb() -> Any:
    return session().resource("dynamodb", config=_BASE_CONFIG)


@cache
def table() -> Any:
    """The single DynamoDB table as a boto3 ``Table`` resource (plain Python values, numbers as ``Decimal``)."""
    return _dynamodb().Table(settings.table_name())


@cache
def s3_presigner() -> Any:
    """S3 client configured for presigned URLs/POST policies (SigV4, virtual-hosted, regional endpoint)."""
    return session().client("s3", config=_S3_PRESIGN_CONFIG)


@cache
def cognito_unsigned() -> Any:
    """Cognito user-pools client that sends unsigned requests (for token-authorised APIs like ``GetUser``)."""
    return session().client("cognito-idp", config=_UNSIGNED_CONFIG)


def apigw_management(endpoint_url: str) -> Any:
    """API Gateway Management API client for one WebSocket stage (``post_to_connection`` and friends).

    ``endpoint_url`` is the stage callback URL (``settings.ws_callback_url()``).
    """
    return client("apigatewaymanagementapi", endpoint_url)


def reset_clients() -> None:
    """Forget every cached session/client/resource (tests call this between cases)."""
    for cached in (session, _client, _dynamodb, table, s3_presigner, cognito_unsigned):
        cached.cache_clear()
