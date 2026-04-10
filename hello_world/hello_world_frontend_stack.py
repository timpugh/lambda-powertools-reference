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
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
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
from cdk_nag import AwsSolutionsChecks, NagSuppressions, NIST80053R5Checks, ServerlessChecks
from constructs import Construct

from hello_world.nag_utils import CDK_LAMBDA_SUPPRESSIONS


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
        Aspects.of(self).add(ServerlessChecks(verbose=True))
        Aspects.of(self).add(NIST80053R5Checks(verbose=True))

        # ── KMS key ──────────────────────────────────────────────────────────
        # Used to encrypt the frontend S3 bucket and CloudWatch log group.
        # CloudWatch Logs requires the Logs service principal in the key policy.
        frontend_encryption_key = kms.Key(
            self,
            "FrontendEncryptionKey",
            description=f"KMS key for {self.stack_name} S3 bucket and log groups",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        frontend_encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                resources=["*"],
            )
        )

        # ── S3 access logging bucket ─────────────────────────────────────────
        # Receives S3 server access logs from FrontendBucket. Must use SSE-S3
        # (not SSE-KMS) because the S3 log delivery service does not support
        # KMS-encrypted target buckets. This bucket itself does not need access
        # logging (that would be circular), versioning, or replication.
        access_log_bucket = s3.Bucket(
            self,
            "FrontendAccessLogBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        NagSuppressions.add_resource_suppressions(
            access_log_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": "This IS the access log bucket — logging to itself would be circular",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketLoggingEnabled",
                    "reason": "This IS the access log bucket — logging to itself would be circular",
                },
                {
                    "id": "NIST.800.53.R5-S3DefaultEncryptionKMS",
                    "reason": "S3 log delivery service does not support KMS-encrypted target buckets; SSE-S3 is used instead",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketVersioningEnabled",
                    "reason": "Versioning not needed for log bucket — logs are append-only and transient",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": "Replication not needed for log bucket in sample app",
                },
            ],
        )

        # ── S3 bucket ────────────────────────────────────────────────────────
        # Fully private — CloudFront OAC is the only allowed reader.
        # KMS-encrypted with server access logging to access_log_bucket.
        bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=frontend_encryption_key,
            enforce_ssl=True,
            server_access_logs_bucket=access_log_bucket,
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
                encryption_key=frontend_encryption_key,
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )

        # ── Per-resource cdk-nag suppressions ──────────────────────────────────
        # All Lambdas in this stack are CDK-managed singletons. They are stack-level
        # siblings, not children of user-facing constructs, so path-based suppression
        # is required. The access log bucket is suppressed separately because its
        # reason differs from the frontend bucket.
        #
        # Stable singleton IDs:
        #   Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C — BucketDeployment provider
        #   Custom::S3AutoDeleteObjectsCustomResourceProvider — auto-delete provider
        #   LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a — log retention singleton

        NagSuppressions.add_resource_suppressions_by_path(
            self,
            f"/{self.stack_name}/Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C",
            CDK_LAMBDA_SUPPRESSIONS,
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions_by_path(
            self,
            f"/{self.stack_name}/LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a",
            CDK_LAMBDA_SUPPRESSIONS,
            apply_to_children=True,
        )
        if auto_delete_provider is not None:
            NagSuppressions.add_resource_suppressions(
                auto_delete_provider,
                CDK_LAMBDA_SUPPRESSIONS,
                apply_to_children=True,
            )

        # ── Stack-level cdk-nag suppressions (genuinely stack-wide) ─────────────
        NagSuppressions.add_stack_suppressions(
            self,
            [
                # ── AWS Solutions ────────────────────────────────────────────────
                {"id": "AwsSolutions-CFR1", "reason": "CloudFront access logging not enabled for sample app"},
                {"id": "AwsSolutions-CFR3", "reason": "CloudFront access logging not enabled for sample app"},
                {
                    "id": "AwsSolutions-CFR4",
                    "reason": "Using default CloudFront certificate — no custom domain for sample app",
                },
                # ── NIST 800-53 R5 ──────────────────────────────────────────────
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": "S3 replication not needed for sample app — static assets are redeployable",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketVersioningEnabled",
                    "reason": "S3 versioning not needed for sample app — static assets are redeployable via cdk deploy",
                },
            ],
        )
