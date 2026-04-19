from typing import Any, cast

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from cdk_nag import NagSuppressions
from constructs import Construct

from hello_world.hello_world_app import HelloWorldApp
from hello_world.nag_utils import CDK_LAMBDA_SUPPRESSIONS, apply_compliance_aspects


class HelloWorldStack(Stack):
    """Thin wrapper stack composing the :class:`HelloWorldApp` construct.

    Per the CDK best practice "model with constructs, deploy with stacks",
    the domain logic lives in the ``HelloWorldApp`` construct; this stack only
    applies stack-wide compliance Aspects, wires CfnOutputs, and attaches the
    stack-level and singleton-scoped cdk-nag suppressions that cannot be
    expressed on individual resources.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        apply_compliance_aspects(self)

        self.app = HelloWorldApp(self, "App")

        # Expose API URL for consumption by the frontend stack
        self.api_url = self.app.api_url

        CfnOutput(
            self,
            "HelloWorldApiOutput",
            description="API Gateway endpoint URL for Prod stage",
            value=f"{self.app.api.url}hello",
        )
        CfnOutput(
            self,
            "HelloWorldFunctionOutput",
            description="Hello World Lambda Function ARN",
            value=self.app.function.function_arn,
        )
        CfnOutput(
            self,
            "HelloWorldFunctionIamRoleOutput",
            description="IAM Role created for Hello World function",
            value=cast(iam.IRole, self.app.function.role).role_arn,
        )
        CfnOutput(
            self,
            "IdempotencyTableName",
            description="DynamoDB table used for Lambda idempotency",
            value=self.app.idempotency_table.table_name,
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
            value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home#dashboards:name={self.stack_name}",
        )

        # ── Singleton-scoped cdk-nag suppressions ───────────────────────────────
        # CDK-managed singleton Lambdas (AwsCustomResource provider, LogRetention)
        # are created at the stack level as siblings of the construct that
        # requested them, not as children. Path-based suppression is the only
        # way to target them precisely.
        #
        # Stable singleton IDs (derived from CDK source hashes — do not change):
        #   AWS679f53fac002430cb0da5b7982bd2287  — AwsCustomResource provider Lambda
        #   LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a — log retention singleton
        for _singleton_id in (
            "AWS679f53fac002430cb0da5b7982bd2287",
            "LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a",
        ):
            NagSuppressions.add_resource_suppressions_by_path(
                self,
                f"/{self.stack_name}/{_singleton_id}",
                CDK_LAMBDA_SUPPRESSIONS,
                apply_to_children=True,
            )

        # ── Stack-level cdk-nag suppressions (genuinely stack-wide) ─────────────
        NagSuppressions.add_stack_suppressions(
            self,
            [
                # ── AWS Solutions ────────────────────────────────────────────────
                {"id": "AwsSolutions-APIG2", "reason": "Request validation not needed for sample app"},
                {
                    "id": "AwsSolutions-APIG3",
                    "reason": "WAF not attached to API Gateway — applied at CloudFront instead",
                },
                {"id": "AwsSolutions-APIG4", "reason": "Authorization not needed for sample app"},
                {"id": "AwsSolutions-COG4", "reason": "Cognito authorizer not needed for sample app"},
                # ── Serverless ───────────────────────────────────────────────────
                {
                    "id": "Serverless-APIGWDefaultThrottling",
                    "reason": "Custom throttling not configured for sample app",
                },
                {
                    "id": "CdkNagValidationFailure",
                    "reason": "Serverless-APIGWStructuredLogging validation fails due to intrinsic function reference in access log destination — structured JSON logging is configured via logging_format=JSON on the Lambda",
                },
                # ── NIST 800-53 R5 ──────────────────────────────────────────────
                {
                    "id": "NIST.800.53.R5-APIGWAssociatedWithWAF",
                    "reason": "WAF not attached to API Gateway — applied at CloudFront instead",
                },
                {
                    "id": "NIST.800.53.R5-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "NIST.800.53.R5-DynamoDBInBackupPlan",
                    "reason": "AWS Backup plan not configured for sample app — PITR is enabled for point-in-time recovery",
                },
                # ── HIPAA Security ───────────────────────────────────────────────
                {
                    "id": "HIPAA.Security-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
                {
                    "id": "HIPAA.Security-DynamoDBInBackupPlan",
                    "reason": "AWS Backup plan not configured for sample app — PITR is enabled for point-in-time recovery",
                },
                # ── PCI DSS 3.2.1 ────────────────────────────────────────────────
                {
                    "id": "PCI.DSS.321-APIGWAssociatedWithWAF",
                    "reason": "WAF not attached to API Gateway — applied at CloudFront instead",
                },
                {
                    "id": "PCI.DSS.321-APIGWSSLEnabled",
                    "reason": "Client-side SSL certificates not required for sample app",
                },
            ],
        )
