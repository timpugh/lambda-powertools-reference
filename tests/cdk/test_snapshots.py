"""CloudFormation template snapshot tests.

A tripwire complementing the fine-grained assertions in ``test_stacks.py``:
those check the properties that *must* hold; these fail on *any* unreviewed
change to a stack's synthesized template, catching drift the targeted
assertions don't look for (a removed resource, a flipped default, an
accidental property). Snapshots are committed under ``tests/cdk/snapshots/``; a
failure means "explain or update," never an auto-bless — pair an intentional
snapshot update with the matching fine-grained assertion in ``test_stacks.py``
so the *why* is reviewable, not just the *what*.

Asset content hashes — the per-build-volatile parts of a template (Lambda/asset
S3 keys and the parameter logical-ids CDK derives from them) — are normalized
out, so editing ``lambda/`` code doesn't churn the snapshots; the snapshot
tracks infrastructure *shape*, which is what these tests exist to pin.
(Construct logical IDs are path-derived and stable across runs;
``TestLogicalIdStability`` guards those separately.)

Regenerate after an intentional change:

    UPDATE_SNAPSHOTS=1 make test-cdk
"""

import json
import os
import re
from pathlib import Path

import pytest

aws_cdk = pytest.importorskip("aws_cdk", reason="aws_cdk not installed — skipping CDK snapshot tests")

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infrastructure.app_stage import AppStage
from infrastructure.nag_utils import attach_nag_packs

# Skip Docker bundling so these tests run without Docker (same key the CLI honours).
_NO_BUNDLING = {"aws:cdk:bundling-stacks": []}
_SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
# The five stacks in a prod-shaped stage, addressed by their AppStage attribute.
_STACK_ATTRS = ("waf", "data", "backend", "frontend", "audit")


def _normalize(template: dict) -> str:
    """Serialize a template to stable, hash-free JSON for diffing.

    ``sort_keys`` makes the output independent of dict ordering; the regex subs
    replace per-build-volatile asset hashes (64-hex content hashes and the
    8-hex logical-id suffixes CDK derives from them) with fixed placeholders so
    the snapshot tracks infrastructure shape rather than asset content.
    """
    text = json.dumps(template, indent=2, sort_keys=True)
    # Asset content hashes (64 lowercase hex) — Lambda bundle, bucket deployment, etc.
    text = re.sub(r"[a-f0-9]{64}", "ASSET_HASH", text)
    # Asset-parameter logical-id suffixes (8 uppercase hex CDK appends).
    text = re.sub(r"(S3Bucket|S3VersionKey|ArtifactHash)[0-9A-F]{8}", r"\1", text)
    # Lambda version logical-id content hash (32 lowercase hex CDK derives from the
    # function's code asset + config). It varies with the asset, which itself differs
    # across build environments (e.g. __pycache__ in the un-bundled source dir), so it
    # would make this snapshot non-portable between a local run and CI. Keep the stable
    # construct-hash prefix, drop the asset-derived tail.
    text = re.sub(r"(CurrentVersion[0-9A-F]{8})[0-9a-f]{32}", r"\1", text)
    return text + "\n"


@pytest.fixture(scope="module")
def prod_stage() -> AppStage:
    """Synthesize the default (prod) stage for us-east-1.

    Packs attached as in app.py: cdk-nag v3's write-suppressions aspect is
    what keeps the cdk_nag Metadata audit trail in the snapshotted templates.
    """
    app = cdk.App(context=_NO_BUNDLING)
    attach_nag_packs(app)
    return AppStage(app, "ServerlessApp-us-east-1-stage", region="us-east-1")


@pytest.mark.parametrize("stack_attr", _STACK_ATTRS)
def test_template_matches_snapshot(prod_stage: AppStage, stack_attr: str) -> None:
    """Each stack's synthesized template matches its committed snapshot."""
    stack = getattr(prod_stage, stack_attr)
    rendered = _normalize(Template.from_stack(stack).to_json())
    snapshot_path = _SNAPSHOT_DIR / f"{stack.stack_name}.json"

    if os.environ.get("UPDATE_SNAPSHOTS"):
        _SNAPSHOT_DIR.mkdir(exist_ok=True)
        snapshot_path.write_text(rendered)
        pytest.skip(f"snapshot updated: {snapshot_path.name}")

    assert snapshot_path.exists(), (
        f"missing snapshot {snapshot_path.name} — generate it with 'UPDATE_SNAPSHOTS=1 make test-cdk'"
    )
    assert rendered == snapshot_path.read_text(), (
        f"{stack.stack_name} template drifted from its committed snapshot. If the change is "
        f"intentional, review the diff, regenerate with 'UPDATE_SNAPSHOTS=1 make test-cdk', and pair it "
        f"with the matching assertion change in test_stacks.py."
    )
