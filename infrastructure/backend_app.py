"""BackendApp construct — the domain-level application.

Encapsulates all resources that make up the backend serverless application:
KMS key, DynamoDB idempotency table, SSM greeting parameter, AppConfig feature
flags, Lambda function, API Gateway, Application Insights monitoring, dashboard,
Logs Insights saved queries, and per-resource cdk-nag suppressions.

Following the CDK best practice "model with constructs, deploy with stacks":
the Stack only composes this construct, applies stack-wide Aspects, and wires
outputs. Any deployment shape (multiple copies in one stack, multi-tenant,
dev-next-to-prod) can be achieved by instantiating this construct multiple
times without subclassing the Stack.
"""

import json
from pathlib import Path
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
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_codedeploy as codedeploy,
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
    aws_sns as sns,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from aws_cdk import (
    aws_wafv2 as wafv2,
)
from aws_cdk import (
    custom_resources as cr,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from cdk_monitoring_constructs import (
    CustomMetricGroup,
    DefaultDashboardFactory,
    ErrorRateThreshold,
    LatencyThreshold,
    MetricStatistic,
    MonitoringFacade,
    SnsAlarmActionStrategy,
)
from constructs import Construct

from infrastructure.nag_utils import (
    acknowledge_rules,
    build_managed_threat_rules,
    create_auto_delete_objects_log_group,
    create_waf_logs_bucket,
    grant_cloudwatch_alarms_to_key,
    grant_logs_service_to_key,
)


class BackendApp(Construct):
    """Domain-level backend application.

    Exposes the top-level resources as public attributes so the enclosing
    Stack can reference them for CfnOutputs and cross-stack wiring.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        idempotency_table: dynamodb.ITableV2,
        is_production_env: bool = True,
        appconfig_monitor: bool = False,
    ) -> None:
        """Build the application construct.

        Args:
            scope: The CDK construct scope.
            construct_id: The scoped construct ID.
            idempotency_table: The Powertools idempotency table, created in the
                separate :class:`DataStack` and passed in cross-stack.
                This construct wires it into the Lambda (the
                ``IDEMPOTENCY_TABLE_NAME`` env var and a scoped read/write
                grant) but does not own its lifecycle — see that stack for the
                stateful-resource separation rationale.
            is_production_env: When True (the default, and what the default
                ``prod`` deployment environment passes), alarm notifications
                are routed to a CMK-encrypted SNS topic. Non-production
                environments — ephemeral per-developer/per-branch stacks in
                particular — still get the full dashboard and alarm set, but
                without the SNS topic so short-lived stacks never page anyone.
            appconfig_monitor: Opt-in production switch for AppConfig feature-flag
                rollouts. When True, the flag deployment uses a gradual strategy
                (LINEAR 25%/step over 10 min, 5-min bake) and the environment
                carries a CloudWatch alarm monitor that auto-rolls-back a bad flag
                config (``_attach_appconfig_rollback_monitor``). Defaults to False
                — all-at-once, no monitor — because a monitored CFN-managed
                deployment **cannot create a cold stack** (the monitor alarm starts
                ``INSUFFICIENT_DATA``, which AppConfig treats as a rollback signal).
                Enable it only AFTER a first all-at-once deploy has produced metric
                data; see that method and README "Deployment safety".
        """
        super().__init__(scope, construct_id)

        stack = Stack.of(self)

        # Stateful data layer lives in its own stack (DataStack); the
        # table is passed in cross-stack. This construct's own CMK below covers
        # the compute-side encryption (Lambda env vars, log groups, AppConfig,
        # SNS) — the table is encrypted by the data stack's separate key.
        self.idempotency_table = idempotency_table

        # Compute-side KMS key, shared across this stack's CloudWatch log groups,
        # Lambda env vars, AppConfig hosted configuration content, and the SNS
        # alarm topic. The DynamoDB table has its own key in the data stack
        # (see DataStack) — keys are not shared across the stack
        # boundary, so each carries a tighter, least-privilege key policy.
        # CloudWatch Logs requires the Logs service principal to be granted access
        # so it can encrypt data on behalf of the service.
        # Note: SSM StringParameter cannot use CMK — CloudFormation does not support
        # creating SecureString parameters. AppConfig support arrived later (via
        # the kms_key_identifier property on CfnConfigurationProfile), wired below.
        self.encryption_key = kms.Key(
            self,
            "EncryptionKey",
            description=f"KMS key for {stack.stack_name} log groups, Lambda env, AppConfig, and SNS",
            enable_key_rotation=True,
            # 90 days is a common compliance-aligned cadence (PCI/HIPAA forks
            # default to 90). Rotation is fully managed by AWS — key ID/ARN
            # and policies stay constant, prior versions are retained for
            # transparent decryption, no dependent redeploys required.
            rotation_period=Duration.days(90),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Confused-deputy guard: scope the Logs service principal grant to
        # log-group ARNs in this account+region. See ``grant_logs_service_to_key``
        # in ``nag_utils.py`` — three CMKs in this project share the statement.
        grant_logs_service_to_key(
            self.encryption_key,
            region=stack.region,
            account=stack.account,
            partition=stack.partition,
        )
        # Deliberately NO grant for GuardDuty (or DevOps Guru / Application
        # Insights / Resource Explorer): those services' service-linked roles
        # probe kms:Decrypt against this CMK when introspecting the Lambda's
        # CMK-encrypted env vars, and are denied — by design. A previous
        # service-principal grant (Principal: guardduty.amazonaws.com +
        # SourceAccount/SourceArn conditions) was removed after live CloudTrail
        # evidence (2026-07-02, GuardDuty enabled in-account) showed it can
        # never match: the call is made by the assumed
        # AWSServiceRoleForAmazonGuardDuty role, and a role-session principal
        # is not matched by a service-principal key statement — the grant was
        # dead policy. The SLR's AWS-managed policy carries no kms:Decrypt
        # either, and GuardDuty's documented Lambda coverage (function
        # metadata, tags, network activity) does not require reading env-var
        # plaintext, so nothing is lost. A fork that wants these services to
        # read CMK-encrypted config must add a key-policy statement naming the
        # specific service-linked role ARN — possible only in accounts where
        # that SLR already exists (KMS validates key-policy principals), which
        # is why this template doesn't ship it.

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

        # deletion_protection_check=BYPASS — same teardown rationale as the
        # configuration profile below: the account-level deletion-protection
        # window would otherwise fail `cdk destroy` for any environment a
        # Lambda polled recently.
        app_config_env = appconfig.CfnEnvironment(
            self,
            "FeatureFlagsEnv",
            application_id=self.app_config_app.ref,
            name=f"{stack.stack_name}-env",
            deletion_protection_check="BYPASS",
        )

        # kms_key_identifier CMK-encrypts the hosted configuration content at
        # rest in AppConfig. This compute-side CMK already covers the Lambda's
        # log groups and env vars; pinning AppConfig to the same key keeps the
        # compute-side auditable encryption surface inside one ARN. (The Lambda
        # also gets an explicit kms:Decrypt grant on this key below for the
        # GetLatestConfiguration read path — see the grant near the role policy.)
        #
        # Type is FREEFORM on purpose, not AWS.AppConfig.FeatureFlags: the
        # native flags type stores the authoring format but its data plane
        # serves the flattened {"<flag>":{"enabled":bool}} form, which
        # Powertools FeatureFlags rejects with SchemaValidationError ("feature
        # 'default' boolean key must be present"). Powertools consumes its OWN
        # schema ({"<flag>":{"default":bool,"rules":{...}}}) from a freeform
        # profile — see the Powertools feature-flags docs. Found on a live
        # deployment; the handler's fallback path masks it at synth/test time.
        # Name is "-flags" (not the application's "-features") deliberately:
        # changing a profile's Type forces CFN replacement, and replacement is
        # create-before-delete — a replacement that keeps the same pinned name
        # collides with the not-yet-deleted old profile ("Resource already
        # exists outside the stack"). AppConfig L1s require a name (no CDK
        # auto-generation), so any future property change that replaces this
        # profile must change the name in the same commit.
        #
        # deletion_protection_check=BYPASS: AppConfig deletion protection (an
        # account-level setting) refuses to delete environments and hosted
        # configurations that a client polled within the protection window.
        # The Lambda polls this profile continuously, so with the account
        # default enabled every `cdk destroy` of a recently used stack would
        # fail mid-teardown — the same class of dangling-teardown problem
        # `make destroy-clean` exists to solve. BYPASS keeps teardown
        # deterministic; production forks that want the guardrail can flip
        # these to ACCOUNT_DEFAULT and accept manual deletes on destroy.
        app_config_profile = appconfig.CfnConfigurationProfile(
            self,
            "FeatureFlagsProfile",
            application_id=self.app_config_app.ref,
            name=f"{stack.stack_name}-flags",
            location_uri="hosted",
            type="AWS.Freeform",
            kms_key_identifier=self.encryption_key.key_arn,
            deletion_protection_check="BYPASS",
        )

        # Initial feature flags configuration, in the Powertools feature-flags
        # schema. "rules" (conditional enablement, e.g. by source_ip /
        # user_agent from the evaluation context) can be authored here later.
        #
        # The flag content lives in feature_flags.json next to this module —
        # one file read by both this construct (at synth) and the unit test
        # that validates it against the Powertools feature-flags schema
        # (tests/unit/test_feature_flags_schema.py, which runs in the venv
        # where Powertools is importable). json.loads is the synth-side guard:
        # it can't check the flag schema (Powertools isn't installable next to
        # CDK — see the attrs conflict in pyproject.toml) but it does fail the
        # build on malformed JSON instead of shipping it to AppConfig.
        flags_content = (Path(__file__).parent / "feature_flags.json").read_text()
        json.loads(flags_content)
        flags_version = appconfig.CfnHostedConfigurationVersion(
            self,
            "FeatureFlagsVersion",
            application_id=self.app_config_app.ref,
            configuration_profile_id=app_config_profile.ref,
            content_type="application/json",
            content=flags_content,
        )

        # A hosted configuration version is inert until DEPLOYED: the AppConfig
        # data plane (GetLatestConfiguration, which Powertools FeatureFlags
        # calls) only serves configuration that has been deployed to the
        # environment. Without this Deployment the enhanced_greeting flag could
        # never evaluate true and every fetch would take the handler's
        # error-fallback path. CFN re-runs the deployment whenever the hosted
        # version changes (ConfigurationVersion references flags_version.ref).
        #
        # Deployment strategy: all-at-once by default; gradual when the opt-in
        # appconfig_monitor switch is set (it pairs with the environment monitor
        # wired below — the bake window is what gives the monitor time to act).
        #
        # Why all-at-once is the default: a *gradual* strategy with a CloudWatch
        # alarm **monitor** for automatic rollback is the production-grade pattern
        # for protecting ongoing flag changes — but it cannot be wired into this
        # CFN-managed deployment when it runs during *initial* stack creation.
        # AppConfig rolls back when a monitored alarm is in ALARM **or
        # INSUFFICIENT_DATA**, and on a cold stack the rollback metric
        # (FeatureFlagEvaluationFailure, emitted only by the running Lambda) has
        # no data, so the fresh alarm sits in INSUFFICIENT_DATA and AppConfig
        # aborts the initial deploy (verified on a live deploy). The metric is
        # always emitted for observability; gradual + alarm rollback is opt-in via
        # appconfig_monitor and must be turned on only AFTER a first all-at-once
        # deploy — see _attach_appconfig_rollback_monitor and README
        # "Deployment safety" / TODO "AppConfig".
        if appconfig_monitor:
            flags_deployment_strategy = appconfig.CfnDeploymentStrategy(
                self,
                "FeatureFlagsDeployStrategy",
                name=f"{stack.stack_name}-gradual",
                deployment_duration_in_minutes=10,
                growth_factor=25,
                growth_type="LINEAR",
                final_bake_time_in_minutes=5,
                replicate_to="NONE",
            )
        else:
            flags_deployment_strategy = appconfig.CfnDeploymentStrategy(
                self,
                "FeatureFlagsDeployStrategy",
                name=f"{stack.stack_name}-all-at-once",
                deployment_duration_in_minutes=0,
                growth_factor=100,
                growth_type="LINEAR",
                final_bake_time_in_minutes=0,
                replicate_to="NONE",
            )
        appconfig.CfnDeployment(
            self,
            "FeatureFlagsDeployment",
            application_id=self.app_config_app.ref,
            environment_id=app_config_env.ref,
            configuration_profile_id=app_config_profile.ref,
            configuration_version=flags_version.ref,
            deployment_strategy_id=flags_deployment_strategy.ref,
            # Same CMK as the hosted content — keeps the deployment data inside
            # the one auditable key, matching the profile's kms_key_identifier.
            kms_key_identifier=self.encryption_key.key_arn,
        )

        # Explicit Lambda log group with 1-week retention (implicit group has no retention).
        # log_group_name omitted — CDK auto-generates a unique name and wires it into the
        # Lambda function via the log_group property below.
        lambda_log_group = logs.LogGroup(
            self,
            "FunctionLogGroup",
            encryption_key=self.encryption_key,
            # 90 days for operational app logs — enough debugging history and
            # satisfies a "3 months immediately available" clause without paying
            # CloudWatch storage for a long tail. Audit-relevant logs (CloudTrail,
            # access logs) go to S3 for cheaper long-term retention instead — see
            # README "Audit stack and log retention".
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Lambda function with automatic dependency bundling.
        # environment_encryption pins the env-var encryption to our CMK so the
        # security boundary stays inside one key — without it Lambda falls back
        # to an AWS-managed key.
        self.function = PythonFunction(
            self,
            "ApiFunction",
            runtime=_lambda.Runtime.PYTHON_3_14,
            entry="lambda",
            index="app.py",
            handler="lambda_handler",
            architecture=_lambda.Architecture.ARM_64,
            memory_size=256,
            timeout=Duration.seconds(10),
            # Async-retry posture made explicit: this function is only invoked
            # synchronously (API Gateway), so Lambda's default of two automatic
            # async retries is dead config — pinning it to 0 documents that no
            # async path exists, and any future async event source must revisit
            # this together with an on_failure destination (see the LambdaDLQ
            # suppressions below).
            retry_attempts=0,
            # Reserved concurrency caps how much of the account's concurrency pool
            # this one function can consume, so a runaway loop or a traffic spike
            # on /greeting can't starve every other Lambda in the account. 100 is
            # a deliberately modest ceiling for a reference workload — size it to
            # real peak traffic in a fork (and note that a reserved value also
            # guarantees that headroom is always available to this function).
            # First-deploy precondition: Lambda rejects any reservation that
            # would leave the account's UNRESERVED pool below 100, so this line
            # needs an applied account concurrency quota of at least 200. The
            # documented default is 1000, but fresh accounts often start with a
            # much lower applied quota — check `aws lambda get-account-settings`
            # and request an increase (or lower this value) before the first
            # deploy in a new account, or the stack fails on this resource.
            # Retires the NIST.800.53.R5-LambdaConcurrency /
            # HIPAA.Security-LambdaConcurrency suppressions below.
            reserved_concurrent_executions=100,
            tracing=_lambda.Tracing.ACTIVE,
            log_group=lambda_log_group,
            logging_format=_lambda.LoggingFormat.JSON,
            # Platform/system log records (START, REPORT, platform.report, …)
            # are filtered at INFO. That is the service default today, but the
            # posture is pinned in code — same "visible in code, not implicit
            # in the runtime default" rationale as recursive_loop below. Note
            # the SlowInvocations saved query reads platform.report records,
            # so this must never be raised above INFO without updating it.
            system_log_level_v2=_lambda.SystemLogLevel.INFO,
            environment_encryption=self.encryption_key,
            environment={
                "POWERTOOLS_SERVICE_NAME": "serverless-app",
                "POWERTOOLS_METRICS_NAMESPACE": "ServerlessApp",
                "POWERTOOLS_LOG_LEVEL": "INFO",
                "IDEMPOTENCY_TABLE_NAME": self.idempotency_table.table_name,
                "GREETING_PARAM_NAME": self.greeting_param.parameter_name,
                # Sourcing AppConfig identifiers from the CFN constructs (instead
                # of re-formatting f"{stack.stack_name}-...") keeps the Lambda's
                # reads in lockstep with the IAM grant below: any future rename
                # of the AppConfig resources flows through .name automatically.
                "APPCONFIG_APP_NAME": self.app_config_app.name,
                "APPCONFIG_ENV_NAME": app_config_env.name,
                "APPCONFIG_PROFILE_NAME": app_config_profile.name,
                # In-memory TTL for fetched feature flags (seconds). The handler
                # defaults to 300 anyway; set explicitly so the caching posture
                # is visible and tunable here rather than buried in code.
                "APPCONFIG_MAX_AGE_SECONDS": "300",
            },
        )

        # Recursive-loop detection. Default is Terminate, but the L2 PythonFunction
        # construct doesn't surface this property — set it explicitly on the
        # underlying CfnFunction so the posture is visible in code rather than
        # implicit in the runtime default. A runtime isinstance check (instead of a
        # bare cast) makes a future CDK change to the L2->L1 default_child
        # relationship fail loudly at synth rather than silently dropping the
        # Terminate posture — mirroring the provider-lookup guard in the frontend stack.
        cfn_function = self.function.node.default_child
        if not isinstance(cfn_function, _lambda.CfnFunction):
            raise TypeError(f"Expected ApiFunction default_child to be CfnFunction, got {type(cfn_function)}")
        cfn_function.recursive_loop = "Terminate"

        # Lambda alias for CodeDeploy traffic-shifting deployments. The API
        # integration targets this alias rather than $LATEST, so a code change
        # rolls out through CodeDeploy (canary in prod, all-at-once in dev) with
        # automatic rollback on the alias error alarm — see
        # _attach_canary_deployment. current_version publishes a new Lambda
        # version whenever the function's code or config changes; CodeDeploy
        # shifts the alias onto it. (Reserved concurrency stays on the function;
        # it is shared across versions, so the alias needs none of its own.)
        self.alias = _lambda.Alias(
            self,
            "LiveAlias",
            alias_name="live",
            version=self.function.current_version,
        )

        # Grant permissions
        self.idempotency_table.grant_read_write_data(self.function)
        self.greeting_param.grant_read(self.function)

        # AppConfig least-privilege: both calls authorize against the
        # application/environment/configuration ARN. The session token in the
        # GetLatestConfiguration request body is opaque request data, not the
        # IAM resource — IAM still evaluates the call against this profile ARN.
        appconfig_profile_arn = (
            f"arn:{stack.partition}:appconfig:{stack.region}:{stack.account}:"
            f"application/{self.app_config_app.ref}/"
            f"environment/{app_config_env.ref}/"
            f"configuration/{app_config_profile.ref}"
        )
        self.function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"],
                resources=[appconfig_profile_arn],
            )
        )
        # AppConfig CMK-decrypt grant. The hosted configuration is encrypted at
        # rest with this stack's CMK (kms_key_identifier on the profile +
        # deployment above), and AppConfig evaluates kms:Decrypt against the
        # *caller's* role on GetLatestConfiguration — so the Lambda needs decrypt
        # on this key. This permission used to ride the DynamoDB grant back when
        # the table shared this CMK; now that the table has its own key in
        # DataStack, the AppConfig decrypt path needs an explicit,
        # scoped grant here (read-only path, so kms:Decrypt only).
        self.encryption_key.grant_decrypt(self.function)

        # Explicit API Gateway access log group with 1-week retention.
        # log_group_name omitted — CDK auto-generates and passes it into the
        # RestApi via LogGroupLogDestination below.
        api_log_group = logs.LogGroup(
            self,
            "ApiAccessLogs",
            encryption_key=self.encryption_key,
            # 90 days — operational retention (see FunctionLogGroup).
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # API Gateway REST API
        # cloud_watch_role=True (default) creates an implicit IAM role scoped to
        # allow API Gateway to write execution logs to CloudWatch — this is a
        # region-level account setting managed by CDK automatically.
        self.api = apigw.RestApi(
            self,
            "RestApi",
            cloud_watch_role=True,
            cloud_watch_role_removal_policy=RemovalPolicy.DESTROY,
            deploy_options=apigw.StageOptions(
                stage_name="Prod",
                tracing_enabled=True,
                # Stage-level throttling caps steady-state and burst request rates
                # across the whole stage, bounding both abuse and runaway cost.
                # These are method-level defaults applied to every route; per-method
                # overrides or a usage plan can layer tighter limits later. Values
                # are deliberately modest for a reference workload — raise them in a
                # fork sized to real traffic. This retires the
                # Serverless-APIGWDefaultThrottling suppression in BackendStack.
                throttling_rate_limit=100,
                throttling_burst_limit=200,
                access_log_destination=apigw.LogGroupLogDestination(api_log_group),
                access_log_format=apigw.AccessLogFormat.custom(
                    # Built from typed AccessLogField references — json_with_standard_fields
                    # only supports 10 fixed fields; custom() is the CDK API for extended formats.
                    #
                    # errorMessage stays the quoted RAW $context.error.message on
                    # purpose. $context.error.messageString looks like the
                    # JSON-safe variant but is unusable here: absent access-log
                    # variables render as a bare dash, so the unquoted form
                    # corrupts every success line ("errorMessage":-) and the
                    # quoted form double-quotes every error line. The residual
                    # exposure of the raw form — a message containing a double
                    # quote breaks JSON parsing for that one line — is accepted.
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
                            # Feeds the SlowestRequests saved query in
                            # _create_insights_queries — total request latency in ms.
                            f'"responseLatency":"{apigw.AccessLogField.context_response_latency()}"',
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

        greeting_resource = self.api.root.add_resource("greeting")
        # Integrate with the alias (not $LATEST) so CodeDeploy traffic shifting
        # is what actually moves production traffic onto a new version.
        greeting_resource.add_method("GET", apigw.LambdaIntegration(self.alias))
        greeting_resource.add_cors_preflight(
            allow_origins=apigw.Cors.ALL_ORIGINS,
            allow_methods=["GET", "OPTIONS"],
            # X-Amzn-Trace-Id is required for CloudWatch RUM to propagate the
            # client-side X-Ray trace header into the API Gateway → Lambda
            # segments so the browser and backend appear on the same trace.
            # Idempotency-Key must be allowed by the preflight or browsers will
            # block the actual request — the Lambda requires it (returns 400
            # without it) so the preflight has to permit it explicitly.
            allow_headers=[*apigw.Cors.DEFAULT_HEADERS, "X-Amzn-Trace-Id", "Idempotency-Key"],
        )

        # Explicit execution log group — API Gateway creates this outside CloudFormation
        # when logging_level is enabled. Pre-creating it here transfers ownership to CFN
        # so it is deleted on cdk destroy. Name format is fixed by the API Gateway service.
        execution_log_group = logs.LogGroup(
            self,
            "ApiExecutionLogs",
            log_group_name=f"API-Gateway-Execution-Logs_{self.api.rest_api_id}/Prod",
            encryption_key=self.encryption_key,
            # 90 days — operational retention (see FunctionLogGroup).
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Order the stage after the group: if the stage goes live first and a
        # request arrives mid-deploy, API Gateway auto-creates the group
        # (unencrypted, no retention) and this LogGroup CREATE then fails with
        # "already exists". No cycle: the group depends only on the RestApi
        # (via rest_api_id in its name), not on the stage.
        self.api.deployment_stage.node.add_dependency(execution_log_group)

        # Regional WAF on API Gateway — closes the CloudFront-bypass window on the
        # public execute-api URL. See _attach_regional_waf for the full rationale.
        self._attach_regional_waf()

        # CodeDeploy traffic-shifting deployment for the Lambda alias, with
        # automatic rollback if the alias error alarm fires during the shift.
        self._attach_canary_deployment(is_production_env)

        # AppConfig deployment monitor (opt-in): a CloudWatch alarm AppConfig
        # watches during a flag rollout, auto-rolling-back the config if it fires.
        # Off by default — it cannot create a cold stack (see the method docstring).
        if appconfig_monitor:
            self._attach_appconfig_rollback_monitor(app_config_env)

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

        # CMK-encrypted log group for the AwsCustomResource provider Lambda.
        # Passing log_group= here (instead of log_retention=) avoids the legacy
        # LogRetention singleton path and lets us own every log group with our
        # CMK — no dangling AWS-managed-key log group left after cdk destroy.
        custom_resource_log_group = logs.LogGroup(
            self,
            "AwsCustomResourceLogGroup",
            encryption_key=self.encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Custom resource to delete the Application Insights auto-created CloudWatch
        # dashboard on stack destroy. Application Insights creates a dashboard named
        # "ApplicationInsights-{resource-group-name}" outside of CloudFormation, so
        # CDK cannot own it directly. Because the resource group's own name already
        # starts with "ApplicationInsights-", the real dashboard name carries the
        # DOUBLED prefix — deleting resource_group.name verbatim silently deletes
        # nothing (verified against a live teardown). This Lambda-backed custom
        # resource calls DeleteDashboards at destroy time so no dashboard is left
        # behind after cdk destroy. Policy is scoped to the exact dashboard ARN —
        # CloudWatch dashboards have a known global ARN format.
        app_insights_dashboard_name = f"ApplicationInsights-{resource_group.name}"
        app_insights_dashboard_arn = (
            f"arn:{stack.partition}:cloudwatch::{stack.account}:dashboard/{app_insights_dashboard_name}"
        )
        app_insights_dashboard_cleanup = cr.AwsCustomResource(
            self,
            "AppInsightsDashboardCleanup",
            on_delete=cr.AwsSdkCall(
                service="CloudWatch",
                action="deleteDashboards",
                parameters={"DashboardNames": [app_insights_dashboard_name]},
                physical_resource_id=cr.PhysicalResourceId.of(app_insights_dashboard_name),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[app_insights_dashboard_arn],
            ),
            install_latest_aws_sdk=False,
            log_group=custom_resource_log_group,
        )
        # Must run after Application Insights has had a chance to create the dashboard
        app_insights_dashboard_cleanup.node.add_dependency(app_insights)

        # Monitoring dashboard, alarms, and (in production) SNS alarm routing.
        self.alarm_topic = self._build_monitoring(lambda_log_group, is_production_env)

        # Expose API URL for consumption by the enclosing stack and cross-stack refs
        self.api_url = self.api.url

        self._add_resource_suppressions(app_insights_dashboard_cleanup)

    def _build_monitoring(self, lambda_log_group: logs.LogGroup, is_production_env: bool) -> sns.Topic | None:
        """Build the CloudWatch dashboard, alarms, and alarm routing.

        CloudWatch dashboards are global — the name is scoped to the stack so
        multiple regional deployments don't collide on the same dashboard name.

        Alarm routing: in production the alarm factory wires every alarm to
        the CMK-encrypted SNS topic built in ``_build_alarm_topic`` — an alarm
        that pages nobody is a dashboard widget, not an alert. Non-production
        (ephemeral dev) stacks keep the alarms but skip the topic, with the
        NIST/HIPAA alarm-action rules suppressed for that shape only.

        Returns:
            The alarm SNS topic in production environments, else None.
        """
        stack = Stack.of(self)
        alarm_topic: sns.Topic | None = None
        alarm_factory_defaults: dict[str, object] = {
            "actions_enabled": True,
            "alarm_name_prefix": stack.stack_name,
        }
        if is_production_env:
            alarm_topic = self._build_alarm_topic()
            alarm_factory_defaults["action"] = SnsAlarmActionStrategy(on_alarm_topic=alarm_topic)

        monitoring = MonitoringFacade(
            self,
            "Monitoring",
            alarm_factory_defaults=alarm_factory_defaults,
            dashboard_factory=DefaultDashboardFactory(
                self,
                "MonitoringDashboardFactory",
                dashboard_name_prefix=stack.stack_name,
            ),
        )

        # ── Service health: alarms + widgets ─────────────────────────────────
        # Thresholds are deliberately modest reference-workload values — size
        # them to real traffic in a fork. p90 of 3s sits under the function's
        # 10s timeout but far above the warm path; a 1% 5xx fault rate catches
        # systematic failure without paging on a single cold-start blip.
        monitoring.add_large_header("Service health")
        monitoring.monitor_lambda_function(
            lambda_function=self.function,
            add_latency_p90_alarm={"p90": LatencyThreshold(max_latency=Duration.seconds(3))},
        )
        # Surfaces recent ERROR-level records next to the Lambda metrics so an
        # alarm investigation starts on the dashboard, not in Logs Insights.
        monitoring.monitor_log(
            log_group_name=lambda_log_group.log_group_name,
            human_readable_name="Lambda error logs",
            pattern="ERROR",
            alarm_friendly_name="lambda error logs",
        )
        # Explicit names: the facade otherwise derives them from
        # api.rest_api_name, which is an unresolved CDK token at synth (the
        # RestApi physical name is generated at deploy) — the token then
        # stringifies into the template as a literal "TokenTOKEN<n>" fragment
        # in both the alarm name and the dashboard widget title.
        monitoring.monitor_api_gateway(
            api=self.api,
            human_readable_name="RestApi",
            alarm_friendly_name="RestApi",
            add5_xx_fault_rate_alarm={"internal_error": ErrorRateThreshold(max_error_rate=1)},
        )
        monitoring.monitor_dynamo_table(table=self.idempotency_table)

        # ── Business KPIs ─────────────────────────────────────────────────────
        # The handler emits GreetingRequests (Powertools EMF) into the ServerlessApp
        # namespace with the service dimension Powertools adds from
        # POWERTOOLS_SERVICE_NAME. Surfacing it here keeps the business signal
        # on the same dashboard as the operational metrics it explains.
        metric_factory = monitoring.create_metric_factory()
        greeting_requests_metric = metric_factory.create_metric(
            metric_name="GreetingRequests",
            namespace="ServerlessApp",
            statistic=MetricStatistic.SUM,
            dimensions_map={"service": "serverless-app"},
            label="greeting requests",
            period=Duration.hours(1),
        )
        monitoring.add_large_header("Business KPIs")
        monitoring.monitor_custom(
            metric_groups=[CustomMetricGroup(metrics=[greeting_requests_metric], title="Hourly greeting requests")],
            human_readable_name="Business KPIs",
            alarm_friendly_name="KPIs",
        )

        if not is_production_env:
            # Non-prod keeps the alarms as dashboard signals but deliberately
            # has no notification channel (no SNS topic — ephemeral stacks must
            # never page anyone), which trips the NIST/HIPAA alarm-action rules
            # on every alarm under the facade. Scoped to the monitoring subtree;
            # the production shape routes every alarm to SNS and needs no
            # suppression.
            acknowledge_rules(
                monitoring,
                [
                    {
                        "id": "NIST.800.53.R5-CloudWatchAlarmAction",
                        "reason": "Ephemeral/dev environment — alarms are dashboard signals only; no paging channel by design",
                    },
                    {
                        "id": "HIPAA.Security-CloudWatchAlarmAction",
                        "reason": "Ephemeral/dev environment — alarms are dashboard signals only; no paging channel by design",
                    },
                ],
            )

        return alarm_topic

    def _build_alarm_topic(self) -> sns.Topic:
        """Create the CMK-encrypted SNS topic that alarm actions publish to.

        The topic itself is deliberately subscription-free: where alerts land
        (email, Chatbot, PagerDuty, …) is an operational choice a fork makes,
        and subscriptions usually need out-of-band confirmation anyway. The
        load-bearing parts are the wiring around it — the same project CMK
        (with the CloudWatch-via-SNS key grant from ``nag_utils``), TLS-only
        publish enforcement, and a topic policy that admits only this
        account's alarms.
        """
        stack = Stack.of(self)
        topic = sns.Topic(
            self,
            "AlarmTopic",
            # Same CMK as logs/DDB/env-vars — keeps the alerting path inside
            # the project's single auditable encryption surface.
            master_key=self.encryption_key,
            # Rejects plaintext-HTTP publishes via an aws:SecureTransport deny;
            # also satisfies the SSL-only SNS rules in the nag packs.
            enforce_ssl=True,
        )
        # Without this key grant, an alarm transition "succeeds" but the
        # CMK-encrypted publish is denied at KMS and the notification silently
        # vanishes — see grant_cloudwatch_alarms_to_key for the full rationale.
        grant_cloudwatch_alarms_to_key(self.encryption_key, account=stack.account, region=stack.region)
        # Topic policy: CloudWatch's service principal may publish, but only on
        # behalf of alarms in this account (standard confused-deputy guard —
        # CloudWatch documents aws:SourceArn/aws:SourceAccount for SNS topic
        # policies on alarm actions).
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmsPublish",
                actions=["sns:Publish"],
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                resources=[topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{stack.partition}:cloudwatch:{stack.region}:{stack.account}:alarm:*"
                    },
                },
            )
        )
        return topic

    def _attach_regional_waf(self) -> None:
        """Attach a REGIONAL WAF WebACL to the API Gateway stage.

        The CLOUDFRONT-scoped WebACL in ``WafStack`` only inspects
        traffic that arrives through CloudFront. The API Gateway's public
        ``https://{id}.execute-api.{region}.amazonaws.com/Prod`` URL — published
        in the ``ApiUrlOutput`` CfnOutput — bypasses CloudFront entirely,
        so without a second ACL here a caller hitting that URL directly evades
        every managed rule group. This REGIONAL ACL mirrors the four managed
        threat rule groups onto the origin so both paths get the same protection.

        Scope must be ``REGIONAL`` and the ACL must live in the API's own region
        (which is why it's here, in the backend stack, not in the us-east-1-pinned
        WAF stack). The shared ``build_managed_threat_rules`` helper guarantees the
        rule set never drifts from the CloudFront ACL.

        The shared set omits a rate rule because edge and origin need different
        aggregation, so this ACL carries its own (``RateLimitDirectCallers``,
        priority 4): funnelled traffic reaches the origin from CloudFront edge
        IPs (a plain IP-aggregated limit would pool many legitimate users behind
        a few shared edge IPs), while a *direct* ``execute-api`` caller — the
        bypass path this ACL exists to cover — arrives from its own IP and
        without the ``X-Forwarded-For`` header CloudFront always appends toward
        the origin. Scoping an IP-aggregated limit to XFF-less requests
        rate-limits each direct caller per client IP while leaving funnelled
        traffic untouched; without it, one direct caller could exhaust the
        *shared* stage throttle (throttling_rate_limit=100) and starve the
        legitimate CloudFront-funnelled users behind it.

        This is defence in depth, not a replacement for the documented fixes: a
        direct caller can spoof ``X-Forwarded-For`` to evade the rate rule, so
        a fork can still add a CloudFront-injected secret header + API Gateway
        resource policy to make the origin reject any non-CloudFront request
        outright (TODO "Close the CloudFront-bypass window" option (b)).
        """
        stack = Stack.of(self)

        # Rate-limit DIRECT callers only (priority 4, after the shared managed
        # groups at 0-3). The scope-down statement matches only requests WITHOUT
        # an X-Forwarded-For header — CloudFront always appends XFF toward the
        # origin, so funnelled traffic never matches and only direct execute-api
        # callers are counted, each aggregated by its own client IP. See the
        # method docstring for the full rationale and the spoofing caveat.
        direct_caller_rate_rule = wafv2.CfnWebACL.RuleProperty(
            name="RateLimitDirectCallers",
            priority=4,
            action=wafv2.CfnWebACL.RuleActionProperty(block={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                    # 300 requests / 5 min per direct-caller IP — above any
                    # plausible legitimate direct use (the frontend goes through
                    # CloudFront), well under the shared stage throttle it protects.
                    limit=300,
                    aggregate_key_type="IP",
                    scope_down_statement=wafv2.CfnWebACL.StatementProperty(
                        not_statement=wafv2.CfnWebACL.NotStatementProperty(
                            statement=wafv2.CfnWebACL.StatementProperty(
                                # "Header present and non-empty" — WAF has no
                                # first-class exists-check, so match length > 0.
                                size_constraint_statement=wafv2.CfnWebACL.SizeConstraintStatementProperty(
                                    field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                                        single_header={"Name": "x-forwarded-for"}
                                    ),
                                    comparison_operator="GT",
                                    size=0,
                                    text_transformations=[
                                        wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="NONE")
                                    ],
                                )
                            )
                        )
                    ),
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{stack.stack_name}-api-RateLimitDirectCallers",
                sampled_requests_enabled=True,
            ),
        )

        regional_acl = wafv2.CfnWebACL(
            self,
            "ApiRegionalWebACL",
            # Explicit name so the WAF→S3 log path (…/WAFLogs/{region}/{name}/) is
            # deterministic for the frontend's Athena Glue table. Pinned physical
            # name: a future replacement-forcing property change collides with the
            # not-yet-deleted old ACL (CFN replacement is create-before-delete), so
            # such a change must also change the name in the same commit — see the
            # AppConfig profile note above.
            name=f"{stack.stack_name}-api",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{stack.stack_name}ApiRegionalWebACL",
                sampled_requests_enabled=True,
            ),
            rules=[*build_managed_threat_rules(f"{stack.stack_name}-api"), direct_caller_rate_rule],
        )

        # WAF logging — required by NIST/HIPAA/PCI WAFv2LoggingEnabled, mirroring
        # the CloudFront ACL in WafStack. Logs go to S3 (cheaper
        # long-term retention than CloudWatch) via the shared create_waf_logs_bucket
        # helper, which builds the aws-waf-logs-* bucket + its delivery bucket
        # policy. The regional bucket lives in this (target-region) stack because
        # WAF requires the S3 destination in the same region as the ACL.
        regional_waf_logs_bucket = create_waf_logs_bucket(self, "api")
        regional_waf_logging = wafv2.CfnLoggingConfiguration(
            self,
            "ApiRegionalWafLogging",
            log_destination_configs=[regional_waf_logs_bucket.bucket_arn],
            resource_arn=regional_acl.attr_arn,
        )
        # Order after the bucket policy so WAF leaves the CDK-managed policy alone.
        if regional_waf_logs_bucket.policy is not None:
            regional_waf_logging.node.add_dependency(regional_waf_logs_bucket.policy)
        # The bucket uses auto_delete_objects; give the S3 auto-delete singleton an
        # explicit CMK log group (helper also suppresses its singleton nag findings).
        # The provider + bucket are stack-level, so pass the stack, not the construct.
        create_auto_delete_objects_log_group(stack, self.encryption_key)

        # Associate the ACL with the deployed Prod stage. stage_arn is the
        # resource ARN WAFv2 expects for an API Gateway stage.
        wafv2.CfnWebACLAssociation(
            self,
            "ApiRegionalWebACLAssociation",
            resource_arn=self.api.deployment_stage.stage_arn,
            web_acl_arn=regional_acl.attr_arn,
        )

    def _attach_canary_deployment(self, is_production_env: bool) -> None:
        """Wire a CodeDeploy traffic-shifting deployment onto the Lambda alias.

        The API integration targets ``self.alias`` (see ``__init__``), so this is
        what actually moves production traffic onto a new function version. A
        code change publishes a new version; CodeDeploy then shifts the alias
        onto it per the deployment config and watches the error alarm — if the
        new version starts erroring during the shift, CodeDeploy rolls the alias
        back to the previous version automatically.

        The deployment config is environment-aware, mirroring the AppConfig
        strategy and alarm-routing splits: production shifts *gradually* (canary:
        10% of traffic for 5 minutes, then the remainder) so a bad version is
        caught on a small blast radius; dev/ephemeral environments shift
        all-at-once so iterating doesn't wait out a canary window. The alias,
        deployment group, and rollback alarm exist in both shapes — only the
        shift speed differs.
        """
        deployment_config = (
            codedeploy.LambdaDeploymentConfig.CANARY_10_PERCENT_5_MINUTES
            if is_production_env
            else codedeploy.LambdaDeploymentConfig.ALL_AT_ONCE
        )

        # Rollback trigger: any error on the alias during the shift. A 1-minute
        # period with a single evaluation makes CodeDeploy react fast; missing
        # data is NOT breaching so an idle window never trips a false rollback.
        canary_errors_alarm = cloudwatch.Alarm(
            self,
            "CanaryErrorsAlarm",
            metric=self.alias.metric_errors(period=Duration.minutes(1)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Lambda alias errors during a CodeDeploy traffic shift — triggers rollback",
        )
        self._suppress_rollback_alarm_actions(canary_errors_alarm, "the CodeDeploy canary rollback")

        deployment_group = codedeploy.LambdaDeploymentGroup(
            self,
            "CanaryDeploymentGroup",
            alias=self.alias,
            deployment_config=deployment_config,
            alarms=[canary_errors_alarm],
            # Roll back on a failed deployment or if the alarm fires mid-shift.
            auto_rollback=codedeploy.AutoRollbackConfig(failed_deployment=True, deployment_in_alarm=True),
        )
        acknowledge_rules(
            deployment_group,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "CodeDeploy service role uses the AWS managed AWSCodeDeployRoleForLambdaLimited policy — the documented least-privilege role for Lambda traffic-shifting deployments",
                    "applies_to": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSCodeDeployRoleForLambdaLimited"
                    ],
                },
            ],
        )

    def _attach_appconfig_rollback_monitor(self, app_config_env: appconfig.CfnEnvironment) -> None:
        """Attach a CloudWatch alarm monitor to the AppConfig environment.

        Only called when the ``appconfig_monitor`` switch is set — it is an opt-in
        production add-on, NOT part of the default template. AppConfig watches the
        alarm during a flag rollout (and its bake window); if the alarm enters
        ALARM, AppConfig rolls the configuration back to the previous version
        automatically. This pairs with the gradual deployment strategy selected in
        ``__init__`` under the same switch — the gradual rollout buys the window in
        which the monitor can act before a bad flag reaches every caller.

        **Cold-deploy constraint (why this is opt-in).** AppConfig rolls back when
        the monitored alarm is in ALARM **or INSUFFICIENT_DATA**. A freshly created
        alarm starts in INSUFFICIENT_DATA until CloudWatch's first evaluation, and
        the ``FeatureFlagEvaluationFailure`` metric below has no datapoints at all on
        a brand-new stack — so a monitored deployment that runs during *initial*
        stack creation gets rolled back and the stack never reaches CREATE_COMPLETE
        (verified on a live deploy, even with ``NOT_BREACHING`` set). Enable
        ``appconfig_monitor`` only AFTER a first all-at-once deploy has produced
        metric data; see README "Deployment safety".

        The monitored signal is the handler's ``FeatureFlagEvaluationFailure``
        metric, NOT Lambda errors. A bad flag config is caught and degraded
        gracefully (the request still returns 200 with the default flag value),
        so it produces no Lambda error or 5xx — the custom metric is the only
        signal a config is broken. AppConfig reads the alarm state through
        ``alarm_role_arn``.

        Watching a metric addressed purely by name + static dimensions (rather
        than ``self.function.metric_errors()``) is also what keeps this wiring
        acyclic: the environment references this alarm (monitors), so the alarm
        must not transitively reference the function — which depends on the
        environment via its AppConfig IAM grant. A by-name metric carries no such
        CDK dependency.
        """
        # Mirror the handler's Powertools EMF emission: POWERTOOLS_METRICS_NAMESPACE
        # is "ServerlessApp" and Powertools adds the service dimension from
        # POWERTOOLS_SERVICE_NAME ("serverless-app"). Addressed by name only — no
        # reference to the function construct (see the docstring's acyclicity note).
        failure_metric = cloudwatch.Metric(
            namespace="ServerlessApp",
            metric_name="FeatureFlagEvaluationFailure",
            dimensions_map={"service": "serverless-app"},
            statistic="Sum",
            period=Duration.minutes(1),
        )
        rollback_alarm = cloudwatch.Alarm(
            self,
            "AppConfigRollbackAlarm",
            metric=failure_metric,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Feature-flag evaluation failures during an AppConfig rollout — triggers config rollback",
        )
        self._suppress_rollback_alarm_actions(rollback_alarm, "the AppConfig deployment rollback")

        # AppConfig assumes this role to read the alarm state. cloudwatch:Describe
        # Alarms has no resource-level scoping (the action only supports "*"), so
        # the wildcard is unavoidable — see the IAM5 suppression below.
        monitor_role = iam.Role(
            self,
            "AppConfigMonitorRole",
            assumed_by=iam.ServicePrincipal("appconfig.amazonaws.com"),
            description="Lets AppConfig read the rollback alarm state during a flag deployment",
        )
        monitor_role.add_to_policy(iam.PolicyStatement(actions=["cloudwatch:DescribeAlarms"], resources=["*"]))
        acknowledge_rules(
            monitor_role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "applies_to": ["Resource::*"],
                    "reason": "cloudwatch:DescribeAlarms does not support resource-level permissions — the wildcard is required for AppConfig to read the rollback alarm state",
                },
                {
                    "id": "NIST.800.53.R5-IAMNoInlinePolicy",
                    "reason": "CDK-generated inline policy on the AppConfig monitor role",
                },
                {
                    "id": "HIPAA.Security-IAMNoInlinePolicy",
                    "reason": "CDK-generated inline policy on the AppConfig monitor role",
                },
                {
                    "id": "PCI.DSS.321-IAMNoInlinePolicy",
                    "reason": "CDK-generated inline policy on the AppConfig monitor role",
                },
            ],
        )

        # Wire the monitor onto the environment created earlier. Set here (rather
        # than at environment-creation time) because the alarm and its role don't
        # exist until the Lambda does.
        app_config_env.monitors = [
            appconfig.CfnEnvironment.MonitorsProperty(
                alarm_arn=rollback_alarm.alarm_arn,
                alarm_role_arn=monitor_role.role_arn,
            )
        ]

    def _suppress_rollback_alarm_actions(self, alarm: cloudwatch.Alarm, consumer: str) -> None:
        """Suppress the alarm-action nag rules on a deployment-rollback alarm.

        ``CloudWatchAlarmAction`` (NIST/HIPAA) requires every alarm to carry a
        notification action. These alarms are consumed by a deployment service
        (CodeDeploy always; AppConfig when the opt-in monitor is enabled) that
        polls the alarm state to decide whether to roll back — they are not a
        human-paging channel, so they intentionally carry no SNS action. (The
        operational alarms under the MonitoringFacade DO route to SNS in prod;
        only these deployment-control alarms don't.)
        """
        reason = (
            f"Alarm is consumed by {consumer}, which polls its state to decide on rollback — "
            "not a notification channel, so no SNS action by design"
        )
        acknowledge_rules(
            alarm,
            [
                {"id": "NIST.800.53.R5-CloudWatchAlarmAction", "reason": reason},
                {"id": "HIPAA.Security-CloudWatchAlarmAction", "reason": reason},
            ],
        )

    def _add_resource_suppressions(self, app_insights_dashboard_cleanup: cr.AwsCustomResource) -> None:
        """Attach per-resource cdk-nag suppressions for resources owned by this construct.

        ApiFunction passes Lambda rules natively (tracing=ACTIVE,
        memory_size=256, sync invocation). Suppressions below document the
        intentional design decisions (no VPC, no DLQ, no concurrency) and work
        around CDK-level limitations (inline policies, KMS wildcard actions).
        """
        acknowledge_rules(
            self.function,
            [
                # AwsSolutions-L1 / Serverless-LambdaLatestVersion suppressions
                # were retired when the runtime moved to Python 3.14, which the
                # pinned cdk-nag release recognizes as latest.
                {
                    "id": "Serverless-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                # retry_attempts=0 pins the async posture explicitly, which
                # synthesizes an EventInvokeConfig; the async-failure rule then
                # fires on it for the same not-applicable reason as LambdaDLQ.
                {
                    "id": "Serverless-LambdaAsyncFailureDestination",
                    "reason": (
                        "Invoked synchronously via API Gateway — no async event source exists; "
                        "the EventInvokeConfig exists only to pin retry_attempts=0"
                    ),
                },
                {
                    "id": "NIST.800.53.R5-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                {
                    "id": "HIPAA.Security-LambdaDLQ",
                    "reason": "Invoked synchronously via API Gateway — async DLQ pattern does not apply",
                },
                # NIST.800.53.R5-LambdaConcurrency / HIPAA.Security-LambdaConcurrency
                # are no longer suppressed — reserved_concurrent_executions is set
                # on the function above.
                {"id": "NIST.800.53.R5-LambdaInsideVPC", "reason": "No VPC — adds significant operational complexity"},
                {"id": "HIPAA.Security-LambdaInsideVPC", "reason": "No VPC — adds significant operational complexity"},
                {"id": "PCI.DSS.321-LambdaInsideVPC", "reason": "No VPC — adds significant operational complexity"},
                # Service role uses AWSLambdaBasicExecutionRole managed policy.
                # The Policy:: finding id contains multiple '::', which routes
                # through acknowledge_rules' metadata fallback (see nag_utils).
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AWSLambdaBasicExecutionRole is the minimal managed policy for Lambda execution",
                    "applies_to": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                },
                # Default policy has KMS wildcard actions (required for CMK use).
                # X-Ray segments have no resource-level ARN, so the auto-generated
                # X-Ray statement uses Resource::*. AppConfig calls are
                # resource-scoped to this stack's profile ARN — see the
                # add_to_role_policy grant above.
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "kms:GenerateDataKey* and kms:ReEncrypt* require wildcard action suffix — standard KMS usage pattern",
                    "applies_to": ["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "X-Ray segments have no resource-level ARN — wildcard is required for the X-Ray write statement only",
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
        )

        # AppInsights cleanup custom resource policy: scoped to one dashboard ARN,
        # so only the inline-policy nag rules need a suppression — IAM5 wildcard
        # no longer applies since the policy is resource-scoped.
        acknowledge_rules(
            app_insights_dashboard_cleanup,
            [
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
        )

        # API Gateway CloudWatch role — CDK-managed, uses managed policy.
        # cloud_watch_role=True is required for execution logging (NIST.800.53.R5-
        # APIGWExecutionLoggingEnabled / AwsSolutions-APIG6). The disableCloudWatchRole
        # CDK flag is intentionally NOT enabled because NIST compliance requires
        # execution logging, which requires the account-level CloudWatch role.
        api_cw_role = self.api.node.try_find_child("CloudWatchRole")
        if api_cw_role is not None:
            acknowledge_rules(
                cast(Construct, api_cw_role),
                [
                    {
                        "id": "AwsSolutions-IAM4",
                        "reason": "CDK-managed API Gateway CloudWatch role uses AWS managed policy",
                        "applies_to": [
                            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
                        ],
                    }
                ],
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
            # The function uses JSON log format (logging_format=JSON above), so
            # the platform REPORT line is a structured platform.report record and
            # Logs Insights' auto-extracted @duration — parsed from the *text*
            # REPORT format — never populates. Query the structured record's
            # metrics instead.
            query_string=logs.QueryString(
                fields=[
                    "@timestamp",
                    "record.metrics.durationMs",
                    "record.requestId",
                    "record.metrics.maxMemoryUsedMB",
                ],
                filter_statements=["type = 'platform.report'", "record.metrics.durationMs > 3000"],
                sort="record.metrics.durationMs desc",
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
            # responseLatency is logged by the access-log format above
            # ($context.responseLatency, total request latency in ms) — sorting
            # on it is what makes this query actually return the *slowest*
            # requests rather than the most recent ones.
            query_string=logs.QueryString(
                fields=[
                    "@timestamp",
                    "responseLatency",
                    "status",
                    "httpMethod",
                    "resourcePath",
                    "responseLength",
                    "ip",
                    "xrayTraceId",
                    "requestId",
                ],
                sort="responseLatency desc",
                limit=50,
            ),
            log_groups=[api_log_group],
        )
