"""Data stack: single table + idempotency table (TableV2), receipts and reports buckets."""

from __future__ import annotations

from typing import Any

from aws_cdk.assertions import Match, Template

from expenseflow_infra.config import LOCAL_DEV_ORIGIN, StageConfig
from expenseflow_infra.stacks.data_stack import DataStack
from expenseflow_infra.stacks.edge_stack import EdgeStack
from tests.conftest import AppFactory, NagCheck

SITE_URL = "https://d111111abcdef8.cloudfront.net"


def _data(make_app: AppFactory, **context: object) -> DataStack:
    app = make_app(**context)
    config = StageConfig.from_app(app)
    return DataStack(app, config.stack_id("Data"), config=config, site_url=SITE_URL)


def _key_schema(partition: str, sort: str | None = None) -> list[dict[str, str]]:
    keys = [{"AttributeName": partition, "KeyType": "HASH"}]
    if sort:
        keys.append({"AttributeName": sort, "KeyType": "RANGE"})
    return keys


def _bucket_with_logs_prefix(template: Template, prefix: str) -> dict[str, Any]:
    buckets = template.find_resources(
        "AWS::S3::Bucket", {"Properties": {"LoggingConfiguration": {"LogFilePrefix": prefix}}}
    )
    assert len(buckets) == 1, f"expected one bucket logging to {prefix!r}"
    properties: dict[str, Any] = next(iter(buckets.values()))["Properties"]
    return properties


def test_single_table_design(make_app: AppFactory) -> None:
    template = Template.from_stack(_data(make_app))

    template.resource_count_is("AWS::DynamoDB::GlobalTable", 2)
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {
            "KeySchema": _key_schema("PK", "SK"),
            "BillingMode": "PAY_PER_REQUEST",
            "StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"},
            "TimeToLiveSpecification": {"AttributeName": "expiresAt", "Enabled": True},
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": name,
                    "KeySchema": _key_schema(f"{name}PK", f"{name}SK"),
                    "Projection": {"ProjectionType": "ALL"},
                }
                for name in ("GSI1", "GSI2")
            ],
            "Replicas": [
                Match.object_like(
                    {
                        # cdk-nag never checks PITR on TableV2, so this test is the guard rail.
                        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
                        "DeletionProtectionEnabled": False,
                    }
                )
            ],
        },
    )


def test_idempotency_table(make_app: AppFactory) -> None:
    template = Template.from_stack(_data(make_app))

    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {
            "KeySchema": _key_schema("id"),
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {"AttributeName": "expiration", "Enabled": True},
            "Replicas": [
                Match.object_like({"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}})
            ],
        },
    )


def test_receipts_bucket(make_app: AppFactory) -> None:
    template = Template.from_stack(_data(make_app))
    receipts = _bucket_with_logs_prefix(template, "receipts/")

    assert receipts["VersioningConfiguration"] == {"Status": "Enabled"}
    # Escape hatch on the L1 CfnBucket: native EventBridge notifications, no custom resource.
    assert receipts["NotificationConfiguration"] == {"EventBridgeConfiguration": {"EventBridgeEnabled": True}}
    template.resource_count_is("Custom::S3BucketNotifications", 0)
    [cors_rule] = receipts["CorsConfiguration"]["CorsRules"]
    assert sorted(cors_rule["AllowedMethods"]) == ["GET", "POST"]
    assert cors_rule["AllowedOrigins"] == [SITE_URL, LOCAL_DEV_ORIGIN]

    rules = {rule["Id"]: rule for rule in receipts["LifecycleConfiguration"]["Rules"]}
    archive = rules["archive-and-expire"]
    assert archive["Transitions"] == [{"StorageClass": "GLACIER_IR", "TransitionInDays": 365}]
    assert archive["ExpirationInDays"] == 2557
    assert archive["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert archive["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}
    assert rules["remove-expired-delete-markers"]["ExpiredObjectDeleteMarker"] is True


def test_reports_bucket_expires_reports(make_app: AppFactory) -> None:
    template = Template.from_stack(_data(make_app))
    reports = _bucket_with_logs_prefix(template, "reports/")

    assert reports["LifecycleConfiguration"]["Rules"] == [
        {"Id": "expire-reports", "ExpirationInDays": 400, "Status": "Enabled"}
    ]
    assert reports["CorsConfiguration"]["CorsRules"][0]["AllowedMethods"] == ["GET"]


def test_buckets_log_to_the_stacks_own_access_logs_bucket(make_app: AppFactory) -> None:
    template = Template.from_stack(_data(make_app))
    logs_buckets = [
        logical_id
        for logical_id in template.find_resources("AWS::S3::Bucket")
        if logical_id.startswith("AccessLogsBucket")
    ]

    assert len(logs_buckets) == 1
    for prefix in ("receipts/", "reports/"):
        bucket = _bucket_with_logs_prefix(template, prefix)
        assert bucket["LoggingConfiguration"]["DestinationBucketName"] == {"Ref": logs_buckets[0]}
        assert bucket["PublicAccessBlockConfiguration"]["RestrictPublicBuckets"] is True


def test_outputs(make_app: AppFactory) -> None:
    template = Template.from_stack(_data(make_app))

    assert set(template.find_outputs("*")) >= {"TableName", "ReceiptsBucketName", "ReportsBucketName"}


def test_prod_protects_the_table(make_app: AppFactory) -> None:
    template = Template.from_stack(_data(make_app, stage="prod"))

    template.has_resource(
        "AWS::DynamoDB::GlobalTable",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "KeySchema": _key_schema("PK", "SK"),
                    "Replicas": [Match.object_like({"DeletionProtectionEnabled": True})],
                }
            ),
        },
    )


def test_site_url_from_edge_is_a_weak_cross_stack_reference(make_app: AppFactory) -> None:
    app = make_app()
    config = StageConfig.from_app(app)
    edge = EdgeStack(app, config.stack_id("Edge"), config=config)
    data = DataStack(app, config.stack_id("Data"), config=config, site_url=edge.site_url)

    template = Template.from_stack(data)

    # `defaultCrossStackReferences: "weak"`: Fn::GetStackOutput instead of Export/ImportValue.
    receipts = _bucket_with_logs_prefix(template, "receipts/")
    site_origin = receipts["CorsConfiguration"]["CorsRules"][0]["AllowedOrigins"][0]
    assert "Fn::GetStackOutput" in str(site_origin)
    assert edge in data.dependencies


def test_cdk_nag_passes(make_app: AppFactory, assert_nag_clean: NagCheck) -> None:
    assert_nag_clean(_data(make_app))
    assert_nag_clean(_data(make_app, stage="prod"))
