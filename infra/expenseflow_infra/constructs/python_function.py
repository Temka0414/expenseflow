"""PythonFunction: one place for the operational defaults every ExpenseFlow Lambda shares."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from expenseflow_infra import paths
from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.nag import acknowledge

RUNTIME: Final = lambda_.Runtime.PYTHON_3_13
ARCHITECTURE: Final = lambda_.Architecture.ARM_64
MIN_MEMORY_MB: Final = 128
MAX_MEMORY_MB: Final = 1024
CODE_EXCLUDES: Final = ("tests", "**/__pycache__", "*.pyc")


class PythonFunction(Construct):
    """A Python 3.13 / arm64 Lambda function running code from `backend/src`.

    Defaults applied to every function:

    * **Graviton (arm64)**: about 20% cheaper per GB-second than x86_64 for the same code.
    * **Structured JSON logs** (`logging_format=JSON`) with Lambda-side level filtering: INFO for
      application logs, WARN for the platform's own START/END/REPORT lines.
    * **An explicit log group** with stage-specific retention. Without it Lambda creates
      `/aws/lambda/<name>` on first invoke, it never expires and `cdk destroy` leaves it behind.
    * **A dedicated execution role** that may write only to that log group. The usual
      AWSLambdaBasicExecutionRole managed policy allows writing to *every* log group in the account.
    * **X-Ray active tracing**, so each invocation and its AWS SDK calls (DynamoDB, S3, EventBridge, ...)
      show up in the CloudWatch trace map. HTTP APIs don't support X-Ray, so a request's trace starts at
      the Lambda function.

    Deliberately not set: reserved concurrency. New AWS accounts start with reduced concurrency
    and memory quotas, and reserving capacity can fail the deployment; memory is capped at 1024 MB
    for the same reason.

    Every function uses the same code asset (the whole `backend/src` tree). CDK hashes the folder
    once, so it is uploaded once and shared; each function just points at a different `handler`.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        handler: str,
        service: str,
        description: str,
        timeout: Duration,
        memory_size: int,
        layer: lambda_.ILayerVersion | None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Create the function.

        Args:
            config: Stage settings (log retention, removal policy, stage name).
            handler: Dotted path inside `backend/src`, e.g. `handlers.api.app.handler`.
            service: Short service name for Powertools logs/metrics/traces, e.g. `api`.
            description: Shown in the Lambda console.
            timeout: Hard limit for one invocation.
            memory_size: MB of memory (CPU scales with it); 128-1024.
            layer: Shared third-party dependencies (the DependenciesLayer), or None for
                stdlib + boto3 code that must start as fast as possible.
            environment: Function-specific environment variables, added to the common ones.
        """
        super().__init__(scope, construct_id)
        if not MIN_MEMORY_MB <= memory_size <= MAX_MEMORY_MB:
            raise ValueError(f"{construct_id}: memory_size must be {MIN_MEMORY_MB}-{MAX_MEMORY_MB} MB")

        self.log_group = logs.LogGroup(
            self, "LogGroup", retention=config.log_retention, removal_policy=config.removal_policy
        )
        self.role = iam.Role(
            self,
            "ServiceRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"{PROJECT_NAME} {config.stage} {service} Lambda execution role",
        )
        # logs:CreateLogStream + logs:PutLogEvents on this function's log group only.
        self.log_group.grant_write(self.role)

        self.function = lambda_.Function(
            self,
            "Function",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            code=lambda_.Code.from_asset(str(paths.BACKEND_SRC), exclude=list(CODE_EXCLUDES)),
            handler=handler,
            description=description,
            timeout=timeout,
            memory_size=memory_size,
            role=self.role,
            layers=[layer] if layer else None,
            tracing=lambda_.Tracing.ACTIVE,
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            system_log_level_v2=lambda_.SystemLogLevel.WARN,
            log_group=self.log_group,
            environment={
                "STAGE": config.stage,
                "POWERTOOLS_SERVICE_NAME": service,
                "POWERTOOLS_METRICS_NAMESPACE": PROJECT_NAME,
                **(environment or {}),
            },
        )

        acknowledge(
            self,
            (
                "AwsSolutions-L1",
                "Pinned to python3.13 on purpose: it matches the local toolchain and the cp313 wheels "
                "in the dependencies layer. Supported by Lambda until 2029; upgrade deliberately.",
            ),
            (
                "AwsSolutions-IAM5[Resource::*]",
                "Active tracing needs xray:PutTraceSegments/PutTelemetryRecords, which do not support "
                "resource-level permissions, so AWS requires Resource '*'.",
            ),
        )
