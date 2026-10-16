"""Cognito Post confirmation trigger: put every newly confirmed user into the default group (SPEC 7.7).

Deliberately dependency-free (standard library + boto3, which the Lambda runtime provides). This function lives in
the Auth stack and is deployed WITHOUT the dependencies layer: it must not import ``expenseflow`` or Powertools.

Cognito trigger facts worth knowing:

* Cognito waits **5 seconds** for a trigger (not adjustable) and may retry on timeout. The client below therefore
  uses short timeouts and few attempts, so a slow call fails inside the budget instead of being cut off.
* Post confirmation also fires after a password reset (``PostConfirmation_ConfirmForgotPassword``); only a sign-up
  confirmation (``PostConfirmation_ConfirmSignUp``) adds the group. Users created by an admin never trigger it.
* The trigger must return the event it received. Adding a user to a group they are already in succeeds, so a
  Cognito retry is harmless (the call is idempotent).
"""

from __future__ import annotations

import logging
import os
from functools import cache
from typing import Any, Final

import boto3
from botocore.config import Config

SIGN_UP_CONFIRMED: Final = "PostConfirmation_ConfirmSignUp"
_DEFAULT_GROUP: Final = "employees"
# Worst case: 2 attempts x (1 s connect + 1 s read) + at most 1 s of retry backoff ("standard" mode waits a random
# 0-1 s before the first retry) = 5 s. A typical AdminAddUserToGroup call takes ~50 ms.
_CLIENT_CONFIG: Final = Config(connect_timeout=1, read_timeout=1, retries={"mode": "standard", "total_max_attempts": 2})

logger = logging.getLogger(__name__)


@cache
def cognito_client() -> Any:
    """Created on first use and reused while the execution environment stays warm."""
    return boto3.client("cognito-idp", config=_CLIENT_CONFIG)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("triggerSource") != SIGN_UP_CONFIRMED:
        return event
    group = os.environ.get("DEFAULT_GROUP") or _DEFAULT_GROUP
    # The pool id comes from the event: an environment variable would need the pool's id while the pool needs this
    # function's ARN - a circular dependency between the two CloudFormation resources.
    cognito_client().admin_add_user_to_group(
        UserPoolId=event["userPoolId"], Username=event["userName"], GroupName=group
    )
    logger.info("added user %s to group %s", event["userName"], group)
    return event
