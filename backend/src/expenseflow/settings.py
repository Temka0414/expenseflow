"""Typed, lazy access to Lambda configuration.

CDK passes configuration to each function as environment variables (SPEC section 7). Every accessor here reads
``os.environ`` when it is *called*, never at import time, because:

* each Lambda only receives the variables it needs, so importing a module must not fail on a missing one;
* tests can change a value with ``monkeypatch.setenv`` without reloading modules.

Reading an environment variable is a dictionary lookup, so there is nothing worth caching.
A missing required value is a deployment (CDK wiring) bug, reported as :class:`ConfigurationError`.
"""

from __future__ import annotations

import os
from typing import Final

# Cognito user-pool groups (created by the Auth stack). Membership arrives in the `cognito:groups` claim.
GROUP_EMPLOYEES: Final = "employees"
GROUP_MANAGERS: Final = "managers"
GROUP_ADMINS: Final = "admins"
GROUPS: Final = (GROUP_EMPLOYEES, GROUP_MANAGERS, GROUP_ADMINS)

# Bedrock accepts images up to 5 MB *base64-encoded*, i.e. about 3.75 MB of raw bytes; keep a safety margin.
DEFAULT_MAX_RECEIPT_BYTES: Final = 3_500_000
DEFAULT_CURRENCY: Final = "JPY"
# Values meaning "feature switched off" for optional model ids ("none" is what `-c ...=none` produces).
_DISABLED_VALUES: Final = frozenset({"", "none"})


class ConfigurationError(RuntimeError):
    """A required environment variable is missing or malformed."""


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _required(name: str) -> str:
    value = _optional(name)
    if value is None:
        raise ConfigurationError(f"environment variable {name} is not set")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = _optional(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(f"environment variable {name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ConfigurationError(f"environment variable {name} must be positive, got {value}")
    return value


# --- AWS runtime -------------------------------------------------------------------------------------------


def aws_region() -> str:
    """Region the function runs in (Lambda always sets ``AWS_REGION``)."""
    return _optional("AWS_REGION") or _required("AWS_DEFAULT_REGION")


def stage() -> str:
    """Deployment stage, e.g. ``dev`` or ``prod``."""
    return _required("STAGE")


# --- Data -------------------------------------------------------------------------------------------------


def table_name() -> str:
    """The single DynamoDB table (expenses, history, totals, WebSocket connections)."""
    return _required("TABLE_NAME")


def idempotency_table_name() -> str:
    """The Powertools idempotency table (partition key ``id``, TTL ``expiration``)."""
    return _required("IDEMPOTENCY_TABLE_NAME")


def receipts_bucket() -> str:
    return _required("RECEIPTS_BUCKET")


def reports_bucket() -> str:
    return _required("REPORTS_BUCKET")


def max_receipt_bytes() -> int:
    """Largest receipt upload accepted (enforced by the presigned POST policy and by extraction)."""
    return _positive_int("MAX_RECEIPT_BYTES", DEFAULT_MAX_RECEIPT_BYTES)


def default_currency() -> str:
    return _optional("DEFAULT_CURRENCY") or DEFAULT_CURRENCY


# --- Integration ------------------------------------------------------------------------------------------


def event_bus_name() -> str:
    """The custom EventBridge bus that carries ExpenseFlow domain events."""
    return _required("EVENT_BUS_NAME")


def report_function_name() -> str:
    """The report generator Lambda that ``POST /reports`` invokes asynchronously."""
    return _required("REPORT_FUNCTION_NAME")


def ws_callback_url() -> str:
    """WebSocket stage callback URL (``https://{api}.execute-api.{region}.amazonaws.com/live``) for ``@connections``."""
    return _required("WS_CALLBACK_URL").rstrip("/")


# --- Identity ---------------------------------------------------------------------------------------------


def user_pool_id() -> str:
    return _required("USER_POOL_ID")


def user_pool_client_id() -> str:
    return _required("USER_POOL_CLIENT_ID")


# --- Notifications ----------------------------------------------------------------------------------------


def sender_email() -> str | None:
    """Verified SES sender, or ``None`` when e-mail is switched off (CDK passes an empty string)."""
    return _optional("SENDER_EMAIL")


def app_url() -> str:
    """Public site URL used to build links in e-mails (no trailing slash)."""
    return _required("APP_URL").rstrip("/")


# --- Receipt extraction (Claude on Amazon Bedrock) --------------------------------------------------------


def bedrock_region() -> str:
    """Region of the bedrock-runtime endpoint; defaults to the function's own region."""
    return _optional("BEDROCK_REGION") or aws_region()


def extraction_model_id() -> str:
    """Model (inference profile) id used for receipt extraction, e.g. ``jp.anthropic.claude-opus-5-5``."""
    return _required("EXTRACTION_MODEL_ID")


def extraction_fallback_model_id() -> str | None:
    """Model used when the primary model refuses; ``None`` when the fallback is switched off."""
    value = _optional("EXTRACTION_FALLBACK_MODEL_ID")
    return None if value is None or value.lower() in _DISABLED_VALUES else value
