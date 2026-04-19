from typing import Any, cast

from aws_cdk import (
    CfnOutput,
    CustomResourceProvider,
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
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3deploy,
)
from cdk_nag import NagSuppressions
from constructs import Construct

from hello_world.nag_utils import CDK_LAMBDA_SUPPRESSIONS, apply_compliance_aspects, suppress_cdk_singletons


class HelloWorldFrontendStack(Stack):
    """CDK stack for the Hello World frontend.

    Provisions a private S3 bucket for static assets and a CloudFront
    distribution with OAC, HTTPS-only enforcement, and security response
    headers. WAF protection is provided by a WebACL ARN passed in from
    HelloWorldWafStack, which is always deployed in us-east-1.

    This stack can be deployed to any region. When the target region differs
    from us-east-1, CDK bridges the WAF ARN cross-region automatically via
    SSM Parameter Store (enabled by cross_region_references=True in app.py).
    """

    def __init__(self, scope: Construct, construct_id: str, api_url: str, waf_acl_arn: str, **kwargs: Any) -> None:
        """Provision all frontend AWS resources.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            api_url: The backend API Gateway URL, injected into config.json at deploy time.
            waf_acl_arn: ARN of the WAF WebACL from HelloWorldWafStack (always in us-east-1).
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, **kwargs)

        apply_compliance_aspects(self)

        # ── KMS key ──────────────────────────────────────────────────────────
        # Used to encrypt the frontend S3 bucket and CloudWatch log group.
        # CloudWatch Logs requires the Logs service principal in the key policy.
        frontend_encryption_key = kms.Key(
            self,
            "FrontendEncryptionKey",
            description=f"KMS key for {self.stack_name} S3 bucket and log groups",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        frontend_encryption_key.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                resources=["*"],
            )
        )

        # ── S3 access logging bucket ─────────────────────────────────────────
        # Receives both S3 server access logs and CloudFront standard access
        # logs. Must use SSE-S3 (not SSE-KMS) because neither the S3 log
        # delivery service nor CloudFront standard logging support KMS-encrypted
        # target buckets. This bucket itself does not need access logging (that
        # would be circular), versioning, or replication.
        access_log_bucket = s3.Bucket(
            self,
            "FrontendAccessLogBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # CloudFront standard logging requires ACL-based delivery — the bucket owner
            # needs FULL_CONTROL on delivered log objects. BUCKET_OWNER_PREFERRED keeps
            # Object Ownership set so ACLs remain usable for CloudFront log delivery.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        NagSuppressions.add_resource_suppressions(
            access_log_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": "This IS the access log bucket — logging to itself would be circular",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketLoggingEnabled",
                    "reason": "This IS the access log bucket — logging to itself would be circular",
                },
                {
                    "id": "NIST.800.53.R5-S3DefaultEncryptionKMS",
                    "reason": "S3 log delivery service does not support KMS-encrypted target buckets; SSE-S3 is used instead",
                },
                {
                    "id": "HIPAA.Security-S3DefaultEncryptionKMS",
                    "reason": "S3 log delivery service does not support KMS-encrypted target buckets; SSE-S3 is used instead",
                },
                {
                    "id": "PCI.DSS.321-S3DefaultEncryptionKMS",
                    "reason": "S3 log delivery service does not support KMS-encrypted target buckets; SSE-S3 is used instead",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketVersioningEnabled",
                    "reason": "Versioning not needed for log bucket — logs are append-only and transient",
                },
                {
                    "id": "HIPAA.Security-S3BucketVersioningEnabled",
                    "reason": "Versioning not needed for log bucket — logs are append-only and transient",
                },
                {
                    "id": "PCI.DSS.321-S3BucketVersioningEnabled",
                    "reason": "Versioning not needed for log bucket — logs are append-only and transient",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": "Replication not needed for log bucket in sample app",
                },
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": "Replication not needed for log bucket in sample app",
                },
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": "Replication not needed for log bucket in sample app",
                },
            ],
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

        # ── CloudFront distribution ──────────────────────────────────────────
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
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

        # ── Deploy frontend assets ───────────────────────────────────────────
        # Uploads frontend/ to S3 and generates config.json with the API URL
        # injected at deploy time. Triggers a CloudFront invalidation so the
        # new assets are served immediately without waiting for cache expiry.
        s3deploy.BucketDeployment(
            self,
            "DeployFrontend",
            sources=[
                s3deploy.Source.asset("frontend"),
                s3deploy.Source.json_data("config.json", {"apiUrl": api_url}),
            ],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            log_retention=logs.RetentionDays.ONE_WEEK,
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

        # ── Explicit log group for the CDK auto-delete Lambda ────────────────
        # CDK creates a singleton Lambda to empty the bucket before deletion.
        # It is a CloudFormation-managed Lambda, but its log group is created
        # implicitly by Lambda and has no retention — it would dangle after
        # cdk destroy. We find the provider via the construct tree and create
        # an explicit log group so CloudFormation owns and deletes it.
        auto_delete_provider = cast(
            CustomResourceProvider,
            self.node.try_find_child("Custom::S3AutoDeleteObjectsCustomResourceProvider"),
        )
        if auto_delete_provider is not None:
            # service_token is the Lambda ARN; index 6 of the colon-split is the function name
            fn_name = Fn.select(6, Fn.split(":", auto_delete_provider.service_token))
            logs.LogGroup(
                self,
                "AutoDeleteObjectsLogGroup",
                log_group_name=Fn.join("", ["/aws/lambda/", fn_name]),
                encryption_key=frontend_encryption_key,
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )

        self._create_athena_glue_resources(access_log_bucket)

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
        #   LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a — log retention singleton
        suppress_cdk_singletons(
            self,
            (
                "Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C",
                "LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a",
            ),
        )

        # minimizePolicies restructures the BucketDeployment handler's inline
        # policy into a separate resource under DeployFrontend/CustomResourceHandler.
        deploy_frontend = self.node.try_find_child("DeployFrontend")
        if deploy_frontend is not None:
            suppress_cdk_singletons(deploy_frontend, ("CustomResourceHandler",))
        if auto_delete_provider is not None:
            NagSuppressions.add_resource_suppressions(
                auto_delete_provider,
                CDK_LAMBDA_SUPPRESSIONS,
                apply_to_children=True,
            )

        # ── Stack-level cdk-nag suppressions (genuinely stack-wide) ─────────────
        NagSuppressions.add_stack_suppressions(
            self,
            [
                # ── AWS Solutions ────────────────────────────────────────────────
                {"id": "AwsSolutions-CFR1", "reason": "Geo restriction not required for sample app"},
                {
                    "id": "AwsSolutions-CFR4",
                    "reason": "Using default CloudFront certificate — no custom domain for sample app",
                },
                # ── NIST 800-53 R5 ──────────────────────────────────────────────
                {
                    "id": "NIST.800.53.R5-S3BucketReplicationEnabled",
                    "reason": "S3 replication not needed for sample app — static assets are redeployable",
                },
                {
                    "id": "NIST.800.53.R5-S3BucketVersioningEnabled",
                    "reason": "S3 versioning not needed for sample app — static assets are redeployable via cdk deploy",
                },
                # ── HIPAA Security ───────────────────────────────────────────────
                {
                    "id": "HIPAA.Security-S3BucketReplicationEnabled",
                    "reason": "S3 replication not needed for sample app — static assets are redeployable",
                },
                {
                    "id": "HIPAA.Security-S3BucketVersioningEnabled",
                    "reason": "S3 versioning not needed for sample app — static assets are redeployable via cdk deploy",
                },
                # ── PCI DSS 3.2.1 ────────────────────────────────────────────────
                {
                    "id": "PCI.DSS.321-S3BucketReplicationEnabled",
                    "reason": "S3 replication not needed for sample app — static assets are redeployable",
                },
                {
                    "id": "PCI.DSS.321-S3BucketVersioningEnabled",
                    "reason": "S3 versioning not needed for sample app — static assets are redeployable via cdk deploy",
                },
            ],
        )

    def _create_athena_glue_resources(self, access_log_bucket: s3.Bucket) -> None:
        """Create Glue catalog tables and Athena workgroup for CloudFront/S3 access log analytics."""
        # ── Glue Database ────────────────────────────────────────────────
        # Glue database names: lowercase, alphanumeric + underscores only.
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
        # Query results stored in the access log bucket under athena-results/.
        # SSE-S3 encryption matches the bucket default (SSE-KMS not supported
        # by S3/CloudFront log delivery to this bucket).
        workgroup_name = f"{self.node.id}-access-logs"
        workgroup = athena.CfnWorkGroup(
            self,
            "AccessLogsWorkGroup",
            name=workgroup_name,
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{access_log_bucket.bucket_name}/athena-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_S3",
                    ),
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
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

        # ── Outputs ──────────────────────────────────────────────────────
        CfnOutput(
            self,
            "GlueDatabaseName",
            description="Glue catalog database for CloudFront and S3 access log analytics",
            value=db_name,
        )
        CfnOutput(
            self,
            "AthenaWorkGroupName",
            description="Athena workgroup for querying access logs",
            value=workgroup_name,
        )
