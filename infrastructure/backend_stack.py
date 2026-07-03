from typing import Any, cast

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.backend_app import BackendApp
from infrastructure.nag_utils import (
    AWS_CUSTOM_RESOURCE_PROVIDER_ID,
    acknowledge_rules,
    apply_compliance_aspects,
    attach_async_failure_destination,
    suppress_cdk_singletons,
)

# CDK-managed singleton Lambda construct IDs to apply CDK_LAMBDA_SUPPRESSIONS to.
# The provider ID is shared (nag_utils) so the suppression pass and the async-DLQ
# attachment below provably target the same construct.
_CDK_SINGLETON_IDS = (AWS_CUSTOM_RESOURCE_PROVIDER_ID,)


class BackendStack(Stack):
    """Thin wrapper stack composing the :class:`BackendApp` construct.

    Per the CDK best practice "model with constructs, deploy with stacks",
    the domain logic lives in the ``BackendApp`` construct; this stack only
    applies stack-wide compliance Aspects, wires CfnOutputs, and attaches the
    stack-level and singleton-scoped cdk-nag suppressions that cannot be
    expressed on individual resources.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        idempotency_table: dynamodb.ITableV2,
        is_production_env: bool = True,
        appconfig_monitor: bool = False,
        **kwargs: Any,
    ) -> None:
        """Compose the application construct into a deployable stack.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            idempotency_table: The Powertools idempotency table from the
                separate :class:`DataStack`, forwarded to
                :class:`BackendApp` for cross-stack wiring (env var + grant).
            is_production_env: Forwarded to :class:`BackendApp` — production
                environments route alarm notifications to an SNS topic;
                ephemeral/dev environments skip the topic. Defaults to True so
                direct instantiation (tests, single-environment deploys)
                matches the default ``prod`` deployment environment.
            appconfig_monitor: Forwarded to :class:`BackendApp` — when True,
                the AppConfig flag deployment is gradual and the environment
                carries an alarm rollback monitor. Defaults to False (all-at-once,
                no monitor) so a cold/first deploy always succeeds; see
                :meth:`BackendApp._attach_appconfig_rollback_monitor`.
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, **kwargs)

        apply_compliance_aspects(self)

        self.app = BackendApp(
            self,
            "App",
            idempotency_table=idempotency_table,
            is_production_env=is_production_env,
            appconfig_monitor=appconfig_monitor,
        )

        # Expose API URL + ID for consumption by the frontend stack (api_id lets the
        # frontend CSP pin the exact execute-api host instead of a region wildcard).
        self.api_url = self.app.api_url
        self.api_id = self.app.api.rest_api_id

        CfnOutput(
            self,
            "ApiUrlOutput",
            description="API Gateway endpoint URL for Prod stage",
            value=f"{self.app.api.url}greeting",
        )
        CfnOutput(
            self,
            "FunctionArnOutput",
            description="ARN of the Lambda function backing the greeting API",
            value=self.app.function.function_arn,
        )
        CfnOutput(
            self,
            "FunctionIamRoleOutput",
            description="IAM Role created for Serverless App function",
            value=cast(iam.IRole, self.app.function.role).role_arn,
        )
        CfnOutput(
            self,
            "GreetingParameterName",
            description="SSM parameter name for the greeting message",
            value=self.app.greeting_param.parameter_name,
        )
        CfnOutput(
            self,
            "AppConfigAppName",
            description="AppConfig application name for feature flags",
            value=self.app.app_config_app.name,
        )
        CfnOutput(
            self,
            "CloudWatchDashboardUrl",
            description="CloudWatch dashboard URL for this stack",
            value=(
                f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
                f"?region={self.region}#dashboards/dashboard/{self.stack_name}"
            ),
        )
        # Only present in production environments — non-prod skips the alarm
        # topic entirely (see BackendApp.__init__). Surfaced so operators
        # can attach subscriptions (email/Chatbot/PagerDuty) without console
        # archaeology.
        if self.app.alarm_topic is not None:
            CfnOutput(
                self,
                "AlarmTopicName",
                description="SNS topic that CloudWatch alarms publish to (attach subscriptions here)",
                value=self.app.alarm_topic.topic_name,
            )

        # ── Singleton-scoped cdk-nag suppressions ───────────────────────────────
        # CDK-managed singleton Lambdas (currently just the AwsCustomResource
        # provider) are created at the stack level as siblings of the construct
        # that requested them, not as children. ``suppress_cdk_singletons`` looks
        # them up via ``try_find_child`` so the suppressions keep working
        # regardless of whether the stack is at the App root or nested inside
        # a cdk.Stage. (The LogRetention singleton was eliminated when log
        # groups were made explicit via ``log_group=`` everywhere.)
        suppress_cdk_singletons(self, _CDK_SINGLETON_IDS)

        # ── Async failure destination for the AwsCustomResource provider ────────
        # CFN invokes the provider Lambda asynchronously; without an on_failure
        # destination, a crash that exhausts Lambda's two automatic retries is
        # silently dropped — only the CFN rollback error remains. Capturing the
        # failed event envelope to SQS preserves the AWS API response and full
        # request payload for post-mortem.
        # attach_async_failure_destination also emits a CfnOutput of the DLQ URL,
        # so the returned queue is surfaced for operators rather than unused.
        self.cr_provider_dlq = attach_async_failure_destination(
            self,
            AWS_CUSTOM_RESOURCE_PROVIDER_ID,
            encryption_key=self.app.encryption_key,
            queue_id="AwsCustomResourceProviderDlq",
        )

        # ── Stack-level cdk-nag suppressions (genuinely stack-wide) ─────────────
        acknowledge_rules(
            self,
            [
                # ── AWS Solutions ────────────────────────────────────────────────
                {"id": "AwsSolutions-APIG2", "reason": "Request validation not needed for sample app"},
                # AwsSolutions-APIG3 (WAF on API Gateway) is no longer suppressed —
                # a REGIONAL WebACL is now associated with the Prod stage in
                # BackendApp._attach_regional_waf, in addition to the
                # CloudFront-scoped ACL.
                {"id": "AwsSolutions-APIG4", "reason": "Authorization not needed for sample app"},
                {"id": "AwsSolutions-COG4", "reason": "Cognito authorizer not needed for sample app"},
                # ── Serverless ───────────────────────────────────────────────────
                # Serverless-APIGWDefaultThrottling is no longer suppressed —
                # stage-level throttling_rate_limit / throttling_burst_limit are
                # configured on the Prod stage in BackendApp.
                {
                    "id": "CdkNagValidationFailure",
                    "reason": "Serverless-APIGWStructuredLogging validation fails due to intrinsic function reference in access log destination — structured JSON logging is configured via logging_format=JSON on the Lambda",
                },
                # ── NIST 800-53 R5 ──────────────────────────────────────────────
                # NIST.800.53.R5-APIGWAssociatedWithWAF is no longer suppressed —
                # a REGIONAL WebACL is associated with the Prod stage (see
                # BackendApp._attach_regional_waf).
                {
                    "id": "NIST.800.53.R5-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "NIST.800.53.R5-APIGWCacheEnabledAndEncrypted",
                    "reason": (
                        "API Gateway cache cluster intentionally disabled for cost reasons — the smallest "
                        "0.5 GB cluster is ~$14/month for a sample app. Caching GET /greeting would also serve "
                        "stale values across SSM parameter and AppConfig feature-flag changes."
                    ),
                },
                # DynamoDBInBackupPlan suppressions moved to DataStack —
                # the idempotency table (and its backup posture) now lives there.
                # ── HIPAA Security ───────────────────────────────────────────────
                {
                    "id": "HIPAA.Security-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "HIPAA.Security-APIGWCacheEnabledAndEncrypted",
                    "reason": (
                        "API Gateway cache cluster intentionally disabled for cost reasons — "
                        "see NIST.800.53.R5-APIGWCacheEnabledAndEncrypted rationale above."
                    ),
                },
                # ── PCI DSS 3.2.1 ────────────────────────────────────────────────
                # PCI.DSS.321-APIGWAssociatedWithWAF is no longer suppressed —
                # a REGIONAL WebACL is associated with the Prod stage (see
                # BackendApp._attach_regional_waf).
                {
                    "id": "PCI.DSS.321-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "PCI.DSS.321-APIGWCacheEnabledAndEncrypted",
                    "reason": (
                        "API Gateway cache cluster intentionally disabled for cost reasons — "
                        "see NIST.800.53.R5-APIGWCacheEnabledAndEncrypted rationale above."
                    ),
                },
            ],
        )
