"""Frontend stack: publishes the built SPA (`frontend/dist`) and its `config.json` to the site bucket."""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnWaitConditionHandle, Stack, Validations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.nag import LAMBDA_BASIC_EXECUTION_ROLE, acknowledge, acknowledge_wildcards
from expenseflow_infra.stacks.auth_stack import AuthStack
from expenseflow_infra.stacks.backend_stack import BackendStack
from expenseflow_infra.stacks.data_stack import DataStack
from expenseflow_infra.stacks.edge_stack import EdgeStack

CONFIG_FILE: Final = "config.json"
ENTRY_FILE: Final = "index.html"

# Vite names every built asset after a hash of its content (`assets/index-B4x9.js`): a new build
# produces new file names, so browsers and CloudFront may keep these files forever. Files copied
# from `public/` (e.g. a favicon) keep their names: rename such a file when its content changes.
_IMMUTABLE: Final = s3deploy.CacheControl.from_string("public, max-age=31536000, immutable")
# `index.html` and `config.json` keep their names across releases: "no-cache" makes browsers
# revalidate them on every load, so a deployment is visible on the next page refresh.
_REVALIDATE: Final = s3deploy.CacheControl.no_cache()

_DEPLOYMENT_MEMORY_MB: Final = 512
"""Both deployments use the same size, so they share ONE handler Lambda (a per-stack singleton)."""

MISSING_BUILD_WARNING: Final = "FrontendBuildMissing"
"""Warning id; `--strict` synths can acknowledge it as `Annotation::FrontendBuildMissing`."""


class FrontendStack(Stack):
    """Publishes the web app: hashed assets, then `index.html` + a generated `config.json`.

    `BucketDeployment` is a CloudFormation *custom resource*: during `cdk deploy`, a CDK-managed
    Lambda copies the zipped `frontend/dist` asset from the bootstrap bucket into the site bucket and
    can invalidate the CloudFront cache afterwards.

    The SPA reads `config.json` at startup instead of baking API URLs into the JavaScript bundle at
    build time. One build therefore runs against any stage, and the values (user pool id, API URLs,
    ...) only exist after the other stacks deployed: they are *deploy-time tokens*, which
    `Source.json_data` writes into the file during the deployment.

    If `frontend_dist/index.html` is missing, nothing is uploaded and synth prints a warning; the
    other stacks still deploy (useful while working on the backend only).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        edge: EdgeStack,
        data: DataStack,
        auth: AuthStack,
        backend: BackendStack,
    ) -> None:
        """Create the stack.

        Args:
            config: Stage settings; `frontend_dist` is the Vite build output to publish.
            edge: Site bucket (destination) and CloudFront distribution (cache invalidation).
            data: Table and bucket names for the "AWS console" links in `config.json`.
            auth: User pool and app client the SPA signs in with.
            backend: API URLs and resource names for `config.json`.
        """
        super().__init__(
            scope,
            construct_id,
            env=config.env,
            description=f"{PROJECT_NAME} ({config.stage}): web app files and config.json in the site bucket",
        )
        if not (config.frontend_dist / ENTRY_FILE).is_file():
            self._skip_deployment(config)
            return

        dist = str(config.frontend_dist)
        # One explicit log group for the shared handler; otherwise Lambda creates one that never expires.
        log_group = logs.LogGroup(
            self,
            "DeploymentLogs",
            retention=config.log_retention,
            removal_policy=config.removal_policy,
        )
        # `prune=False` on both deployments: neither deletes files the other uploaded, and files of
        # the previous release stay available. A browser that still runs the old `index.html` can then
        # lazy-load its old chunks instead of failing in the middle of a release.
        assets = s3deploy.BucketDeployment(
            self,
            "DeployAssets",
            sources=[s3deploy.Source.asset(dist, exclude=[ENTRY_FILE, CONFIG_FILE, "*.map"])],
            destination_bucket=edge.site_bucket,
            cache_control=[_IMMUTABLE],
            prune=False,
            memory_limit=_DEPLOYMENT_MEMORY_MB,
            log_group=log_group,
        )
        # `cache_control` applies to every file of a deployment, hence a second one for the two
        # mutable files. Gitignore-style patterns: exclude everything, then re-include index.html.
        entry = s3deploy.BucketDeployment(
            self,
            "DeployEntry",
            sources=[
                s3deploy.Source.asset(dist, exclude=["*", f"!{ENTRY_FILE}"]),
                s3deploy.Source.json_data(CONFIG_FILE, app_config(config, edge, data, auth, backend)),
            ],
            destination_bucket=edge.site_bucket,
            cache_control=[_REVALIDATE],
            prune=False,
            memory_limit=_DEPLOYMENT_MEMORY_MB,
            log_group=log_group,
            # Only the two mutable files can be stale at the edge; hashed assets never are.
            distribution=edge.distribution,
            distribution_paths=[f"/{ENTRY_FILE}", f"/{CONFIG_FILE}"],
        )
        # Upload the assets first: the new index.html must never reference files not yet uploaded.
        entry.node.add_dependency(assets)
        self._acknowledge_deployment_handler(edge.site_bucket, [assets, entry])

    def _skip_deployment(self, config: StageConfig) -> None:
        Validations.of(self).add_warning(
            MISSING_BUILD_WARNING,
            f"No frontend build at {config.frontend_dist}: run `npm run build` in frontend/ first. "
            "The Frontend stack uploads nothing until then (files already in the site bucket stay).",
        )
        # CloudFormation rejects a template without resources (validation rule F0001). This no-op
        # resource keeps the stack deployable, so `cdk deploy --all` still works without a build.
        CfnWaitConditionHandle(self, "NoFrontendBuild")

    def _acknowledge_deployment_handler(
        self, site_bucket: s3.IBucket, deployments: list[s3deploy.BucketDeployment]
    ) -> None:
        """Acknowledge the findings on the CDK-provided `BucketDeployment` handler Lambda.

        The handler is a singleton construct named `Custom::CDKBucketDeployment<uuid><memory>MiB`
        directly under the stack; its code, runtime and IAM policy are maintained by the CDK team.
        Each acknowledgment names the exact wildcard, so a broader grant in a future CDK version
        would surface as a new finding instead of being accepted silently.
        """
        handler = next(
            child for child in self.node.children if child.node.id.startswith("Custom::CDKBucketDeployment")
        )
        cdk_managed = "BucketDeployment handler Lambda provided and maintained by the AWS CDK"
        acknowledge(
            handler,
            (
                "AwsSolutions-L1",
                f"{cdk_managed}; its runtime (python3.13) is chosen by aws-cdk-lib, not by this app.",
            ),
            (
                LAMBDA_BASIC_EXECUTION_ROLE,
                f"{cdk_managed}; the AWS managed policy only allows writing its own CloudWatch Logs.",
            ),
        )
        # Each Source.asset is an `Asset` child of its deployment, stored in the CDK bootstrap bucket.
        staged_assets = [
            child.bucket.arn_for_objects("*")
            for deployment in deployments
            for child in deployment.node.children
            if isinstance(child, s3_assets.Asset)
        ]
        acknowledge_wildcards(
            handler,
            f"{cdk_managed}: read-only access (grant_read) to the CDK bootstrap bucket to download the "
            "zipped build that `cdk deploy` uploaded there.",
            actions=["s3:GetBucket*", "s3:GetObject*", "s3:List*"],
            resources=staged_assets,
        )
        acknowledge_wildcards(
            handler,
            f"{cdk_managed}: grant_read_write limited to the objects of this app's site bucket, the "
            "deployment destination (prune=False, so it never deletes files in practice).",
            actions=["s3:Abort*", "s3:DeleteObject*"],
            resources=[site_bucket.arn_for_objects("*")],
        )
        acknowledge_wildcards(
            handler,
            f"{cdk_managed}: CDK grants only cloudfront:CreateInvalidation and cloudfront:GetInvalidation, "
            "on '*', to invalidate /index.html and /config.json after each deployment.",
            resources=["*"],
        )


def app_config(
    config: StageConfig, edge: EdgeStack, data: DataStack, auth: AuthStack, backend: BackendStack
) -> dict[str, object]:
    """The `config.json` document the SPA loads at startup (contract: frontend/src/config.ts).

    Values from other stacks are tokens; the deployment fills them in. Numbers and booleans stay
    JSON numbers and booleans. `console` holds resource names for the in-app "open in AWS console" links.
    """
    return {
        "region": backend.region,
        "stage": config.stage,
        "userPoolId": auth.user_pool.user_pool_id,
        "userPoolClientId": auth.user_pool_client.user_pool_client_id,
        "apiUrl": backend.api_url,
        "wsUrl": backend.websocket_url,
        "maxReceiptBytes": config.max_receipt_bytes,
        "defaultCurrency": config.default_currency,
        "autoApproveLimit": config.auto_approve_limit,
        "extractionEnabled": config.extraction_enabled,
        "console": {
            "tableName": data.table.table_name,
            "receiptsBucket": data.receipts_bucket.bucket_name,
            "reportsBucket": data.reports_bucket.bucket_name,
            "eventBusName": backend.event_bus.event_bus_name,
            "approvalStateMachineArn": backend.approval_state_machine.state_machine_arn,
            "receiptStateMachineArn": backend.receipt_state_machine.state_machine_arn,
            "dashboardName": backend.dashboard_name,
            "httpApiId": backend.http_api.api_id,
            "webSocketApiId": backend.websocket_api.api_id,
            "userPoolId": auth.user_pool.user_pool_id,
            "distributionId": edge.distribution.distribution_id,
        },
    }
