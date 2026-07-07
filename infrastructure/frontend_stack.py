from typing import Any, cast

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_athena as athena,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_glue as glue,
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
    aws_rum as rum,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3deploy,
)
from aws_cdk import (
    custom_resources as cr,
)
from constructs import Construct

from infrastructure.nag_utils import (
    AWS_CUSTOM_RESOURCE_PROVIDER_ID,
    BUCKET_DEPLOYMENT_PROVIDER_ID,
    acknowledge_rules,
    apply_compliance_aspects,
    attach_async_failure_destination,
    create_auto_delete_objects_log_group,
    create_sse_s3_log_bucket,
    grant_logs_service_to_key,
    suppress_cdk_singletons,
)

# AWS WAF S3 log schema for the Athena Glue table — the column set + types AWS
# documents at athena/.../create-waf-table-partition-projection.html. WAF logs
# are one JSON object per line; the openx JSON SerDe maps the camelCase keys to
# these lowercase columns case-insensitively. Nested fields are declared with
# Hive struct<>/array<> type strings. (name, hive_type) pairs.
_WAF_LOG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("timestamp", "bigint"),
    ("formatversion", "int"),
    ("webaclid", "string"),
    ("terminatingruleid", "string"),
    ("terminatingruletype", "string"),
    ("action", "string"),
    (
        "terminatingrulematchdetails",
        "array<struct<conditiontype:string,sensitivitylevel:string,location:string,matcheddata:array<string>>>",
    ),
    ("httpsourcename", "string"),
    ("httpsourceid", "string"),
    (
        "rulegrouplist",
        "array<struct<rulegroupid:string,terminatingrule:struct<ruleid:string,action:string,"
        "rulematchdetails:array<struct<conditiontype:string,sensitivitylevel:string,location:string,"
        "matcheddata:array<string>>>>,nonterminatingmatchingrules:array<struct<ruleid:string,action:string,"
        "overriddenaction:string,rulematchdetails:array<struct<conditiontype:string,sensitivitylevel:string,"
        "location:string,matcheddata:array<string>>>,challengeresponse:struct<responsecode:string,"
        "solvetimestamp:string>,captcharesponse:struct<responsecode:string,solvetimestamp:string>>>,"
        "excludedrules:string>>",
    ),
    ("ratebasedrulelist", "array<struct<ratebasedruleid:string,limitkey:string,maxrateallowed:int>>"),
    (
        "nonterminatingmatchingrules",
        "array<struct<ruleid:string,action:string,rulematchdetails:array<struct<conditiontype:string,"
        "sensitivitylevel:string,location:string,matcheddata:array<string>>>,challengeresponse:struct<"
        "responsecode:string,solvetimestamp:string>,captcharesponse:struct<responsecode:string,"
        "solvetimestamp:string>>>",
    ),
    ("requestheadersinserted", "array<struct<name:string,value:string>>"),
    ("responsecodesent", "string"),
    (
        "httprequest",
        "struct<clientip:string,country:string,headers:array<struct<name:string,value:string>>,uri:string,"
        "args:string,httpversion:string,httpmethod:string,requestid:string,fragment:string,scheme:string,"
        "host:string>",
    ),
    ("labels", "array<struct<name:string>>"),
    ("captcharesponse", "struct<responsecode:string,solvetimestamp:string,failurereason:string>"),
    ("challengeresponse", "struct<responsecode:string,solvetimestamp:string,failurereason:string>"),
    ("ja3fingerprint", "string"),
    ("ja4fingerprint", "string"),
    ("oversizefields", "string"),
    ("requestbodysize", "int"),
    ("requestbodysizeinspectedbywaf", "int"),
)

# WAF threat-triage Athena named queries, shared by both WAF tables. Static SQL
# templates (no f-string) with a __WAF_TABLE__ sentinel substituted per table via
# str.replace — the table names are hardcoded literals, so there's no injection
# surface; the static-template form also keeps the SQL-construction linter quiet.
#
# Every query filters on the log_time partition key (last 30 days, rendered in
# the projection's yyyy/MM/dd format — Athena prunes projected partitions only
# when the predicate string matches that format exactly). This is the query
# half of the partition-projection contract with _create_waf_glue_table: the
# filter is what turns projection into pruning, and per the Athena docs a
# condition in any OTHER date format (dashes, DATE literals) silently disables
# it. Keep the predicate and the table's projection.log_time.format in lockstep.
# Each tuple: (construct-id suffix, display-name suffix, description, SQL template).
_WAF_NAMED_QUERIES: tuple[tuple[str, str, str, str], ...] = (
    (
        "RecentBlocked",
        "Recent Blocked Requests",
        "Most recent BLOCK actions (last 30 days) with client IP, country, URI, and terminating rule",
        """\
SELECT from_unixtime(timestamp / 1000) AS request_time,
       httprequest.clientip, httprequest.country, httprequest.uri,
       terminatingruleid
FROM __WAF_TABLE__
WHERE action = 'BLOCK'
  AND log_time >= date_format(current_timestamp - interval '30' day, '%Y/%m/%d')
ORDER BY timestamp DESC
LIMIT 50""",
    ),
    (
        "TopBlockedIps",
        "Top Blocked Client IPs",
        "Client IPs with the most BLOCK actions (last 30 days)",
        """\
SELECT httprequest.clientip, httprequest.country, COUNT(*) AS block_count
FROM __WAF_TABLE__
WHERE action = 'BLOCK'
  AND log_time >= date_format(current_timestamp - interval '30' day, '%Y/%m/%d')
GROUP BY httprequest.clientip, httprequest.country
ORDER BY block_count DESC
LIMIT 25""",
    ),
    (
        "TopRules",
        "Top Terminating Rules",
        "Which rules terminated the most requests (last 30 days)",
        """\
SELECT terminatingruleid, action, COUNT(*) AS hit_count
FROM __WAF_TABLE__
WHERE log_time >= date_format(current_timestamp - interval '30' day, '%Y/%m/%d')
GROUP BY terminatingruleid, action
ORDER BY hit_count DESC
LIMIT 25""",
    ),
    (
        "ByCountry",
        "Blocked by Country",
        "BLOCK actions grouped by client country (last 30 days)",
        """\
SELECT httprequest.country, COUNT(*) AS block_count
FROM __WAF_TABLE__
WHERE action = 'BLOCK'
  AND log_time >= date_format(current_timestamp - interval '30' day, '%Y/%m/%d')
GROUP BY httprequest.country
ORDER BY block_count DESC
LIMIT 25""",
    ),
)


class FrontendStack(Stack):
    """CDK stack for the Serverless App frontend.

    Provisions a private S3 bucket for static assets and a CloudFront
    distribution with OAC, HTTPS-only enforcement, and security response
    headers. WAF protection is provided by a WebACL ARN passed in from
    WafStack, which is always deployed in us-east-1.

    This stack can be deployed to any region. When the target region differs
    from us-east-1, CDK bridges the WAF ARN cross-region automatically via
    SSM Parameter Store (enabled by cross_region_references=True in app.py).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        api_url: str,
        api_id: str,
        waf_acl_arn: str,
        cf_waf_logs_location: str,
        regional_waf_logs_location: str,
        **kwargs: Any,
    ) -> None:
        """Provision all frontend AWS resources.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            api_url: The backend API Gateway URL, injected into config.json at deploy time.
            api_id: The backend API Gateway REST API ID, used to pin the CSP connect-src
                to the exact execute-api host instead of a region-wide wildcard.
            waf_acl_arn: ARN of the WAF WebACL from WafStack (always in us-east-1).
            cf_waf_logs_location: ``s3://…/`` prefix of the CloudFront WebACL's WAF logs,
                for the Athena Glue table (computed by the Stage — see its docstring).
            regional_waf_logs_location: ``s3://…/`` prefix of the regional (API Gateway)
                WebACL's WAF logs, for the Athena Glue table.
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, **kwargs)
        self._api_id = api_id
        self._cf_waf_logs_location = cf_waf_logs_location
        self._regional_waf_logs_location = regional_waf_logs_location

        apply_compliance_aspects(self)

        # ── KMS key ──────────────────────────────────────────────────────────
        # Used to encrypt the frontend S3 bucket and CloudWatch log group.
        # CloudWatch Logs requires the Logs service principal in the key policy.
        frontend_encryption_key = kms.Key(
            self,
            "FrontendEncryptionKey",
            description=f"KMS key for {self.stack_name} S3 bucket and log groups",
            enable_key_rotation=True,
            # See BackendApp.encryption_key for the rationale — automated
            # rotation, no dependent redeploys, 90-day compliance baseline.
            rotation_period=Duration.days(90),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Confused-deputy guard on the CMK's CloudWatch Logs service grant.
        # See ``grant_logs_service_to_key`` in ``nag_utils.py``.
        grant_logs_service_to_key(
            frontend_encryption_key,
            region=self.region,
            account=self.account,
            partition=self.partition,
        )

        # ── S3 access logging bucket ─────────────────────────────────────────
        # Receives S3 server access logs (asset bucket), CloudFront standard
        # access logs, and Athena query results. SSE-S3 (not SSE-KMS) because
        # neither S3 log delivery nor CloudFront standard logging support
        # KMS-encrypted target buckets. Built via the shared log-sink helper;
        # object_ownership=BUCKET_OWNER_PREFERRED keeps ACLs usable for CloudFront
        # standard logging (which delivers via ACL). 7-day lifecycle is tunable —
        # extend or tier to Glacier for longer retention (see "Audit stack and
        # log retention" in the README).
        access_log_bucket = create_sse_s3_log_bucket(
            self,
            "FrontendAccessLogBucket",
            suppression_reason=(
                "Access-log bucket — SSE-S3 (S3/CloudFront log delivery doesn't support "
                "KMS-CMK target buckets), self-logging would be circular, no versioning/"
                "replication for append-only transient logs"
            ),
            expiration_days=7,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
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
            server_access_logs_prefix="s3-access-logs/",
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Expose the buckets for the audit stack to consume cross-stack — the
        # CloudTrail trail recording object-level data events on them lives in
        # AuditStack (a one-way dependency: audit -> frontend).
        self.bucket = bucket
        self.access_log_bucket = access_log_bucket

        # ── CloudFront response headers ──────────────────────────────────────
        # Custom ResponseHeadersPolicy (replaces the AWS-managed SECURITY_HEADERS)
        # adding HSTS + CSP on top of the four headers that policy provided.
        # See _build_response_headers_policy for the full rationale.
        response_headers_policy = self._build_response_headers_policy()

        # ── CloudFront distribution ──────────────────────────────────────────
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=response_headers_policy,
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
            enable_logging=True,
            log_bucket=access_log_bucket,
            log_file_prefix="cloudfront/",
        )

        # ── CloudWatch RUM + X-Ray ───────────────────────────────────────────
        # RUM collects browser telemetry (page loads, JS errors, fetch latency)
        # and — with enable_x_ray — emits a client-side trace segment that joins
        # the backend Lambda/API Gateway segments into a single X-Ray trace.
        # Guest (unauthenticated) browsers authenticate via Cognito Identity
        # Pool → STS AssumeRoleWithWebIdentity → scoped rum:PutRumEvents role.
        # The monitor ARN is constructed from the known monitor name so the
        # IAM role can reference it without a circular dependency on the
        # CfnAppMonitor resource.
        rum_identity_pool = cognito.CfnIdentityPool(
            self,
            "RumIdentityPool",
            allow_unauthenticated_identities=True,
            identity_pool_name=f"{self.stack_name}-rum",
        )
        # Pinned physical name (the IAM role below references the constructed
        # ARN): a future replacement-forcing property change collides with the
        # not-yet-deleted old monitor (CFN replacement is create-before-delete),
        # so such a change must also change the name in the same commit — see
        # the AppConfig profile note in backend_app.py.
        rum_monitor_name = f"{self.stack_name}-rum"
        rum_monitor_arn = f"arn:{self.partition}:rum:{self.region}:{self.account}:appmonitor/{rum_monitor_name}"
        rum_unauth_role = iam.Role(
            self,
            "RumUnauthenticatedRole",
            assumed_by=iam.FederatedPrincipal(
                "cognito-identity.amazonaws.com",
                conditions={
                    "StringEquals": {"cognito-identity.amazonaws.com:aud": rum_identity_pool.ref},
                    "ForAnyValue:StringLike": {"cognito-identity.amazonaws.com:amr": "unauthenticated"},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            description=f"Guest role assumed by browser RUM clients for {rum_monitor_name}",
            inline_policies={
                "AllowPutRumEvents": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["rum:PutRumEvents"],
                            resources=[rum_monitor_arn],
                        )
                    ]
                )
            },
        )
        cognito.CfnIdentityPoolRoleAttachment(
            self,
            "RumIdentityPoolRoleAttachment",
            identity_pool_id=rum_identity_pool.ref,
            roles={"unauthenticated": rum_unauth_role.role_arn},
        )
        rum_app_monitor = rum.CfnAppMonitor(
            self,
            "RumAppMonitor",
            name=rum_monitor_name,
            domain=distribution.distribution_domain_name,
            cw_log_enabled=True,
            # Enable custom events so the frontend can call cwr('recordEvent', ...)
            # for domain telemetry. Without this set to ENABLED, custom event
            # uploads are silently dropped at the data plane.
            custom_events=rum.CfnAppMonitor.CustomEventsProperty(status="ENABLED"),
            app_monitor_configuration=rum.CfnAppMonitor.AppMonitorConfigurationProperty(
                allow_cookies=True,
                enable_x_ray=True,
                # session_sample_rate is a CLIENT-SIDE knob: the RUM browser
                # client reads it to decide what fraction of sessions to record.
                # It does NOT bound the data plane — the public unauthenticated
                # identity pool above lets anyone mint guest credentials and call
                # rum:PutRumEvents directly, ignoring this rate entirely. So
                # lowering it is a legitimate-traffic COST lever only; the
                # adversarial-ingestion vector needs a Budgets / RUM-volume alarm,
                # not a smaller sample rate (see TODO.md "CloudWatch RUM").
                session_sample_rate=1.0,
                # CloudFormation's schema only accepts ["errors", "performance", "http"] here —
                # "interaction" is rejected as an invalid enum value despite being a real RUM
                # plugin. This server-side list is metadata used by the AWS-generated snippet,
                # not the live plugin loader. The actual plugin set is controlled by the
                # client-side `telemetries` array in frontend/index.html, which DOES include
                # "interaction" alongside the http tuple form. Keep these two lists divergent
                # on purpose; do not "sync" them.
                telemetries=["errors", "performance", "http"],
                identity_pool_id=rum_identity_pool.ref,
                guest_role_arn=rum_unauth_role.role_arn,
            ),
        )

        # CMK-encrypted log group for the BucketDeployment provider Lambda.
        # Passing log_group= here (instead of log_retention=) avoids the legacy
        # LogRetention singleton path and keeps every log group encrypted with
        # this stack's CMK.
        bucket_deployment_log_group = logs.LogGroup(
            self,
            "BucketDeploymentLogGroup",
            encryption_key=frontend_encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Shared CMK-encrypted log group for all AwsCustomResource singletons in
        # this stack (RumMetricsDestination, RumExtendedMetrics, InvalidateCloudFrontCache).
        # CDK reuses one provider Lambda across every AwsCustomResource in a stack,
        # so a single log group serves all three.
        custom_resource_log_group = logs.LogGroup(
            self,
            "AwsCustomResourceLogGroup",
            encryption_key=frontend_encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        rum_extended_metrics = self._wire_rum_metrics_extras(
            rum_app_monitor, rum_monitor_name, rum_monitor_arn, custom_resource_log_group
        )
        self._wire_rum_log_group_cleanup(rum_app_monitor, rum_monitor_name, custom_resource_log_group)

        # ── Deploy frontend assets ───────────────────────────────────────────
        # Uploads frontend/ to S3 and generates config.json with the API URL
        # and RUM client config injected at deploy time. Cache invalidation is
        # handled by a separate AwsCustomResource below — the BucketDeployment's
        # built-in `distribution=` parameter is intentionally not used because
        # its delete-time invalidation races with CloudFront's own deletion on
        # `cdk destroy` (aws/aws-cdk#15891).
        bucket_deployment = s3deploy.BucketDeployment(
            self,
            "DeployFrontend",
            sources=[
                s3deploy.Source.asset("frontend"),
                s3deploy.Source.json_data(
                    "config.json",
                    {
                        "apiUrl": api_url,
                        "rum": {
                            "appMonitorId": rum_app_monitor.attr_id,
                            "identityPoolId": rum_identity_pool.ref,
                            "region": self.region,
                            # Session attributes are attached to every RUM event
                            # in the session. Sourcing them from deploy-time
                            # config (rather than hardcoding in the HTML) lets
                            # multiple deploys feed the same dashboard while
                            # remaining filterable.
                            "sessionAttributes": {
                                "applicationName": self.stack_name,
                            },
                        },
                    },
                ),
            ],
            destination_bucket=bucket,
            log_group=bucket_deployment_log_group,
        )
        # Defer the slow asset deploy until after the RUM custom resources
        # have succeeded. If RumExtendedMetrics fails (it depends on IAM
        # propagation), the BucketDeployment never starts — saving the most
        # expensive single resource from being repeated on every retry until
        # the cheaper IAM dance settles.
        bucket_deployment.node.add_dependency(rum_extended_metrics)

        # CloudFront cache invalidation, decoupled from BucketDeployment.
        # Defines on_create and on_update only — no on_delete — so CFN simply
        # removes this resource from stack state during teardown without any
        # CloudFront API call to race with the distribution's own deletion.
        # This is the permanent fix for aws/aws-cdk#15891, replacing the
        # BucketDeployment's built-in invalidation hook.
        #
        # CallerReference is gated on the BucketDeployment's content-hashed S3
        # object key. Same assets → same key → CFN sees no change → no invalidation
        # fires (correct: nothing to invalidate). Different assets → different key →
        # CFN fires on_update → invalidation runs. Prevents backend-only deploys from
        # burning the 1000/month free invalidation quota. See README "Design
        # decisions" for the longer write-up.
        #
        # NOTE: CloudFront caps CallerReference at 128 characters, so we cannot fold
        # the api_url (or other identifiers) into it — the content-hashed object key
        # alone is already ~68 chars. If a backend-only API replacement changes
        # config.json's contents, that path is covered because Source.json_data
        # writes config.json as part of the BucketDeployment asset, so its hash —
        # and thus this object key — changes when api_url changes.
        #
        # Rollback caveat (accepted): CloudFront treats a repeated CallerReference
        # as an idempotent replay of the earlier invalidation, so rolling assets
        # back to a previously deployed content hash reuses that deploy's
        # CallerReference and NO new invalidation runs — viewers keep the
        # rolled-back-FROM version cached until TTL expiry. A deploy-unique nonce
        # would fix that but would also invalidate on every backend-only deploy,
        # defeating the quota-saving design. After an asset rollback, run a manual
        # invalidation (CloudFrontDistributionId output) if freshness matters.
        # object_keys is a CDK list-token, not a Python list — use Fn.select.
        cf_invalidation_call = cr.AwsSdkCall(
            service="CloudFront",
            action="createInvalidation",
            parameters={
                "DistributionId": distribution.distribution_id,
                "InvalidationBatch": {
                    "Paths": {"Quantity": 1, "Items": ["/*"]},
                    "CallerReference": Fn.select(0, bucket_deployment.object_keys),
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"{self.stack_name}-cf-invalidation"),
        )
        invalidate_cf_cache = cr.AwsCustomResource(
            self,
            "InvalidateCloudFrontCache",
            on_create=cf_invalidation_call,
            on_update=cf_invalidation_call,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["cloudfront:CreateInvalidation"],
                        resources=[
                            f"arn:{Stack.of(self).partition}:cloudfront::{Stack.of(self).account}:distribution/{distribution.distribution_id}"
                        ],
                    ),
                ]
            ),
            log_group=custom_resource_log_group,
        )
        invalidate_cf_cache.node.add_dependency(bucket_deployment)
        # CDK generates an inline default policy on the AwsCustomResource's
        # auto-created role. Same constraint as the RUM custom resources;
        # apply the same IAMNoInlinePolicy suppressions.
        cf_invalidation_inline_reason = (
            "AwsCustomResource policy is a single least-privilege inline statement scoped to "
            "cloudfront:CreateInvalidation on this stack's distribution ARN — managed-policy "
            "reuse adds nothing"
        )
        acknowledge_rules(
            invalidate_cf_cache,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": cf_invalidation_inline_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": cf_invalidation_inline_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": cf_invalidation_inline_reason},
            ],
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
        CfnOutput(
            self,
            "RumAppMonitorId",
            description="CloudWatch RUM app monitor ID — used by the browser RUM client",
            value=rum_app_monitor.attr_id,
        )
        CfnOutput(
            self,
            "RumIdentityPoolId",
            description="Cognito Identity Pool ID — used by the browser RUM client for guest credentials",
            value=rum_identity_pool.ref,
        )

        # ── RUM / Cognito cdk-nag suppressions ───────────────────────────────
        # Unauthenticated identities are intentional — browsers have no prior
        # identity and RUM's guest-credentials model is the standard pattern.
        # The role's only permission is rum:PutRumEvents on this monitor.
        acknowledge_rules(
            rum_identity_pool,
            [
                {
                    "id": "AwsSolutions-COG7",
                    "reason": "RUM requires unauthenticated guest credentials for anonymous browser telemetry",
                },
            ],
        )
        # The guest role has a single least-privilege permission — rum:PutRumEvents
        # on exactly one monitor ARN — tightly bound to this role's one purpose.
        # A managed policy would add indirection without changing the security
        # posture, since the policy is used by nothing else and is scoped to a
        # resource that is itself one-to-one with the role.
        inline_policy_reason = (
            "Single least-privilege inline policy (rum:PutRumEvents on one monitor ARN) "
            "is tightly bound to this role's sole purpose — anonymous browser telemetry upload"
        )
        acknowledge_rules(
            rum_unauth_role,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": inline_policy_reason},
            ],
        )

        # Explicit CMK log group + singleton nag suppressions for the CDK
        # auto-delete-objects provider Lambda (see create_auto_delete_objects_log_group
        # in nag_utils — it owns both, so there's no per-stack block here).
        create_auto_delete_objects_log_group(self, frontend_encryption_key)

        self._create_athena_glue_resources(access_log_bucket, frontend_encryption_key)

        # ── Per-resource cdk-nag suppressions ──────────────────────────────────
        # All Lambdas in this stack are CDK-managed singletons. Their construct
        # IDs are stable (hashed from CDK's own source) but they are created as
        # stack-level siblings of the construct that requested them, so we look
        # them up with ``try_find_child`` rather than absolute path strings —
        # this keeps the suppression working regardless of whether the stack is
        # at the App root or nested inside a cdk.Stage.
        #
        # Stable singleton IDs:
        #   Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C — BucketDeployment provider
        #   Custom::S3AutoDeleteObjectsCustomResourceProvider — auto-delete provider
        #   AWS679f53fac002430cb0da5b7982bd2287 — AwsCustomResource provider Lambda
        #     (used by RumMetricsDestination, RumExtendedMetrics, InvalidateCloudFrontCache)
        suppress_cdk_singletons(
            self,
            (
                BUCKET_DEPLOYMENT_PROVIDER_ID,
                AWS_CUSTOM_RESOURCE_PROVIDER_ID,
            ),
        )

        # ── Async failure destinations for the CDK-managed provider Lambdas ─────
        # See BackendStack for the full rationale — CFN invokes the providers
        # async, and without on_failure a crashed provider's payload is lost.
        # Both stack-level Function-based singletons get the same treatment: the
        # AwsCustomResource provider AND the BucketDeployment handler. (The S3
        # auto-delete provider is a CustomResourceProvider, not a Function — its
        # async config is not reachable through the L2, which is exactly what
        # its Serverless-LambdaDLQ suppression in CDK_LAMBDA_SUPPRESSIONS
        # documents.) attach_async_failure_destination also emits a CfnOutput of
        # each DLQ URL, so the returned queues are surfaced for operators.
        self.cr_provider_dlq = attach_async_failure_destination(
            self,
            AWS_CUSTOM_RESOURCE_PROVIDER_ID,
            encryption_key=frontend_encryption_key,
            queue_id="AwsCustomResourceProviderDlq",
        )
        self.bucket_deployment_dlq = attach_async_failure_destination(
            self,
            BUCKET_DEPLOYMENT_PROVIDER_ID,
            encryption_key=frontend_encryption_key,
            queue_id="BucketDeploymentProviderDlq",
        )

        self._acknowledge_bucket_deployment_grants(bucket)

        # minimizePolicies restructures the BucketDeployment handler's inline
        # policy into a separate resource under DeployFrontend/CustomResourceHandler.
        deploy_frontend = self.node.try_find_child("DeployFrontend")
        if deploy_frontend is not None:
            suppress_cdk_singletons(deploy_frontend, ("CustomResourceHandler",))

        # ── Stack-level cdk-nag suppressions (genuinely stack-wide) ─────────────
        replication_reason = "S3 replication not needed for sample app — static assets are redeployable"
        versioning_reason = "S3 versioning not needed for sample app — static assets are redeployable via cdk deploy"
        stack_suppressions = [
            ("AwsSolutions-CFR1", "Geo restriction not required for sample app"),
            ("AwsSolutions-CFR4", "Using default CloudFront certificate — no custom domain for sample app"),
            ("NIST.800.53.R5-S3BucketReplicationEnabled", replication_reason),
            ("NIST.800.53.R5-S3BucketVersioningEnabled", versioning_reason),
            ("HIPAA.Security-S3BucketReplicationEnabled", replication_reason),
            ("HIPAA.Security-S3BucketVersioningEnabled", versioning_reason),
            ("PCI.DSS.321-S3BucketReplicationEnabled", replication_reason),
            ("PCI.DSS.321-S3BucketVersioningEnabled", versioning_reason),
        ]
        acknowledge_rules(
            self,
            [{"id": rule, "reason": reason} for rule, reason in stack_suppressions],
        )

    def _acknowledge_bucket_deployment_grants(self, bucket: s3.Bucket) -> None:
        """Acknowledge the BucketDeployment handler's CDK-generated s3 grants.

        cdk-nag v3 matches IAM5 findings individually, so these are enumerated
        here rather than in the shared CDK_LAMBDA_SUPPRESSIONS list: the
        asset-bucket finding id embeds this stack's region (and the default
        bootstrap qualifier hnb659fds), and the destination-bucket finding id
        embeds the bucket's logical id — neither is expressible in a static
        shared list. The handler needs read on the CDK bootstrap asset bucket
        and read/write/delete on the destination bucket; these seven findings
        are exactly that policy, and anything new the handler grows will fail
        the nag gate rather than being silently absorbed.
        """
        bucket_deployment_provider = self.node.try_find_child(BUCKET_DEPLOYMENT_PROVIDER_ID)
        if bucket_deployment_provider is None:
            return
        deployment_reason = (
            "CDK BucketDeployment handler policy — CDK-generated s3 read on the bootstrap asset "
            "bucket and read/write/delete on the destination bucket; not configurable by the caller"
        )
        bucket_logical_id = self.get_logical_id(cast(s3.CfnBucket, bucket.node.default_child))
        acknowledge_rules(
            bucket_deployment_provider,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": deployment_reason,
                    "applies_to": [
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:Abort*",
                        # Both partition renderings — see the RUM cleanup note:
                        # the CLI synth resolves arn:aws: literals, the
                        # flag-less test synth renders <AWS::Partition>.
                        f"Resource::arn:aws:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-{self.region}/*",
                        f"Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-<AWS::AccountId>-{self.region}/*",
                        f"Resource::<{bucket_logical_id}.Arn>/*",
                    ],
                },
            ],
        )

    def _build_response_headers_policy(self) -> cloudfront.ResponseHeadersPolicy:
        """Build the CloudFront ResponseHeadersPolicy (managed SECURITY_HEADERS + HSTS + CSP).

        A custom policy instead of the AWS-managed SECURITY_HEADERS policy, which
        omits two headers a production edge wants: Strict-Transport-Security (HSTS)
        and Content-Security-Policy (CSP). The four headers the managed policy
        already provided (X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
        X-XSS-Protection) are reproduced here so nothing regresses.

        HSTS: 1-year max-age with includeSubDomains. ``preload`` is intentionally
        False — the HSTS preload list is keyed on the registrable domain, and a
        shared ``*.cloudfront.net`` host cannot (and should not) be submitted. A
        fork that wires a custom domain (see TODO "Enforce TLS 1.2+ minimum")
        should flip preload=True once ready to commit the domain to the list.

        CSP: the reference page ships inline ``<script>`` and ``<style>`` blocks
        (the RUM bootstrap in ``<head>`` shares ``window.__appConfigPromise`` with
        the body handler, relying on synchronous head execution), so
        ``'unsafe-inline'`` is required on script-src/style-src or the page's own
        scripts would be blocked. Everything else is locked down:
        default-src/object-src/base-uri/frame-ancestors are constrained, and
        script-src/connect-src are scoped to the exact AWS endpoints the page talks
        to (RUM client script + data plane, Cognito Identity for guest credentials,
        and this stack's specific execute-api host for the API call). To reach a
        strict (nonce/hash) CSP without ``'unsafe-inline'``, externalize the two
        inline scripts into ``'self'``-served .js assets first — CloudFront cannot
        mint per-response nonces, and static hashes would break on any edit.
        Tracked in TODO "CSP header on the static page".

        The RUM client script URL is hardcoded to us-east-1 in frontend/index.html
        regardless of deploy region, so script-src pins us-east-1 specifically
        while connect-src follows ``self.region`` for the data plane and Cognito.
        The execute-api host is pinned to this API's exact ID (``self._api_id``, a
        CDK token resolved to ``{id}.execute-api.{region}.amazonaws.com`` at synth)
        rather than a ``*.execute-api`` wildcard, so an injected script can only
        reach this app's own API — not every API in the region/account.
        """
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://client.rum.us-east-1.amazonaws.com; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' "
            f"https://dataplane.rum.{self.region}.amazonaws.com "
            f"https://cognito-identity.{self.region}.amazonaws.com "
            f"https://{self._api_id}.execute-api.{self.region}.amazonaws.com; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        return cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeadersPolicy",
            response_headers_policy_name=f"{self.stack_name}-security-headers",
            comment="Security headers for the Serverless App frontend (managed SECURITY_HEADERS + HSTS + CSP)",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True,
                    mode_block=True,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    preload=False,
                    override=True,
                ),
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp,
                    override=True,
                ),
            ),
        )

    def _wire_rum_metrics_extras(
        self,
        rum_app_monitor: rum.CfnAppMonitor,
        rum_monitor_name: str,
        rum_monitor_arn: str,
        custom_resource_log_group: logs.LogGroup,
    ) -> cr.AwsCustomResource:
        """Wire CloudWatch metrics destination and dimensioned metric definitions to the AppMonitor.

        Returns the metric-definitions custom resource so callers can wire a dependency on it.

        Implementation notes (these are non-obvious — see README "CloudWatch RUM" section):
        - Each definition needs an explicit ``EventPattern``; the API rejects vended-metric
          submissions with just ``Name`` + ``DimensionKeys`` (returns 200 OK with an Errors[]
          body that AwsCustomResource treats as success).
        - All three ``rum:*`` actions are bundled on the destination CR's policy so the
          BatchCreate call benefits from a full putRumMetricsDestination round-trip of IAM
          propagation lead time. Splitting them per-CR loses the IAM race ~100% of the time.
        - ``on_update`` mirrors ``on_create``; without it AwsCustomResource no-ops on
          CloudFormation UPDATE events and changes to the metric list never reach AWS.
        - Http5xx omits the explicit numeric range filter that Http4xx requires (RUM applies
          the 5xx filter internally for that vended metric).
        """
        rum_metrics_destination = cr.AwsCustomResource(
            self,
            "RumMetricsDestination",
            on_create=cr.AwsSdkCall(
                service="rum",
                action="putRumMetricsDestination",
                parameters={"AppMonitorName": rum_monitor_name, "Destination": "CloudWatch"},
                physical_resource_id=cr.PhysicalResourceId.of(f"{rum_monitor_name}/CloudWatch"),
            ),
            on_delete=cr.AwsSdkCall(
                service="rum",
                action="deleteRumMetricsDestination",
                parameters={"AppMonitorName": rum_monitor_name, "Destination": "CloudWatch"},
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "rum:PutRumMetricsDestination",
                            "rum:DeleteRumMetricsDestination",
                            "rum:BatchCreateRumMetricDefinitions",
                        ],
                        resources=[rum_monitor_arn],
                    ),
                ]
            ),
            log_group=custom_resource_log_group,
        )
        rum_metrics_destination.node.add_dependency(rum_app_monitor)

        js_pat = '{{"event_type":["com.amazon.rum.js_error_event"],"metadata":{{"{k}":[{{"exists":true}}]}}}}'
        http_pat = '{{"event_type":["com.amazon.rum.http_event"],"metadata":{{"browserName":[{{"exists":true}}]}}{s}}}'
        http4xx_status = ',"event_details":{"response":{"status":[{"numeric":[">=",400,"<",500]}]}}'
        page_pat = '{"event_type":["com.amazon.rum.page_view_event"],"metadata":{"pageId":[{"exists":true}]}}'
        defs: list[dict[str, Any]] = [
            {
                "Name": "JsErrorCount",
                "EventPattern": js_pat.format(k="browserName"),
                "DimensionKeys": {"metadata.browserName": "BrowserName"},
            },
            {
                "Name": "JsErrorCount",
                "EventPattern": js_pat.format(k="deviceType"),
                "DimensionKeys": {"metadata.deviceType": "DeviceType"},
            },
            {
                "Name": "JsErrorCount",
                "EventPattern": js_pat.format(k="countryCode"),
                "DimensionKeys": {"metadata.countryCode": "CountryCode"},
            },
            {
                "Name": "Http4xxCount",
                "EventPattern": http_pat.format(s=http4xx_status),
                "DimensionKeys": {"metadata.browserName": "BrowserName"},
            },
            {
                "Name": "Http5xxCount",
                "EventPattern": http_pat.format(s=""),
                "DimensionKeys": {"metadata.browserName": "BrowserName"},
            },
            {"Name": "PageViewCount", "EventPattern": page_pat, "DimensionKeys": {"metadata.pageId": "PageId"}},
        ]
        batch_create = cr.AwsSdkCall(
            service="rum",
            action="batchCreateRumMetricDefinitions",
            parameters={
                "AppMonitorName": rum_monitor_name,
                "Destination": "CloudWatch",
                "MetricDefinitions": defs,
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"{rum_monitor_name}/extended-metrics"),
        )
        rum_extended_metrics = cr.AwsCustomResource(
            self,
            "RumExtendedMetrics",
            on_create=batch_create,
            on_update=batch_create,
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=[rum_monitor_arn]),
            log_group=custom_resource_log_group,
        )
        rum_extended_metrics.node.add_dependency(rum_metrics_destination)

        # Same single-purpose, monitor-scoped justification as the RumUnauthenticatedRole
        # inline policy. Cdk-nag flags both per-construct CustomResourcePolicy resources.
        reason = (
            "Single least-privilege inline policy attached to the CDK AwsCustomResource handler — "
            "scoped to specific rum:* actions on one monitor ARN; managed-policy reuse adds nothing"
        )
        for construct in (rum_metrics_destination, rum_extended_metrics):
            acknowledge_rules(
                construct,
                [
                    {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": reason},
                    {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": reason},
                    {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": reason},
                ],
            )
        return rum_extended_metrics

    def _wire_rum_log_group_cleanup(
        self,
        rum_app_monitor: rum.CfnAppMonitor,
        rum_monitor_name: str,
        custom_resource_log_group: logs.LogGroup,
    ) -> None:
        """Delete the RUM-auto-created CloudWatch Logs group at stack destroy.

        CloudWatch RUM creates a log group at
        ``/aws/vendedlogs/RUMService_{monitor-name}{first-8-hex-of-monitor-id}``
        the first time it ingests an event. That log group is owned by this
        account but created outside CloudFormation, so ``cdk destroy`` deletes
        the AppMonitor without touching the log group — same dangling-resource
        shape as the Application Insights dashboard that
        ``AppInsightsDashboardCleanup`` in the backend stack solves.

        ``ResourceNotFoundException`` is ignored so destroy succeeds even when
        no events were ever ingested (the log group only materializes on the
        first event — common in CI / dev / no-traffic deploys).
        """
        monitor_id_prefix = Fn.select(0, Fn.split("-", rum_app_monitor.attr_id))
        log_group_name = Fn.join("", [f"/aws/vendedlogs/RUMService_{rum_monitor_name}", monitor_id_prefix])
        # The IAM scope uses a name-suffix wildcard (RUMService_{name}*) instead
        # of folding the runtime-resolved monitor-id token into the ARN. The
        # delete call itself still targets the exact log group (log_group_name
        # above); widening only the *grant* from "this monitor id" to "this
        # monitor name, any id" costs nothing real — the name embeds the stack
        # name — and keeps the ARN a plain literal, which cdk-nag v3 needs: its
        # granular IAM5 finding id is the verbatim resource string, and a token
        # in the ARN would serialize an Fn::Select JSON blob into the finding id
        # this code must then reproduce byte-for-byte to acknowledge.
        log_group_arn = (
            f"arn:{self.partition}:logs:{self.region}:{self.account}:"
            f"log-group:/aws/vendedlogs/RUMService_{rum_monitor_name}*:*"
        )
        cleanup = cr.AwsCustomResource(
            self,
            "RumLogGroupCleanup",
            on_delete=cr.AwsSdkCall(
                service="CloudWatchLogs",
                action="deleteLogGroup",
                parameters={"logGroupName": log_group_name},
                physical_resource_id=cr.PhysicalResourceId.of("RumLogGroupCleanup"),
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(resources=[log_group_arn]),
            install_latest_aws_sdk=False,
            log_group=custom_resource_log_group,
        )
        # The implicit attr_id reference already forces this dependency at the
        # CFN level; add_dependency makes the intent visible in code.
        cleanup.node.add_dependency(rum_app_monitor)

        # Matches the IAMNoInlinePolicy suppression pattern on the other RUM CRs
        # in this stack — CDK generates the handler's policy inline.
        reason = (
            "Single least-privilege inline policy attached to the CDK AwsCustomResource handler — "
            "scoped to logs:DeleteLogGroup on one log-group ARN; managed-policy reuse adds nothing"
        )
        # AwsSolutions-IAM5 fires on the two wildcards in the grant ARN: the
        # standard :* log-stream suffix required by every CloudWatch Logs
        # resource ARN per the IAM docs, and the monitor-name suffix wildcard
        # explained on log_group_arn above. The acknowledgment pins the exact
        # finding id (cdk-nag v3 renders the verbatim resource string, with
        # pseudo-parameters as <AWS::Partition>/<AWS::AccountId> placeholders).
        iam5_reason = (
            "Log-group ARN carries the standard :* log-stream wildcard suffix (required for any "
            "CloudWatch Logs resource ARN per the IAM service authorization docs) plus a monitor-name "
            "suffix wildcard that keeps the ARN literal for cdk-nag v3's verbatim finding ids — the "
            "grant still reaches only this stack's RUM vended log group namespace."
        )
        # Both partition renderings: the CLI synth resolves arn:aws: literals
        # (cdk.json's @aws-cdk/core:enablePartitionLiterals + target-partitions
        # ["aws"]), while the flag-less in-process test synth renders the
        # <AWS::Partition> placeholder — and cdk-nag v3's raw IAM5 resource
        # finding ids reproduce whichever form the template carries (unlike
        # IAM4, which normalizes the partition). Acknowledge both so the CLI
        # gate and the test gate stay in lockstep.
        rum_log_group_findings = [
            f"Resource::arn:{partition}:logs:{self.region}:<AWS::AccountId>:"
            f"log-group:/aws/vendedlogs/RUMService_{rum_monitor_name}*:*"
            for partition in ("aws", "<AWS::Partition>")
        ]
        acknowledge_rules(
            cleanup,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": reason},
                {"id": "AwsSolutions-IAM5", "reason": iam5_reason, "applies_to": rum_log_group_findings},
            ],
        )

    def _create_athena_glue_resources(self, access_log_bucket: s3.Bucket, encryption_key: kms.Key) -> None:
        """Create Glue catalog tables and Athena workgroup for CloudFront/S3 access log analytics."""
        # ── Glue Database ────────────────────────────────────────────────
        # Glue database names: lowercase, alphanumeric + underscores only.
        #
        # Pinned physical names, stack-wide caveat: the Glue database and every
        # table in it (cloudfront_logs, s3_access_logs, waf_*), plus the Athena
        # workgroup below, carry explicit names (Glue/Athena L1s require them).
        # A replacement-forcing property change — e.g. editing a table's
        # partition_keys — collides with the not-yet-deleted old resource (CFN
        # replacement is create-before-delete: AlreadyExistsException), so such
        # a change must also change the pinned name in the same commit — see
        # the AppConfig profile note in backend_app.py.
        db_name = self.node.id.lower().replace("-", "_") + "_access_logs"

        glue_db = glue.CfnDatabase(
            self,
            "AccessLogsDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=db_name,
                description="Glue catalog for CloudFront and S3 access logs",
            ),
        )

        # ── CloudFront Standard Logs Table ───────────────────────────────
        # 33-field tab-separated format; 2 header lines (#Version, #Fields).
        # All columns typed as string — CloudFront uses '-' for missing values.
        cf_table = glue.CfnTable(
            self,
            "CloudFrontLogsTable",
            catalog_id=self.account,
            database_name=db_name,
            table_input=glue.CfnTable.TableInputProperty(
                name="cloudfront_logs",
                description="CloudFront standard access logs",
                table_type="EXTERNAL_TABLE",
                parameters={"skip.header.line.count": "2", "EXTERNAL": "TRUE"},
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{access_log_bucket.bucket_name}/cloudfront/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
                        parameters={"field.delim": "\t", "serialization.null.format": "-"},
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="log_date", type="string"),
                        glue.CfnTable.ColumnProperty(name="log_time", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_location", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_bytes", type="string"),
                        glue.CfnTable.ColumnProperty(name="c_ip", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_method", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_host", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_uri_stem", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_status", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_referer", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_user_agent", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_uri_query", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_cookie", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_result_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_request_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_host_header", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_protocol", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_bytes", type="string"),
                        glue.CfnTable.ColumnProperty(name="time_taken", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_forwarded_for", type="string"),
                        glue.CfnTable.ColumnProperty(name="ssl_protocol", type="string"),
                        glue.CfnTable.ColumnProperty(name="ssl_cipher", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_response_result_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="cs_protocol_version", type="string"),
                        glue.CfnTable.ColumnProperty(name="fle_status", type="string"),
                        glue.CfnTable.ColumnProperty(name="fle_encrypted_fields", type="string"),
                        glue.CfnTable.ColumnProperty(name="c_port", type="string"),
                        glue.CfnTable.ColumnProperty(name="time_to_first_byte", type="string"),
                        glue.CfnTable.ColumnProperty(name="x_edge_detailed_result_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_content_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_content_len", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_range_start", type="string"),
                        glue.CfnTable.ColumnProperty(name="sc_range_end", type="string"),
                    ],
                ),
            ),
        )
        cf_table.add_dependency(glue_db)

        # ── S3 Server Access Logs Table ──────────────────────────────────
        # 26-field format with quoted strings and optional trailing fields.
        # RegexSerDe handles the complex delimiter pattern reliably.
        s3_log_regex = (
            r"([^ ]*) ([^ ]*) \[(.*?)\] ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) "
            r'("[^"]*"|-) (-|[0-9]*) ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) '
            r'([^ ]*) ("[^"]*"|-) ([^ ]*)(?: ([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*) '
            r"([^ ]*) ([^ ]*) ([^ ]*) ([^ ]*))?.*$"
        )
        s3_table = glue.CfnTable(
            self,
            "S3AccessLogsTable",
            catalog_id=self.account,
            database_name=db_name,
            table_input=glue.CfnTable.TableInputProperty(
                name="s3_access_logs",
                description="S3 server access logs",
                table_type="EXTERNAL_TABLE",
                parameters={"EXTERNAL": "TRUE"},
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{access_log_bucket.bucket_name}/s3-access-logs/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.serde2.RegexSerDe",
                        parameters={"input.regex": s3_log_regex},
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="bucket_owner", type="string"),
                        glue.CfnTable.ColumnProperty(name="bucket_name", type="string"),
                        glue.CfnTable.ColumnProperty(name="request_datetime", type="string"),
                        glue.CfnTable.ColumnProperty(name="remote_ip", type="string"),
                        glue.CfnTable.ColumnProperty(name="requester", type="string"),
                        glue.CfnTable.ColumnProperty(name="request_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="operation", type="string"),
                        glue.CfnTable.ColumnProperty(name="key", type="string"),
                        glue.CfnTable.ColumnProperty(name="request_uri", type="string"),
                        glue.CfnTable.ColumnProperty(name="http_status", type="string"),
                        glue.CfnTable.ColumnProperty(name="error_code", type="string"),
                        glue.CfnTable.ColumnProperty(name="bytes_sent", type="string"),
                        glue.CfnTable.ColumnProperty(name="object_size", type="string"),
                        glue.CfnTable.ColumnProperty(name="total_time", type="string"),
                        glue.CfnTable.ColumnProperty(name="turn_around_time", type="string"),
                        glue.CfnTable.ColumnProperty(name="referrer", type="string"),
                        glue.CfnTable.ColumnProperty(name="user_agent", type="string"),
                        glue.CfnTable.ColumnProperty(name="version_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="host_id", type="string"),
                        glue.CfnTable.ColumnProperty(name="signature_version", type="string"),
                        glue.CfnTable.ColumnProperty(name="cipher_suite", type="string"),
                        glue.CfnTable.ColumnProperty(name="authentication_type", type="string"),
                        glue.CfnTable.ColumnProperty(name="host_header", type="string"),
                        glue.CfnTable.ColumnProperty(name="tls_version", type="string"),
                        glue.CfnTable.ColumnProperty(name="access_point_arn", type="string"),
                        glue.CfnTable.ColumnProperty(name="acl_required", type="string"),
                    ],
                ),
            ),
        )
        s3_table.add_dependency(glue_db)

        # ── Athena WorkGroup ─────────────────────────────────────────────
        # Query results stored in the access log bucket under athena-results/
        # encrypted with this stack's CMK. The bucket itself uses SSE-S3
        # because S3/CloudFront log delivery cannot write to a KMS-encrypted
        # bucket, but Athena PutObject calls can override the bucket default
        # on a per-object basis to use SSE-KMS for the query results.
        workgroup_name = f"{self.node.id}-access-logs"
        workgroup = athena.CfnWorkGroup(
            self,
            "AccessLogsWorkGroup",
            name=workgroup_name,
            state="ENABLED",
            # Once any saved query has been *run*, the workgroup holds query-
            # execution history, and Athena refuses to delete a non-empty
            # workgroup — so `cdk destroy` fails with "WorkGroup ... is not
            # empty" (the named queries this stack ships exist to be run, so
            # this is the normal teardown path, not an edge case — hit on a
            # live teardown). recursive_delete_option lets CloudFormation drop
            # the workgroup's query history with it. The query *results* in
            # s3://.../athena-results/ are emptied separately by the
            # auto-delete-objects custom resource on the access-log bucket.
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{access_log_bucket.bucket_name}/athena-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS",
                        kms_key=encryption_key.key_arn,
                    ),
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                # Hard ceiling on bytes scanned per query — Athena cancels any query
                # that would exceed it, capping the per-query cost (Athena bills
                # $5/TB scanned) against a forgotten WHERE clause or an accidental
                # full-table scan. 1 GiB is generous for these access-log tables at
                # sample-app volume and the 7-day log retention; raise it in a fork
                # with larger log volumes or wider date-range queries. AWS enforces a
                # 10 MB floor on this field.
                bytes_scanned_cutoff_per_query=1024 * 1024 * 1024,
            ),
        )

        # ── Athena Named Queries — CloudFront ────────────────────────────
        # Each named query must wait for the workgroup to exist.
        nq_cf_top_uris = athena.CfnNamedQuery(
            self,
            "CfTopRequestedUris",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Top Requested URIs",
            description="Most frequently requested URIs with error counts",
            query_string="""\
SELECT cs_uri_stem, cs_method,
       COUNT(*) as request_count,
       COUNT(CASE WHEN sc_status LIKE '4%' OR sc_status LIKE '5%' THEN 1 END) as errors
FROM cloudfront_logs
GROUP BY cs_uri_stem, cs_method
ORDER BY request_count DESC
LIMIT 25""",
        )
        nq_cf_top_uris.add_dependency(workgroup)
        nq_cf_errors = athena.CfnNamedQuery(
            self,
            "CfErrorResponses",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Error Responses",
            description="Recent 4xx/5xx error responses with client and edge details",
            query_string="""\
SELECT log_date, log_time, c_ip, cs_method, cs_uri_stem, sc_status,
       x_edge_result_type, x_edge_detailed_result_type
FROM cloudfront_logs
WHERE sc_status LIKE '4%' OR sc_status LIKE '5%'
ORDER BY log_date DESC, log_time DESC
LIMIT 50""",
        )
        nq_cf_errors.add_dependency(workgroup)
        nq_cf_top_ips = athena.CfnNamedQuery(
            self,
            "CfTopClientIps",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Top Client IPs",
            description="Highest-traffic client IPs with error counts",
            query_string="""\
SELECT c_ip, COUNT(*) as request_count,
       COUNT(CASE WHEN sc_status LIKE '4%' OR sc_status LIKE '5%' THEN 1 END) as errors
FROM cloudfront_logs
GROUP BY c_ip
ORDER BY request_count DESC
LIMIT 25""",
        )
        nq_cf_top_ips.add_dependency(workgroup)
        nq_cf_bandwidth = athena.CfnNamedQuery(
            self,
            "CfBandwidthByEdge",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Bandwidth by Edge Location",
            description="Total bytes transferred per edge location",
            query_string="""\
SELECT x_edge_location, COUNT(*) as requests,
       SUM(CAST(sc_bytes AS bigint)) as bytes_out,
       SUM(CAST(cs_bytes AS bigint)) as bytes_in
FROM cloudfront_logs
GROUP BY x_edge_location
ORDER BY bytes_out DESC
LIMIT 25""",
        )
        nq_cf_bandwidth.add_dependency(workgroup)
        nq_cf_cache = athena.CfnNamedQuery(
            self,
            "CfCacheHitRatio",
            database=db_name,
            work_group=workgroup_name,
            name="CloudFront - Cache Hit Ratio",
            description="Request counts and percentages by edge result type (Hit/Miss/Error)",
            query_string="""\
SELECT x_edge_result_type, COUNT(*) as request_count,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) as pct
FROM cloudfront_logs
GROUP BY x_edge_result_type
ORDER BY request_count DESC""",
        )
        nq_cf_cache.add_dependency(workgroup)

        # ── Athena Named Queries — S3 ────────────────────────────────────
        nq_s3_ops = athena.CfnNamedQuery(
            self,
            "S3TopOperations",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Top Operations",
            description="Most common S3 operations with error counts",
            query_string="""\
SELECT operation, COUNT(*) as op_count,
       COUNT(CASE WHEN http_status NOT IN ('200','204','206','304') THEN 1 END) as errors
FROM s3_access_logs
GROUP BY operation
ORDER BY op_count DESC
LIMIT 25""",
        )
        nq_s3_ops.add_dependency(workgroup)
        nq_s3_errors = athena.CfnNamedQuery(
            self,
            "S3ErrorRequests",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Error Requests",
            description="Recent failed S3 requests with error details",
            query_string="""\
SELECT request_datetime, remote_ip, requester, operation, key,
       request_uri, http_status, error_code
FROM s3_access_logs
WHERE http_status NOT IN ('200', '204', '206', '304', '-')
ORDER BY request_datetime DESC
LIMIT 50""",
        )
        nq_s3_errors.add_dependency(workgroup)
        nq_s3_requesters = athena.CfnNamedQuery(
            self,
            "S3TopRequesters",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Top Requesters",
            description="Highest-traffic S3 requesters with error counts",
            query_string="""\
SELECT remote_ip, requester, COUNT(*) as request_count,
       COUNT(CASE WHEN http_status NOT IN ('200','204','206','304') THEN 1 END) as errors
FROM s3_access_logs
GROUP BY remote_ip, requester
ORDER BY request_count DESC
LIMIT 25""",
        )
        nq_s3_requesters.add_dependency(workgroup)
        nq_s3_slow = athena.CfnNamedQuery(
            self,
            "S3SlowRequests",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Slow Requests",
            description="Highest-latency S3 requests by total_time (ms)",
            query_string="""\
SELECT request_datetime, remote_ip, operation, key, http_status,
       total_time, turn_around_time, bytes_sent
FROM s3_access_logs
WHERE total_time != '-'
ORDER BY CAST(total_time AS integer) DESC
LIMIT 50""",
        )
        nq_s3_slow.add_dependency(workgroup)
        nq_s3_access_denied = athena.CfnNamedQuery(
            self,
            "S3AccessDenied",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Access Denied (403)",
            description="Recent 403 AccessDenied responses with requester and operation details",
            query_string="""\
SELECT request_datetime, remote_ip, requester, operation, key,
       request_uri, error_code
FROM s3_access_logs
WHERE http_status = '403'
ORDER BY request_datetime DESC
LIMIT 50""",
        )
        nq_s3_access_denied.add_dependency(workgroup)
        nq_s3_object_reads = athena.CfnNamedQuery(
            self,
            "S3ObjectReads",
            database=db_name,
            work_group=workgroup_name,
            name="S3 - Object Read Audit",
            description="Who read which object (GET.OBJECT operations) with status and bytes",
            query_string="""\
SELECT request_datetime, remote_ip, requester, key,
       http_status, bytes_sent, user_agent
FROM s3_access_logs
WHERE operation LIKE '%GET.OBJECT%'
ORDER BY request_datetime DESC
LIMIT 100""",
        )
        nq_s3_object_reads.add_dependency(workgroup)

        # ── WAF logs (CloudFront + regional WebACLs) ─────────────────────────
        # WAF delivers to S3 (see create_waf_logs_bucket); these Glue tables use
        # partition projection over WAF's date-partitioned log layout so Athena
        # needs no ALTER TABLE ADD PARTITION as new logs arrive. The CloudFront
        # WAF logs live in us-east-1; when the deployment region differs, Athena
        # queries that table cross-region (a no-op in the default us-east-1 deploy).
        self._create_waf_glue_table(
            table_id="WafCloudFrontLogsTable",
            table_name="waf_cloudfront_logs",
            db_name=db_name,
            location=self._cf_waf_logs_location,
            glue_db=glue_db,
        )
        self._create_waf_glue_table(
            table_id="WafRegionalLogsTable",
            table_name="waf_regional_logs",
            db_name=db_name,
            location=self._regional_waf_logs_location,
            glue_db=glue_db,
        )
        self._create_waf_named_queries(
            db_name=db_name,
            workgroup_name=workgroup_name,
            workgroup=workgroup,
            table_name="waf_cloudfront_logs",
            id_prefix="WafCf",
            display_prefix="WAF CloudFront",
        )
        self._create_waf_named_queries(
            db_name=db_name,
            workgroup_name=workgroup_name,
            workgroup=workgroup,
            table_name="waf_regional_logs",
            id_prefix="WafApi",
            display_prefix="WAF Regional",
        )

        # ── Outputs ──────────────────────────────────────────────────────
        CfnOutput(
            self,
            "GlueDatabaseName",
            description="Glue catalog database for CloudFront, S3 access, and WAF log analytics",
            value=db_name,
        )
        CfnOutput(
            self,
            "AthenaWorkGroupName",
            description="Athena workgroup for querying access logs",
            value=workgroup_name,
        )

    def _create_waf_glue_table(
        self,
        *,
        table_id: str,
        table_name: str,
        db_name: str,
        location: str,
        glue_db: glue.CfnDatabase,
    ) -> None:
        """Create a partition-projected Glue table over a WAF S3 log location.

        ``location`` is the ``s3://…/{web-acl-name}/`` prefix (computed by the
        Stage). Partition projection on a single ``log_time`` partition means
        Athena resolves partitions in-memory — no crawler, no ``ALTER TABLE ADD
        PARTITION``. Schema follows AWS's documented WAF Athena DDL; the openx
        JSON SerDe maps WAF's camelCase keys to the lowercase columns
        case-insensitively.

        **Projection granularity is DAY, range NOW-90DAYS (don't widen either
        casually).** WAF's key layout is ``…/{acl}/yyyy/MM/dd/HH/mm/``, but a
        day-level partition prefix still matches the deeper hour/minute keys —
        S3 listing is prefix-based — and AWS's documented WAF projection DDL
        partitions by day for exactly this reason. Athena issues an S3 LIST per
        projected partition a query's pruning leaves in scope, so granularity x
        range is the real cost/latency knob: an earlier minute-granularity
        config anchored at a fixed 2025 date projected ~800k partitions, and a
        single unfiltered named query ran for 6+ minutes issuing an S3 LIST per
        partition (found live). Day x the bucket's own 90-day lifecycle = at
        most ~90 partitions, so even an unfiltered query stays fast — and the
        relative NOW-90DAYS start never projects dates the lifecycle has
        already expired. The named queries additionally filter on log_time
        (see _WAF_NAMED_QUERIES) in the projection's exact date format, which
        is what Athena requires for pruning.
        """
        table = glue.CfnTable(
            self,
            table_id,
            catalog_id=self.account,
            database_name=db_name,
            table_input=glue.CfnTable.TableInputProperty(
                name=table_name,
                description="AWS WAF access logs (partition-projected)",
                table_type="EXTERNAL_TABLE",
                partition_keys=[glue.CfnTable.ColumnProperty(name="log_time", type="string")],
                parameters={
                    "EXTERNAL": "TRUE",
                    "projection.enabled": "true",
                    "projection.log_time.type": "date",
                    "projection.log_time.format": "yyyy/MM/dd",
                    "projection.log_time.interval": "1",
                    "projection.log_time.interval.unit": "DAYS",
                    "projection.log_time.range": "NOW-90DAYS,NOW",
                    "storage.location.template": f"{location}${{log_time}}",
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=location,
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.openx.data.jsonserde.JsonSerDe",
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name=name, type=hive_type) for name, hive_type in _WAF_LOG_COLUMNS
                    ],
                ),
            ),
        )
        table.add_dependency(glue_db)

    def _create_waf_named_queries(
        self,
        *,
        db_name: str,
        workgroup_name: str,
        workgroup: athena.CfnWorkGroup,
        table_name: str,
        id_prefix: str,
        display_prefix: str,
    ) -> None:
        """Create the standard WAF threat-triage Athena named queries for one WAF table.

        Mirrors (and extends) the CloudWatch Logs Insights saved queries the WAF→S3
        move retired: blocked requests, top blocked client IPs, top terminating
        rules, and a country breakdown. Parameterized by table so the CloudFront and
        regional WAF tables share one definition.

        The SQL templates are static module constants with a ``__WAF_TABLE__``
        sentinel filled in by ``str.replace`` (not an f-string) — the table names
        are hardcoded literals, but a static template + replace keeps it free of
        the SQL-construction lint heuristic and obviously injection-free.
        """
        for suffix, name_suffix, description, query_template in _WAF_NAMED_QUERIES:
            nq = athena.CfnNamedQuery(
                self,
                f"{id_prefix}{suffix}",
                database=db_name,
                work_group=workgroup_name,
                name=f"{display_prefix} - {name_suffix}",
                description=description,
                query_string=query_template.replace("__WAF_TABLE__", table_name),
            )
            nq.add_dependency(workgroup)
