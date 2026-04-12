from typing import Any

from aws_cdk import (
    Aspects,
    CfnOutput,
    RemovalPolicy,
    Stack,
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
    aws_wafv2 as wafv2,
)
from cdk_nag import AwsSolutionsChecks, NagSuppressions, NIST80053R5Checks, ServerlessChecks
from constructs import Construct


class HelloWorldWafStack(Stack):
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

        Aspects.of(self).add(AwsSolutionsChecks(verbose=True))
        Aspects.of(self).add(ServerlessChecks(verbose=True))
        Aspects.of(self).add(NIST80053R5Checks(verbose=True))

        # KMS key for WAF log group encryption.
        # CloudWatch Logs requires the key policy to grant the Logs service
        # principal access so it can encrypt log data on write.
        waf_encryption_key = kms.Key(
            self,
            "WafEncryptionKey",
            description=f"KMS key for {self.stack_name} WAF log group encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        waf_encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                resources=["*"],
            )
        )

        # WAF log group — name must start with "aws-waf-logs-" (AWS requirement).
        # WAFv2 uses its service-linked role (AWSServiceRoleForWAFv2Logging) to
        # write log events; no additional log group resource policy is needed.
        waf_log_group = logs.LogGroup(
            self,
            "WafLogGroup",
            log_group_name=f"aws-waf-logs-{self.stack_name}",
            encryption_key=waf_encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        web_acl = wafv2.CfnWebACL(
            self,
            "WebACL",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{self.stack_name}WebACL",
                sampled_requests_enabled=True,
            ),
            rules=[
                # Blocks IPs with a poor reputation (scanners, botnets, TOR exits)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesAmazonIpReputationList",
                    priority=0,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesAmazonIpReputationList",
                        )
                    ),
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{self.stack_name}-IpReputationList",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Core rule set — protects against OWASP Top 10 web exploits
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesCommonRuleSet",
                    priority=1,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                        )
                    ),
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{self.stack_name}-CommonRuleSet",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Blocks requests containing known malicious inputs (SQLi, XSS patterns)
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesKnownBadInputsRuleSet",
                    priority=2,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesKnownBadInputsRuleSet",
                        )
                    ),
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{self.stack_name}-KnownBadInputs",
                        sampled_requests_enabled=True,
                    ),
                ),
                # Rate limiting — blocks a single IP exceeding 1000 requests per 5 minutes.
                # Prevents scraping, credential stuffing, and unintentional runaway clients.
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIP",
                    priority=3,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=1000,
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

        # Enable WAF logging to the CloudWatch Logs log group.
        wafv2.CfnLoggingConfiguration(
            self,
            "WAFLogging",
            log_destination_configs=[waf_log_group.log_group_arn],
            resource_arn=web_acl.attr_arn,
        )

        # Exposed for HelloWorldFrontendStack to attach to CloudFront.
        # When the frontend stack is in a different region, CDK bridges this
        # value automatically via SSM (cross_region_references=True on the consumer).
        self.web_acl_arn = web_acl.attr_arn

        # ── CloudWatch Logs Insights saved queries ────────────────────────────
        logs.QueryDefinition(
            self,
            "WafBlockedRequests",
            query_definition_name=f"{self.stack_name}/WAF/BlockedRequests",
            query_string=logs.QueryString(
                fields=[
                    "@timestamp",
                    "action",
                    "httpRequest.clientIp",
                    "httpRequest.uri",
                    "httpRequest.httpMethod",
                    "httpRequest.country",
                ],
                filter_statements=["action = 'BLOCK'"],
                sort="@timestamp desc",
                limit=50,
            ),
            log_groups=[waf_log_group],
        )
        logs.QueryDefinition(
            self,
            "WafTopBlockedRules",
            query_definition_name=f"{self.stack_name}/WAF/TopBlockedRules",
            query_string=logs.QueryString(
                filter_statements=["action = 'BLOCK'"],
                stats_statements=["count(*) as blockCount by terminatingRuleId"],
                sort="blockCount desc",
                limit=25,
            ),
            log_groups=[waf_log_group],
        )
        logs.QueryDefinition(
            self,
            "WafRateLimited",
            query_definition_name=f"{self.stack_name}/WAF/RateLimitedIPs",
            query_string=logs.QueryString(
                filter_statements=["terminatingRuleId = 'RateLimitPerIP'"],
                stats_statements=["count(*) as blockCount by httpRequest.clientIp"],
                sort="blockCount desc",
                limit=25,
            ),
            log_groups=[waf_log_group],
        )

        NagSuppressions.add_stack_suppressions(
            self,
            [
                {
                    "id": "NIST.800.53.R5-IAMNoInlinePolicy",
                    "reason": "KMS key grants use inline statements — not directly replaceable with managed policies",
                },
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
            "WafLogGroupName",
            description="CloudWatch Logs log group receiving WAF access logs",
            value=waf_log_group.log_group_name,
        )
