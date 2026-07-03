#!/usr/bin/env python3
"""Fail when the cdk-nag v3 policy-validation report contains violations.

cdk-nag v3 packs are CDK policy-validation plugins, and CDK core signals a
failed validation by setting ``process.exitCode = 1`` **in the Node process**
(aws-cdk-lib ``core/lib/private/synthesis.js``). For a Node CDK app that fails
the synth; for a Python app the Node process is jsii's throwaway kernel, so
the exit code is discarded — ``app.synth()`` returns normally and ``cdk synth``
exits 0 with findings only *printed*. Verified against aws-cdk-lib 2.261.0 +
cdk-nag 3.0.1: a deliberately non-compliant stack synthesized "successfully"
via the CLI while the report listed errors.

This script is therefore the hard gate for CLI-driven synthesis (``make
cdk-synth`` and the CI ``cdk-check`` job run it right after ``cdk synth``):

- a **missing report** fails: the packs always attach in ``app.py``, so no
  report means validation never ran — the vacuous-gate case, not a pass;
- any **violation** fails, printed with its resources so the finding is
  actionable from the build log.

The in-process equivalent lives in ``tests/cdk/test_stage.py`` (report
parsing + a canary test proving the gate can fail). Drop this script if CDK
ever makes jsii-app synthesis fail natively on validation errors.

Usage: python scripts/check_validation_report.py [cdk.out]
"""

import json
import sys
from pathlib import Path

outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "cdk.out")
report_path = outdir / "validation-report.json"

if not report_path.exists():
    print(
        f"ERROR: {report_path} not found — policy validation never ran. "
        "The cdk-nag packs must be attached (attach_nag_packs in app.py); "
        "a missing report is a broken gate, not a pass.",
        file=sys.stderr,
    )
    sys.exit(1)

report = json.loads(report_path.read_text())
violations = [
    (violation.get("ruleName", "?"), [r.get("resourceLogicalId", "?") for r in violation.get("violatingResources", [])])
    for plugin_report in report.get("pluginReports", [])
    for violation in plugin_report.get("violations", [])
]

if violations:
    print(f"ERROR: {len(violations)} unacknowledged cdk-nag finding(s) in {report_path}:", file=sys.stderr)
    for rule, resources in violations:
        print(f"  {rule} @ {', '.join(resources)}", file=sys.stderr)
    sys.exit(1)

print(f"cdk-nag validation clean ({report_path})")
