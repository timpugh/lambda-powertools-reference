from typing import Any, cast

from aws_cdk import (
    Aspects,
    CfnOutput,
    CustomResourceProvider,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3deploy,
)
from cdk_nag import AwsSolutionsChecks, NagSuppressions
from constructs import Construct


class HelloWorldFrontendStack(Stack):
    """CDK stack for the Hello World frontend.

    Provisions a private S3 bucket for static assets and a CloudFront
    distribution with OAC, HTTPS-only enforcement, and security response
    headers. WAF protection is provided by a WebACL ARN passed in from
    HelloWorldWafStack, which is always deployed in us-east-1.

    This stack can be deployed to any region. When the target region differs
    from us-east-1, CDK bridges the WAF ARN cross-region automatically via
    SSM Parameter Store (enabled by cross_region_references=True in app.py).
    """

    def __init__(self, scope: Construct, construct_id: str, api_url: str, waf_acl_arn: str, **kwargs: Any) -> None:
        """Provision all frontend AWS resources.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            api_url: The backend API Gateway URL, injected into config.json at deploy time.
            waf_acl_arn: ARN of the WAF WebACL from HelloWorldWafStack (always in us-east-1).
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, **kwargs)

        Aspects.of(self).add(AwsSolutionsChecks(verbose=True))

        # ── S3 bucket ────────────────────────────────────────────────────────
        # Fully private — CloudFront OAC is the only allowed reader.
        bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ── CloudFront distribution ──────────────────────────────────────────
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            default_root_object="index.html",
            error_responses=[
                # Return index.html for 403/404 so SPA client-side routing works
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            web_acl_id=waf_acl_arn,
        )

        # ── Deploy frontend assets ───────────────────────────────────────────
        # Uploads frontend/ to S3 and generates config.json with the API URL
        # injected at deploy time. Triggers a CloudFront invalidation so the
        # new assets are served immediately without waiting for cache expiry.
        s3deploy.BucketDeployment(
            self,
            "DeployFrontend",
            sources=[
                s3deploy.Source.asset("frontend"),
                s3deploy.Source.json_data("config.json", {"apiUrl": api_url}),
            ],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        CfnOutput(
            self,
            "CloudFrontDomainName",
            description="CloudFront distribution domain name — use this as your frontend URL",
            value=f"https://{distribution.distribution_domain_name}",
        )
        CfnOutput(
            self,
            "CloudFrontDistributionId",
            description="CloudFront distribution ID — needed for manual cache invalidations",
            value=distribution.distribution_id,
        )
        CfnOutput(
            self,
            "FrontendBucketName",
            description="S3 bucket storing the frontend static assets",
            value=bucket.bucket_name,
        )

        # ── Explicit log group for the CDK auto-delete Lambda ────────────────
        # CDK creates a singleton Lambda to empty the bucket before deletion.
        # It is a CloudFormation-managed Lambda, but its log group is created
        # implicitly by Lambda and has no retention — it would dangle after
        # cdk destroy. We find the provider via the construct tree and create
        # an explicit log group so CloudFormation owns and deletes it.
        auto_delete_provider = cast(
            CustomResourceProvider,
            self.node.try_find_child("Custom::S3AutoDeleteObjectsCustomResourceProvider"),
        )
        if auto_delete_provider is not None:
            # service_token is the Lambda ARN; index 6 of the colon-split is the function name
            fn_name = Fn.select(6, Fn.split(":", auto_delete_provider.service_token))
            logs.LogGroup(
                self,
                "AutoDeleteObjectsLogGroup",
                log_group_name=Fn.join("", ["/aws/lambda/", fn_name]),
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )

        # ── cdk-nag suppressions ─────────────────────────────────────────────
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": "Access logging to a second S3 bucket not warranted for sample app",
                },
                {"id": "AwsSolutions-CFR1", "reason": "CloudFront access logging not enabled for sample app"},
                {"id": "AwsSolutions-CFR3", "reason": "CloudFront access logging not enabled for sample app"},
                {
                    "id": "AwsSolutions-CFR4",
                    "reason": "Using default CloudFront certificate — no custom domain for sample app",
                },
                {"id": "AwsSolutions-IAM4", "reason": "BucketDeployment custom resource uses managed policies"},
                {"id": "AwsSolutions-IAM5", "reason": "BucketDeployment requires wildcard on destination bucket"},
                {"id": "AwsSolutions-L1", "reason": "BucketDeployment custom resource Lambda runtime is CDK-managed"},
            ],
        )
