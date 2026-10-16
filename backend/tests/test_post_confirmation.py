"""Cognito Post confirmation trigger: new sign-ups join the default group (SPEC 7.7)."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from handlers import post_confirmation
from tests.conftest import UserPool

MODULE_PATH = Path(post_confirmation.__file__)


@pytest.fixture(autouse=True)
def _fresh_client() -> Iterator[None]:
    """The handler caches its client like any warm Lambda; each test starts from a clean cache."""
    post_confirmation.cognito_client.cache_clear()
    yield
    post_confirmation.cognito_client.cache_clear()


def trigger_event(user_pool: UserPool, username: str, trigger_source: str) -> dict[str, Any]:
    """The Post confirmation event Cognito sends (``request``/``response`` per the trigger documentation)."""
    return {
        "version": "1",
        "region": "us-east-1",
        "userPoolId": user_pool.pool_id,
        "userName": username,
        "callerContext": {"awsSdkVersion": "aws-sdk-unknown-unknown", "clientId": user_pool.client_id},
        "triggerSource": trigger_source,
        "request": {
            "userAttributes": {"sub": username, "email": "taro@example.com", "email_verified": "true"},
        },
        "response": {},
    }


def unconfirmed_user(user_pool: UserPool) -> str:
    """A user in no group; returns the Cognito username (the sub, because the pool signs in by e-mail)."""
    user = user_pool.idp.admin_create_user(
        UserPoolId=user_pool.pool_id,
        Username="taro@example.com",
        MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "email", "Value": "taro@example.com"}],
    )["User"]
    username: str = user["Username"]
    return username


def groups_of(user_pool: UserPool, username: str) -> list[str]:
    groups = user_pool.idp.admin_list_groups_for_user(UserPoolId=user_pool.pool_id, Username=username)["Groups"]
    return [group["GroupName"] for group in groups]


def test_confirmed_sign_up_joins_the_default_group(user_pool: UserPool) -> None:
    username = unconfirmed_user(user_pool)
    event = trigger_event(user_pool, username, "PostConfirmation_ConfirmSignUp")

    assert post_confirmation.handler(event, None) == event  # a trigger must return the event
    assert groups_of(user_pool, username) == ["employees"]


def test_a_cognito_retry_is_harmless(user_pool: UserPool) -> None:
    username = unconfirmed_user(user_pool)
    event = trigger_event(user_pool, username, "PostConfirmation_ConfirmSignUp")

    post_confirmation.handler(event, None)
    post_confirmation.handler(event, None)

    assert groups_of(user_pool, username) == ["employees"]


def test_default_group_comes_from_the_environment(user_pool: UserPool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_GROUP", "managers")
    username = unconfirmed_user(user_pool)

    post_confirmation.handler(trigger_event(user_pool, username, "PostConfirmation_ConfirmSignUp"), None)

    assert groups_of(user_pool, username) == ["managers"]


def test_password_reset_confirmation_changes_nothing(user_pool: UserPool) -> None:
    username = unconfirmed_user(user_pool)
    event = trigger_event(user_pool, username, "PostConfirmation_ConfirmForgotPassword")

    assert post_confirmation.handler(event, None) == event
    assert groups_of(user_pool, username) == []


def test_client_fails_fast_inside_cognitos_five_second_budget() -> None:
    config = post_confirmation.cognito_client().meta.config
    attempts = config.retries["total_max_attempts"]

    retry_backoff = 1  # "standard" retry mode waits a random 0-1 s before the first retry

    assert attempts * (config.connect_timeout + config.read_timeout) + retry_backoff <= 5


def test_imports_only_the_standard_library_and_boto3() -> None:
    """Deployed without the dependencies layer: an `expenseflow` or Powertools import would fail at cold start."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported |= {
        node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imported - sys.stdlib_module_names <= {"boto3", "botocore"}
