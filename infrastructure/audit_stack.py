"""AuditStack — the audit-trail data store (CloudTrail + its bucket + CMK).

Holds the stateful, compliance-relevant audit data — the CloudTrail object-level
S3 data-event trail, the S3 bucket its log files land in, and a dedicated CMK —
separate from the stateless frontend that *produces* the events. This mirrors the
data-stack pattern: the *trail + bucket + key* is the stateful unit and lives
here; the buckets it merely **audits** (the frontend asset + access-log buckets)
stay in the frontend stack and are referenced one-way (this stack depends on the
frontend; the frontend never depends on this one).

**Why the trail lives with its bucket, not with the producers.** A CloudTrail
trail and its log bucket are inseparable — the bucket policy references the
trail's ARN. Splitting them across stacks creates a dependency cycle with the
frontend. Keeping the pair here, auditing the frontend buckets via a one-way
import, is the only cycle-free boundary that doesn't require pinning bucket
names (which would forfeit replacement-safety — see CLAUDE.md).

**Dedicated CMK.** The trail's log files are encrypted with *this* stack's key,
not the frontend's — so retaining audit logs in production retains the audit key,
not the frontend key (which also encrypts the destroy-friendly asset bucket).

**retain_data.** Default ``False`` keeps the bucket and CMK ``DESTROY`` (clean
teardown). ``True`` flips both to ``RETAIN`` and turns on stack termination
protection — the production posture for long-lived audit data. (A 90-day S3
lifecycle bounds storage either way; a compliance fork extends it and adds AWS
Backup / Object Lock — see TODO.md.)
"""

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infrastructure.nag_utils import (
    acknowledge_rules,
    apply_compliance_aspects,
    create_auto_delete_objects_log_group,
    create_sse_s3_log_bucket,
    grant_cloudtrail_service_to_key,
    grant_logs_service_to_key,
)


class AuditStack(Stack):
    """CloudTrail S3 data-event trail + its log bucket + a dedicated CMK.

    Exposes nothing for cross-stack consumption — it is a leaf that *depends on*
    the frontend (it audits the frontend's buckets) and is depended on by no one.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        audited_buckets: list[s3.IBucket],
        retain_data: bool = False,
        **kwargs: Any,
    ) -> None:
        """Build the audit stack.

        Args:
            scope: The CDK construct scope.
            construct_id: The unique identifier for this stack.
            audited_buckets: Buckets whose object-level S3 data events the trail
                records (the frontend asset + access-log buckets). Passed in
                cross-stack so the dependency is one-way (audit -> frontend).
            retain_data: Production switch. ``False`` (default) keeps the bucket
                and CMK ``DESTROY`` with clean teardown; ``True`` flips both to
                ``RETAIN`` and enables stack termination protection.
            **kwargs: Additional keyword arguments passed to the parent Stack.
        """
        super().__init__(scope, construct_id, termination_protection=retain_data, **kwargs)

        apply_compliance_aspects(self)

        removal_policy = RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY
        # A retained bucket can't auto-empty on destroy (and shouldn't); a
        # destroy-friendly one must, or `cdk destroy` fails on a non-empty bucket.
        auto_delete = not retain_data

        # Pin the trail name so its ARN is known *before* the trail resource is
        # created — needed both to break the dependency cycle that would
        # otherwise form between the trail (which CDK auto-wires to depend on
        # its bucket policy) and the confused-deputy Deny statements on the
        # bucket policy (which reference the trail ARN), and to let the CMK's
        # CloudTrail service grant below scope aws:SourceArn to this exact
        # trail instead of a trail/* wildcard. Same constructed-ARN technique
        # as the RUM monitor in the frontend stack. Pinned-name caveat: a
        # future replacement-forcing property change collides with the
        # not-yet-deleted old trail (CFN replacement is create-before-delete),
        # so such a change must also change the name in the same commit — see
        # the AppConfig profile note in backend_app.py.
        trail_name = f"{self.stack_name}-S3DataEventsTrail"
        trail_arn = f"arn:{self.partition}:cloudtrail:{self.region}:{self.account}:trail/{trail_name}"

        # ── Dedicated audit CMK ──────────────────────────────────────────────
        # Encrypts the CloudTrail log files (per-object SSE-KMS) and the trail's
        # CloudWatch log group. Kept here with the audit data so retention is
        # meaningful — see the module docstring.
        self.encryption_key = kms.Key(
            self,
            "AuditEncryptionKey",
            description=f"KMS key for {self.stack_name} CloudTrail audit logs",
            enable_key_rotation=True,
            rotation_period=Duration.days(90),
            removal_policy=removal_policy,
        )
        grant_logs_service_to_key(
            self.encryption_key,
            region=self.region,
            account=self.account,
            partition=self.partition,
        )
        grant_cloudtrail_service_to_key(
            self.encryption_key,
            account=self.account,
            trail_arn=trail_arn,
        )

        # ── CloudTrail log bucket ────────────────────────────────────────────
        # SSE-S3 at rest (CloudTrail delivery can't target a KMS-CMK *bucket*),
        # with the trail writing each object SSE-KMS under the audit CMK. 90-day
        # lifecycle bounds storage; a compliance fork extends it (and adds AWS
        # Backup / Object Lock — see TODO.md). Built via the shared log-sink helper.
        cloudtrail_log_bucket = create_sse_s3_log_bucket(
            self,
            "CloudTrailLogsBucket",
            suppression_reason=(
                "CloudTrail log bucket — SSE-S3 (CloudTrail delivery doesn't support KMS-CMK "
                "destination buckets; trail log files are per-object SSE-KMS), self-logging would "
                "create circular audit trails, no versioning/replication for an append-only, "
                "integrity-validated log sink"
            ),
            expiration_days=90,
            removal_policy=removal_policy,
            auto_delete=auto_delete,
        )

        cloudtrail_log_group = logs.LogGroup(
            self,
            "S3DataEventsTrailLogs",
            encryption_key=self.encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Confused-deputy guard on the CloudTrail bucket policy. CDK's Trail L2
        # grants cloudtrail.amazonaws.com s3:GetBucketAcl + s3:PutObject without
        # an aws:SourceArn condition, so any trail in any account that discovered
        # this bucket name could write to it. Two explicit Deny statements (one
        # per condition key) close the gap on either mismatch — kept separate so
        # IAM ORs them (a single StringNotEquals block would AND the keys, letting
        # a same-account trail with a different name slip past).
        ct_principals = [iam.ServicePrincipal("cloudtrail.amazonaws.com")]
        ct_resources = [cloudtrail_log_bucket.bucket_arn, cloudtrail_log_bucket.arn_for_objects("*")]
        cloudtrail_log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:GetBucketAcl", "s3:PutObject"],
                principals=ct_principals,
                resources=ct_resources,
                conditions={"StringNotEquals": {"aws:SourceArn": trail_arn}},
            )
        )
        cloudtrail_log_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:GetBucketAcl", "s3:PutObject"],
                principals=ct_principals,
                resources=ct_resources,
                conditions={"StringNotEquals": {"aws:SourceAccount": self.account}},
            )
        )

        s3_data_events_trail = cloudtrail.Trail(
            self,
            "S3DataEventsTrail",
            trail_name=trail_name,
            bucket=cloudtrail_log_bucket,
            send_to_cloud_watch_logs=True,
            cloud_watch_log_group=cloudtrail_log_group,
            encryption_key=self.encryption_key,
            enable_file_validation=True,
            include_global_service_events=False,
            is_multi_region_trail=False,
        )
        # include_management_events=False keeps this trail scoped to object-level
        # S3 data events. The CDK default (True) would record EVERY regional
        # management event — a billed second copy in any account that already has
        # a management trail, on every fork.
        s3_data_events_trail.add_s3_event_selector(
            [cloudtrail.S3EventSelector(bucket=b) for b in audited_buckets],
            include_management_events=False,
        )
        inline_policy_reason = "CDK generates the trail's LogsRole default policy inline — not directly configurable"
        acknowledge_rules(
            s3_data_events_trail,
            [
                {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": inline_policy_reason},
                {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": inline_policy_reason},
            ],
        )

        # The destroy-friendly bucket uses auto_delete_objects, which synthesizes
        # the S3 auto-delete singleton Lambda; the helper gives it an explicit CMK
        # log group and suppresses its CDK-managed-singleton nag findings. (No-op
        # when retain_data=True, since auto_delete is then off and no provider exists.)
        create_auto_delete_objects_log_group(self, self.encryption_key)

        CfnOutput(
            self,
            "CloudTrailLogsBucketName",
            description="S3 bucket storing the CloudTrail object-level data-event logs",
            value=cloudtrail_log_bucket.bucket_name,
        )
