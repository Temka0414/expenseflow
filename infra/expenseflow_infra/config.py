"""Stage configuration: CDK context parsed into one typed, validated object.

Every tunable of the app is read here, so stacks never call `try_get_context` themselves.
Context values arrive in two shapes:

* `cdk.json` "context" holds JSON values (numbers stay numbers);
* `npx cdk deploy -c autoApproveLimit=1000` always passes strings.

Both are accepted and an empty string means "not set". Invalid values fail `cdk synth` right away
with a message naming the `-c` key, instead of surfacing minutes later as a CloudFormation error.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aws_cdk import Environment, RemovalPolicy
from aws_cdk import aws_logs as logs
from constructs import Construct

from expenseflow_infra.paths import FRONTEND_DIR

PROJECT_NAME: Final = "ExpenseFlow"

LOCAL_DEV_ORIGIN: Final = "http://localhost:5173"
"""Vite dev-server origin; CORS allows it next to the CloudFront URL so the SPA can run locally."""

DISABLED: Final = "none"
"""Context value that switches a model off, e.g. `-c extractionModelId=none`."""

MAX_RECEIPT_BYTES: Final = 3_500_000
"""Bedrock accepts images up to 5 MB *base64-encoded*; base64 adds a third, so ~3.75 MB raw."""

CURRENCIES: Final = frozenset(
    {"JPY", "USD", "EUR", "GBP", "CNY", "KRW", "TWD", "HKD", "SGD", "AUD", "CAD", "CHF", "THB", "INR", "MNT"}
)
EXTRACTION_BASE_MODEL: Final = "anthropic.claude-opus-5-5"
FALLBACK_BASE_MODEL: Final = "anthropic.claude-opus-4-8"

DEFAULT_STAGE: Final = "dev"
DEFAULT_CURRENCY: Final = "JPY"
DEFAULT_AUTO_APPROVE_LIMIT: Final = 5000
DEFAULT_APPROVAL_TIMEOUT_MINUTES: Final = 3 * 24 * 60
DEFAULT_MONTHLY_BUDGET_USD: Final = 10

MAX_AMOUNT: Final = 100_000_000
MAX_APPROVAL_TIMEOUT_MINUTES: Final = 14 * 24 * 60
"""The approval workflow waits up to twice (normal + escalated) inside a 30-day execution timeout."""

_INFERENCE_PROFILE_PREFIXES: Final = frozenset({"jp", "au", "us", "eu", "apac", "global"})
_GEO_BY_REGION: Final = {
    "ap-northeast-1": "jp",  # Tokyo
    "ap-northeast-3": "jp",  # Osaka
    "ap-southeast-2": "au",  # Sydney
    "ap-southeast-4": "au",  # Melbourne
}
_GEO_BY_REGION_PREFIX: Final = (("us-", "us"), ("eu-", "eu"))

_STAGE_PATTERN: Final = re.compile(r"[a-z](?:[a-z0-9-]{0,14}[a-z0-9])?")
_REGION_PATTERN: Final = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d+")
_MODEL_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
_EMAIL_PATTERN: Final = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# `owner/repo`, or `owner@<id>/repo@<id>`: GitHub's immutable OIDC subject format, used by repositories
# created after 2026-07-15 (`gh api repos/OWNER/REPO --jq '.owner.id, .id'` prints the ids).
_GITHUB_REPO_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]+(?:@\d+)?/[A-Za-z0-9_.-]+(?:@\d+)?")
_OIDC_PROVIDER_ARN_PATTERN: Final = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:oidc-provider/\S+")
_INTEGER_PATTERN: Final = re.compile(r"-?\d+")


class ConfigError(ValueError):
    """An invalid CDK context value; the message names the `-c` key to fix."""


def inference_profile_prefix(region: str | None) -> str:
    """Return the Bedrock inference-profile prefix for calling Claude from `region`.

    Claude on Bedrock is invoked through cross-Region *inference profiles*. A geographic profile
    (`jp.`, `au.`, `us.`, `eu.`) only routes requests to Regions inside that geography (data
    residency), while `global.` may route anywhere for the most capacity. Tokyo has no in-Region
    capacity for the newest Claude models, so the `jp.` profile (Tokyo + Osaka) is what makes them
    work there. Regions without a geographic profile, or an unresolved Region, use `global`.
    """
    if region is None:
        return "global"
    if region in _GEO_BY_REGION:
        return _GEO_BY_REGION[region]
    for region_prefix, geo in _GEO_BY_REGION_PREFIX:
        if region.startswith(region_prefix):
            return geo
    return "global"


def foundation_model_id(model_id: str) -> str:
    """Strip an inference-profile prefix: `jp.anthropic.claude-opus-5-5` -> `anthropic.claude-opus-5-5`.

    IAM for an inference profile must allow both the profile ARN and the foundation-model ARN of
    every Region the profile can route to, so policies need the bare model id as well.
    """
    prefix, _, rest = model_id.partition(".")
    return rest if rest and prefix in _INFERENCE_PROFILE_PREFIXES else model_id


def default_model_id(base_model: str, *, bedrock_region: str | None) -> str:
    """The geo inference profile id bedrock-runtime needs, e.g. `jp.anthropic.claude-opus-5-5`."""
    return f"{inference_profile_prefix(bedrock_region)}.{base_model}"


@dataclass(frozen=True)
class StageConfig:
    """Everything that varies between deployments of ExpenseFlow (`-c stage=prod` makes a second copy)."""

    stage: str
    stage_title: str
    is_prod: bool
    account: str | None
    region: str | None
    alarm_email: str | None
    sender_email: str | None
    budget_email: str | None
    monthly_budget_usd: int
    bedrock_region: str | None
    """Region of the Bedrock endpoint; None means "the stack's own Region" (resolved at deploy time)."""
    extraction_model_id: str
    """Model id or inference profile for receipt extraction; `DISABLED` ("none") turns AI extraction off."""
    extraction_fallback_model_id: str | None
    default_currency: str
    auto_approve_limit: int
    approval_timeout_minutes: int
    max_receipt_bytes: int
    github_repo: str | None
    """`owner/repo` allowed to deploy through GitHub Actions OIDC; None skips the GitHubOidc stack."""
    github_oidc_provider_arn: str | None
    frontend_dist: Path
    log_retention: logs.RetentionDays
    removal_policy: RemovalPolicy

    @property
    def extraction_enabled(self) -> bool:
        """False when `-c extractionModelId=none`: receipts are stored but not read by Claude."""
        return self.extraction_model_id != DISABLED

    @property
    def env(self) -> Environment:
        """Deployment target. Without CDK_DEFAULT_* variables the templates are environment-agnostic.

        Gotcha: the CDK CLI always (re)sets CDK_DEFAULT_REGION before running `app.py`, from
        `--profile` / AWS_REGION / `~/.aws/config`, falling back to us-east-1; a value you export
        yourself is overwritten. CDK_DEFAULT_ACCOUNT is only set when credentials resolve.
        """
        return Environment(account=self.account, region=self.region)

    @property
    def tags(self) -> dict[str, str]:
        """Cost-allocation tags applied to every resource of the stage."""
        return {"Project": PROJECT_NAME, "Stage": self.stage, "ManagedBy": "CDK"}

    def stack_id(self, name: str) -> str:
        """Stack id (and CloudFormation stack name) such as `ExpenseFlow-Dev-Data`."""
        return f"{PROJECT_NAME}-{self.stage_title}-{name}"

    @classmethod
    def from_app(cls, app: Construct) -> StageConfig:
        """Read and validate the context of `app`; account and Region come from the CDK CLI's environment."""
        ctx = _Context(app)
        stage = ctx.matching(
            "stage", _STAGE_PATTERN, hint="1-16 lowercase letters, digits or '-', starting with a letter"
        )
        stage = stage or DEFAULT_STAGE
        is_prod = stage == "prod"
        region = os.environ.get("CDK_DEFAULT_REGION") or None

        bedrock_region = (
            ctx.matching("bedrockRegion", _REGION_PATTERN, hint="an AWS Region such as us-east-1") or region
        )
        extraction_model_id, fallback_model_id = _extraction_models(ctx, bedrock_region=bedrock_region)

        github_repo = ctx.matching(
            "githubRepo", _GITHUB_REPO_PATTERN, hint="owner/repo or owner@<id>/repo@<id>"
        )
        github_oidc_provider_arn = ctx.matching(
            "githubOidcProviderArn", _OIDC_PROVIDER_ARN_PATTERN, hint="an IAM OIDC provider ARN"
        )
        if github_oidc_provider_arn and not github_repo:
            raise ConfigError("-c githubOidcProviderArn is only used together with -c githubRepo=owner/repo")

        frontend_dist = ctx.text("frontendDist")
        email_hint = "an email address"
        return cls(
            stage=stage,
            stage_title=stage.capitalize(),
            is_prod=is_prod,
            account=os.environ.get("CDK_DEFAULT_ACCOUNT") or None,
            region=region,
            alarm_email=ctx.matching("alarmEmail", _EMAIL_PATTERN, hint=email_hint),
            sender_email=ctx.matching("senderEmail", _EMAIL_PATTERN, hint=email_hint),
            budget_email=ctx.matching("budgetEmail", _EMAIL_PATTERN, hint=email_hint),
            monthly_budget_usd=ctx.integer(
                "monthlyBudgetUsd", default=DEFAULT_MONTHLY_BUDGET_USD, minimum=1, maximum=100_000
            ),
            bedrock_region=bedrock_region,
            extraction_model_id=extraction_model_id,
            extraction_fallback_model_id=fallback_model_id,
            default_currency=ctx.choice(
                "defaultCurrency", sorted(CURRENCIES), default=DEFAULT_CURRENCY, normalize=str.upper
            ),
            auto_approve_limit=ctx.integer(
                "autoApproveLimit", default=DEFAULT_AUTO_APPROVE_LIMIT, minimum=0, maximum=MAX_AMOUNT
            ),
            approval_timeout_minutes=ctx.integer(
                "approvalTimeoutMinutes",
                default=DEFAULT_APPROVAL_TIMEOUT_MINUTES,
                minimum=1,
                maximum=MAX_APPROVAL_TIMEOUT_MINUTES,
            ),
            max_receipt_bytes=MAX_RECEIPT_BYTES,
            github_repo=github_repo,
            github_oidc_provider_arn=github_oidc_provider_arn,
            frontend_dist=(
                Path(frontend_dist).expanduser().resolve() if frontend_dist else FRONTEND_DIR / "dist"
            ),
            # Production keeps data and logs; dev stages clean up completely on `cdk destroy`.
            log_retention=logs.RetentionDays.SIX_MONTHS if is_prod else logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY,
        )


def _extraction_models(ctx: _Context, *, bedrock_region: str | None) -> tuple[str, str | None]:
    """Primary and fallback model ids for receipt extraction, defaulting to the derived ids.

    `-c extractionModelId=none` turns extraction off (and with it the fallback);
    `-c extractionFallbackModelId=none` keeps extraction but without a refusal fallback.
    """
    model_id = _model_setting(ctx, "extractionModelId") or default_model_id(
        EXTRACTION_BASE_MODEL, bedrock_region=bedrock_region
    )
    fallback_model_id = _model_setting(ctx, "extractionFallbackModelId") or default_model_id(
        FALLBACK_BASE_MODEL, bedrock_region=bedrock_region
    )
    if DISABLED in (model_id, fallback_model_id):
        return model_id, None
    return model_id, fallback_model_id


def _model_setting(ctx: _Context, key: str) -> str | None:
    """A model id from context; any spelling of "none" normalises to `DISABLED`."""
    value = ctx.matching(key, _MODEL_ID_PATTERN, hint=f"a Bedrock model id or '{DISABLED}'")
    if value is not None and value.lower() == DISABLED:
        return DISABLED
    return value


class _Context:
    """Typed accessors over `node.try_get_context` that accept JSON values and `-c` strings alike."""

    def __init__(self, scope: Construct) -> None:
        self._scope = scope

    def raw(self, key: str) -> object:
        value: object = self._scope.node.try_get_context(key)
        return value

    def text(self, key: str) -> str | None:
        value = self.raw(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ConfigError(f"-c {key}: expected a single value, got {value!r}")
        return str(value).strip() or None

    def matching(self, key: str, pattern: re.Pattern[str], *, hint: str) -> str | None:
        value = self.text(key)
        if value is not None and not pattern.fullmatch(value):
            raise ConfigError(f"-c {key}={value!r}: expected {hint}")
        return value

    def choice(
        self, key: str, choices: Collection[str], *, default: str, normalize: Callable[[str], str]
    ) -> str:
        value = self.text(key)
        if value is None:
            return default
        value = normalize(value)
        if value not in choices:
            raise ConfigError(f"-c {key}={value!r}: expected one of {', '.join(choices)}")
        return value

    def integer(self, key: str, *, default: int, minimum: int, maximum: int) -> int:
        value = self.raw(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        number = _as_int(value)
        if number is None:
            raise ConfigError(f"-c {key}={value!r}: expected a whole number")
        if not minimum <= number <= maximum:
            raise ConfigError(f"-c {key}={number}: must be between {minimum} and {maximum}")
        return number


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        return int(value)
    return None
