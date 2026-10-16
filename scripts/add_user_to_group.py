"""Add a signed-up user to a Cognito group (``employees``, ``managers`` or ``admins``).

New users land in ``employees`` automatically (the post-confirmation Lambda trigger). Promote
yourself to manager or admin with this script; the HTTP API reads the groups from the
``cognito:groups`` claim of the ID token.

Example (PowerShell)::

    python scripts/add_user_to_group.py --stage dev --profile expenseflow `
        --email you@example.com --group managers
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any

from botocore.exceptions import ClientError

from _common import ScriptError, add_target_arguments, create_session, run_script, stack_outputs

GROUPS = ("employees", "managers", "admins")


def email_filter(email: str) -> str:
    """Build a ListUsers filter: Cognito's mini-language is ``attribute = "value"`` (exact match)."""
    if "@" not in email or any(char in email for char in '"\\'):
        raise ScriptError(f"{email!r} does not look like an e-mail address.")
    return f'email = "{email}"'


def find_user(cognito: Any, pool_id: str, email: str) -> dict[str, Any]:
    """Return the ListUsers entry of the single user whose ``email`` attribute matches."""
    response = cognito.list_users(UserPoolId=pool_id, Filter=email_filter(email), Limit=2)
    users: list[dict[str, Any]] = response["Users"]
    if not users:
        raise ScriptError(
            f"No user with e-mail {email} in user pool {pool_id} (the match is exact). "
            "Sign up in the app first and confirm the e-mail code."
        )
    if len(users) > 1:
        raise ScriptError(f"More than one user has e-mail {email}; use the Cognito console.")
    return users[0]


def add_to_group(cognito: Any, pool_id: str, username: str, group: str) -> list[str]:
    """Add the user to ``group`` (idempotent) and return all of the user's groups."""
    try:
        cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            raise ScriptError(f"Cognito says: {exc}. Is the Auth stack (which creates the groups) deployed?") from exc
        raise
    response = cognito.admin_list_groups_for_user(UserPoolId=pool_id, Username=username)
    return sorted(str(g["GroupName"]) for g in response["Groups"])


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add an existing ExpenseFlow user (looked up by e-mail) to a Cognito group.",
    )
    add_target_arguments(parser)
    parser.add_argument("--email", required=True, help="e-mail address the user signed up with")
    parser.add_argument("--group", required=True, choices=GROUPS, help="group to add the user to")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    session = create_session(args)
    pool_id = stack_outputs(session, args.stage, "Auth")["UserPoolId"]
    cognito = session.client("cognito-idp")

    user = find_user(cognito, pool_id, args.email)
    username = str(user["Username"])
    status = str(user.get("UserStatus", "UNKNOWN"))
    if status != "CONFIRMED":
        print(f"Note: the user's status is {status}; they cannot sign in until it is CONFIRMED.")

    groups = add_to_group(cognito, pool_id, username, args.group)
    print(f"Added {args.email} to '{args.group}' in user pool {pool_id}.")
    print(f"Groups now: {', '.join(groups)}")
    print(
        "\nThe groups are copied into the cognito:groups claim when tokens are ISSUED, so the\n"
        "user must sign out and sign in again (or wait for the hourly token refresh) before the\n"
        "API sees the change. Live updates also need a reconnect: the WebSocket's audiences are\n"
        "decided once, at $connect."
    )
    return 0


if __name__ == "__main__":
    run_script(main)
