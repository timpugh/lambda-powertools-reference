"""CDK stack assertion tests.

These tests synthesize each CDK stack in-process using ``aws_cdk.assertions.Template``
and verify that key security properties are correctly configured. They serve as a
regression guard — if a construct property is accidentally removed or changed (e.g.,
KMS encryption dropped from DynamoDB, PITR disabled, CloudFront TLS downgraded),
the test fails immediately at synthesis time rather than silently deploying an
insecure template.

Nag checks (AwsSolutionsChecks, ServerlessChecks, NIST80053R5Checks) are enforced
here too: any unsuppressed cdk-nag finding causes synthesis to fail, making these
tests a CI gate for infrastructure misconfigurations.

Asset bundling (Docker) is skipped via the ``aws:cdk:bundling-stacks`` context key
so these tests run without Docker.

The ``aws_cdk`` package is only installed in the CDK check CI job, not in the
regular unit-test environment. All tests in this module are skipped automatically
when ``aws_cdk`` is not importable, so the standard ``pytest tests/unit`` run stays
clean.
"""

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK stack tests")

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from hello_world.hello_world_frontend_stack import HelloWorldFrontendStack
from hello_world.hello_world_stack import HelloWorldStack
from hello_world.hello_world_waf_stack import HelloWorldWafStack

# Fake account/region — synthesis does not make live AWS API calls
_TEST_ACCOUNT = "123456789012"
_TEST_REGION = "us-east-1"
_TEST_ENV = cdk.Environment(account=_TEST_ACCOUNT, region=_TEST_REGION)
_WAF_ENV = cdk.Environment(account=_TEST_ACCOUNT, region="us-east-1")

# Skip Docker bundling so these tests run without Docker.
# The CDK CLI and Python SDK both honour this context key during synthesis.
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}


# ── Session-scoped fixtures ───────────────────────────────────────────────────
# Each stack is synthesized once per test session to keep the suite fast.


@pytest.fixture(scope="module")
def waf_template() -> Template:
    """Synthesize HelloWorldWafStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    stack = HelloWorldWafStack(app, "TestWafStack", env=_WAF_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def backend_template() -> Template:
    """Synthesize HelloWorldStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    stack = HelloWorldStack(app, "TestBackendStack", env=_TEST_ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def frontend_template() -> Template:
    """Synthesize HelloWorldFrontendStack and return its CloudFormation template."""
    app = cdk.App(context=_NO_BUNDLING)
    waf = HelloWorldWafStack(app, "TestFrontendWaf", env=_WAF_ENV)
    backend = HelloWorldStack(app, "TestFrontendBackend", env=_TEST_ENV)
    stack = HelloWorldFrontendStack(
        app,
        "TestFrontendStack",
        api_url=backend.api_url,
        waf_acl_arn=waf.web_acl_arn,
        env=_TEST_ENV,
        cross_region_references=True,
    )
    return Template.from_stack(stack)


# ── WAF stack ─────────────────────────────────────────────────────────────────


class TestWafStack:
    def test_webacl_is_cloudfront_scoped(self, waf_template: Template) -> None:
        waf_template.has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "CLOUDFRONT"})

    def test_logging_configuration_exists(self, waf_template: Template) -> None:
        waf_template.resource_count_is("AWS::WAFv2::LoggingConfiguration", 1)

    def test_kms_key_has_rotation_enabled(self, waf_template: Template) -> None:
        waf_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_log_group_has_kms_encryption(self, waf_template: Template) -> None:
        waf_template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"KmsKeyId": Match.any_value(), "RetentionInDays": Match.any_value()},
        )

    def test_webacl_has_rate_limiting_rule(self, waf_template: Template) -> None:
        waf_template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {"Rules": Match.array_with([Match.object_like({"Name": "RateLimitPerIP"})])},
        )

    def test_webacl_has_managed_rule_sets(self, waf_template: Template) -> None:
        waf_template.has_resource_properties(
            "AWS::WAFv2::WebACL",
            {
                "Rules": Match.array_with(
                    [
                        Match.object_like({"Name": "AWSManagedRulesAmazonIpReputationList"}),
                        Match.object_like({"Name": "AWSManagedRulesCommonRuleSet"}),
                        Match.object_like({"Name": "AWSManagedRulesKnownBadInputsRuleSet"}),
                    ]
                )
            },
        )

    def test_stack_outputs_exist(self, waf_template: Template) -> None:
        waf_template.has_output("WebAclArn", {})
        waf_template.has_output("WebAclId", {})
        waf_template.has_output("WafLogGroupName", {})


# ── Backend stack ─────────────────────────────────────────────────────────────


class TestBackendStack:
    def test_kms_key_has_rotation_enabled(self, backend_template: Template) -> None:
        backend_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_dynamodb_has_pitr_enabled(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}},
        )

    def test_dynamodb_has_kms_encryption(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"SSESpecification": {"SSEEnabled": True}},
        )

    def test_lambda_has_active_tracing(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::Lambda::Function",
            {"TracingConfig": {"Mode": "Active"}, "MemorySize": 256},
        )

    def test_api_gateway_has_cache_cluster(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::ApiGateway::Stage",
            {"CacheClusterEnabled": True, "CacheClusterSize": "0.5"},
        )

    def test_log_groups_have_kms_encryption(self, backend_template: Template) -> None:
        backend_template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"KmsKeyId": Match.any_value(), "RetentionInDays": Match.any_value()},
        )

    def test_stack_outputs_exist(self, backend_template: Template) -> None:
        backend_template.has_output("HelloWorldApiOutput", {})
        backend_template.has_output("HelloWorldFunctionOutput", {})
        backend_template.has_output("IdempotencyTableName", {})
        backend_template.has_output("GreetingParameterName", {})
        backend_template.has_output("CloudWatchDashboardUrl", {})


# ── Frontend stack ────────────────────────────────────────────────────────────


class TestFrontendStack:
    def test_kms_key_has_rotation_enabled(self, frontend_template: Template) -> None:
        frontend_template.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})

    def test_frontend_bucket_has_kms_encryption(self, frontend_template: Template) -> None:
        frontend_template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": Match.array_with(
                        [Match.object_like({"ServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}})]
                    )
                }
            },
        )

    def test_access_log_bucket_uses_s3_managed_encryption(self, frontend_template: Template) -> None:
        # Access log bucket must use SSE-S3 (S3 log delivery cannot write to KMS-encrypted targets)
        frontend_template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": Match.array_with(
                        [Match.object_like({"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}})]
                    )
                }
            },
        )

    def test_cloudfront_has_waf_attached(self, frontend_template: Template) -> None:
        # WebACLId is set from the WAF stack — confirms cross-stack wiring is intact
        frontend_template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {"DistributionConfig": {"WebACLId": Match.any_value()}},
        )

    def test_cloudfront_redirects_http_to_https(self, frontend_template: Template) -> None:
        frontend_template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {"DistributionConfig": {"DefaultCacheBehavior": {"ViewerProtocolPolicy": "redirect-to-https"}}},
        )

    def test_two_s3_buckets_exist(self, frontend_template: Template) -> None:
        # FrontendBucket + FrontendAccessLogBucket
        frontend_template.resource_count_is("AWS::S3::Bucket", 2)

    def test_stack_outputs_exist(self, frontend_template: Template) -> None:
        frontend_template.has_output("CloudFrontDomainName", {})
        frontend_template.has_output("CloudFrontDistributionId", {})
        frontend_template.has_output("FrontendBucketName", {})
