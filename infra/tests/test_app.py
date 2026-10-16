"""The whole CDK app as `cdk synth` builds it: stack set, wiring, tags and cdk-nag on every stack."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aws_cdk import App, Stack
from aws_cdk import aws_sqs as sqs
from aws_cdk.assertions import Match, Template
from cdk_nag import AwsSolutionsChecks

from app import build
from tests.conftest import AppFactory

CORE_STACKS = ["Edge", "Data", "Auth", "Backend", "Frontend"]
ALL_OPTIONAL = {
    "alarmEmail": "a@example.com",
    "senderEmail": "b@example.com",
    "budgetEmail": "c@example.com",
    "githubRepo": "octo/expenseflow",
}


def _app(make_app: AppFactory, **context: object) -> App:
    app = make_app(**context)
    build(app)
    return app


def _stacks(app: App) -> dict[str, Stack]:
    return {child.node.id: child for child in app.node.children if isinstance(child, Stack)}


def _nag_findings(app: App) -> list[str]:
    """Unacknowledged cdk-nag findings of every stack (call after synthesis)."""
    return [
        f"{violation.rule_name}: {found.construct_path}"
        for stack in _stacks(app).values()
        for violation in AwsSolutionsChecks(verbose=True).validate_scope(stack).violations
        for found in violation.violating_resources
    ]


def test_default_app_has_the_five_stage_stacks(make_app: AppFactory) -> None:
    app = _app(make_app)

    assert list(_stacks(app)) == [f"ExpenseFlow-Dev-{name}" for name in CORE_STACKS]


def test_stage_selects_a_separate_copy(make_app: AppFactory) -> None:
    app = _app(make_app, stage="prod")

    assert list(_stacks(app)) == [f"ExpenseFlow-Prod-{name}" for name in CORE_STACKS]


def test_optional_context_adds_the_account_wide_stacks(make_app: AppFactory) -> None:
    app = _app(make_app, **ALL_OPTIONAL)

    assert list(_stacks(app))[-2:] == ["ExpenseFlow-Budget", "ExpenseFlow-GitHubOidc"]


def test_stacks_deploy_in_dependency_order(make_app: AppFactory) -> None:
    app = _app(make_app)
    app.synth()  # references between stacks become dependencies while the app is prepared
    prefix = "ExpenseFlow-Dev-"
    dependencies = {
        stack_id.removeprefix(prefix): {dep.node.id.removeprefix(prefix) for dep in stack.dependencies}
        for stack_id, stack in _stacks(app).items()
    }

    assert dependencies == {
        "Edge": set(),
        "Data": {"Edge"},
        "Auth": set(),
        "Backend": {"Edge", "Data", "Auth"},
        "Frontend": {"Edge", "Data", "Auth", "Backend"},
    }


def test_frontend_waits_for_the_backend_even_without_a_build(make_app: AppFactory, tmp_path: Path) -> None:
    app = _app(make_app, frontendDist=str(tmp_path / "missing"))
    app.synth()

    assert {dep.node.id for dep in _stacks(app)["ExpenseFlow-Dev-Frontend"].dependencies} == {
        "ExpenseFlow-Dev-Backend"
    }


@pytest.mark.parametrize(
    "context",
    [
        pytest.param({}, id="defaults"),
        pytest.param(ALL_OPTIONAL, id="all-optional"),
        pytest.param({"extractionModelId": "none"}, id="extraction-off"),
        pytest.param({"stage": "prod", **ALL_OPTIONAL}, id="prod"),
    ],
)
def test_every_stack_passes_cdk_nag(make_app: AppFactory, context: dict[str, object]) -> None:
    app = _app(make_app, **context)
    # build() registered cdk-nag on the App, exactly like `cdk synth`: any finding raises here.
    app.synth()

    assert _nag_findings(app) == []


def test_synth_without_an_account_passes_cdk_nag(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI synthesizes without AWS credentials; the CDK CLI then sets no CDK_DEFAULT_ACCOUNT."""
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT")
    app = _app(make_app, **ALL_OPTIONAL)
    app.synth()

    assert _nag_findings(app) == []


def test_cdk_nag_is_registered_for_the_whole_app(make_app: AppFactory) -> None:
    app = _app(make_app)
    # A queue without enforce_ssl or a DLQ, added after build(): cdk-nag must still catch it.
    sqs.Queue(_stacks(app)["ExpenseFlow-Dev-Auth"], "UnreviewedQueue")

    with pytest.raises(RuntimeError, match="ValidationFailed"):
        app.synth()


def test_missing_frontend_build_still_synthesizes(make_app: AppFactory, tmp_path: Path) -> None:
    app = _app(make_app, frontendDist=str(tmp_path / "missing"))
    app.synth()

    assert _nag_findings(app) == []
    frontend = Template.from_stack(_stacks(app)["ExpenseFlow-Dev-Frontend"])
    frontend.resource_count_is("AWS::CloudFormation::WaitConditionHandle", 1)


def test_cross_stack_references_are_weak(make_app: AppFactory) -> None:
    """No CloudFormation Exports: `Fn::GetStackOutput` never locks a producer stack's outputs."""
    app = _app(make_app)
    assembly = app.synth()

    for stack in assembly.stacks:
        template = json.dumps(stack.template)
        assert "Fn::ImportValue" not in template, stack.stack_name
        assert all("Export" not in output for output in stack.template.get("Outputs", {}).values())


def test_tags_reach_every_stack(make_app: AppFactory) -> None:
    stacks = _stacks(_app(make_app, **ALL_OPTIONAL))
    tags = Match.array_with(
        [
            {"Key": "ManagedBy", "Value": "CDK"},
            {"Key": "Project", "Value": "ExpenseFlow"},
            {"Key": "Stage", "Value": "dev"},
        ]
    )

    Template.from_stack(stacks["ExpenseFlow-Dev-Edge"]).has_resource_properties(
        "AWS::CloudFront::Distribution", {"Tags": tags}
    )
    Template.from_stack(stacks["ExpenseFlow-Dev-Data"]).has_resource_properties(
        "AWS::DynamoDB::GlobalTable", {"Replicas": [Match.object_like({"Tags": tags})]}
    )
    Template.from_stack(stacks["ExpenseFlow-GitHubOidc"]).has_resource_properties(
        "AWS::IAM::Role", {"Tags": tags}
    )
