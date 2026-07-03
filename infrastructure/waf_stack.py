from typing import Any

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_wafv2 as wafv2,
)
from constructs import Construct

from infrastructure.nag_utils import (
    acknowledge_rules,
    apply_compliance_aspects,
    build_managed_threat_rules,
    create_auto_delete_objects_log_group,
    create_waf_logs_bucket,
    grant_logs_service_to_key,
)


class WafStack(Stack):
    """WAF WebACL stack, always deployed in us-east-1.

    CloudFront requires its associated WAF WebACL to exist in us-east-1
    regardless of where CloudFront itself or other stacks are deployed.
    Isolating WAF into its own stack allows the backend and frontend stacks
    to be deployed to any region while the WAF constraint is always satisfied.

    The WebACL ARN is exposed as ``web_acl_arn`` for the frontend stack to
    consume. When the frontend stack is in a different region, CDK bridges
    the reference automatically via SSM Parameter Store (cross_region_references=True).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        """Provision the WAF WebACL.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, **kwargs)

        apply_compliance_aspects(self)

        # KMS key encrypting the S3 auto-delete provider's CloudWatch log group
        # (the WAF logs themselves go to S3 — see below). CloudWatch Logs requires
        # the key policy to grant the Logs service principal access.
        waf_encryption_key = kms.Key(
            self,
            "WafEncryptionKey",
            description=f"KMS key for {self.stack_name} provider log group encryption",
            enable_key_rotation=True,
            # See BackendApp.encryption_key for the rationale — automated
            # rotation, no dependent redeploys, 90-day compliance baseline.
            rotation_period=Duration.days(90),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Confused-deputy guard on the CMK's CloudWatch Logs service grant.
        # See ``grant_logs_service_to_key`` in ``nag_utils.py``.
        grant_logs_service_to_key(
            waf_encryption_key,
            region=self.region,
            account=self.account,
            partition=self.partition,
        )

        # WAF logs go to S3 (cheaper long-term retention, WORM-capable) rather
        # than CloudWatch — see README "Audit stack and log retention". The
        # aws-waf-logs-* bucket + its delivery bucket policy are built by the
        # shared helper. Query the logs via Athena (the CloudWatch Logs Insights
        # saved queries that previously sat on a WAF log group were retired with
        # the move — a WAF Glue/Athena table is a documented follow-up in TODO.md).
        waf_logs_bucket = create_waf_logs_bucket(self, "cf")

        web_acl = wafv2.CfnWebACL(
            self,
            "WebACL",
            # Explicit name so the WAF→S3 log path (…/WAFLogs/cloudfront/{name}/) is
            # deterministic — the frontend's Athena Glue table points at it. WAF
            # log delivery uses the WebACL *name* in the S3 key prefix. Pinned
            # physical name: a future replacement-forcing property change collides
            # with the not-yet-deleted old ACL (CFN replacement is
            # create-before-delete), so such a change must also change the name in
            # the same commit — see the AppConfig profile note in backend_app.py.
            name=f"{self.stack_name}-cf",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{self.stack_name}WebACL",
                sampled_requests_enabled=True,
            ),
            rules=[
                # The four AWS managed rule groups (priorities 0-3) are shared with
                # the REGIONAL WebACL on API Gateway — see build_managed_threat_rules
                # in nag_utils.py for why the list lives in one place.
                *build_managed_threat_rules(self.stack_name),
                # Rate limiting — blocks a single client exceeding 200 requests per 5 minutes.
                # Aggregates by plain IP: a CLOUDFRONT-scoped ACL inspects the *viewer*
                # request at the edge, where the source IP already is the real client's —
                # CloudFront only appends X-Forwarded-For later, toward the origin. A
                # FORWARDED_IP aggregation here would make the rule a no-op: browsers don't
                # send XFF, and per the WAF docs a request that is missing the configured
                # header skips the rule entirely (fallback behavior fires only for headers
                # that are present but invalid).
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIP",
                    priority=4,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=200,
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{self.stack_name}-RateLimitPerIP",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )

        # Enable WAF logging to the S3 bucket (destination = the bucket ARN).
        # Order it after the bucket policy so WAF finds the delivery grant already
        # present and leaves the CDK-managed policy alone (see create_waf_logs_bucket).
        waf_logging = wafv2.CfnLoggingConfiguration(
            self,
            "WAFLogging",
            log_destination_configs=[waf_logs_bucket.bucket_arn],
            resource_arn=web_acl.attr_arn,
        )
        if waf_logs_bucket.policy is not None:
            waf_logging.node.add_dependency(waf_logs_bucket.policy)

        # The WAF logs bucket uses auto_delete_objects; give the S3 auto-delete
        # singleton an explicit CMK log group (the helper also suppresses its
        # CDK-managed-singleton nag findings).
        create_auto_delete_objects_log_group(self, waf_encryption_key)

        # Exposed for FrontendStack to attach to CloudFront.
        # When the frontend stack is in a different region, CDK bridges this
        # value automatically via SSM (cross_region_references=True on the consumer).
        self.web_acl_arn = web_acl.attr_arn

        # Stack-level on purpose: when the frontend stack consumes web_acl_arn
        # from another region (cross_region_references=True), CDK lazily adds a
        # Custom::CrossRegionExportWriter provider — role + CDK-generated inline
        # policy — to THIS stack *after* this constructor returns, so a
        # per-resource suppression can never target it. Same-region deploys
        # never create the writer and the suppression is simply unused.
        cross_region_writer_reason = (
            "CDK's CrossRegionExportWriter custom resource (created lazily by "
            "cross_region_references after stack construction) uses a CDK-generated "
            "inline policy on its provider role — not directly configurable"
        )
        acknowledge_rules(
            self,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": cross_region_writer_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": cross_region_writer_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": cross_region_writer_reason},
            ],
        )

        CfnOutput(
            self,
            "WebAclArn",
            description="WAF WebACL ARN — attach to CloudFront distributions in any region",
            value=web_acl.attr_arn,
        )
        CfnOutput(
            self,
            "WebAclId",
            description="WAF WebACL logical ID",
            value=web_acl.attr_id,
        )
        CfnOutput(
            self,
            "WafLogsBucketName",
            description="S3 bucket receiving WAF (CloudFront-scoped WebACL) access logs",
            value=waf_logs_bucket.bucket_name,
        )
