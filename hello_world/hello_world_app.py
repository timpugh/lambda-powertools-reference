"""HelloWorldApp construct — the domain-level application.

Encapsulates all resources that make up the Hello World serverless application:
KMS key, DynamoDB idempotency table, SSM greeting parameter, AppConfig feature
flags, Lambda function, API Gateway, Application Insights monitoring, dashboard,
Logs Insights saved queries, and per-resource cdk-nag suppressions.

Following the CDK best practice "model with constructs, deploy with stacks":
the Stack only composes this construct, applies stack-wide Aspects, and wires
outputs. Any deployment shape (multiple copies in one stack, multi-tenant,
dev-next-to-prod) can be achieved by instantiating this construct multiple
times without subclassing the Stack.
"""

from typing import cast

from aws_cdk import (
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
from cdk_nag import NagSuppressions
from constructs import Construct


class HelloWorldApp(Construct):
    """Domain-level Hello World application.

    Exposes the top-level resources as public attributes so the enclosing
    Stack can reference them for CfnOutputs and cross-stack wiring.
    """

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        stack = Stack.of(self)

        # KMS key shared across CloudWatch log groups and DynamoDB in this app.
        # CloudWatch Logs requires the Logs service principal to be granted access
        # so it can encrypt data on behalf of the service.
        # Note: SSM StringParameter cannot use CMK — CloudFormation does not support
        # creating SecureString parameters. AppConfig hosted configs use AWS-managed
        # keys and do not expose a CMK option via CDK.
        self.encryption_key = kms.Key(
            self,
            "EncryptionKey",
            description=f"KMS key for {stack.stack_name} log groups and DynamoDB",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
                principals=[iam.ServicePrincipal(f"logs.{stack.region}.amazonaws.com")],
                resources=["*"],
            )
        )

        # DynamoDB table for Powertools idempotency.
        # No table_name set — CDK generates one. Avoids blocking replacement-style
        # schema changes and two deployments colliding in one account.
        self.idempotency_table = dynamodb.Table(
            self,
            "IdempotencyTable",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expiration",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.encryption_key,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # SSM parameter for Powertools Parameters.
        # parameter_name omitted so CDK auto-generates. Lambda reads the value
        # through the GREETING_PARAM_NAME env var, so the name doesn't need to
        # be human-memorable.
        self.greeting_param = ssm.StringParameter(
            self,
            "GreetingParameter",
            string_value="hello world",
        )

        # AppConfig for Powertools Feature Flags
        self.app_config_app = appconfig.CfnApplication(
            self,
            "FeatureFlagsApp",
            name=f"{stack.stack_name}-features",
        )

        app_config_env = appconfig.CfnEnvironment(  # noqa: F841
            self,
            "FeatureFlagsEnv",
            application_id=self.app_config_app.ref,
            name=f"{stack.stack_name}-env",
        )

        app_config_profile = appconfig.CfnConfigurationProfile(
            self,
            "FeatureFlagsProfile",
            application_id=self.app_config_app.ref,
            name=f"{stack.stack_name}-features",
            location_uri="hosted",
            type="AWS.AppConfig.FeatureFlags",
        )

        # Initial feature flags configuration
        app_config_version = appconfig.CfnHostedConfigurationVersion(  # noqa: F841
            self,
            "FeatureFlagsVersion",
            application_id=self.app_config_app.ref,
            configuration_profile_id=app_config_profile.ref,
            content_type="application/json",
            content=(
                '{"version":"1","flags":{"enhanced_greeting":'
                '{"name":"Enhanced Greeting","default":false}},'
                '"values":{"enhanced_greeting":{"enabled":false}}}'
            ),
        )

        # Explicit Lambda log group with 30-day retention (implicit group has no retention).
        # log_group_name omitted — CDK auto-generates a unique name and wires it into the
        # Lambda function via the log_group property below.
        lambda_log_group = logs.LogGroup(
            self,
            "HelloWorldFunctionLogGroup",
            encryption_key=self.encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Lambda function with automatic dependency bundling
        self.function = PythonFunction(
            self,
            "HelloWorldFunction",
            runtime=_lambda.Runtime.PYTHON_3_13,
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
                "IDEMPOTENCY_TABLE_NAME": self.idempotency_table.table_name,
                "GREETING_PARAM_NAME": self.greeting_param.parameter_name,
                "APPCONFIG_APP_NAME": f"{stack.stack_name}-features",
                "APPCONFIG_ENV_NAME": f"{stack.stack_name}-env",
                "APPCONFIG_PROFILE_NAME": f"{stack.stack_name}-features",
            },
        )

        # Grant permissions
        self.idempotency_table.grant_read_write_data(self.function)
        self.greeting_param.grant_read(self.function)
        self.function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=[
                    "appconfig:GetLatestConfiguration",
                    "appconfig:StartConfigurationSession",
                ],
                resources=["*"],
            )
        )

        # Explicit API Gateway access log group with 30-day retention.
        # log_group_name omitted — CDK auto-generates and passes it into the
        # RestApi via LogGroupLogDestination below.
        api_log_group = logs.LogGroup(
            self,
            "HelloWorldApiAccessLogs",
            encryption_key=self.encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # API Gateway REST API
        # cloud_watch_role=True (default) creates an implicit IAM role scoped to
        # allow API Gateway to write execution logs to CloudWatch — this is a
        # region-level account setting managed by CDK automatically.
        self.api = apigw.RestApi(
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
                access_log_format=apigw.AccessLogFormat.custom(
                    # Built from typed AccessLogField references — json_with_standard_fields
                    # only supports 10 fixed fields; custom() is the CDK API for extended formats.
                    "{"
                    + ",".join(
                        [
                            f'"requestId":"{apigw.AccessLogField.context_request_id()}"',
                            f'"accountId":"{apigw.AccessLogField.context_owner_account_id()}"',
                            f'"apiId":"{apigw.AccessLogField.context_api_id()}"',
                            f'"stage":"{apigw.AccessLogField.context_stage()}"',
                            f'"resourcePath":"{apigw.AccessLogField.context_resource_path()}"',
                            f'"httpMethod":"{apigw.AccessLogField.context_http_method()}"',
                            f'"protocol":"{apigw.AccessLogField.context_protocol()}"',
                            f'"status":"{apigw.AccessLogField.context_status()}"',
                            f'"responseType":"{apigw.AccessLogField.context_error_response_type()}"',
                            f'"errorMessage":"{apigw.AccessLogField.context_error_message()}"',
                            f'"requestTime":"{apigw.AccessLogField.context_request_time()}"',
                            f'"ip":"{apigw.AccessLogField.context_identity_source_ip()}"',
                            f'"caller":"{apigw.AccessLogField.context_identity_caller()}"',
                            f'"user":"{apigw.AccessLogField.context_identity_user()}"',
                            f'"responseLength":"{apigw.AccessLogField.context_response_length()}"',
                            f'"xrayTraceId":"{apigw.AccessLogField.context_xray_trace_id()}"',
                        ]
                    )
                    + "}"
                ),
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=False,
            ),
        )

        hello_resource = self.api.root.add_resource("hello")
        hello_resource.add_method("GET", apigw.LambdaIntegration(self.function))
        hello_resource.add_cors_preflight(
            allow_origins=apigw.Cors.ALL_ORIGINS,
            allow_methods=["GET", "OPTIONS"],
            # X-Amzn-Trace-Id is required for CloudWatch RUM to propagate the
            # client-side X-Ray trace header into the API Gateway → Lambda
            # segments so the browser and backend appear on the same trace.
            allow_headers=[*apigw.Cors.DEFAULT_HEADERS, "X-Amzn-Trace-Id"],
        )

        # Explicit execution log group — API Gateway creates this outside CloudFormation
        # when logging_level is enabled. Pre-creating it here transfers ownership to CFN
        # so it is deleted on cdk destroy. Name format is fixed by the API Gateway service.
        logs.LogGroup(
            self,
            "HelloWorldApiExecutionLogs",
            log_group_name=f"API-Gateway-Execution-Logs_{self.api.rest_api_id}/Prod",
            encryption_key=self.encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self._create_insights_queries(lambda_log_group, api_log_group)

        # Application Insights
        resource_group = rg.CfnGroup(
            self,
            "ApplicationResourceGroup",
            name=f"ApplicationInsights-{stack.stack_name}",
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
                "alarm_name_prefix": stack.stack_name,
            },
            dashboard_factory=DefaultDashboardFactory(
                self,
                "MonitoringDashboardFactory",
                dashboard_name_prefix=stack.stack_name,
            ),
        )
        monitoring.monitor_lambda_function(lambda_function=self.function)
        monitoring.monitor_api_gateway(api=self.api)
        monitoring.monitor_dynamo_table(table=self.idempotency_table)

        # Expose API URL for consumption by the enclosing stack and cross-stack refs
        self.api_url = self.api.url

        self._add_resource_suppressions(app_insights_dashboard_cleanup)

    def _add_resource_suppressions(self, app_insights_dashboard_cleanup: cr.AwsCustomResource) -> None:
        """Attach per-resource cdk-nag suppressions for resources owned by this construct.

        HelloWorldFunction passes Lambda rules natively (tracing=ACTIVE,
        memory_size=256, sync invocation). Suppressions below document the
        intentional design decisions (no VPC, no DLQ, no concurrency) and work
        around CDK-level limitations (inline policies, KMS wildcard actions).
        """
        NagSuppressions.add_resource_suppressions(
            self.function,
            [
                # cdk-nag has not updated its rule to recognize Python 3.13 as the latest Lambda runtime
                {
                    "id": "AwsSolutions-L1",
                    "reason": "Python 3.13 is the latest Lambda runtime — cdk-nag rule not yet updated",
                },
                {
                    "id": "Serverless-LambdaLatestVersion",
                    "reason": "Python 3.13 is the latest Lambda runtime — cdk-nag rule not yet updated",
                },
                {
                    "id": "Serverless-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                {
                    "id": "NIST.800.53.R5-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                {
                    "id": "HIPAA.Security-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                {
                    "id": "NIST.800.53.R5-LambdaConcurrency",
                    "reason": "Concurrency limits not configured for sample app",
                },
                {
                    "id": "HIPAA.Security-LambdaConcurrency",
                    "reason": "Concurrency limits not configured for sample app",
                },
                {"id": "NIST.800.53.R5-LambdaInsideVPC", "reason": "No VPC — adds significant operational complexity"},
                {"id": "HIPAA.Security-LambdaInsideVPC", "reason": "No VPC — adds significant operational complexity"},
                {"id": "PCI.DSS.321-LambdaInsideVPC", "reason": "No VPC — adds significant operational complexity"},
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
                {
                    "id": "HIPAA.Security-IAMNoInlinePolicy",
                    "reason": "CDK generates the default policy inline on the Lambda service role — not directly configurable",
                },
                {
                    "id": "PCI.DSS.321-IAMNoInlinePolicy",
                    "reason": "CDK generates the default policy inline on the Lambda service role — not directly configurable",
                },
            ],
            apply_to_children=True,  # covers service role and default policy
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
                {
                    "id": "HIPAA.Security-IAMNoInlinePolicy",
                    "reason": "AwsCustomResource generates an inline policy — not directly configurable",
                },
                {
                    "id": "PCI.DSS.321-IAMNoInlinePolicy",
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
        api_cw_role = self.api.node.try_find_child("CloudWatchRole")
        if api_cw_role is not None:
            NagSuppressions.add_resource_suppressions(
                cast(Construct, api_cw_role),
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": "CDK-managed API Gateway CloudWatch role uses AWS managed policy",
                    }
                ],
                apply_to_children=True,
            )

    def _create_insights_queries(self, lambda_log_group: logs.LogGroup, api_log_group: logs.LogGroup) -> None:
        """Create CloudWatch Logs Insights saved queries for Lambda and API Gateway."""
        stack_name = Stack.of(self).stack_name
        # ── Lambda queries ────────────────────────────────────────────────────
        logs.QueryDefinition(
            self,
            "LambdaRecentErrors",
            query_definition_name=f"{stack_name}/Lambda/RecentErrors",
            query_string=logs.QueryString(
                fields=[
                    "@timestamp",
                    "level",
                    "message",
                    "xray_trace_id",
                    "function_request_id",
                    "exception",
                    "exception_name",
                ],
                filter_statements=["level = 'ERROR'"],
                sort="@timestamp desc",
                limit=50,
            ),
            log_groups=[lambda_log_group],
        )
        logs.QueryDefinition(
            self,
            "LambdaColdStarts",
            query_definition_name=f"{stack_name}/Lambda/ColdStarts",
            query_string=logs.QueryString(
                fields=["@timestamp", "function_name", "function_request_id", "xray_trace_id"],
                filter_statements=["cold_start = true"],
                sort="@timestamp desc",
                limit=50,
            ),
            log_groups=[lambda_log_group],
        )
        logs.QueryDefinition(
            self,
            "LambdaSlowInvocations",
            query_definition_name=f"{stack_name}/Lambda/SlowInvocations",
            query_string=logs.QueryString(
                fields=["@timestamp", "@duration", "function_request_id", "xray_trace_id", "message"],
                filter_statements=["@duration > 3000"],
                sort="@duration desc",
                limit=50,
            ),
            log_groups=[lambda_log_group],
        )

        # ── API Gateway queries ───────────────────────────────────────────────
        logs.QueryDefinition(
            self,
            "ApiGatewayErrors",
            query_definition_name=f"{stack_name}/ApiGateway/4xx5xxErrors",
            query_string=logs.QueryString(
                fields=[
                    "@timestamp",
                    "status",
                    "httpMethod",
                    "resourcePath",
                    "errorMessage",
                    "responseType",
                    "ip",
                    "xrayTraceId",
                    "requestId",
                ],
                filter_statements=["status >= 400"],
                sort="@timestamp desc",
                limit=50,
            ),
            log_groups=[api_log_group],
        )
        logs.QueryDefinition(
            self,
            "ApiGatewayRequestsByIp",
            query_definition_name=f"{stack_name}/ApiGateway/RequestsByIP",
            query_string=logs.QueryString(
                fields=["ip"],
                stats_statements=["count(*) as requestCount by ip"],
                sort="requestCount desc",
                limit=25,
            ),
            log_groups=[api_log_group],
        )
        logs.QueryDefinition(
            self,
            "ApiGatewayLatency",
            query_definition_name=f"{stack_name}/ApiGateway/SlowestRequests",
            query_string=logs.QueryString(
                fields=[
                    "@timestamp",
                    "status",
                    "httpMethod",
                    "resourcePath",
                    "responseLength",
                    "ip",
                    "xrayTraceId",
                    "requestId",
                ],
                sort="@timestamp desc",
                limit=50,
            ),
            log_groups=[api_log_group],
        )
