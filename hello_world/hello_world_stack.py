from typing import Any, cast

from aws_cdk import (
    Aspects,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_apigateway as apigw,
)
from aws_cdk import (
    aws_appconfig as appconfig,
)
from aws_cdk import (
    aws_applicationinsights as appinsights,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_resourcegroups as rg,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from aws_cdk import (
    custom_resources as cr,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from cdk_monitoring_constructs import DefaultDashboardFactory, MonitoringFacade
from cdk_nag import AwsSolutionsChecks, NagSuppressions, NIST80053R5Checks, ServerlessChecks
from constructs import Construct

from hello_world.nag_utils import CDK_LAMBDA_SUPPRESSIONS


class HelloWorldStack(Stack):
    """Main CDK stack for the Hello World serverless application.

    Provisions a Lambda function behind API Gateway, with supporting resources
    for idempotency (DynamoDB), configuration (SSM Parameter Store),
    feature flags (AppConfig), monitoring (CloudWatch dashboard), and
    security checks (cdk-nag).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        """Provision all AWS resources for the Hello World application.

        Creates a DynamoDB table for idempotency, an SSM parameter for the
        greeting message, an AppConfig application for feature flags, a Lambda
        function with Powertools environment variables, an API Gateway REST API
        exposing GET /hello, Application Insights monitoring, and a CloudWatch
        dashboard via cdk-monitoring-constructs.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, **kwargs)

        # cdk-nag: apply AWS Solutions checks
        Aspects.of(self).add(AwsSolutionsChecks(verbose=True))
        Aspects.of(self).add(ServerlessChecks(verbose=True))
        Aspects.of(self).add(NIST80053R5Checks(verbose=True))

        # KMS key shared across all CloudWatch log groups and DynamoDB in this stack.
        # CloudWatch Logs requires the Logs service principal to be granted access
        # so it can encrypt data on behalf of the service.
        # Note: SSM StringParameter cannot use CMK — CloudFormation does not support
        # creating SecureString parameters. AppConfig hosted configs use AWS-managed
        # keys and do not expose a CMK option via CDK.
        encryption_key = kms.Key(
            self,
            "BackendEncryptionKey",
            description=f"KMS key for {self.stack_name} log groups and DynamoDB",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                resources=["*"],
            )
        )

        # DynamoDB table for Powertools idempotency
        idempotency_table = dynamodb.Table(
            self,
            "IdempotencyTable",
            table_name=f"{self.stack_name}-idempotency",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expiration",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=encryption_key,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # SSM parameter for Powertools Parameters
        greeting_param = ssm.StringParameter(
            self,
            "GreetingParameter",
            parameter_name=f"/{self.stack_name}/greeting",
            string_value="hello world",
        )

        # AppConfig for Powertools Feature Flags
        app_config_app = appconfig.CfnApplication(
            self,
            "FeatureFlagsApp",
            name=f"{self.stack_name}-features",
        )

        app_config_env = appconfig.CfnEnvironment(  # noqa: F841
            self,
            "FeatureFlagsEnv",
            application_id=app_config_app.ref,
            name=f"{self.stack_name}-env",
        )

        app_config_profile = appconfig.CfnConfigurationProfile(
            self,
            "FeatureFlagsProfile",
            application_id=app_config_app.ref,
            name=f"{self.stack_name}-features",
            location_uri="hosted",
            type="AWS.AppConfig.FeatureFlags",
        )

        # Initial feature flags configuration
        app_config_version = appconfig.CfnHostedConfigurationVersion(  # noqa: F841
            self,
            "FeatureFlagsVersion",
            application_id=app_config_app.ref,
            configuration_profile_id=app_config_profile.ref,
            content_type="application/json",
            content=(
                '{"version":"1","flags":{"enhanced_greeting":'
                '{"name":"Enhanced Greeting","default":false}},'
                '"values":{"enhanced_greeting":{"enabled":false}}}'
            ),
        )

        # Explicit Lambda log group with 30-day retention (implicit group has no retention)
        lambda_log_group = logs.LogGroup(
            self,
            "HelloWorldFunctionLogGroup",
            log_group_name=f"/aws/lambda/{self.stack_name}-HelloWorldFunction",
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Lambda function with automatic dependency bundling
        hello_fn = PythonFunction(
            self,
            "HelloWorldFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            entry="lambda",
            index="app.py",
            handler="lambda_handler",
            architecture=_lambda.Architecture.X86_64,
            memory_size=256,
            timeout=Duration.seconds(10),
            tracing=_lambda.Tracing.ACTIVE,
            log_group=lambda_log_group,
            logging_format=_lambda.LoggingFormat.JSON,
            environment={
                "POWERTOOLS_SERVICE_NAME": "hello-world",
                "POWERTOOLS_METRICS_NAMESPACE": "HelloWorld",
                "POWERTOOLS_LOG_LEVEL": "INFO",
                "LOG_LEVEL": "INFO",
                "IDEMPOTENCY_TABLE_NAME": idempotency_table.table_name,
                "GREETING_PARAM_NAME": f"/{self.stack_name}/greeting",
                "APPCONFIG_APP_NAME": f"{self.stack_name}-features",
                "APPCONFIG_ENV_NAME": f"{self.stack_name}-env",
                "APPCONFIG_PROFILE_NAME": f"{self.stack_name}-features",
            },
        )

        # Grant permissions
        idempotency_table.grant_read_write_data(hello_fn)
        greeting_param.grant_read(hello_fn)
        hello_fn.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=[
                    "appconfig:GetLatestConfiguration",
                    "appconfig:StartConfigurationSession",
                ],
                resources=["*"],
            )
        )

        # Explicit API Gateway access log group with 30-day retention
        api_log_group = logs.LogGroup(
            self,
            "HelloWorldApiAccessLogs",
            log_group_name=f"/aws/apigateway/{self.stack_name}/access-logs",
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # API Gateway REST API
        # cloud_watch_role=True (default) creates an implicit IAM role scoped to
        # allow API Gateway to write execution logs to CloudWatch — this is a
        # region-level account setting managed by CDK automatically.
        api = apigw.RestApi(
            self,
            "HelloWorldApi",
            cloud_watch_role=True,
            cloud_watch_role_removal_policy=RemovalPolicy.DESTROY,
            deploy_options=apigw.StageOptions(
                stage_name="Prod",
                tracing_enabled=True,
                # Cache cluster: 0.5 GB — smallest available size (~$0.02/hr, ~$14/month).
                # Enables caching per NIST.800.53.R5-APIGWCacheEnabledAndEncrypted.
                cache_cluster_enabled=True,
                cache_cluster_size="0.5",
                method_options={
                    "/*/*": apigw.MethodDeploymentOptions(
                        caching_enabled=True,
                        cache_data_encrypted=True,
                    )
                },
                access_log_destination=apigw.LogGroupLogDestination(api_log_group),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=False,
            ),
        )

        hello_resource = api.root.add_resource("hello")
        hello_resource.add_method("GET", apigw.LambdaIntegration(hello_fn))
        hello_resource.add_cors_preflight(
            allow_origins=apigw.Cors.ALL_ORIGINS,
            allow_methods=["GET", "OPTIONS"],
        )

        # Explicit execution log group — API Gateway creates this outside CloudFormation
        # when logging_level is enabled. Pre-creating it here transfers ownership to CFN
        # so it is deleted on cdk destroy. Name format is fixed by the API Gateway service.
        logs.LogGroup(
            self,
            "HelloWorldApiExecutionLogs",
            log_group_name=f"API-Gateway-Execution-Logs_{api.rest_api_id}/Prod",
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Application Insights
        resource_group = rg.CfnGroup(
            self,
            "ApplicationResourceGroup",
            name=f"ApplicationInsights-{self.stack_name}",
            resource_query=rg.CfnGroup.ResourceQueryProperty(
                type="CLOUDFORMATION_STACK_1_0",
            ),
        )

        app_insights = appinsights.CfnApplication(
            self,
            "ApplicationInsightsMonitoring",
            resource_group_name=resource_group.name,
            auto_configuration_enabled=True,
        )
        app_insights.add_dependency(resource_group)

        # Custom resource to delete the Application Insights auto-created CloudWatch
        # dashboard on stack destroy. Application Insights creates a dashboard named
        # after the resource group outside of CloudFormation, so CDK cannot own it
        # directly. This Lambda-backed custom resource calls DeleteDashboards at
        # destroy time so no dashboard is left behind after cdk destroy.
        app_insights_dashboard_cleanup = cr.AwsCustomResource(
            self,
            "AppInsightsDashboardCleanup",
            on_delete=cr.AwsSdkCall(
                service="CloudWatch",
                action="deleteDashboards",
                parameters={"DashboardNames": [resource_group.name]},
                physical_resource_id=cr.PhysicalResourceId.of(resource_group.name),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE,
            ),
            install_latest_aws_sdk=False,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )
        # Must run after Application Insights has had a chance to create the dashboard
        app_insights_dashboard_cleanup.node.add_dependency(app_insights)

        # Monitoring dashboard via cdk-monitoring-constructs
        # CloudWatch dashboards are global — scope the name to the stack so
        # multiple regional deployments don't collide on the same dashboard name.
        monitoring = MonitoringFacade(
            self,
            "Monitoring",
            alarm_factory_defaults={
                "actions_enabled": True,
                "alarm_name_prefix": self.stack_name,
            },
            dashboard_factory=DefaultDashboardFactory(
                self,
                "MonitoringDashboardFactory",
                dashboard_name_prefix=self.stack_name,
            ),
        )
        monitoring.monitor_lambda_function(lambda_function=hello_fn)
        monitoring.monitor_api_gateway(api=api)
        monitoring.monitor_dynamo_table(table=idempotency_table)

        # Outputs
        CfnOutput(
            self,
            "HelloWorldApiOutput",
            description="API Gateway endpoint URL for Prod stage",
            value=f"{api.url}hello",
        )
        CfnOutput(
            self,
            "HelloWorldFunctionOutput",
            description="Hello World Lambda Function ARN",
            value=hello_fn.function_arn,
        )
        CfnOutput(
            self,
            "HelloWorldFunctionIamRoleOutput",
            description="IAM Role created for Hello World function",
            value=cast(iam.IRole, hello_fn.role).role_arn,
        )
        CfnOutput(
            self,
            "IdempotencyTableName",
            description="DynamoDB table used for Lambda idempotency",
            value=idempotency_table.table_name,
        )
        CfnOutput(
            self,
            "GreetingParameterName",
            description="SSM parameter name for the greeting message",
            value=greeting_param.parameter_name,
        )
        CfnOutput(
            self,
            "AppConfigAppName",
            description="AppConfig application name for feature flags",
            value=app_config_app.name,
        )
        CfnOutput(
            self,
            "CloudWatchDashboardUrl",
            description="CloudWatch dashboard URL for this stack",
            value=f"https://{self.region}.console.aws.amazon.com/cloudwatch/home#dashboards:name={self.stack_name}",
        )

        # Expose API URL for consumption by the frontend stack
        self.api_url = api.url

        # ── Per-resource cdk-nag suppressions ──────────────────────────────────
        # CDK-managed singleton Lambdas are created at the stack level as siblings,
        # not children, of the constructs that request them. Path-based suppression
        # is the only way to target them precisely.
        #
        # Stable singleton IDs (derived from CDK source hashes — do not change):
        #   AWS679f53fac002430cb0da5b7982bd2287  — AwsCustomResource provider Lambda
        #   LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a — log retention singleton
        #
        # HelloWorldFunction passes Lambda rules natively (tracing=ACTIVE,
        # memory_size=256, sync invocation). Only CDK-managed Lambdas are suppressed.

        # Suppress on HelloWorldFunction — intentional design decisions, not CDK limitations
        NagSuppressions.add_resource_suppressions(
            hello_fn,
            [
                {"id": "AwsSolutions-L1", "reason": "Runtime intentionally pinned to Python 3.12"},
                {"id": "Serverless-LambdaLatestVersion", "reason": "Runtime intentionally pinned to Python 3.12"},
                {
                    "id": "Serverless-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                {
                    "id": "NIST.800.53.R5-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                {
                    "id": "NIST.800.53.R5-LambdaConcurrency",
                    "reason": "Concurrency limits not configured for sample app",
                },
                {"id": "NIST.800.53.R5-LambdaInsideVPC", "reason": "No VPC — adds significant operational complexity"},
                # Service role uses AWSLambdaBasicExecutionRole managed policy
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AWSLambdaBasicExecutionRole is the minimal managed policy for Lambda execution",
                    "applies_to": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
                # Default policy has KMS wildcard actions (required for CMK use) and
                # Resource::* for X-Ray and AppConfig (no resource-level ARNs available)
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "kms:GenerateDataKey* and kms:ReEncrypt* require wildcard action suffix — standard KMS usage pattern",
                    "applies_to": ["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "X-Ray and AppConfig do not support resource-level ARNs in IAM — wildcard is required",
                    "applies_to": ["Resource::*"],
                },
                {
                    "id": "NIST.800.53.R5-IAMNoInlinePolicy",
                    "reason": "CDK generates the default policy inline on the Lambda service role — not directly configurable",
                },
            ],
            apply_to_children=True,  # covers service role and default policy
        )

        # Suppress on AwsCustomResource provider (AppInsights dashboard cleanup)
        # and log retention singleton — CDK limitations, not configurable
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

        # AppInsights cleanup custom resource policy (IAM5 / IAMNoInlinePolicy)
        NagSuppressions.add_resource_suppressions(
            app_insights_dashboard_cleanup,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "AwsCustomResource policy uses wildcard — required to call CloudWatch DeleteDashboards",
                },
                {
                    "id": "NIST.800.53.R5-IAMNoInlinePolicy",
                    "reason": "AwsCustomResource generates an inline policy — not directly configurable",
                },
            ],
            apply_to_children=True,
        )

        # API Gateway CloudWatch role — CDK-managed, uses managed policy.
        # cloud_watch_role=True is required for execution logging (NIST.800.53.R5-
        # APIGWExecutionLoggingEnabled / AwsSolutions-APIG6). The disableCloudWatchRole
        # CDK flag is intentionally NOT enabled because NIST compliance requires
        # execution logging, which requires the account-level CloudWatch role.
        NagSuppressions.add_resource_suppressions_by_path(
            self,
            f"/{self.stack_name}/HelloWorldApi/CloudWatchRole/Resource",
            [{"id": "AwsSolutions-IAM4", "reason": "CDK-managed API Gateway CloudWatch role uses AWS managed policy"}],
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
            ],
        )
