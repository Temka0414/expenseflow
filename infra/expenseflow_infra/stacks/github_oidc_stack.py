"""GitHub OIDC stack (optional, account-wide): lets GitHub Actions deploy without stored AWS keys."""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, DefaultStackSynthesizer, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig

GITHUB_OIDC_HOST: Final = "token.actions.githubusercontent.com"
GITHUB_OIDC_URL: Final = f"https://{GITHUB_OIDC_HOST}"
STS_AUDIENCE: Final = "sts.amazonaws.com"
"""`aud` claim that aws-actions/configure-aws-credentials requests for AWS."""
DEPLOY_BRANCH_REF: Final = "refs/heads/main"

CDK_BOOTSTRAP_QUALIFIER: Final = DefaultStackSynthesizer.DEFAULT_QUALIFIER
"""`hnb659fds`: the default qualifier of `cdk bootstrap`, part of every bootstrap resource name."""
# The roles `cdk bootstrap` creates and the CDK CLI assumes: deploy (CloudFormation changes),
# file/image publishing (asset uploads) and lookup (read-only context lookups, also used by diff).
_BOOTSTRAP_ROLES: Final = ("deploy-role", "file-publishing-role", "image-publishing-role", "lookup-role")


class GitHubOidcStack(Stack):
    """IAM OIDC identity provider for GitHub Actions plus the role the deploy workflow assumes.

    How keyless CI/CD works: for every job, GitHub signs a short-lived OIDC token (a JWT) describing
    the run (`sub` = repository + branch). `configure-aws-credentials` exchanges it with
    `sts:AssumeRoleWithWebIdentity` for 1-hour credentials. The role's trust policy decides which
    tokens are accepted, so no long-lived access key ever exists in GitHub secrets.

    Least privilege: the role itself can do nothing except assume the roles `cdk bootstrap` created.
    Those roles (and the CloudFormation execution role behind them) carry the actual deploy rights,
    exactly as when you run `npx cdk deploy` on your machine.

    Attributes:
        deploy_role: Role for `role-to-assume` in .github/workflows/deploy.yml (output `DeployRoleArn`).
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        if config.github_repo is None:
            raise ValueError("GitHubOidcStack needs a repository (-c githubRepo=owner/repo)")
        super().__init__(
            scope,
            construct_id,
            env=config.env,
            description=f"{PROJECT_NAME}: GitHub Actions OIDC provider and CDK deploy role (whole account)",
        )
        provider_arn = self._provider_arn(config.github_oidc_provider_arn)
        self.deploy_role = iam.Role(
            self,
            "DeployRole",
            description=f"Assumed by GitHub Actions ({config.github_repo}, main branch) to run cdk deploy",
            assumed_by=iam.WebIdentityPrincipal(
                provider_arn, conditions=trust_conditions(config.github_repo)
            ),
            max_session_duration=Duration.hours(1),
        )
        # The same two actions the bootstrap roles' trust policies allow (bootstrap template v32):
        # AssumeRole, plus TagSession for callers that attach session tags when chaining into them.
        self.deploy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=self._bootstrap_role_arns(),
            )
        )
        CfnOutput(
            self,
            "DeployRoleArn",
            value=self.deploy_role.role_arn,
            description="role-to-assume for the GitHub deploy workflow",
        )
        # configure-aws-credentials logs "Authenticated as assumedRoleId AROA...:GitHubActions" before it
        # masks the account ID, and a role's unique ID can be resolved back to its ARN (with the account ID).
        # deploy.yml masks it when this value is stored as the secret AWS_DEPLOY_ROLE_ID.
        CfnOutput(
            self,
            "DeployRoleId",
            value=self.deploy_role.role_id,
            description="unique id of the deploy role, masked in the public GitHub Actions log",
        )

    def _provider_arn(self, existing_arn: str | None) -> str:
        """Create GitHub's OIDC provider, or reuse an existing one (`-c githubOidcProviderArn=`).

        An account can register each provider URL only once; if another project already created
        it, creating it again fails, so pass its ARN instead.
        """
        if existing_arn is not None:
            return iam.OidcProviderNative.from_oidc_provider_arn(
                self, "GitHubProvider", existing_arn
            ).oidc_provider_arn
        # `OidcProviderNative` is the plain AWS::IAM::OIDCProvider resource (the older
        # `OpenIdConnectProvider` needs a custom-resource Lambda). No thumbprint: IAM validates
        # GitHub's TLS certificate against trusted root CAs.
        provider = iam.OidcProviderNative(
            self,
            "GitHubProvider",
            url=GITHUB_OIDC_URL,
            client_ids=[STS_AUDIENCE],
            removal_policy=RemovalPolicy.DESTROY,
        )
        return provider.oidc_provider_arn

    def _bootstrap_role_arns(self) -> list[str]:
        # Exact names instead of `role/cdk-hnb659fds-*`: only this account and Region's bootstrap
        # roles, and no wildcard resource to justify.
        return [
            f"arn:{self.partition}:iam::{self.account}:role/"
            f"cdk-{CDK_BOOTSTRAP_QUALIFIER}-{role}-{self.account}-{self.region}"
            for role in _BOOTSTRAP_ROLES
        ]


def trust_conditions(github_repo: str) -> dict[str, dict[str, object]]:
    """Trust-policy conditions accepting only main-branch runs of `github_repo`.

    `aud` must be exactly `sts.amazonaws.com`. GitHub sends `sub` in one of two formats:

    * classic `repo:OWNER/REPO:ref:refs/heads/main` (repositories created before 2026-07-15);
    * immutable `repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:refs/heads/main` (created after). The numeric
      ids never change, so a deleted-and-recreated repository with the same name cannot match.

    For `owner/repo` both formats are allowed (the ids are wildcards). For `owner@<id>/repo@<id>`
    only the exact immutable subject is allowed. Never use `repo:OWNER/*`: every branch and pull
    request run of every repository of that owner would get deploy access.
    """
    owner, repo = github_repo.split("/")
    if "@" in github_repo:
        subjects = [_subject(owner, repo)]
    else:
        subjects = [_subject(owner, repo), _subject(f"{owner}@*", f"{repo}@*")]
    return {
        "StringEquals": {f"{GITHUB_OIDC_HOST}:aud": STS_AUDIENCE},
        "StringLike": {f"{GITHUB_OIDC_HOST}:sub": subjects},
    }


def _subject(owner: str, repo: str) -> str:
    return f"repo:{owner}/{repo}:ref:{DEPLOY_BRANCH_REF}"
