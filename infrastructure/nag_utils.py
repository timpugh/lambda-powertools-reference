"""Shared cdk-nag helpers.

cdk-nag v3 rule packs are ``IPolicyValidationPlugin``s evaluated over the
synthesized assembly, not per-stack ``IAspect``s — so pack attachment and
suppression plumbing both live here in v3 form:

``attach_nag_packs`` registers the five rule packs as policy-validation
plugins on an App (or test App) root — once per tree, not once per stack.
``apply_compliance_aspects`` remains per-stack but now carries only this
project's own ``TemplateConventionChecks`` validation Aspect (log-group
retention + explicit removal policy on stateful resources — see
``infrastructure.validation_aspects``). NIST 800-53 R4 is intentionally
omitted — R5 supersedes it and running both would duplicate findings on
overlapping controls.

``acknowledge_rules`` is the project-wide adapter from this repo's
suppression data shape (``{id, reason, applies_to?}`` dicts, unchanged from
the v2 era so every call site's *data* stays diff-stable) onto v3's
``Validations.of(construct).acknowledge()``: each ``applies_to`` entry
expands to the granular ``Rule[Finding]`` id v3 requires, and entries
without ``applies_to`` acknowledge the bare rule id. Acknowledgments apply
to the construct's whole subtree (v3 walks the ancestor tree when matching),
which subsumes v2's ``apply_to_children=True``.

``CDK_LAMBDA_SUPPRESSIONS`` is the canonical suppression list for CDK-managed
singleton Lambdas (AwsCustomResource provider, BucketDeployment, S3AutoDeleteObjects).
Their runtime, memory, tracing, DLQ, VPC, and IAM policies are all managed by
CDK and cannot be configured by the caller. Import it and pass it to
``acknowledge_rules``, or use the ``suppress_cdk_singletons`` helper.
Absolute-path suppression is intentionally avoided throughout this project:
the singletons are resolved via ``node.try_find_child`` so suppressions
keep working when the stacks are nested under a ``cdk.Stage`` (a path string
would break on the added Stage prefix).
"""

import hashlib
from collections.abc import Iterable, Mapping
from typing import cast

from aws_cdk import (
    Acknowledgment,
    Annotations,
    Aspects,
    CfnOutput,
    CustomResourceProviderBase,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Validations,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_wafv2 as wafv2
from cdk_nag import (
    AwsSolutionsChecks,
    HIPAASecurityChecks,
    NIST80053R5Checks,
    PCIDSS321Checks,
    ServerlessChecks,
    WriteNagSuppressionsToCloudFormationAspect,
)
from constructs import Construct, IConstruct

from infrastructure.validation_aspects import TemplateConventionChecks

# CDK-managed singleton Lambda construct IDs. Derived from CDK's own source
# hashes; stable for years and unaffected by rescoping stacks under a cdk.Stage.
# Shared here so the stacks that suppress these singletons AND attach async DLQs
# to them via try_find_child provably reference the same construct ID rather
# than re-typing the literal at each site.
AWS_CUSTOM_RESOURCE_PROVIDER_ID = "AWS679f53fac002430cb0da5b7982bd2287"
BUCKET_DEPLOYMENT_PROVIDER_ID = "Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C"


def attach_nag_packs(scope: IConstruct) -> None:
    """Register the five cdk-nag v3 rule packs as policy-validation plugins.

    Call once on the App root (``app.py`` does; the nag-gating test fixtures
    do the same on their test Apps). cdk-nag v3 packs participate in CDK's
    policy validation framework: they evaluate the *synthesized assembly*
    during ``app.synth()`` and fail the synthesis on unacknowledged findings —
    they are no longer ``IAspect``s added per stack.

    The v2-compatible ``cdk_nag`` Metadata audit trail is NOT enabled here:
    the packs' ``write_suppressions_to_cloud_formation`` flag registers its
    aspect at this (App) scope, and CDK aspects do not cross ``cdk.Stage``
    boundaries — Stage-nested stacks would silently lose the metadata
    (verified empirically against 3.0.1). ``apply_compliance_aspects``
    registers ``WriteNagSuppressionsToCloudFormationAspect`` per stack
    instead, which reaches every stack regardless of nesting.
    """
    Validations.of(scope).add_plugins(
        AwsSolutionsChecks(scope, verbose=True),
        ServerlessChecks(scope, verbose=True),
        NIST80053R5Checks(scope, verbose=True),
        HIPAASecurityChecks(scope, verbose=True),
        PCIDSS321Checks(scope, verbose=True),
    )


def apply_compliance_aspects(stack: Stack) -> None:
    """Attach this project's validation Aspect to ``stack``.

    Carries ``TemplateConventionChecks`` plus cdk-nag's
    ``WriteNagSuppressionsToCloudFormationAspect`` since cdk-nag v3: the five
    rule packs are policy-validation plugins registered once at the App root
    via :func:`attach_nag_packs`, not per-stack Aspects. The project-specific
    invariants (log-group retention, explicit removal policies on stateful
    resources) remain a per-stack Aspect surfacing error annotations, and the
    write-suppressions aspect keeps the v2-style ``cdk_nag`` Metadata audit
    trail (acknowledged rules + reasons) in every synthesized template — it
    must be per stack because aspects do not cross ``cdk.Stage`` boundaries.
    """
    Aspects.of(stack).add(TemplateConventionChecks())
    Aspects.of(stack).add(WriteNagSuppressionsToCloudFormationAspect())


def acknowledge_rules(construct: IConstruct, suppressions: Iterable[Mapping[str, object]]) -> None:
    """Acknowledge cdk-nag findings on ``construct`` (v3 ``Validations`` API).

    The project-wide adapter from this repo's suppression data shape —
    ``{"id": rule, "reason": why, "applies_to": [findings...]?}``, unchanged
    from the v2 era — onto ``Validations.of().acknowledge()``:

    - each ``applies_to`` entry expands to the granular ``Rule[Finding]`` id
      v3 matches individually (e.g. ``AwsSolutions-IAM5[Resource::*]``);
    - entries without ``applies_to`` acknowledge the bare rule id, which
      matches rules that report a single, non-granular finding.

    Acknowledgments cover the construct's whole subtree — v3 walks the
    ancestor tree when checking a finding — so v2's ``apply_to_children``
    distinction no longer exists.

    **Metadata fallback for validator-rejected ids.** CDK's
    ``Validations.acknowledge`` rejects any id containing more than one
    ``::``, but cdk-nag 3.0.1's own IAM4 findings embed managed-policy ARNs
    (``AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/…]``) —
    the packs emit finding ids their acknowledge API refuses to accept. The
    plugin's matching reads the ``aws:cdk:acknowledged-rules`` construct
    metadata that ``acknowledge()`` writes, WITHOUT re-validating (verified
    empirically against 3.0.1), so ids the front door rejects are written to
    that metadata key directly. Drop the fallback once cdk-nag/CDK reconcile
    the id grammar upstream.
    """
    front_door: list[Acknowledgment] = []
    fallback: dict[str, str] = {}
    for suppression in suppressions:
        rule_id = cast(str, suppression["id"])
        reason = cast(str, suppression["reason"])
        applies_to = cast("list[str] | None", suppression.get("applies_to"))
        finding_ids = [f"{rule_id}[{finding}]" for finding in applies_to] if applies_to else [rule_id]
        for finding_id in finding_ids:
            if finding_id.count("::") > 1:
                fallback[finding_id] = reason
            else:
                front_door.append(Acknowledgment(id=finding_id, reason=reason))
    if front_door:
        Validations.of(construct).acknowledge(*front_door)
    if fallback:
        construct.node.add_metadata("aws:cdk:acknowledged-rules", fallback)


def grant_logs_service_to_key(key: kms.Key, *, region: str, account: str, partition: str) -> None:
    """Add the standard CloudWatch Logs service-principal grant to a CMK.

    Three CMKs in this project (backend, frontend, WAF) need the same statement:
    a grant to ``logs.{region}.amazonaws.com`` for symmetric encrypt/decrypt
    operations, conditioned via ``kms:EncryptionContext:aws:logs:arn`` so only
    log groups in this account+region can request key operations. Defining it
    in one place keeps the three call sites in lockstep — pylint's R0801
    duplicate-code check correctly flags any drift between them, and the
    confused-deputy condition is exactly the kind of thing that's harmful to
    forget on one of the three CMKs.
    """
    key.add_to_resource_policy(
        iam.PolicyStatement(
            actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
            principals=[iam.ServicePrincipal(f"logs.{region}.amazonaws.com")],
            resources=["*"],
            conditions={
                "ArnLike": {
                    "kms:EncryptionContext:aws:logs:arn": f"arn:{partition}:logs:{region}:{account}:log-group:*",
                },
            },
        )
    )


def grant_cloudtrail_service_to_key(key: kms.Key, *, account: str, trail_arn: str) -> None:
    """Grant CloudTrail the KMS operations needed to write CMK-encrypted trail log files.

    CloudTrail needs explicit KMS grants on the encryption key to deliver
    SSE-KMS log files. CDK's auto-grants from passing ``encryption_key=`` to
    ``cloudtrail.Trail`` don't always extend to the cloudtrail service
    principal when the key is shared with other services (CloudWatch Logs,
    CloudFront, etc.), so the principal is added explicitly — mirroring the
    logs grant above so all service-principal statements on the
    project's CMKs live in one module and stay in lockstep.

    Confused-deputy guard: scoped to the EXACT trail. The trail's name is
    pinned in ``AuditStack`` (its ARN is constructed before the trail resource
    exists — the same technique as the bucket-policy Deny statements there), so
    ``aws:SourceArn`` can name the one trail allowed to use this key rather
    than a ``trail/*`` wildcard — any *other* trail in this account is denied
    too. CloudTrail sets aws:SourceArn to the trail ARN on every encrypt call;
    ``aws:SourceAccount`` is checked as defense in depth (some older trail
    integrations omit aws:SourceArn).
    """
    key.add_to_resource_policy(
        iam.PolicyStatement(
            actions=["kms:GenerateDataKey*", "kms:DescribeKey"],
            principals=[iam.ServicePrincipal("cloudtrail.amazonaws.com")],
            resources=["*"],
            conditions={
                "StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {"aws:SourceArn": trail_arn},
            },
        )
    )


def grant_cloudwatch_alarms_to_key(key: kms.Key, *, account: str, region: str) -> None:
    """Grant CloudWatch alarms the KMS operations needed to publish to a CMK-encrypted SNS topic.

    When an alarm fires against a topic with SSE enabled, SNS performs the KMS
    data-key operations *as the publishing service principal* — so per the SNS
    key-management docs, ``cloudwatch.amazonaws.com`` needs ``kms:Decrypt`` and
    ``kms:GenerateDataKey*`` in the key policy. Without this grant the alarm
    transitions to ALARM but the notification is silently dropped (the publish
    is denied at KMS, and CloudWatch does not surface the failure anywhere
    actionable) — the worst failure mode for an alerting path.

    Confused-deputy guard: ``kms:ViaService`` pins the grant to KMS calls made
    through SNS in this region (the only path CloudWatch alarm actions use),
    and ``aws:SourceAccount`` restricts to alarms in this account.
    ``aws:SourceArn`` is deliberately omitted: unlike CloudTrail
    above, CloudWatch is not documented to set it on the via-SNS KMS calls, and
    an unmatched required condition would deny the publish — recreating the
    silent-drop failure this grant exists to prevent.

    Verified on a live deployment (2026-06): forcing an alarm into ALARM via
    ``set-alarm-state`` recorded "Successfully executed action <topic-arn>" in
    the alarm history, confirming the publish (including the KMS data-key
    operation) clears this exact condition set. Re-verify the same way when
    changing anything in this statement — a KMS denial appears in alarm
    history as "Failed to execute action", nowhere else.
    """
    key.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AllowCloudWatchAlarmsViaSns",
            actions=["kms:Decrypt", "kms:GenerateDataKey*"],
            principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": account,
                    "kms:ViaService": f"sns.{region}.amazonaws.com",
                },
            },
        )
    )


def build_managed_threat_rules(metric_prefix: str) -> list[wafv2.CfnWebACL.RuleProperty]:
    """Build the four AWS managed rule groups shared by every WebACL in this project.

    Two WebACLs use these: the CLOUDFRONT-scoped ACL in ``WafStack``
    (browser traffic at the edge) and the REGIONAL-scoped ACL on API Gateway in
    ``BackendApp`` (closes the ``execute-api`` CloudFront-bypass window). Both
    need the identical IP-reputation / common / known-bad-inputs / anonymous-IP
    protections, so the list is defined once here — pylint's R0801 duplicate-code
    check would otherwise (correctly) flag two ~60-line copies drifting apart, and
    a managed rule group that's added to one ACL but forgotten on the other is
    exactly the kind of asymmetry this consolidation prevents.

    No rate-based rule is part of this shared set because edge and origin need
    different aggregation, so each ACL carries its own. The CLOUDFRONT ACL
    aggregates by plain ``IP``: it inspects the *viewer* request, so the source
    IP it sees is already the real client's. On the regional ACL guarding the
    origin, *funnelled* traffic arrives from CloudFront edge IPs (plain-IP
    aggregation would pool many users behind a few shared edge IPs) — but
    *direct* ``execute-api`` callers, the bypass path the regional ACL exists
    to cover, arrive from their own IPs without the ``X-Forwarded-For`` header
    CloudFront appends toward the origin. The regional ACL therefore carries an
    IP-aggregated rate rule scoped down to XFF-less requests only — see
    ``BackendApp._attach_regional_waf``.

    Args:
        metric_prefix: Prefix for each rule's CloudWatch metric name (the caller
            passes its stack name so metrics stay unique across deployments).

    Returns:
        The four managed-rule-group ``RuleProperty`` objects at priorities 0-3.
        Callers that add a rate rule place it at priority 4.
    """

    def _managed_group(name: str, priority: int, metric_suffix: str) -> wafv2.CfnWebACL.RuleProperty:
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name=name,
                )
            ),
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{metric_prefix}-{metric_suffix}",
                sampled_requests_enabled=True,
            ),
        )

    return [
        # Blocks IPs with a poor reputation (scanners, botnets, TOR exits)
        _managed_group("AWSManagedRulesAmazonIpReputationList", 0, "IpReputationList"),
        # Core rule set — protects against OWASP Top 10 web exploits
        _managed_group("AWSManagedRulesCommonRuleSet", 1, "CommonRuleSet"),
        # Blocks requests containing known malicious inputs (SQLi, XSS patterns)
        _managed_group("AWSManagedRulesKnownBadInputsRuleSet", 2, "KnownBadInputs"),
        # Blocks requests from anonymizing services (VPN, Tor exits, hosting providers)
        _managed_group("AWSManagedRulesAnonymousIpList", 3, "AnonymousIpList"),
    ]


def attach_async_failure_destination(
    scope: IConstruct,
    singleton_id: str,
    *,
    encryption_key: kms.Key,
    queue_id: str,
) -> sqs.Queue | None:
    """Wire an SQS DLQ to a CDK-managed async singleton Lambda.

    CDK-managed provider Lambdas (the AwsCustomResource provider, the
    BucketDeployment handler) are invoked asynchronously by
    CloudFormation during stack lifecycle events. Without an on_failure
    destination, a provider crash that exhausts Lambda's two automatic
    async retries is silently dropped — the stack rollback still surfaces
    a CFN error, but the *cause* (Python traceback, AWS API error response)
    is gone unless someone catches it in CloudWatch within the retention
    window. SQS as the on_failure destination preserves the failed-event
    envelope (full request payload + responseContext) for post-mortem.

    The queue uses the same CMK as the surrounding stack, with 14-day
    retention (Lambda's max meaningful window — events older than that
    have already aged past most rollback investigations).

    Returns the created queue so callers can attach alarms or outputs;
    returns None if the singleton isn't present under ``scope`` (which
    happens when no AwsCustomResource has been instantiated in this stack).
    """
    singleton = scope.node.try_find_child(singleton_id)
    # IFunction is a JSII protocol that isn't runtime-checkable, so we check
    # the concrete Function class. SingletonFunction is a subclass, so the
    # isinstance check covers both.
    if not isinstance(singleton, _lambda.Function):
        return None

    dlq = sqs.Queue(
        cast(Construct, scope),
        queue_id,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=encryption_key,
        retention_period=Duration.days(14),
        enforce_ssl=True,
        removal_policy=RemovalPolicy.DESTROY,
    )

    # This queue IS the dead-letter destination. cdk-nag flags any SQS queue
    # without a DLQ or a redrive policy, but recursing DLQs into more DLQs
    # makes no sense — when this terminal queue's consumer fails, manual
    # inspection of the queue content is the recovery path, not another DLQ.
    dlq_terminal_reason = (
        "Terminal DLQ: this queue IS the dead-letter destination — recursing into another DLQ has no recovery value"
    )
    acknowledge_rules(
        dlq,
        [
            {"id": "AwsSolutions-SQS3", "reason": dlq_terminal_reason},
            {"id": "Serverless-SQSRedrivePolicy", "reason": dlq_terminal_reason},
        ],
    )

    singleton.configure_async_invoke(on_failure=destinations.SqsDestination(dlq))

    # configure_async_invoke + SqsDestination + KMS-encrypted queue adds
    # kms:GenerateDataKey* and kms:ReEncrypt* wildcards to the singleton's
    # auto-generated default policy so it can encrypt messages to the DLQ.
    # These are granular IAM5 findings that need applies_to scoping rather
    # than the blanket suppression in CDK_LAMBDA_SUPPRESSIONS. Also
    # re-applies the inline-policy suppressions because the DefaultPolicy
    # resource only materialized when configure_async_invoke modified the
    # role above, after the initial suppress_cdk_singletons run.
    kms_wildcard_reason = (
        "KMS wildcards required by configure_async_invoke to encrypt messages to the CMK-encrypted DLQ"
    )
    acknowledge_rules(
        cast(Construct, singleton),
        [
            {
                "id": "AwsSolutions-IAM5",
                "applies_to": ["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"],
                "reason": kms_wildcard_reason,
            },
            {
                "id": "NIST.800.53.R5-IAMNoInlinePolicy",
                "reason": "CDK-generated inline policy on singleton service role",
            },
            {
                "id": "HIPAA.Security-IAMNoInlinePolicy",
                "reason": "CDK-generated inline policy on singleton service role",
            },
            {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
        ],
    )

    # Surface the DLQ URL so operators can find captured provider failures. Emitted
    # here (rather than in each calling stack) so the two call sites don't duplicate
    # the same CfnOutput block — keeping the queue genuinely consumed, not just bound.
    CfnOutput(
        cast(Construct, scope),
        f"{queue_id}Url",
        description=f"SQS DLQ capturing failed async invocations of the {singleton_id} provider Lambda",
        value=dlq.queue_url,
    )

    return dlq


def suppress_cdk_singletons(scope: IConstruct, singleton_ids: Iterable[str]) -> None:
    """Apply ``CDK_LAMBDA_SUPPRESSIONS`` to any CDK-managed singletons present under ``scope``.

    Resolves each ID via ``node.try_find_child`` rather than an absolute path
    string so suppressions survive being nested in a ``cdk.Stage``. Missing IDs
    are tolerated — some singletons only appear when the construct that needs
    them is instantiated.
    """
    for singleton_id in singleton_ids:
        singleton = scope.node.try_find_child(singleton_id)
        if singleton is not None:
            acknowledge_rules(
                cast(Construct, singleton),
                CDK_LAMBDA_SUPPRESSIONS,
            )


def create_auto_delete_objects_log_group(scope: Stack, encryption_key: kms.Key) -> CustomResourceProviderBase | None:
    """Create an explicit CMK log group for the CDK S3 auto-delete-objects singleton.

    ``auto_delete_objects=True`` makes CDK synthesize a singleton Lambda that
    empties a bucket before deletion. That Lambda's log group is created
    implicitly by Lambda on first write — unencrypted and with no retention — so
    it dangles after ``cdk destroy``. This pre-creates an explicit CMK-encrypted,
    retention-bounded log group with the Lambda's exact name so CloudFormation
    owns and deletes it. Shared by every stack that uses ``auto_delete_objects``
    (frontend, audit) so the wiring stays in lockstep — pylint's R0801 would
    otherwise flag the duplicated ~40-line block.

    The provider lookup is type-checked at runtime: if a CDK upgrade swaps the
    provider for an incompatible type, the ``isinstance`` returns None and the
    block is skipped (surfaced as a warning) rather than crashing at synth. We
    match ``CustomResourceProviderBase`` (not the narrower ``CustomResourceProvider``)
    because CDK 2.248's S3 auto-delete singleton synthesizes as the base type.
    Returns the provider (or None) so the caller can attach suppressions to it.
    """
    node = scope.node.try_find_child("Custom::S3AutoDeleteObjectsCustomResourceProvider")
    provider = node if isinstance(node, CustomResourceProviderBase) else None
    if node is not None and provider is None:
        Annotations.of(scope).add_warning(
            "S3 auto-delete provider node found but is not a CustomResourceProviderBase — "
            "its log group will not be created and may dangle after cdk destroy. "
            "A CDK version bump likely changed the provider type; update AutoDeleteObjectsLogGroup wiring."
        )
    if provider is not None:
        # service_token is the Lambda ARN; index 6 of the colon-split is the function name.
        fn_name = Fn.select(6, Fn.split(":", provider.service_token))
        log_group = logs.LogGroup(
            scope,
            "AutoDeleteObjectsLogGroup",
            log_group_name=Fn.join("", ["/aws/lambda/", fn_name]),
            encryption_key=encryption_key,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # The provider logs on its CREATE invocation too; without explicit
        # ordering that first write races this LogGroup CREATE ("already exists").
        # Order every bucket's auto-delete custom resource after the group.
        for child in scope.node.children:
            if isinstance(child, s3.Bucket):
                auto_delete_cr = child.node.try_find_child("AutoDeleteObjectsCustomResource")
                if auto_delete_cr is not None:
                    auto_delete_cr.node.add_dependency(log_group)
        # The provider is a CDK-managed singleton Lambda; suppress its standard
        # singleton nag findings here so callers don't each repeat the block.
        acknowledge_rules(provider, CDK_LAMBDA_SUPPRESSIONS)
    return provider


# Nag rules every SSE-S3 log-sink bucket suppresses: it can't self-log (circular),
# can't use a KMS-CMK default (delivery services don't support it), and doesn't
# need versioning/replication for an append-only log sink.
_LOG_SINK_SUPPRESSION_RULES = (
    "AwsSolutions-S1",
    "NIST.800.53.R5-S3BucketLoggingEnabled",
    "HIPAA.Security-S3BucketLoggingEnabled",
    "PCI.DSS.321-S3BucketLoggingEnabled",
    "NIST.800.53.R5-S3DefaultEncryptionKMS",
    "HIPAA.Security-S3DefaultEncryptionKMS",
    "PCI.DSS.321-S3DefaultEncryptionKMS",
    "NIST.800.53.R5-S3BucketVersioningEnabled",
    "HIPAA.Security-S3BucketVersioningEnabled",
    "PCI.DSS.321-S3BucketVersioningEnabled",
    "NIST.800.53.R5-S3BucketReplicationEnabled",
    "HIPAA.Security-S3BucketReplicationEnabled",
    "PCI.DSS.321-S3BucketReplicationEnabled",
)


def create_sse_s3_log_bucket(
    scope: Construct,
    construct_id: str,
    *,
    suppression_reason: str,
    expiration_days: int,
    removal_policy: RemovalPolicy,
    auto_delete: bool,
    bucket_name: str | None = None,
    object_ownership: s3.ObjectOwnership | None = None,
) -> s3.Bucket:
    """Create a standardized SSE-S3 **log-sink** bucket (+ the log-bucket nag suppressions).

    The three log destinations in this project — the frontend access-log bucket,
    the CloudTrail-logs bucket, and the WAF-logs bucket — share the same posture:
    block all public access, SSE-S3 (the S3/CloudTrail/WAF delivery services don't
    support KMS-CMK destination *buckets*), SSL enforced, no versioning, and a
    lifecycle that expires objects. Centralizing it keeps them in lockstep (and
    keeps pylint's R0801 from flagging three near-identical bucket blocks). What
    varies — name, ACL ownership, lifecycle length, removal policy, auto-delete —
    is passed in.

    Args:
        scope: Construct scope to create the bucket under.
        construct_id: The bucket's construct id.
        suppression_reason: The reason recorded on the log-bucket nag suppressions.
        expiration_days: Objects expire after this many days.
        removal_policy: DESTROY for the destroy-friendly default, RETAIN behind retain_data.
        auto_delete: Whether to empty the bucket on stack delete (must be False when RETAIN).
        bucket_name: Explicit name (only where AWS forces it, e.g. WAF's aws-waf-logs- prefix).
        object_ownership: Set BUCKET_OWNER_PREFERRED where ACL-based log delivery needs it.

    Returns:
        The created bucket.
    """
    bucket = s3.Bucket(
        scope,
        construct_id,
        bucket_name=bucket_name,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        object_ownership=object_ownership,
        versioned=False,
        lifecycle_rules=[
            s3.LifecycleRule(
                id=f"ExpireAfter{expiration_days}Days",
                enabled=True,
                expiration=Duration.days(expiration_days),
                abort_incomplete_multipart_upload_after=Duration.days(1),
            ),
        ],
        removal_policy=removal_policy,
        auto_delete_objects=auto_delete,
    )
    acknowledge_rules(
        bucket,
        [{"id": rule, "reason": suppression_reason} for rule in _LOG_SINK_SUPPRESSION_RULES],
    )
    return bucket


def waf_logs_bucket_name(*, account: str, stack_name: str, suffix: str) -> str:
    """Deterministic name for a WAF log bucket: ``aws-waf-logs-{account}-{hash}-{suffix}``.

    AWS forces the ``aws-waf-logs-`` prefix, so the name is pinned. Account-qualified
    for S3's global uniqueness; a short hash of the stack name (which already encodes
    env + region) keeps it collision-free and well under the 63-char limit. Shared by
    ``create_waf_logs_bucket`` (the producer) and the Stage (which computes the WAF
    log S3 path for the Athena Glue tables without a cross-stack reference) so the
    name formula lives in exactly one place.
    """
    name_hash = hashlib.sha256(stack_name.encode()).hexdigest()[:12]
    return f"aws-waf-logs-{account}-{name_hash}-{suffix}"


def create_waf_logs_bucket(scope: Construct, suffix: str) -> s3.Bucket:
    """Create an ``aws-waf-logs-*`` S3 bucket wired for AWS WAF log delivery.

    AWS WAF requires the destination bucket name to start with ``aws-waf-logs-``
    (an AWS hard requirement — so this is a pinned name, unlike the auto-named
    buckets elsewhere). It's account-qualified for S3's global uniqueness plus a
    short hash of the stack name so multi-env / multi-region deployments in one
    account never collide on the name. Pinned-name caveat: a future
    replacement-forcing property change collides with the not-yet-deleted old
    bucket (CFN replacement is create-before-delete), so such a change must
    also change the name (e.g. the ``suffix``) in the same commit — see the
    AppConfig profile note in ``backend_app.py``.

    **The bucket policy must be complete before logging is enabled.** When WAF
    logging is turned on it ensures the ``delivery.logs.amazonaws.com`` write +
    ACL-check grant is on the bucket; if no policy exists yet WAF creates one,
    which then collides with CDK's own bucket policy (verified on a live deploy:
    ``The bucket policy already exists``). So this bucket *pre-declares* those
    exact delivery statements (plus the SSL-deny and the auto-delete grant) in
    its CDK-managed policy, and the caller orders the ``CfnLoggingConfiguration``
    *after* that policy (``logging.node.add_dependency(bucket.policy)``) — WAF
    then finds the grant already present and leaves the policy alone. ACLs are
    enabled (``BucketOwnerPreferred``) because WAF writes objects with a
    ``bucket-owner-full-control`` ACL.

    The caller points its ``CfnLoggingConfiguration.log_destination_configs`` at
    the returned bucket's ARN, adds the policy dependency above, and must call
    :func:`create_auto_delete_objects_log_group` once in the same stack (the
    bucket uses ``auto_delete_objects``). SSE-S3 (not CMK) because the WAF/Logs
    delivery service doesn't support KMS-CMK destination *buckets*.

    Args:
        scope: Any construct in the target stack (the bucket is created at the
            owning stack's level so the auto-delete-provider wiring can find it).
        suffix: Short discriminator for the bucket name (e.g. ``cf`` / ``api``).

    Returns:
        The created bucket (point WAF logging at ``bucket.bucket_arn``).
    """
    stack = Stack.of(scope)
    waf_bucket_reason = (
        "WAF log destination bucket — SSE-S3 (delivery doesn't support KMS-CMK buckets), "
        "no self-logging/versioning/replication for an append-only log sink"
    )
    bucket = create_sse_s3_log_bucket(
        stack,
        "WafLogsBucket",
        suppression_reason=waf_bucket_reason,
        expiration_days=90,
        removal_policy=RemovalPolicy.DESTROY,
        auto_delete=True,
        bucket_name=waf_logs_bucket_name(account=stack.account, stack_name=stack.stack_name, suffix=suffix),
        # WAF delivers objects with a bucket-owner-full-control ACL, so ACLs must
        # be enabled (the bucket-owner-enforced default would reject them).
        object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
    )
    # Pre-declare the exact delivery grant WAF would otherwise attach itself, so
    # WAF leaves the CDK-managed policy alone (the caller orders the logging
    # config after bucket.policy). The WAF→S3 path delivers through the
    # CloudWatch Logs vended-delivery service, so aws:SourceArn is a logs ARN.
    log_source_arn = f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:*"
    bucket.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AWSLogDeliveryWrite",
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
            actions=["s3:PutObject"],
            resources=[bucket.arn_for_objects(f"AWSLogs/{stack.account}/*")],
            conditions={
                "StringEquals": {
                    "s3:x-amz-acl": "bucket-owner-full-control",
                    "aws:SourceAccount": stack.account,
                },
                "ArnLike": {"aws:SourceArn": log_source_arn},
            },
        )
    )
    bucket.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AWSLogDeliveryAclCheck",
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
            # s3:ListBucket included per the WAF docs to avoid CloudTrail AccessDenied noise.
            actions=["s3:GetBucketAcl", "s3:ListBucket"],
            resources=[bucket.bucket_arn],
            conditions={
                "StringEquals": {"aws:SourceAccount": stack.account},
                "ArnLike": {"aws:SourceArn": log_source_arn},
            },
        )
    )
    return bucket


CDK_LAMBDA_SUPPRESSIONS: list[dict[str, object]] = [
    # cdk-nag v3 matches granular IAM4/IAM5 findings individually, so the
    # v2-era blanket entries are enumerated: every CDK-managed singleton runs
    # on AWSLambdaBasicExecutionRole (the Policy:: finding id routes through
    # acknowledge_rules' metadata fallback — it contains multiple '::').
    # Singleton-specific IAM5 wildcard findings (e.g. the BucketDeployment
    # handler's s3 grants) are acknowledged at their call sites, where the
    # region- and construct-specific finding ids can be built — a blanket
    # AwsSolutions-IAM5 here would match nothing in v3.
    {
        "id": "AwsSolutions-IAM4",
        "reason": "CDK-managed singleton Lambda uses AWS managed execution role",
        "applies_to": ["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
    },
    {"id": "AwsSolutions-L1", "reason": "CDK-managed singleton Lambda runtime is not configurable"},
    {"id": "Serverless-LambdaTracing", "reason": "CDK-managed singleton Lambda — tracing is not configurable"},
    {"id": "Serverless-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {"id": "Serverless-LambdaDefaultMemorySize", "reason": "CDK-managed singleton Lambda — memory is not configurable"},
    {"id": "Serverless-LambdaLatestVersion", "reason": "CDK-managed singleton Lambda runtime is not configurable"},
    {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "NIST.800.53.R5-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {
        "id": "NIST.800.53.R5-LambdaConcurrency",
        "reason": "CDK-managed singleton Lambda — concurrency is not configurable",
    },
    {"id": "NIST.800.53.R5-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
    {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "HIPAA.Security-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {
        "id": "HIPAA.Security-LambdaConcurrency",
        "reason": "CDK-managed singleton Lambda — concurrency is not configurable",
    },
    {"id": "HIPAA.Security-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
    {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "PCI.DSS.321-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
]
