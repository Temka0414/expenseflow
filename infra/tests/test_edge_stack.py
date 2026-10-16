"""Edge stack: private site bucket + CloudFront distribution with Origin Access Control."""

from __future__ import annotations

from aws_cdk.assertions import Match, Template

from expenseflow_infra.config import StageConfig
from expenseflow_infra.stacks.edge_stack import EdgeStack
from tests.conftest import AppFactory, NagCheck

# AWS managed response headers policy "SecurityHeadersPolicy" (HSTS, X-Frame-Options, ...).
SECURITY_HEADERS_POLICY_ID = "67f7725c-6f97-4210-82d7-5512b31e9d03"


def _edge(make_app: AppFactory, **context: object) -> EdgeStack:
    app = make_app(**context)
    config = StageConfig.from_app(app)
    return EdgeStack(app, config.stack_id("Edge"), config=config)


def _bucket_logical_id(template: Template, id_prefix: str) -> str:
    buckets = template.find_resources("AWS::S3::Bucket")
    matches = [logical_id for logical_id in buckets if logical_id.startswith(id_prefix)]
    assert len(matches) == 1, f"expected one bucket {id_prefix}*, found {sorted(buckets)}"
    return matches[0]


def test_distribution_serves_the_spa_over_https_with_oac(make_app: AppFactory) -> None:
    template = Template.from_stack(_edge(make_app))

    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "DefaultRootObject": "index.html",
                    "HttpVersion": "http2and3",
                    "PriceClass": "PriceClass_200",
                    "DefaultCacheBehavior": Match.object_like(
                        {
                            "ViewerProtocolPolicy": "redirect-to-https",
                            "Compress": True,
                            "ResponseHeadersPolicyId": SECURITY_HEADERS_POLICY_ID,
                        }
                    ),
                    "CustomErrorResponses": [
                        {
                            "ErrorCode": status,
                            "ResponseCode": 200,
                            "ResponsePagePath": "/index.html",
                            "ErrorCachingMinTTL": 0,
                        }
                        for status in (403, 404)
                    ],
                    "Origins": [
                        Match.object_like(
                            {
                                "OriginAccessControlId": Match.any_value(),
                                "S3OriginConfig": {"OriginAccessIdentity": ""},
                            }
                        )
                    ],
                }
            )
        },
    )


def test_site_bucket_is_private_and_readable_only_by_the_distribution(make_app: AppFactory) -> None:
    template = Template.from_stack(_edge(make_app))
    site_bucket = _bucket_logical_id(template, "SiteBucket")
    logs_bucket = _bucket_logical_id(template, "AccessLogsBucket")

    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
            "BucketEncryption": Match.object_like({}),
            "LoggingConfiguration": {
                "DestinationBucketName": {"Ref": logs_bucket},
                "LogFilePrefix": "site/",
            },
        },
    )
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "Bucket": {"Ref": site_bucket},
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Action": "s3:*",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        ),
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Action": "s3:GetObject",
                                "Principal": {"Service": "cloudfront.amazonaws.com"},
                                "Condition": {"StringEquals": {"AWS:SourceArn": Match.any_value()}},
                            }
                        ),
                    ]
                )
            },
        },
    )


def test_outputs(make_app: AppFactory) -> None:
    template = Template.from_stack(_edge(make_app))

    assert set(template.find_outputs("*")) >= {"SiteUrl", "DistributionId", "SiteBucketName"}
    template.has_output(
        "SiteUrl",
        {
            "Value": {
                "Fn::Join": [
                    "",
                    ["https://", {"Fn::GetAtt": [Match.string_like_regexp("^Distribution"), "DomainName"]}],
                ]
            }
        },
    )


def test_dev_buckets_are_emptied_and_deleted_on_destroy(make_app: AppFactory) -> None:
    template = Template.from_stack(_edge(make_app))

    template.all_resources("AWS::S3::Bucket", {"DeletionPolicy": "Delete", "UpdateReplacePolicy": "Delete"})
    template.resource_count_is("Custom::S3AutoDeleteObjects", 2)


def test_prod_buckets_are_retained(make_app: AppFactory) -> None:
    template = Template.from_stack(_edge(make_app, stage="prod"))

    template.all_resources("AWS::S3::Bucket", {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"})
    template.resource_count_is("Custom::S3AutoDeleteObjects", 0)


def test_cdk_nag_passes(make_app: AppFactory, assert_nag_clean: NagCheck) -> None:
    assert_nag_clean(_edge(make_app))
    assert_nag_clean(_edge(make_app, stage="prod"))
