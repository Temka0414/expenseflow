"""Reusable building blocks shared by the ExpenseFlow stacks.

The larger constructs live in their own modules and are imported from there, e.g.
`from expenseflow_infra.constructs.python_function import PythonFunction`.

This module holds the S3 baseline used by every bucket of the app. S3 is where most cloud data leaks
happen, so every bucket is created through `private_bucket` and gets the same guard rails:

* **Block Public Access** (all four settings): no ACL or bucket policy can ever make objects public.
  Browsers reach objects only through CloudFront (site) or short-lived presigned URLs (receipts).
* **TLS only** (`enforce_ssl`): the bucket policy denies any request made over plain HTTP.
* **Encryption at rest** with S3-managed keys (SSE-S3): free and transparent; KMS keys would add
  per-request cost and key policies to manage, without a requirement that needs them here.
* **ACLs disabled** (`BUCKET_OWNER_ENFORCED`): the account owns every object, permissions come from
  IAM and bucket policies only. This is the modern S3 default; ACLs are a legacy permission system.
* **Server access logs** into a dedicated logs bucket: an audit trail of every request (cdk-nag S1).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_s3 as s3
from constructs import Construct

from expenseflow_infra.config import StageConfig
from expenseflow_infra.nag import LAMBDA_BASIC_EXECUTION_ROLE, acknowledge

ACCESS_LOG_RETENTION_DAYS: Final = 90
PROD_ACCESS_LOG_RETENTION_DAYS: Final = 365

# Construct id CDK gives the singleton Lambda behind `auto_delete_objects` (one per stack).
_AUTO_DELETE_PROVIDER_ID: Final = "Custom::S3AutoDeleteObjectsCustomResourceProvider"


def access_logs_bucket(scope: Construct, construct_id: str, *, config: StageConfig) -> s3.Bucket:
    """Bucket that receives S3 server access logs from the other buckets of the same stack.

    S3 delivers the logs itself (principal `logging.s3.amazonaws.com`). With the feature flag
    `@aws-cdk/aws-s3:serverAccessLogsUseBucketPolicy` CDK grants that delivery through this bucket's
    *policy*, scoped to each source bucket, instead of the legacy `LogDeliveryWrite` ACL; that is what
    allows ACLs to stay disabled here too. Logs expire automatically to keep storage cost flat.
    """
    retention_days = PROD_ACCESS_LOG_RETENTION_DAYS if config.is_prod else ACCESS_LOG_RETENTION_DAYS
    return _bucket(
        scope,
        construct_id,
        config=config,
        lifecycle_rules=[s3.LifecycleRule(id="expire-logs", expiration=Duration.days(retention_days))],
    )


def private_bucket(
    scope: Construct,
    construct_id: str,
    *,
    config: StageConfig,
    access_logs: s3.IBucket,
    access_logs_prefix: str,
    versioned: bool = False,
    cors: Sequence[s3.CorsRule] = (),
    lifecycle_rules: Sequence[s3.LifecycleRule] = (),
) -> s3.Bucket:
    """A private, encrypted, TLS-only bucket that logs every request to `access_logs`.

    Args:
        config: Stage settings; dev stages delete the bucket *and its objects* on `cdk destroy`,
            prod retains it.
        access_logs: Bucket from `access_logs_bucket` in the same stack.
        access_logs_prefix: Folder (ending in `/`) for this bucket's logs inside `access_logs`.
        versioned: Keep previous versions of overwritten or deleted objects.
        cors: Browser origins allowed to call S3 directly (presigned URLs from the SPA).
        lifecycle_rules: Automatic storage-class transitions and expiry.
    """
    return _bucket(
        scope,
        construct_id,
        config=config,
        server_access_logs_bucket=access_logs,
        server_access_logs_prefix=access_logs_prefix,
        versioned=versioned,
        cors=list(cors) or None,
        lifecycle_rules=list(lifecycle_rules),
    )


def _bucket(
    scope: Construct,
    construct_id: str,
    *,
    config: StageConfig,
    lifecycle_rules: list[s3.LifecycleRule],
    server_access_logs_bucket: s3.IBucket | None = None,
    server_access_logs_prefix: str | None = None,
    versioned: bool = False,
    cors: list[s3.CorsRule] | None = None,
) -> s3.Bucket:
    # CloudFormation cannot delete a bucket that still contains objects. In dev stages CDK's
    # `auto_delete_objects` adds a small custom resource that empties the bucket first, so
    # `cdk destroy` leaves nothing behind. Prod buckets are retained (and never emptied).
    auto_delete = config.removal_policy == RemovalPolicy.DESTROY
    stack = Stack.of(scope)
    # One provider serves every bucket of the stack; acknowledge it once, when it is first created.
    acknowledge_provider = auto_delete and stack.node.try_find_child(_AUTO_DELETE_PROVIDER_ID) is None
    bucket = s3.Bucket(
        scope,
        construct_id,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        enforce_ssl=True,
        encryption=s3.BucketEncryption.S3_MANAGED,
        object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        server_access_logs_bucket=server_access_logs_bucket,
        server_access_logs_prefix=server_access_logs_prefix,
        versioned=versioned,
        cors=cors,
        lifecycle_rules=lifecycle_rules or None,
        removal_policy=config.removal_policy,
        auto_delete_objects=auto_delete,
    )
    if acknowledge_provider:
        _acknowledge_auto_delete_provider(stack)
    return bucket


def _acknowledge_auto_delete_provider(stack: Stack) -> None:
    acknowledge(
        stack.node.find_child(_AUTO_DELETE_PROVIDER_ID),
        (
            LAMBDA_BASIC_EXECUTION_ROLE,
            "CDK-managed helper that empties dev buckets on stack deletion; its role uses the AWS "
            "managed logging policy and cannot be customised.",
        ),
    )
