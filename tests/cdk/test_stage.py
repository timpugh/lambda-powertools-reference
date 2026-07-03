"""Stage-level assertion tests: environment naming, stack tags, and the cdk-nag gate.

The deployment-environment dimension (``env_name``) namespaces stack names so
ephemeral per-developer/per-branch deployments never collide with the
long-lived ``prod`` stacks in a shared account. These tests pin three
contracts:

1. **prod keeps the legacy names byte-for-byte** — any drift would orphan the
   currently deployed stacks (CloudFormation matches stacks by name).
2. **Non-prod names embed the env name and disable alarm paging.**
3. **Every shipped shape synthesizes with zero unacknowledged cdk-nag
   findings.** cdk-nag v3 packs are policy-validation plugins evaluated over
   the synthesized assembly; the gate here synthesizes each deployment shape
   (fixtures attach the packs the same way ``app.py`` does) and parses the
   assembly's ``validation-report.json`` — neither ``app.synth()`` nor the
   CLI fails natively for Python apps (see ``_unacknowledged_findings``).
   The project's own ``TemplateConventionChecks`` Aspect still surfaces
   error-level annotations, asserted separately. The CLI path stays gated by
   ``scripts/check_validation_report.py`` in ``make cdk-synth`` and CI.
"""

import json
from pathlib import Path
from typing import cast

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK stage tests")

import aws_cdk as cdk
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Annotations, Match, Template

from infrastructure.app_stage import DEFAULT_ENV_NAME, AppStage, parse_context_flag, validate_env_name
from infrastructure.nag_utils import attach_nag_packs
from infrastructure.validation_aspects import TemplateConventionChecks

# Skip Docker bundling so these tests run without Docker (same key the CLI honours).
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}


def _nag_app() -> cdk.App:
    """A test App with the five rule packs attached, mirroring app.py."""
    app = cdk.App(context=_NO_BUNDLING)
    attach_nag_packs(app)
    return app


def _unacknowledged_findings(root: cdk.App) -> list[str]:
    """Synthesize an App and return its unacknowledged cdk-nag v3 findings.

    Policy-validation plugins evaluate the produced assembly during
    ``App.synth()``, but the in-process synth does NOT raise on findings
    (observed against cdk-nag 3.0.1) — it prints the report and writes
    ``validation-report.json`` into the cloud assembly. Reading that file is
    therefore the reliable in-process gate. The CLI synth doesn't fail for
    Python apps either (CDK sets process.exitCode in jsii's throwaway Node
    kernel), which is why make cdk-synth / CI run
    scripts/check_validation_report.py over cdk.out after synthesizing.
    """
    assembly = root.synth()
    report_path = Path(assembly.directory) / "validation-report.json"
    if not report_path.exists():
        return []
    report = json.loads(report_path.read_text())
    return [
        f"{violation.get('ruleName')} @ "
        + ", ".join(r.get("resourceLogicalId", "?") for r in violation.get("violatingResources", []))
        for plugin_report in report.get("pluginReports", [])
        for violation in plugin_report.get("violations", [])
    ]


def _assert_nag_clean(stage: AppStage) -> None:
    findings = _unacknowledged_findings(cast(cdk.App, stage.node.root))
    details = "\n".join(f"  {f}" for f in findings)
    assert not findings, (
        f"unacknowledged cdk-nag findings — fix the resource or add a scoped "
        f"acknowledgment with a reason (see CLAUDE.md):\n{details}"
    )


@pytest.fixture(scope="module")
def prod_stage() -> AppStage:
    """Synthesize the default (prod) stage for us-east-1."""
    return AppStage(_nag_app(), "ServerlessApp-us-east-1-stage", region="us-east-1")


@pytest.fixture(scope="module")
def dev_stage() -> AppStage:
    """Synthesize an ephemeral developer stage."""
    return AppStage(
        _nag_app(),
        "ServerlessApp-alice-feature-x-us-east-1-stage",
        region="us-east-1",
        env_name="alice-feature-x",
    )


class TestEnvironmentNaming:
    def test_prod_default_keeps_legacy_stack_names(self, prod_stage: AppStage) -> None:
        # Byte-for-byte: a rename here orphans the deployed prod stacks.
        assert prod_stage.waf.stack_name == "ServerlessAppWaf-us-east-1"
        assert prod_stage.data.stack_name == "ServerlessAppData-us-east-1"
        assert prod_stage.backend.stack_name == "ServerlessAppBackend-us-east-1"
        assert prod_stage.frontend.stack_name == "ServerlessAppFrontend-us-east-1"
        assert prod_stage.audit.stack_name == "ServerlessAppAudit-us-east-1"

    def test_ephemeral_env_namespaces_every_stack(self, dev_stage: AppStage) -> None:
        assert dev_stage.waf.stack_name == "ServerlessAppWaf-alice-feature-x-us-east-1"
        assert dev_stage.data.stack_name == "ServerlessAppData-alice-feature-x-us-east-1"
        assert dev_stage.backend.stack_name == "ServerlessAppBackend-alice-feature-x-us-east-1"
        assert dev_stage.frontend.stack_name == "ServerlessAppFrontend-alice-feature-x-us-east-1"
        assert dev_stage.audit.stack_name == "ServerlessAppAudit-alice-feature-x-us-east-1"

    def test_ephemeral_env_disables_alarm_paging(self, dev_stage: AppStage) -> None:
        # Non-prod environments must not create the SNS alarm topic — an
        # ephemeral branch stack should never page an operator.
        template = Template.from_stack(dev_stage.backend)
        template.resource_count_is("AWS::SNS::Topic", 0)

    def test_prod_env_routes_alarms_to_topic(self, prod_stage: AppStage) -> None:
        template = Template.from_stack(prod_stage.backend)
        template.resource_count_is("AWS::SNS::Topic", 1)

    @pytest.mark.parametrize("bad_name", ["", "has space", "has/slash", "-leading-hyphen", "x" * 40])
    def test_invalid_env_names_fail_at_synth(self, bad_name: str) -> None:
        # Stack names embed the env name; reject illegal values with a clear
        # message at synth instead of an opaque CloudFormation error at deploy.
        with pytest.raises(ValueError, match="Invalid deployment environment name"):
            validate_env_name(bad_name)

    def test_default_env_name_is_prod(self) -> None:
        # app.py and the Makefile lean on this default; changing it silently
        # re-points bare `cdk deploy` away from the long-lived stacks.
        assert DEFAULT_ENV_NAME == "prod"


class TestContextFlagParsing:
    """app.py parses retain_data / appconfig_monitor via parse_context_flag."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, False),  # flag absent — the documented destroy-friendly default
            (True, True),  # native bool from cdk.json
            (False, False),
            ("true", True),  # CLI string forms, any casing
            ("TRUE", True),
            ("false", False),
            ("False", False),
        ],
    )
    def test_recognized_values(self, raw: object, expected: bool) -> None:
        assert parse_context_flag(raw, "retain_data") is expected

    @pytest.mark.parametrize("raw", ["yes", "1", "on", "retain", ""])
    def test_unrecognized_values_fail_at_synth(self, raw: str) -> None:
        # `-c retain_data=yes` silently coercing to False would deploy DESTROY
        # policies with no deletion protection while the operator believes
        # production data is retained — fail the synth loudly instead.
        with pytest.raises(ValueError, match="retain_data"):
            parse_context_flag(raw, "retain_data")


class TestStackTags:
    def test_all_stacks_carry_service_environment_owner_tags(self, prod_stage: AppStage) -> None:
        # Stack tags propagate to every taggable resource at deploy time —
        # cost allocation and console filtering for free. They live in the
        # deploy-time manifest (not the template body), so assert on the
        # stack's tag manager rather than template properties.
        for stack in (prod_stage.waf, prod_stage.data, prod_stage.backend, prod_stage.frontend, prod_stage.audit):
            tags = stack.tags.tag_values()
            assert tags.get("service") == "serverless-app"
            assert tags.get("environment") == "prod"
            assert tags.get("owner"), "owner tag must be non-empty (username or ci fallback)"

    def test_ephemeral_stage_tags_carry_env_name(self, dev_stage: AppStage) -> None:
        assert dev_stage.backend.tags.tag_values().get("environment") == "alice-feature-x"


class TestRetainDataPlumbing:
    """The Stage forwards retain_data to the stateful data stack."""

    def test_default_stage_data_table_is_destroyable(self, prod_stage: AppStage) -> None:
        # Default stage (retain_data omitted) → DESTROY, so the template tears
        # down cleanly.
        Template.from_stack(prod_stage.data).has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Delete"})

    def test_retain_data_stage_data_table_is_retained(self) -> None:
        # retain_data=True flows Stage → DataStack → RETAIN.
        app = cdk.App(context=_NO_BUNDLING)
        stage = AppStage(app, "ServerlessApp-us-east-1-stage", region="us-east-1", retain_data=True)
        Template.from_stack(stage.data).has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Retain"})


class TestAppConfigMonitorPlumbing:
    """The Stage forwards appconfig_monitor → backend → BackendApp.

    Default (off) = all-at-once, no monitor (asserted in test_stacks.py — the
    shape that must create a cold stack). On = gradual rollout + environment
    monitor, the opt-in production add-on for ongoing flag changes.
    """

    @staticmethod
    def _monitor_stage() -> AppStage:
        app = cdk.App(context=_NO_BUNDLING)
        return AppStage(app, "ServerlessApp-us-east-1-stage", region="us-east-1", appconfig_monitor=True)

    def test_monitor_on_uses_gradual_strategy(self) -> None:
        # appconfig_monitor=True flows Stage → BackendStack → BackendApp and
        # swaps the all-at-once strategy for a gradual one with a bake window.
        template = Template.from_stack(self._monitor_stage().backend)
        template.has_resource_properties(
            "AWS::AppConfig::DeploymentStrategy",
            {
                "GrowthType": "LINEAR",
                "GrowthFactor": 25,
                "DeploymentDurationInMinutes": 10,
                "FinalBakeTimeInMinutes": 5,
            },
        )

    def test_monitor_on_attaches_environment_monitor(self) -> None:
        # The environment carries a Monitors entry (alarm ARN + role ARN) so
        # AppConfig auto-rolls-back a bad flag rollout.
        template = Template.from_stack(self._monitor_stage().backend)
        template.has_resource_properties(
            "AWS::AppConfig::Environment",
            {
                "Monitors": Match.array_with(
                    [Match.object_like({"AlarmArn": Match.any_value(), "AlarmRoleArn": Match.any_value()})]
                )
            },
        )
        # The monitor role AppConfig assumes to read the alarm state exists.
        roles = template.find_resources("AWS::IAM::Role")
        assert any(
            "appconfig.amazonaws.com" in json.dumps(r["Properties"].get("AssumeRolePolicyDocument", {}))
            for r in roles.values()
        ), "expected an AppConfig-assumed monitor role when appconfig_monitor=True"


class TestDeploymentAggressivenessByEnv:
    """CodeDeploy canary aggressiveness is environment-gated: canary in prod, fast in dev.

    Prod-shape detail is asserted in test_stacks.py; this proves the *contrast* —
    the dev/ephemeral shape shifts the Lambda alias all-at-once so iterating
    doesn't wait out a canary window. (The AppConfig deployment is all-at-once in
    every environment — see test_stacks.py for why a CFN-managed cold deploy
    can't carry the alarm-monitored gradual rollout.)
    """

    def test_dev_codedeploy_is_all_at_once(self, dev_stage: AppStage) -> None:
        template = Template.from_stack(dev_stage.backend)
        groups = template.find_resources("AWS::CodeDeploy::DeploymentGroup")
        assert groups, "expected a CodeDeploy deployment group even in dev"
        config_names = [g["Properties"].get("DeploymentConfigName", "") for g in groups.values()]
        assert all("AllAtOnce" in name for name in config_names), config_names


class TestNagCompliance:
    """cdk-nag v3 gate: unacknowledged findings fail ``app.synth()`` itself.

    The five rule packs are policy-validation plugins on each fixture's test
    App (attached exactly the way ``app.py`` does), so one full-app synth per
    deployment shape is the compliance gate for every stack in that shape.
    The project's own ``TemplateConventionChecks`` Aspect still surfaces
    error-level annotations and is asserted separately.
    """

    def test_prod_stage_has_no_unacknowledged_findings(self, prod_stage: AppStage) -> None:
        _assert_nag_clean(prod_stage)

    def test_dev_stage_has_no_unacknowledged_findings(self, dev_stage: AppStage) -> None:
        # The non-prod shape (no SNS topic) must be nag-clean too — otherwise
        # ephemeral stacks would fail the CI synth gate when env is overridden.
        _assert_nag_clean(dev_stage)

    def test_appconfig_monitor_shape_has_no_unacknowledged_findings(self) -> None:
        # The opt-in monitor shape adds an alarm + a cloudwatch:DescribeAlarms
        # role; its acknowledgments (IAM5 wildcard, inline-policy,
        # no-alarm-action) must be complete or a fork enabling
        # appconfig_monitor would fail synth.
        stage = AppStage(_nag_app(), "ServerlessApp-us-east-1-stage", region="us-east-1", appconfig_monitor=True)
        _assert_nag_clean(stage)

    def test_retain_data_shape_has_no_unacknowledged_findings(self) -> None:
        # The production-fork shape (retain_data=True: RETAIN + deletion/
        # termination protection, retained buckets with no auto-delete
        # provider) is never synthesized by the CI CLI gate (default context) —
        # this is its only nag gate. A finding unique to the retained shape
        # would otherwise surface for the first time on a production fork's
        # own synth, after they flip the one switch the template tells them to.
        stage = AppStage(_nag_app(), "ServerlessApp-us-east-1-stage", region="us-east-1", retain_data=True)
        _assert_nag_clean(stage)

    def test_nag_gate_can_fail(self) -> None:
        # The assertion that the gate is not vacuous: a deliberately
        # non-compliant resource must produce findings in the validation
        # report. If the packs stop attaching (or the report location/shape
        # changes), every clean-gate test above would pass on nothing — this
        # canary is what catches that. A bare default Bucket violates several
        # pack rules (no access logs, no SSL, no KMS default encryption).
        app = _nag_app()
        stack = cdk.Stack(app, "NagCanaryStack")
        s3.Bucket(stack, "NonCompliantBucket")
        findings = _unacknowledged_findings(app)
        assert findings, "the nag gate reported nothing for a non-compliant bucket — the gate is vacuous"
        assert any("AwsSolutions-S" in f or "S3Bucket" in f for f in findings), findings

    @pytest.mark.parametrize("stack_attr", ["waf", "data", "backend", "frontend", "audit"])
    def test_convention_checks_have_no_error_annotations(self, prod_stage: AppStage, stack_attr: str) -> None:
        # TemplateConventionChecks (the project's own Aspect) still reports
        # via error-level annotations, independent of the v3 plugin packs.
        stack = getattr(prod_stage, stack_attr)
        errors = Annotations.from_stack(stack).find_error("*", Match.string_like_regexp(".*"))
        details = "\n".join(
            f"  {e.id}: {(e.entry.data if isinstance(e.entry.data, str) else str(e.entry.data)).splitlines()[0]}"
            for e in errors
        )
        assert not errors, f"error annotations on {stack.stack_name} (TemplateConventionChecks?):\n{details}"

    @pytest.mark.parametrize("stack_attr", ["waf", "data", "backend", "frontend", "audit"])
    def test_convention_aspect_attached_to_every_stack(self, prod_stage: AppStage, stack_attr: str) -> None:
        # The convention gate hangs on each stack constructor calling
        # apply_compliance_aspects; dropping the call would pass every other
        # gate vacuously. (The five rule packs are covered by
        # test_nag_gate_can_fail — they are App-level plugins now, not
        # per-stack Aspects.)
        aspects = cdk.Aspects.of(getattr(prod_stage, stack_attr)).all
        assert any(isinstance(aspect, TemplateConventionChecks) for aspect in aspects), (
            f"TemplateConventionChecks is not attached to the {stack_attr} stack — "
            "apply_compliance_aspects was likely dropped from its constructor"
        )
