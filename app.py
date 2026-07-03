#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes a :class:`AppStage` per target region and deployment
environment. Each Stage groups the five stacks that make up one regional
deployment (data, WAF, backend, frontend, audit) so ``cdk deploy`` treats them
as a single unit:

  ServerlessAppWaf-{region}      — WAF WebACL, physically in us-east-1
                                (CloudFront constraint), but named per region
                                so each Stage is fully independent and can
                                be destroyed separately.
  ServerlessAppData-{region}     — stateful layer: DynamoDB idempotency table and
                                its dedicated CMK, isolated so its lifecycle is
                                independent of the stateless compute stack
                                (RETAIN/protection togglable via retain_data).
  ServerlessAppBackend-{region}         — Lambda, API Gateway, SSM, AppConfig
  ServerlessAppFrontend-{region} — S3, CloudFront (references WAF ARN cross-region
                                via SSM when target region differs from us-east-1)
  ServerlessAppAudit-{region}    — stateful audit layer: the CloudTrail object-level
                                S3 data-event trail, its log bucket, and a
                                dedicated CMK. Audits the frontend buckets
                                one-way (RETAIN/protection togglable via
                                retain_data).

The target region is controlled by the ``region`` CDK context key.
Defaults to us-east-1 if not specified.

The stateful stacks' retention posture is controlled by the ``retain_data`` CDK
context key (``-c retain_data=true``). It defaults to ``false`` so the template
tears down cleanly; production forks set it true to flip the data and audit
stacks (tables, buckets, CMKs) to RETAIN with deletion/termination protection.

The AppConfig feature-flag rollout posture is controlled by the
``appconfig_monitor`` CDK context key (``-c appconfig_monitor=true``). It defaults
to ``false`` (all-at-once flag deployment, no monitor) so the cold/first deploy
always succeeds; enable it only after a first deploy to get a gradual rollout with
alarm-driven auto-rollback for ongoing flag changes (see README "Deployment safety").

The deployment environment is controlled by the ``env`` CDK context key,
falling back to the ``ENVIRONMENT`` variable, defaulting to ``prod``.
``prod`` keeps the un-suffixed stack names above (so the long-lived
deployment is unaffected) and routes alarms to the SNS topic. Any other
value namespaces every stack (``ServerlessAppBackend-{env}-{region}``), which makes
ephemeral per-developer or per-branch deployments collision-free in a
shared account, and disables alarm paging for them.

Usage:
    cdk deploy --all                            # prod stage in us-east-1 (default)
    cdk deploy --all -c region=ap-southeast-1   # separate Singapore Stage
    cdk deploy --all -c env=alice-feature-x     # ephemeral developer stage
    make deploy ENV=alice-feature-x             # same, via the Makefile

Each Stage is fully independent — destroying one does not affect any other.
All five stacks for a given region+environment are destroyed together:

    cdk destroy --all -c region=ap-southeast-1
    cdk destroy --all -c env=alice-feature-x
"""

import os

import aws_cdk as cdk

from infrastructure.app_stage import DEFAULT_ENV_NAME, AppStage, parse_context_flag, stage_id
from infrastructure.nag_utils import attach_nag_packs

app = cdk.App()

# cdk-nag v3: the five rule packs are policy-validation plugins evaluated over
# the synthesized assembly during app.synth() — attached ONCE here at the App
# root, not per stack (per-stack Aspects carry only the project's own
# TemplateConventionChecks; see nag_utils.attach_nag_packs). Unacknowledged
# findings fail the synth itself, so `cdk synth` (CLI and in-process alike)
# remains the hard gate.
attach_nag_packs(app)

# Target region for the backend and frontend stacks. Defaults to us-east-1
# when no context value is provided. WAF is always pinned to us-east-1
# inside the Stage regardless of this value.
target_region: str = app.node.try_get_context("region") or "us-east-1"

# Deployment environment. Context key wins (explicit per-invocation intent);
# the ENVIRONMENT variable serves pipelines that export it once per job;
# the default keeps `cdk deploy` pointing at the long-lived prod stacks.
env_name: str = app.node.try_get_context("env") or os.environ.get("ENVIRONMENT") or DEFAULT_ENV_NAME

# Retention switch for the stateful data stack. Context values arrive as
# strings on the CLI (`-c retain_data=true`) or as a native bool from cdk.json;
# parse_context_flag normalises both and FAILS SYNTH on anything else — a typo
# like `-c retain_data=yes` silently coercing to False would strip retention
# and deletion protection from a deployment the operator believes is protected.
# Default (flag absent) is False, keeping the template destroy-friendly.
retain_data: bool = parse_context_flag(app.node.try_get_context("retain_data"), "retain_data")

# Opt-in AppConfig gradual rollout + alarm rollback monitor (`-c appconfig_monitor=true`).
# Default False so the cold/first deploy always succeeds: a CFN-managed AppConfig
# deployment with a monitor rolls back when its alarm is INSUFFICIENT_DATA, which a
# fresh stack's metric always is — see backend_app._attach_appconfig_rollback_monitor
# and README "Deployment safety". Turn it on only AFTER a first all-at-once deploy has
# produced metric data, to protect ongoing flag changes.
appconfig_monitor: bool = parse_context_flag(app.node.try_get_context("appconfig_monitor"), "appconfig_monitor")

# Stage id composition lives next to the Stage (app_stage.stage_id):
# prod keeps the legacy id so existing cdk.out assembly paths and tooling
# keyed on the stage name stay stable; other envs get their own id.
AppStage(
    app,
    stage_id(env_name, target_region),
    region=target_region,
    env_name=env_name,
    retain_data=retain_data,
    appconfig_monitor=appconfig_monitor,
)

app.synth()
