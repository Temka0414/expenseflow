"""Optional account-wide stacks: AWS Budgets alerts and the GitHub Actions OIDC deploy role."""

from __future__ import annotations

from typing import Any

import pytest
from aws_cdk import App, Stack
from aws_cdk.assertions import Template

from app import build
from expenseflow_infra.config import StageConfig
from expenseflow_infra.stacks.budget_stack import BudgetStack
from expenseflow_infra.stacks.github_oidc_stack import GitHubOidcStack, trust_conditions
from tests.conftest import TEST_ACCOUNT, TEST_REGION, AppFactory, NagCheck

BUDGET = "ExpenseFlow-Budget"
GITHUB_OIDC = "ExpenseFlow-GitHubOidc"
EXISTING_PROVIDER_ARN = f"arn:aws:iam::{TEST_ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"


def _app(make_app: AppFactory, **context: object) -> App:
    app = make_app(**context)
    build(app)
    return app


def _stack_ids(app: App) -> set[str]:
    return {child.node.id for child in app.node.children if isinstance(child, Stack)}


def _template(app: App, stack_id: str) -> Template:
    stack = app.node.find_child(stack_id)
    assert isinstance(stack, Stack)
    return Template.from_stack(stack)


def _only_resource(template: Template, resource_type: str) -> dict[str, Any]:
    resources = template.find_resources(resource_type)
    assert len(resources) == 1, sorted(resources)
    (resource,) = resources.values()
    properties: dict[str, Any] = resource["Properties"]
    return properties


# --- Budget ------------------------------------------------------------------------------------


def test_optional_stacks_are_absent_by_default(make_app: AppFactory) -> None:
    assert _stack_ids(_app(make_app)).isdisjoint({BUDGET, GITHUB_OIDC})


def test_budget_alerts_on_actual_and_forecasted_cost(make_app: AppFactory) -> None:
    app = _app(make_app, budgetEmail="billing@example.com", monthlyBudgetUsd="25")
    budget = _only_resource(_template(app, BUDGET), "AWS::Budgets::Budget")

    assert budget["Budget"] == {
        "BudgetName": "ExpenseFlow-monthly-cost",
        "BudgetType": "COST",
        "TimeUnit": "MONTHLY",
        "BudgetLimit": {"Amount": 25, "Unit": "USD"},
        "CostTypes": {"IncludeCredit": False},
    }
    subscribers = [{"SubscriptionType": "EMAIL", "Address": "billing@example.com"}]
    assert budget["NotificationsWithSubscribers"] == [
        {
            "Notification": {
                "NotificationType": notification_type,
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": threshold,
                "ThresholdType": "PERCENTAGE",
            },
            "Subscribers": subscribers,
        }
        for notification_type, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
    ]


def test_budget_defaults_to_ten_dollars(make_app: AppFactory) -> None:
    budget = _only_resource(
        _template(_app(make_app, budgetEmail="c@example.com"), BUDGET), "AWS::Budgets::Budget"
    )

    assert budget["Budget"]["BudgetLimit"] == {"Amount": 10, "Unit": "USD"}


def test_budget_stack_requires_an_email(make_app: AppFactory) -> None:
    app = make_app()
    with pytest.raises(ValueError, match="budgetEmail"):
        BudgetStack(app, BUDGET, config=StageConfig.from_app(app))


def test_budget_stack_passes_cdk_nag(make_app: AppFactory, assert_nag_clean: NagCheck) -> None:
    app = _app(make_app, budgetEmail="c@example.com")
    _template(app, BUDGET)
    stack = app.node.find_child(BUDGET)
    assert isinstance(stack, Stack)
    assert_nag_clean(stack)


# --- GitHub OIDC -------------------------------------------------------------------------------


def _trust_statement(template: Template) -> dict[str, Any]:
    role = _only_resource(template, "AWS::IAM::Role")
    (statement,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert role["MaxSessionDuration"] == 3600
    result: dict[str, Any] = statement
    return result


def test_classic_repo_trusts_both_subject_formats_of_main(make_app: AppFactory) -> None:
    template = _template(_app(make_app, githubRepo="octo/expenseflow"), GITHUB_OIDC)
    statement = _trust_statement(template)

    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Effect"] == "Allow"
    assert statement["Condition"] == {
        "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
        "StringLike": {
            "token.actions.githubusercontent.com:sub": [
                "repo:octo/expenseflow:ref:refs/heads/main",
                "repo:octo@*/expenseflow@*:ref:refs/heads/main",
            ]
        },
    }
    provider_id = next(iter(template.find_resources("AWS::IAM::OIDCProvider")))
    assert statement["Principal"] == {"Federated": {"Ref": provider_id}}


def test_repo_with_ids_trusts_only_the_exact_immutable_subject() -> None:
    assert trust_conditions("octo@111/expenseflow@222") == {
        "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
        "StringLike": {
            "token.actions.githubusercontent.com:sub": ["repo:octo@111/expenseflow@222:ref:refs/heads/main"]
        },
    }


def test_new_provider_needs_no_thumbprint(make_app: AppFactory) -> None:
    template = _template(_app(make_app, githubRepo="octo/expenseflow"), GITHUB_OIDC)
    provider = _only_resource(template, "AWS::IAM::OIDCProvider")

    assert provider["Url"] == "https://token.actions.githubusercontent.com"
    assert provider["ClientIdList"] == ["sts.amazonaws.com"]
    assert "ThumbprintList" not in provider


def test_existing_provider_is_imported_by_arn(make_app: AppFactory) -> None:
    app = _app(make_app, githubRepo="octo/expenseflow", githubOidcProviderArn=EXISTING_PROVIDER_ARN)
    template = _template(app, GITHUB_OIDC)

    template.resource_count_is("AWS::IAM::OIDCProvider", 0)
    assert _trust_statement(template)["Principal"] == {"Federated": EXISTING_PROVIDER_ARN}


def test_deploy_role_can_only_assume_the_cdk_bootstrap_roles(make_app: AppFactory) -> None:
    template = _template(_app(make_app, githubRepo="octo/expenseflow"), GITHUB_OIDC)
    policy = _only_resource(template, "AWS::IAM::Policy")
    (statement,) = policy["PolicyDocument"]["Statement"]

    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["sts:AssumeRole", "sts:TagSession"]
    # Exact role names, no wildcard: the partition, account and Region are known at synth time.
    assert statement["Resource"] == [
        f"arn:aws:iam::{TEST_ACCOUNT}:role/cdk-hnb659fds-{role}-{TEST_ACCOUNT}-{TEST_REGION}"
        for role in ("deploy-role", "file-publishing-role", "image-publishing-role", "lookup-role")
    ]
    role_id = next(iter(template.find_resources("AWS::IAM::Role")))
    template.has_output("DeployRoleArn", {"Value": {"Fn::GetAtt": [role_id, "Arn"]}})
    # Stored as a secret so deploy.yml can mask the "assumedRoleId AROA..." log line.
    template.has_output("DeployRoleId", {"Value": {"Fn::GetAtt": [role_id, "RoleId"]}})


def test_github_oidc_stack_requires_a_repository(make_app: AppFactory) -> None:
    app = make_app()
    with pytest.raises(ValueError, match="githubRepo"):
        GitHubOidcStack(app, GITHUB_OIDC, config=StageConfig.from_app(app))


@pytest.mark.parametrize("provider_arn", [None, EXISTING_PROVIDER_ARN])
def test_github_oidc_stack_passes_cdk_nag(
    make_app: AppFactory, assert_nag_clean: NagCheck, provider_arn: str | None
) -> None:
    app = _app(make_app, githubRepo="octo/expenseflow", githubOidcProviderArn=provider_arn)
    _template(app, GITHUB_OIDC)
    stack = app.node.find_child(GITHUB_OIDC)
    assert isinstance(stack, Stack)
    assert_nag_clean(stack)
