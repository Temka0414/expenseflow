from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from _common import ScriptError, add_target_arguments, create_session, read_auto_approve_limit, stack_outputs


def _client(service: str) -> Any:
    """A real boto3 client whose answers come from a Stubber (nothing leaves the PC)."""
    return boto3.client(service, region_name="ap-northeast-1", aws_access_key_id="x", aws_secret_access_key="x")


def _session(client: Any) -> Any:
    return SimpleNamespace(region_name="ap-northeast-1", client=lambda _service: client)


def _stack(status: str, outputs: dict[str, str]) -> dict[str, Any]:
    return {
        "Stacks": [
            {
                "StackName": "ExpenseFlow-Dev-Auth",
                "CreationTime": "2026-10-14T00:00:00Z",
                "StackStatus": status,
                "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
            }
        ]
    }


def test_stack_outputs_reads_the_stack_of_the_stage() -> None:
    cloudformation = _client("cloudformation")
    with Stubber(cloudformation) as stubber:
        stubber.add_response(
            "describe_stacks", _stack("CREATE_COMPLETE", {"UserPoolId": "pool"}), {"StackName": "ExpenseFlow-Dev-Auth"}
        )
        outputs = stack_outputs(_session(cloudformation), "dev", "Auth")

    assert outputs["UserPoolId"] == "pool"


def test_a_missing_output_names_the_stack_and_its_status() -> None:
    cloudformation = _client("cloudformation")
    with Stubber(cloudformation) as stubber:
        stubber.add_response("describe_stacks", _stack("ROLLBACK_COMPLETE", {}))
        outputs = stack_outputs(_session(cloudformation), "dev", "Auth")

    with pytest.raises(ScriptError, match=r"ExpenseFlow-Dev-Auth.*ROLLBACK_COMPLETE.*'UserPoolId'"):
        outputs["UserPoolId"]


def test_a_missing_stack_says_to_deploy_first() -> None:
    cloudformation = _client("cloudformation")
    with Stubber(cloudformation) as stubber:
        stubber.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id ExpenseFlow-Dev-Auth does not exist",
        )
        with pytest.raises(ScriptError, match=r"does not exist.*Deploy first"):
            stack_outputs(_session(cloudformation), "dev", "Auth")


def test_auto_approve_limit_is_read_from_ssm() -> None:
    ssm = _client("ssm")
    with Stubber(ssm) as stubber:
        stubber.add_response(
            "get_parameter", {"Parameter": {"Value": "5000"}}, {"Name": "/expenseflow/dev/auto-approve-limit"}
        )
        assert read_auto_approve_limit(_session(ssm), "dev") == 5000


def test_auto_approve_limit_must_be_a_number() -> None:
    ssm = _client("ssm")
    with Stubber(ssm) as stubber:
        stubber.add_response("get_parameter", {"Parameter": {"Value": "lots"}})
        with pytest.raises(ScriptError, match="not a whole number"):
            read_auto_approve_limit(_session(ssm), "dev")


def test_target_options_default_to_the_dev_stage() -> None:
    parser = argparse.ArgumentParser()
    add_target_arguments(parser)

    args = parser.parse_args([])

    assert (args.stage, args.profile, args.region) == ("dev", None, None)


def test_region_falls_back_to_aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """boto3 itself only reads AWS_DEFAULT_REGION, but the CDK docs and the CI workflow use AWS_REGION."""
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    session = create_session(argparse.Namespace(profile=None, region=None))

    assert session.region_name == "eu-west-1"
