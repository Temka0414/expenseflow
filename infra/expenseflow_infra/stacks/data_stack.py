"""Data stack: everything stateful - DynamoDB tables and the receipts/reports buckets.

Stateful resources get their own stack so that the stacks holding code (Backend, Frontend) can be
changed, destroyed and redeployed freely without ever putting data at risk.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from constructs import Construct

from expenseflow_infra.config import LOCAL_DEV_ORIGIN, PROJECT_NAME, StageConfig
from expenseflow_infra.constructs import access_logs_bucket, private_bucket

TTL_ATTRIBUTE: Final = "expiresAt"
"""Epoch seconds after which DynamoDB deletes an item for free (used by WebSocket connection items)."""

RECEIPT_ARCHIVE_AFTER_DAYS: Final = 365
RECEIPT_RETENTION_DAYS: Final = 2557
"""7 years (7 * 365 + 2 leap days): the retention period for tax records (帳簿書類) in Japan."""
REPORT_RETENTION_DAYS: Final = 400


class DataStack(Stack):
    """DynamoDB single table + idempotency table, receipts and reports buckets.

    Attributes:
        table: The single table holding every entity (key formats: `backend/src/expenseflow/keys.py`),
            with a stream for change events.
        idempotency_table: Powertools idempotency records of the notifier.
        receipts_bucket: Receipt images/PDFs uploaded by browsers with presigned POST.
        reports_bucket: Monthly CSV reports.
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig, site_url: str) -> None:
        """Create the stack.

        Args:
            config: Stage settings.
            site_url: Origin of the deployed SPA (`EdgeStack.site_url`), allowed by the buckets' CORS rules.
        """
        super().__init__(
            scope,
            construct_id,
            env=config.env,
            description=f"{PROJECT_NAME} ({config.stage}): DynamoDB tables and S3 buckets (stateful data)",
            termination_protection=config.is_prod,
        )
        browser_origins = [site_url, LOCAL_DEV_ORIGIN]
        logs = access_logs_bucket(self, "AccessLogsBucket", config=config)

        self.table = self._single_table(config)
        self.idempotency_table = self._idempotency_table()
        self.receipts_bucket = self._receipts_bucket(config, logs, browser_origins)
        self.reports_bucket = private_bucket(
            self,
            "ReportsBucket",
            config=config,
            access_logs=logs,
            access_logs_prefix="reports/",
            # Downloads use presigned GET URLs; CORS lets the SPA fetch() a CSV as well as link to it.
            cors=[_cors_rule(browser_origins, s3.HttpMethods.GET)],
            lifecycle_rules=[
                s3.LifecycleRule(id="expire-reports", expiration=Duration.days(REPORT_RETENTION_DAYS))
            ],
        )

        CfnOutput(self, "TableName", value=self.table.table_name)
        CfnOutput(self, "ReceiptsBucketName", value=self.receipts_bucket.bucket_name)
        CfnOutput(self, "ReportsBucketName", value=self.reports_bucket.bucket_name)

    def _single_table(self, config: StageConfig) -> dynamodb.TableV2:
        """One table for all entities; generic key names (`PK`, `SK`, `GSI1PK`...) carry typed prefixes.

        `TableV2` synthesizes `AWS::DynamoDB::GlobalTable` with a single replica: the same price as a
        classic table, and replicas in other Regions can be added later without replacing it.
        """
        return dynamodb.TableV2(
            self,
            "Table",
            partition_key=_string_key("PK"),
            sort_key=_string_key("SK"),
            # On-demand: pay per request, no capacity planning, scales to zero cost when idle.
            billing=dynamodb.Billing.on_demand(),
            global_secondary_indexes=[_gsi("GSI1"), _gsi("GSI2")],
            # The stream feeds the stream_processor Lambda ("transactional outbox"): every committed
            # change becomes an event, with no dual write to DynamoDB and EventBridge in the API.
            # Gotcha: a TableV2 without `dynamo_stream` still synthesizes, but the event source
            # mapping then fails at deploy time.
            dynamo_stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            # Restore to any second of the last 35 days. cdk-nag only checks this on the classic
            # `Table`, so the tests assert it for this TableV2.
            point_in_time_recovery_specification=_pitr(),
            time_to_live_attribute=TTL_ATTRIBUTE,
            deletion_protection=config.is_prod,
            removal_policy=config.removal_policy,
        )

    def _idempotency_table(self) -> dynamodb.TableV2:
        """Records of already-processed messages, used by Powertools' idempotency utility.

        SQS delivers *at least once*, so the notifier may see the same event twice; Powertools stores
        a hash of each processed event here and skips repeats until the record expires.
        """
        return dynamodb.TableV2(
            self,
            "IdempotencyTable",
            partition_key=_string_key("id"),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=_pitr(),
            time_to_live_attribute="expiration",
            # Short-lived records (24 hours): nothing worth retaining, even in prod.
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _receipts_bucket(self, config: StageConfig, logs: s3.IBucket, origins: list[str]) -> s3.Bucket:
        bucket = private_bucket(
            self,
            "ReceiptsBucket",
            config=config,
            access_logs=logs,
            access_logs_prefix="receipts/",
            # An overwritten or deleted receipt stays recoverable as a noncurrent version for 30 days.
            versioned=True,
            # Browsers upload directly to S3 with a presigned POST and view receipts with presigned
            # GET URLs, so S3 itself must answer CORS for the SPA's origins.
            cors=[_cors_rule(origins, s3.HttpMethods.POST, s3.HttpMethods.GET)],
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="archive-and-expire",
                    # Glacier Instant Retrieval: a fraction of the S3 Standard storage price, still
                    # millisecond reads (billed for at least 90 days and 128 KB per object).
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=Duration.days(RECEIPT_ARCHIVE_AFTER_DAYS),
                        )
                    ],
                    expiration=Duration.days(RECEIPT_RETENTION_DAYS),
                    noncurrent_version_expiration=Duration.days(30),
                    # Parts of interrupted uploads are invisible but billed: clean them up.
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
                # In a versioned bucket, "expiring" an object only adds a delete marker; once the
                # noncurrent versions are gone this rule removes the orphaned marker as well.
                s3.LifecycleRule(id="remove-expired-delete-markers", expired_object_delete_marker=True),
            ],
        )
        _enable_eventbridge_notifications(bucket)
        return bucket


def _enable_eventbridge_notifications(bucket: s3.Bucket) -> None:
    """Send every object-level event of `bucket` to the account's default EventBridge bus.

    This uses the **escape hatch**: `node.default_child` is the L1 `CfnBucket` that the L2 `Bucket`
    generates, and any CloudFormation property can be set on it directly.

    Why not `s3.Bucket(event_bridge_enabled=True)`? That L2 option deploys a custom resource (an
    extra Lambda function and IAM role) which calls `PutBucketNotificationConfiguration` after the
    bucket exists - a workaround needed for SNS/SQS/Lambda destinations, whose permissions would
    otherwise form a circular dependency. EventBridge needs no destination permissions, so
    CloudFormation's native `NotificationConfiguration` does the job with nothing extra to run.
    """
    cfn_bucket = bucket.node.default_child
    if not isinstance(cfn_bucket, s3.CfnBucket):
        raise TypeError(f"expected the L1 CfnBucket under {bucket.node.path}, got {cfn_bucket!r}")
    cfn_bucket.notification_configuration = s3.CfnBucket.NotificationConfigurationProperty(
        event_bridge_configuration=s3.CfnBucket.EventBridgeConfigurationProperty(event_bridge_enabled=True)
    )


def _cors_rule(origins: list[str], *methods: s3.HttpMethods) -> s3.CorsRule:
    return s3.CorsRule(
        allowed_origins=origins,
        allowed_methods=list(methods),
        allowed_headers=["*"],
        exposed_headers=["ETag"],
        max_age=3600,
    )


def _string_key(name: str) -> dynamodb.Attribute:
    return dynamodb.Attribute(name=name, type=dynamodb.AttributeType.STRING)


def _gsi(name: str) -> dynamodb.GlobalSecondaryIndexPropsV2:
    """`GSI1` -> index `GSI1` keyed on `GSI1PK`/`GSI1SK`, projecting every attribute.

    Projection ALL lets list queries return complete items without a second read per item; the cost
    is extra index storage and write units, negligible at this app's size.
    """
    return dynamodb.GlobalSecondaryIndexPropsV2(
        index_name=name,
        partition_key=_string_key(f"{name}PK"),
        sort_key=_string_key(f"{name}SK"),
        projection_type=dynamodb.ProjectionType.ALL,
    )


def _pitr() -> dynamodb.PointInTimeRecoverySpecification:
    return dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True)
