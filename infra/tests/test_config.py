"""StageConfig: CDK context parsing, derived Bedrock defaults and validation errors."""

from __future__ import annotations

import re

import pytest
from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs

from expenseflow_infra.config import (
    DISABLED,
    ConfigError,
    StageConfig,
    foundation_model_id,
    inference_profile_prefix,
)
from expenseflow_infra.paths import FRONTEND_DIR
from tests.conftest import TEST_ACCOUNT, TEST_REGION, AppFactory


def test_defaults_from_cdk_json(make_app: AppFactory) -> None:
    config = StageConfig.from_app(make_app())

    assert (config.stage, config.stage_title, config.is_prod) == ("dev", "Dev", False)
    assert (config.account, config.region) == (TEST_ACCOUNT, TEST_REGION)
    assert config.stack_id("Data") == "ExpenseFlow-Dev-Data"
    assert config.tags == {"Project": "ExpenseFlow", "Stage": "dev", "ManagedBy": "CDK"}
    assert config.default_currency == "JPY"
    assert config.auto_approve_limit == 5000
    assert config.approval_timeout_minutes == 4320
    assert config.monthly_budget_usd == 10
    assert config.max_receipt_bytes == 3_500_000
    assert config.alarm_email is None
    assert config.sender_email is None
    assert config.budget_email is None
    assert config.github_repo is None
    assert config.log_retention == logs.RetentionDays.ONE_MONTH
    assert config.removal_policy == RemovalPolicy.DESTROY


def test_geo_inference_profiles_derive_from_the_stack_region(make_app: AppFactory) -> None:
    config = StageConfig.from_app(make_app())

    assert config.bedrock_region == TEST_REGION
    assert config.extraction_model_id == "jp.anthropic.claude-opus-5-5"
    assert config.extraction_fallback_model_id == "jp.anthropic.claude-opus-4-8"
    assert config.extraction_enabled


def test_explicit_bedrock_region_drives_the_profile_prefix(make_app: AppFactory) -> None:
    config = StageConfig.from_app(make_app(bedrockRegion="us-west-2"))

    assert config.bedrock_region == "us-west-2"
    assert config.extraction_model_id == "us.anthropic.claude-opus-5-5"


def test_environment_agnostic_synth_uses_the_global_profile(
    make_app: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT")
    monkeypatch.delenv("CDK_DEFAULT_REGION")

    config = StageConfig.from_app(make_app())

    assert (config.account, config.region, config.bedrock_region) == (None, None, None)
    assert config.extraction_model_id == "global.anthropic.claude-opus-5-5"


@pytest.mark.parametrize(
    ("region", "prefix"),
    [
        ("ap-northeast-1", "jp"),
        ("ap-northeast-3", "jp"),
        ("ap-southeast-2", "au"),
        ("ap-southeast-4", "au"),
        ("us-east-1", "us"),
        ("eu-central-1", "eu"),
        ("sa-east-1", "global"),
        (None, "global"),
    ],
)
def test_inference_profile_prefix(region: str | None, prefix: str) -> None:
    assert inference_profile_prefix(region) == prefix


@pytest.mark.parametrize(
    ("model_id", "base"),
    [
        ("jp.anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"),
        ("global.anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"),
        ("anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"),
    ],
)
def test_foundation_model_id_strips_the_profile_prefix(model_id: str, base: str) -> None:
    assert foundation_model_id(model_id) == base


def test_extraction_model_none_disables_extraction_and_fallback(make_app: AppFactory) -> None:
    config = StageConfig.from_app(make_app(extractionModelId="None"))

    assert config.extraction_model_id == DISABLED
    assert not config.extraction_enabled
    assert config.extraction_fallback_model_id is None


def test_fallback_none_keeps_extraction_without_fallback(make_app: AppFactory) -> None:
    config = StageConfig.from_app(make_app(extractionFallbackModelId="none"))

    assert config.extraction_enabled
    assert config.extraction_fallback_model_id is None


def test_cli_strings_are_parsed_and_empty_strings_mean_unset(make_app: AppFactory) -> None:
    # `npx cdk synth -c autoApproveLimit=1000 -c alarmEmail=` passes every value as a string.
    app = make_app(
        autoApproveLimit="1000",
        approvalTimeoutMinutes=" 5 ",
        monthlyBudgetUsd="",
        defaultCurrency="usd",
        alarmEmail="",
        senderEmail="noreply@example.com",
    )
    config = StageConfig.from_app(app)

    assert config.auto_approve_limit == 1000
    assert config.approval_timeout_minutes == 5
    assert config.monthly_budget_usd == 10
    assert config.default_currency == "USD"
    assert config.alarm_email is None
    assert config.sender_email == "noreply@example.com"


def test_prod_stage_retains_data_and_keeps_logs_longer(make_app: AppFactory) -> None:
    config = StageConfig.from_app(make_app(stage="prod"))

    assert config.is_prod
    assert config.stack_id("Edge") == "ExpenseFlow-Prod-Edge"
    assert config.removal_policy == RemovalPolicy.RETAIN
    assert config.log_retention == logs.RetentionDays.SIX_MONTHS


def test_frontend_dist_defaults_to_the_repository_build(make_app: AppFactory) -> None:
    config = StageConfig.from_app(make_app(frontendDist=""))

    assert config.frontend_dist == FRONTEND_DIR / "dist"


def test_github_settings(make_app: AppFactory) -> None:
    arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    app = make_app(githubRepo="octo-org@123/expenseflow@456", githubOidcProviderArn=arn)
    config = StageConfig.from_app(app)

    assert config.github_repo == "octo-org@123/expenseflow@456"
    assert config.github_oidc_provider_arn == arn


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stage": "Prod!"}, "-c stage="),
        ({"stage": "x" * 17}, "-c stage="),
        ({"autoApproveLimit": "lots"}, "-c autoApproveLimit="),
        ({"autoApproveLimit": True}, "-c autoApproveLimit="),
        ({"autoApproveLimit": -1}, "-c autoApproveLimit="),
        ({"approvalTimeoutMinutes": 0}, "-c approvalTimeoutMinutes="),
        ({"monthlyBudgetUsd": 2.5}, "-c monthlyBudgetUsd="),
        ({"defaultCurrency": "XYZ"}, "-c defaultCurrency="),
        ({"bedrockRegion": "tokyo"}, "-c bedrockRegion="),
        ({"extractionModelId": "claude opus"}, "-c extractionModelId="),
        ({"alarmEmail": "not-an-email"}, "-c alarmEmail="),
        ({"budgetEmail": ["a@example.com"]}, "-c budgetEmail:"),
        ({"githubRepo": "just-a-name"}, "-c githubRepo="),
        ({"githubOidcProviderArn": "arn:aws:iam::123456789012:oidc-provider/x"}, "githubRepo"),
    ],
)
def test_invalid_values_fail_with_the_context_key(
    make_app: AppFactory, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        StageConfig.from_app(make_app(**overrides))
