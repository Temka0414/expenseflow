"""Shared plumbing for the helper scripts in this folder.

Every script targets one *stage* (an independent copy of the whole app, such as ``dev``) in one AWS
account and region. Nothing is hard-coded: the scripts read the CloudFormation **stack outputs**
(``CfnOutput``) of the stacks named ``ExpenseFlow-{Stage}-{Name}``, which CDK published on deploy.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent

_SIGN_IN_HINT = (
    "If this is a sign-in problem, sign in again and pass the same --profile:\n"
    "  aws login --profile <name>        (AWS CLI 2.32+)\n"
    "  aws sso login --profile <name>    (IAM Identity Center)\n"
    "Profiles made by 'aws login' also need:  python -m pip install \"botocore[crt]\""
)


class ScriptError(Exception):
    """A failure whose message is meant for the person running the script (no traceback)."""


class StackOutputs(dict[str, str]):
    """The outputs of one stack; a missing key explains itself instead of raising a bare KeyError."""

    def __init__(self, stack: str, status: str, values: dict[str, str]) -> None:
        super().__init__(values)
        self.stack, self.status = stack, status

    def __missing__(self, key: str) -> str:
        raise ScriptError(
            f"Stack {self.stack} (status {self.status}) has no output '{key}'. "
            "Is it fully deployed, and is the deployed version up to date with these scripts?"
        )


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the ``--stage/--profile/--region`` options every script shares."""
    parser.add_argument("--stage", default="dev", help="stage, as in 'cdk deploy -c stage=...' (default: dev)")
    parser.add_argument("--profile", help="AWS CLI profile (default: AWS_PROFILE, else the default credentials)")
    parser.add_argument("--region", help="AWS region (default: AWS_REGION, else the profile's region)")


def create_session(args: argparse.Namespace) -> boto3.session.Session:
    """A boto3 session for the ``--profile/--region`` options (boto3 itself ignores ``AWS_REGION``)."""
    region = args.region or os.environ.get("AWS_REGION")
    session = boto3.session.Session(profile_name=args.profile, region_name=region)
    if not session.region_name:
        raise ScriptError("No AWS region. Pass --region (e.g. ap-northeast-1) or set AWS_REGION.")
    return session


def stack_outputs(session: boto3.session.Session, stage: str, name: str) -> StackOutputs:
    """The outputs of ``ExpenseFlow-{Stage}-{name}`` (e.g. ``ExpenseFlow-Dev-Auth``)."""
    stack_name = f"ExpenseFlow-{stage.capitalize()}-{name}"
    try:
        stack = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0]
    except ClientError as exc:
        if "does not exist" in str(exc):  # CloudFormation reports a missing stack as a ValidationError
            raise ScriptError(
                f"Stack {stack_name} does not exist in {session.region_name}. Deploy first "
                "(scripts/deploy.ps1 or scripts/deploy.sh) or check --stage, --region and --profile."
            ) from exc
        raise
    values = {str(o["OutputKey"]): str(o["OutputValue"]) for o in stack.get("Outputs", [])}
    return StackOutputs(stack_name, str(stack["StackStatus"]), values)


def read_auto_approve_limit(session: boto3.session.Session, stage: str) -> int:
    """The live auto-approval limit from SSM Parameter Store (the workflow reads it on every run)."""
    name = f"/expenseflow/{stage}/auto-approve-limit"
    try:
        value = session.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            raise ScriptError(f"SSM parameter {name} does not exist. Is the Backend stack deployed?") from exc
        raise
    try:
        return int(value)
    except ValueError:
        raise ScriptError(f"SSM parameter {name} is {value!r}, not a whole number.") from None


def run_script(main: Callable[[], int]) -> NoReturn:
    """Run ``main`` and turn expected failures into a short message and a non-zero exit code."""
    # Never crash on characters a console cannot show (e.g. cp932 on Japanese Windows).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        sys.exit(main())
    except ScriptError as exc:
        message = str(exc)
    except ClientError as exc:
        message = f"AWS rejected a request: {exc}"
        if exc.response.get("Error", {}).get("Code") in {"ExpiredToken", "ExpiredTokenException"}:
            message += f"\n{_SIGN_IN_HINT}"
    except BotoCoreError as exc:  # no credentials, unknown profile, expired SSO session, no network, ...
        message = f"{exc}\n{_SIGN_IN_HINT}"
    except RuntimeError as exc:
        # botocore raises a plain RuntimeError when temporary credentials (e.g. ones exported into the
        # shell with `aws configure export-credentials`) have expired and cannot be refreshed.
        if "expired" not in str(exc).lower():
            raise
        message = f"Your temporary AWS credentials have expired.\n{_SIGN_IN_HINT}"
    except KeyboardInterrupt:
        message = "Interrupted."
    print(f"\nERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)
