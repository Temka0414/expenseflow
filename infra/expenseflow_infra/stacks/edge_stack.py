"""Edge stack: the single-page app is served from a private S3 bucket through CloudFront."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from constructs import Construct

from expenseflow_infra.config import PROJECT_NAME, StageConfig
from expenseflow_infra.constructs import access_logs_bucket, private_bucket
from expenseflow_infra.nag import acknowledge


class EdgeStack(Stack):
    """Static website hosting: S3 stores the built SPA, CloudFront serves it over HTTPS worldwide.

    The site bucket stays private. CloudFront reads it through **Origin Access Control** (OAC): it
    signs every origin request with SigV4 and the bucket policy allows `s3:GetObject` only for this
    distribution (`AWS:SourceArn`). Nobody can bypass the CDN and read the bucket directly.

    Gotcha: the bucket and the distribution must live in the same stack. The distribution needs the
    bucket's name, and the bucket policy needs the distribution's ARN; across two stacks those
    references point both ways and CDK reports a dependency cycle.

    Attributes:
        site_bucket: Where the Frontend stack uploads `frontend/dist`.
        distribution: The CloudFront distribution (the Frontend stack invalidates its cache).
        site_url: `https://<id>.cloudfront.net`, the app's public URL (a deploy-time token).
    """

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig) -> None:
        super().__init__(
            scope,
            construct_id,
            env=config.env,
            description=f"{PROJECT_NAME} ({config.stage}): web hosting - private S3 bucket behind CloudFront",
            termination_protection=config.is_prod,
        )
        logs = access_logs_bucket(self, "AccessLogsBucket", config=config)
        self.site_bucket = private_bucket(
            self, "SiteBucket", config=config, access_logs=logs, access_logs_prefix="site/"
        )
        self.distribution = self._distribution(config, self.site_bucket)
        self.site_url = f"https://{self.distribution.distribution_domain_name}"

        CfnOutput(self, "SiteUrl", value=self.site_url, description="Open this URL to use the app")
        CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        CfnOutput(self, "SiteBucketName", value=self.site_bucket.bucket_name)

    def _distribution(self, config: StageConfig, site_bucket: s3.IBucket) -> cloudfront.Distribution:
        # A client-side router owns paths such as /expenses/01J...: no S3 object has that key.
        # Without `s3:ListBucket`, S3 answers a missing key with 403 (not 404) so that callers cannot
        # probe which keys exist; both codes are rewritten to /index.html with 200 and the SPA
        # renders the route. TTL 0 keeps a new deployment visible immediately. Side effect: a
        # missing asset also returns index.html, so a stale hashed JS file fails as "unexpected '<'".
        spa_fallback = [
            cloudfront.ErrorResponse(
                http_status=status,
                response_http_status=200,
                response_page_path="/index.html",
                ttl=Duration.seconds(0),
            )
            for status in (403, 404)
        ]
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"{PROJECT_NAME} {config.stage} web app",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # Managed policies: cache by path only (no cookies/query strings), gzip + brotli,
                # and standard security headers (HSTS, X-Content-Type-Options, X-Frame-Options, ...).
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
                compress=True,
            ),
            default_root_object="index.html",
            error_responses=spa_fallback,
            # Edge locations in North America, Europe, Asia (incl. Japan), Middle East and Africa:
            # cheaper than PRICE_CLASS_ALL, which adds South America and Oceania.
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
        )
        acknowledge(
            distribution,
            (
                "AwsSolutions-CFR1",
                "The app is open to users in every country; there is no requirement for geo restrictions.",
            ),
            (
                "AwsSolutions-CFR2",
                "AWS WAF costs a monthly fee per web ACL and rule, out of budget for this learning project; "
                "the distribution only serves static files and the APIs are protected by Cognito JWTs.",
            ),
            (
                "AwsSolutions-CFR3",
                "Legacy CloudFront standard logging (the L2 enable_logging option) needs S3 ACLs, which stay "
                "disabled on all buckets (BUCKET_OWNER_ENFORCED). Origin requests are recorded by the site "
                "bucket's server access logs and traffic is visible in CloudFront's built-in CloudWatch "
                "metrics; standard logging v2 (CloudWatch vended logs) is a possible later addition.",
            ),
            (
                "AwsSolutions-CFR4",
                "Uses the default *.cloudfront.net certificate, for which CloudFront fixes the minimum "
                "protocol at TLSv1; enforcing TLSv1.2_2021 requires a custom domain with an ACM certificate.",
            ),
        )
        return distribution
