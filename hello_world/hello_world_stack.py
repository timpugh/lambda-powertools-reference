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
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from cdk_monitoring_constructs import DefaultDashboardFactory, MonitoringFacade
from cdk_nag import AwsSolutionsChecks, NagSuppressions
from constructs import Construct


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

        # DynamoDB table for Powertools idempotency
        idempotency_table = dynamodb.Table(
            self,
            "IdempotencyTable",
            table_name=f"{self.stack_name}-idempotency",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expiration",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
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
            name="production",
        )

        app_config_profile = appconfig.CfnConfigurationProfile(
            self,
            "FeatureFlagsProfile",
            application_id=app_config_app.ref,
            name="features",
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
                "APPCONFIG_ENV_NAME": "production",
                "APPCONFIG_PROFILE_NAME": "features",
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

        # Expose API URL for consumption by the frontend stack
        self.api_url = api.url

        # cdk-nag suppressions for hello-world sample app
        NagSuppressions.add_stack_suppressions(
            self,
            [
                {"id": "AwsSolutions-APIG2", "reason": "Request validation not needed for sample app"},
                {"id": "AwsSolutions-APIG3", "reason": "WAF not needed for sample app"},
                {"id": "AwsSolutions-APIG4", "reason": "Authorization not needed for sample app"},
                {"id": "AwsSolutions-COG4", "reason": "Cognito authorizer not needed for sample app"},
                {"id": "AwsSolutions-IAM4", "reason": "Managed policies acceptable for sample app"},
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Wildcard permissions for X-Ray and AppConfig",
                },
                {"id": "AwsSolutions-L1", "reason": "Runtime version is intentionally pinned"},
            ],
        )
