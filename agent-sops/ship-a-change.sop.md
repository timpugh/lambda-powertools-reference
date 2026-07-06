# Ship a Change to the Lambda Powertools Reference Architecture

## Overview

This SOP guides an AI agent through making a change to this repository — a
five-stack AWS CDK + Lambda Powertools reference architecture — and shipping it
through every safety gate the project enforces, in the order the project
enforces them. It encodes the operating rules that live in `CLAUDE.md`, the
`README.md` ("CDK best practices", "CDK security checks", "Design decisions and
known limitations", "When forking for production"), the `Makefile`,
`.github/workflows/ci.yml`, and the docstrings across `infrastructure/`,
`lambda/`, and `scripts/`, so that a change lands without tripping the two-venv
split, the cdk-nag hard gate, the committed-artifact drift gates, the CDK
best-practice invariants (logical-ID stability, deterministic synthesis), or the
two deployment traps (`appconfig_monitor` on a cold deploy; `retain_data`
semantics).

Use this SOP when the task is: implement a feature or fix, change
infrastructure, bump or add a dependency, edit the API surface, and/or deploy
the result. It is a *process* SOP — it tells you which gates apply to your
change and how to satisfy them, not what business logic to write.

**The one-line contract of this repo:** a clean `make pr` should mean a green CI
run. Everything below exists to make `make pr` pass for the right reasons.

## Parameters

- **change_description** (required): A description of the change to make (a
  feature, a bug fix, a dependency bump, an API edit, an infrastructure change,
  or a combination). May be a natural-language description, a path to a spec
  file, or a link to an issue.
- **change_type** (optional, default: "auto-detect"): The surface(s) the change
  touches, which determines the applicable gates. One of: `lambda` (runtime code
  under `lambda/`), `infrastructure` (CDK code under `infrastructure/`, `app.py`,
  `cdk.json`), `dependencies` (`pyproject.toml`, `package.json`), `docs`
  (Markdown, `docs/`), `mixed`, or `auto-detect` (infer from the change).
- **deploy_target** (optional, default: "none"): Whether and where to deploy
  after gates pass. `none` = validate only, do not deploy. `prod` = deploy the
  long-lived prod stacks. Any other value = an ephemeral namespaced environment
  (`make deploy ENV=<value>`).
- **deploy_region** (optional, default: "us-east-1"): Target region for a deploy
  or teardown. The WAF stack is always pinned to us-east-1 regardless.

**Constraints for parameter acquisition:**
- If all required parameters are already provided, You MUST proceed to the Steps
- If any required parameters are missing, You MUST ask for them before proceeding
- When asking for parameters, You MUST request all parameters in a single prompt
- When asking for parameters, You MUST use the exact parameter names as defined
- You MUST support multiple input methods for **change_description** including:
  - Direct input: the change described in text
  - File path: a path to a local spec or task file
  - URL: a link to an issue or design doc
- You MUST confirm you understand the change before editing code
- You MUST NOT re-prompt for **deploy_target** or **deploy_region** once provided, because re-confirming a destructive-capable parameter slows the workflow without adding safety

## Steps

### 1. Provision or verify the two-venv workstation

The project resolves CDK and Powertools into two separate virtual environments
because they require incompatible `attrs` versions (CDK pulls `attrs<26` via
jsii; Powertools pulls `attrs>=26`). `.venv` is the CDK workstation; `.venv-lambda`
is the Lambda runtime. One `uv.lock` holds both resolutions via
`[tool.uv.conflicts]` in `pyproject.toml`.

**Constraints:**
- You MUST run `make doctor` first to check whether both venvs, the pinned `npx cdk` CLI, and pre-commit are already wired
- If `make doctor` reports either venv is missing or a group is not installed, You MUST run `make install` (it provisions both venvs, runs `npm ci`, and installs the pre-commit hook)
- You MUST NOT install Powertools into `.venv` or `aws-cdk-lib` into `.venv-lambda`, because that reintroduces the `attrs` conflict the two-venv split exists to resolve and corrupts the resolution
- You MUST NOT run `npm install -g` for the CDK CLI, because the CLI is pinned in `package.json` and invoked via `npx cdk` so Dependabot can track it — a global install is the one un-pinned supply-chain input the project deliberately eliminated
- You SHOULD use `make clean-venvs && make install` as the recovery path if a venv is corrupted, NOT a manual `pip install`
- You MUST NOT edit any `.venv*` directory contents or commit them, because they are gitignored, project-local, and reproducible from `make install`
- If the change will be synthesized or deployed (Steps 5 and 7), You MUST ensure a container runtime is running first — Finch (`finch vm start && export CDK_DOCKER=finch`, the AWS-supported default) or Docker — because `PythonFunction` bundles the Lambda in a container and synth cannot produce the real assembly without one

### 2. Classify the change and select the applicable gates

Different surfaces trigger different gates. Determine which of these the change
touches so you run the right regeneration and validation steps and skip the ones
that do not apply.

**Constraints:**
- If **change_type** is `auto-detect`, You MUST infer the touched surfaces from the files the change will modify
- You MUST treat a change to `lambda/app.py` routes, Pydantic models in `lambda/models.py`, or `responses=` metadata as requiring the **OpenAPI regeneration** gate in Step 4
- You MUST treat any change to `[dependency-groups]` in `pyproject.toml` as requiring the **lockfile regeneration** gate in Step 4 (both `uv.lock` and `lambda/requirements.txt`)
- You MUST treat any change under `infrastructure/`, `app.py`, or `cdk.json` as requiring the **cdk-nag gate** (Step 5) and the CDK assertion tests (Step 6)
- You MUST treat any change that alters synthesized CloudFormation *shape* (resources added/removed/retyped) as requiring a deliberate snapshot regeneration in Step 4
- You MUST treat a change to `infrastructure/feature_flags.json` as requiring the **unit** suite (`make test`, Step 6), NOT only the CDK gate — the CDK synth merely `json.loads` the file (Powertools isn't installable next to CDK), while the real Powertools feature-flags schema is validated by `tests/unit/test_feature_flags_schema.py` in `.venv-lambda`; separately, flipping a flag's committed `default` to `true` silently enables that feature for every caller on the next deploy
- You SHOULD state, before editing, which gates you expect to run, so the plan is auditable

### 3. Make the change following existing patterns

Write code that reads like the surrounding code and preserves the project's
non-negotiable postures (encryption, least-privilege, explicit-in-code over
implicit-runtime-default). The domain logic lives in constructs (`BackendApp`),
composed by thin stacks; the Lambda splits into handler (`app.py`), service
(`service.py`), and models (`models.py`).

**Constraints:**
- You MUST match the surrounding code's idiom, naming, comment density, and layering (handler vs service vs models on the Lambda side; construct vs stack on the CDK side)
- You MUST run Lambda-side edits and their unit tests in `.venv-lambda` and CDK-side edits and their tests in `.venv` — the Makefile targets already select the right venv, so You SHOULD use them (`make test`, `make test-cdk`) rather than invoking `pytest` directly
- You MUST NOT introduce account- or region-wide CDK constructs (for example `glue.CfnDataCatalogEncryptionSettings`, `xray.UpdateEncryptionConfig`) without explicitly flagging them to the user, because they mutate state shared with every other app in the deploying account and a fork would silently override a neighbor team's settings
- You MUST keep every data-bearing resource that supports a per-resource customer-managed key CMK-encrypted, and You MUST scope every service-principal grant on a CMK with an `aws:SourceAccount` + `aws:SourceArn` confused-deputy guard, following the `grant_*_to_key` helpers in `infrastructure/nag_utils.py` — dropping the guard on one CMK is exactly the asymmetry those shared helpers exist to prevent
- If a new resource creates supporting AWS resources outside CloudFormation (a log group, a dashboard), You MUST add a cleanup `cr.AwsCustomResource` mirroring `AppInsightsDashboardCleanup` / `RumLogGroupCleanup` (an `on_delete` SDK call, IAM scoped to the exact ARN, `ignore_error_codes_matching="ResourceNotFoundException"`), because `cdk destroy` will not remove them and they dangle after teardown
- You MUST pin any newly required environment variable in `lambda/models.py::EnvVars` so a misconfiguration fails at cold start with a field-by-field Pydantic report, not deep inside boto3 on the Nth request
- You MUST NOT rename or re-scope an existing **stateful** construct (DynamoDB, KMS key, S3 bucket, CloudFront, SSM, AppConfig, WAF WebACL, log group), because that changes its CDK logical ID, which forces resource replacement and destroys the data — `TestLogicalIdStability` freezes these IDs in a committed list and fails at PR time; if you genuinely must change one, update the expected value in the same commit so the intent is reviewable
- You MUST keep synthesis deterministic: no `*.from_lookup`, no `CfnParameter`, no `CfnCondition` — every decision is made at synth time from CDK context passed down as typed constructor args; if a fork does add a lookup, You MUST commit the resulting `cdk.context.json` so every synth of that commit resolves identically
- You SHOULD prefer L2 constructs and reach for an escape hatch only where no L2 exists; when you do, You MUST go through `node.default_child` guarded by a runtime `isinstance` check and a comment explaining why (the pattern used for `recursive_loop="Terminate"` on the `CfnFunction`), so a future CDK change fails loudly at synth instead of silently dropping the override
- You SHOULD leave physical resource names unset so CDK generates them (avoiding replacement failures and cross-region collisions); where a name is deliberately pinned (WAF WebACLs, the AppConfig profile, the CloudTrail trail, Glue/Athena resources, the RUM monitor), You MUST change the pinned name in the same commit as any replacement-forcing property change, because CFN replacement is create-before-delete and a reused name collides with the not-yet-deleted old resource
- You MUST keep secrets out of SSM `StringParameter` and code — use AWS Secrets Manager (CMK-encryptable, rotatable) for anything secret; SSM/AppConfig hold non-secret config only (CloudFormation cannot even create an SSM `SecureString`)
- For a Lambda change, You MUST follow the established error-handling pattern: catch the *specific* expected exception type for a critical downstream call and re-raise as `InternalServerError`, let unexpected exceptions propagate so their real type surfaces in metrics/X-Ray, and for a non-critical fallback log a warning **with `exc_info=True`** and add a unit test per path — a fallback that hides the cause makes a permanently broken integration indistinguishable from a transient blip (this repo's AppConfig schema mismatch hid behind exactly that until `exc_info` exposed it)
- You SHOULD prefer making a posture explicit in code over relying on a runtime default (the repo does this for `recursive_loop="Terminate"`, `retry_attempts=0`, `system_log_level_v2`, botocore `total_max_attempts`) so the intent is visible and tunable
- You MUST NOT add a `print()` call in `lambda/` or `infrastructure/` because ruff's `T20` rule blocks it — use the Powertools `Logger`; and You SHOULD keep each function within the enforced complexity ceilings (pylint/xenon: ~55 statements, 8 args, 12 branches, 6 returns, 32 locals per function), since `make pr`'s lint gate fails when they are exceeded
- You MUST NOT commit `cdk.out/`, `report.html`, `htmlcov/`, `.coverage*`, `coverage-badge.json`, or `site/`, because they are reproducible build artifacts and are already gitignored

### 4. Regenerate committed derived artifacts whose inputs changed

Three artifacts are generated from source and **committed**, and CI fails on
drift. Regenerate only the ones whose inputs your change touched.

**Constraints:**
- If routes, Pydantic models, or `responses=` metadata changed, You MUST run `make openapi` and commit the updated `docs/openapi.json`, because CI regenerates it hermetically and fails on any byte difference, and a PR also runs an oasdiff breaking-change gate against the base branch
- If `[dependency-groups]` in `pyproject.toml` changed, You MUST run `make lock` (it regenerates `uv.lock` AND re-exports `lambda/requirements.txt`) and commit both, because `PythonFunction` bundles `lambda/requirements.txt` into the deployed Lambda and CI's `check-lock` gate fails when it drifts from `uv.lock`; You SHOULD add a new dependency with `uv add <pkg> --group <group>` (it updates `pyproject.toml` + `uv.lock` atomically) and then still run `make lock`, because `uv add`/`uv lock` alone does NOT re-export the requirements file the deployed Lambda bundles
- If the change intentionally alters synthesized CloudFormation shape, You MUST regenerate the committed stack snapshots with `UPDATE_SNAPSHOTS=1 make test-cdk` and You MUST pair the snapshot update with the matching fine-grained assertion change in `tests/cdk/test_stacks.py`, so the *why* of the shape change stays reviewable rather than being a rubber-stamped baseline churn
- You MUST NOT hand-edit any of these generated files (`docs/openapi.json`, `uv.lock`, `lambda/requirements.txt`, `tests/cdk/snapshots/*.json`), because the next regeneration overwrites the edit and the drift gate re-fails
- You MUST NOT hand-edit `CHANGELOG.md`, because it is generated by git-cliff from commit history; fix changelog grouping in `cliff.toml` instead

### 5. Run the cdk-nag hard gate (only if infrastructure changed)

Five rule packs (AwsSolutions, Serverless, NIST 800-53 R5, HIPAA Security, PCI
DSS 3.2.1) run as cdk-nag v3 policy-validation plugins attached once at the App
root. **The CLI exit code is NOT the gate** — for a Python CDK app, CDK sets the
failure `process.exitCode` in jsii's throwaway Node kernel, so `cdk synth` exits
0 even with findings. The real gate is `scripts/check_validation_report.py` over
`cdk.out/validation-report.json`.

**Constraints:**
- You MUST run `make cdk-synth` (it runs `cdk synth '**'` then `scripts/check_validation_report.py cdk.out`), NOT a bare `cdk synth`, because the `'**'` glob is required to descend into the Stage-nested stacks and the report checker is the actual pass/fail signal
- You MUST have a container runtime running for `make cdk-synth` — Finch (`export CDK_DOCKER=finch`, the AWS-supported default) or Docker — because `PythonFunction` bundles the Lambda in a container; without one the synth cannot produce the real assembly
- You MUST resolve every finding by either (a) fixing the underlying issue — the preferred path — or (b) acknowledging it with a specific reason via `acknowledge_rules(construct, [{"id": ..., "reason": ..., "applies_to": [...]}])`
- When acknowledging a granular IAM4/IAM5 finding, You MUST include the exact `applies_to` finding id(s), because cdk-nag v3 matches these individually and a bare `AwsSolutions-IAM5` acknowledgment matches nothing — the gate's failure output prints the exact ids to use
- You MUST NOT add an absolute-path suppression; resolve singletons via `node.try_find_child` (as the existing helpers do) so suppressions keep working when stacks are nested under a `cdk.Stage`
- You MUST NOT weaken or delete an existing acknowledgment's confused-deputy or least-privilege rationale to make a finding disappear, because the reason string is the audit trail a reviewer relies on
- You SHOULD acknowledge at the resource level (`acknowledge_rules` on the specific construct) to keep the blast radius small, and reserve stack-level acknowledgments for findings that are genuinely stack-wide (no custom domain, no VPC by design)
- You SHOULD treat a *missing* `validation-report.json` as a broken gate (packs not attached), not a pass — the checker already fails on this, and you MUST NOT work around it by skipping the checker
- You MUST run every CDK subcommand through its `make` wrapper (`make cdk-synth`, `make cdk-diff`, `make cdk-ls`, `make deploy`), NOT bare `cdk`, because the five real stacks live inside a `cdk.Stage` and a bare invocation without the `'**'` glob silently walks only the App's direct children and no-ops on the actual stacks

### 6. Run the full local CI mirror

`make pr` runs every CI gate locally, in CI's order: `check-lock`, `lint`
(ruff/mypy/pylint/bandit/xenon/pip-audit via pre-commit), `typecheck` (both
venvs), `lint-docs`, `test` (unit + 100% `lambda/` coverage gate), `test-cdk`
(CDK assertions including the in-process cdk-nag annotations gate), `cdk-synth`
(authoritative CLI synth + report check), and `compare-openapi`.

**Constraints:**
- You MUST run `make pr` and confirm it prints "All local CI gates passed." before considering the change ready to push
- You MUST NOT claim the change passes based on a subset of gates (for example only `make test`), because the unit suite does not exercise the CDK assertion or nag gates and vice versa — evidence for "it passes" is a clean `make pr`, not a partial run
- If the unit-test coverage gate fails at under 100%, You MUST add tests for the uncovered `lambda/` lines rather than lowering the gate, because the 100% `lambda/` gate is intentional (the CDK side carries intentional uncovered defensive lines and is measured only informationally by `make coverage`)
- If `test-cdk` fails on `TestLogicalIdStability`, You MUST treat it as a data-loss warning (a stateful construct's logical ID changed) — either revert the rename or, if the change is genuinely intended, update the expected ID in the same commit; you MUST NOT make the test pass by loosening the frozen list without that intent being reviewable
- If the snapshot test reports a diff, You MUST explain or update it, never auto-bless it — review the diff for an unintended resource removal or flipped default, then regenerate per Step 4 only if the change is intended
- You MUST fix a `typecheck` failure on the correct side (mypy runs over `infrastructure/` in `.venv` and over `lambda/ scripts/` in `.venv-lambda`); You MUST NOT silence it with a blanket `# type: ignore` because a targeted annotation fix almost always exists and a blanket ignore hides future regressions
- If you cannot run `make cdk-synth` locally because no container runtime (Finch or Docker) is available, You MUST say so explicitly and run every other gate, because reporting a full green when a gate was skipped is a false success claim
- For an infrastructure or IAM change, You SHOULD run the project's `wa-review` skill (a Well-Architected / security review it ships for exactly this trigger) before merge, because `make pr`'s automated gates cover posture but not design-level findings (blast radius, confused-deputy scope, entry-path parity)

### 7. Deploy safely (only when deploy_target is not "none")

Deploys are manual and use the `'**'` glob to descend into the Stage. Two
production switches carry hard rules. `retain_data` (`-c retain_data=true`) is
safe from the first deploy and flips the data + audit stacks (tables, buckets,
CMKs) to RETAIN with deletion/termination protection. `appconfig_monitor` must
never be set on a first/cold deploy.

**Constraints:**
- Before the first deploy into an account or a new region, You MUST bootstrap CDK (`npx cdk bootstrap aws://<account>/us-east-1` — always required because the WAF stack lives there — plus `aws://<account>/<deploy_region>` when deploying to another region), because deploy fails without a bootstrapped environment; bootstrap is idempotent if already done
- You MUST deploy with `make deploy` (which runs `cdk deploy '**' --require-approval never`); for `deploy_target` other than `prod` You MUST pass `ENV=<deploy_target>` so the stacks are namespaced and collision-free; for a non-default region You MUST pass `-c region=<deploy_region>`
- You MUST NOT set `appconfig_monitor=true` on a cold/first deploy of a stack, because the monitor's alarm starts in `INSUFFICIENT_DATA`, which AppConfig treats as a rollback signal, so it aborts the create and the stack can never reach `CREATE_COMPLETE` — this is live-verified and happens even with `treat_missing_data=NOT_BREACHING`
- To enable the AppConfig gradual rollout + monitor, You MUST deploy once with the default first, let the `FeatureFlagEvaluationFailure` metric report, then run `make deploy-appconfig-monitor` (a guarded target that refuses unless the backend stack already exists in an updatable `*_COMPLETE` state) or persist `appconfig_monitor: true` in `cdk.json` — never on the cold deploy
- Before the first deploy in a new account, You MUST verify the account's applied Lambda concurrency quota is at least 200 (`aws lambda get-account-settings`), because `ApiFunction` reserves 100 and Lambda rejects any reservation that would drop the unreserved pool below 100 — the stack fails on that resource otherwise
- For a production deployment intended to protect data, You MUST persist the retention posture in `cdk.json` (`"retain_data": true`), NOT via a one-off `-c retain_data=true`, because a CLI context flag is per-run: the next plain `make deploy` reverts the table and CMK to `DESTROY` and turns deletion/termination protection off, silently un-protecting production data (the default is `false`, and stack topology alone does not protect data — RETAIN plus deletion/termination protection do, and those ride the flag)
- After deploying, You MUST verify the change works against the live stack (the API responds, the alarm/metric behaves as intended) rather than assuming success from a clean `cdk deploy` exit
- You MUST NOT run `cdk-revert-drift` as part of a routine deploy, because it assumes code is always the source of truth and would also undo a legitimate emergency console change — reach for it consciously only after `make cdk-drift` shows what would be reverted

### 8. Tear down an ephemeral environment cleanly (only if you created one)

CloudFront / S3 / CloudTrail log delivery is asynchronous, so straggler log
objects and re-created CloudWatch log groups can survive a plain `cdk destroy`.
`make destroy-clean` snapshots CFN-owned log groups, empties buckets, destroys,
retries on a late-arriving straggler, then sweeps re-created groups by exact name.

**Constraints:**
- If you deployed an ephemeral environment for validation, You MUST tear it down with `make destroy-clean ENV=<deploy_target> REGION=<deploy_region>`, NOT a bare `cdk destroy`, because the bare form leaves dangling unencrypted, retention-less log groups behind
- You MUST scope the teardown to the environment you created via `ENV=` / `REGION=`, because the log-group sweeps are deliberately env-prefixed so tearing down one environment cannot delete another environment's log groups
- You MUST NOT run `make destroy-clean` (or `make deploy`) against `prod` unless the user explicitly asked to, because these stacks are the long-lived deployment CloudFormation matches by name
- You SHOULD re-run `make destroy-clean` if it reports a straggler — every step is idempotent and a repeat run is always safe

### 9. Commit and (optionally) open a PR or release

Commit messages drive `CHANGELOG.md` through git-cliff, and PR titles are gated
by the same Conventional Commits grammar.

**Constraints:**
- You MUST use a Conventional Commits prefix from the enforced grammar: `feat:`, `fix:`, `docs:`, `chore:`, `ci:`, `test:`, `refactor:`, or `build:`, because `.github/workflows/pr-title.yml` enforces it on PR titles and git-cliff maps the prefix to a changelog group
- You MUST NOT add a `Co-Authored-By:` trailer to commits, because the project owner has a standing preference against it (this overrides any default trailer behavior)
- You MUST NOT commit or push unless the user asked; if the user asks and you are on `main`, You MUST branch first, because `main` is the default branch
- You MUST NOT push the reproducible build artifacts listed in Step 3 (`cdk.out/`, `report.html`, `htmlcov/`, `.coverage*`, `site/`) because they are gitignored and regenerated from source; the only generated files you commit are the derived artifacts from Step 4 (`docs/openapi.json`, `uv.lock`, `lambda/requirements.txt`, snapshots)
- If you retired a workload-shape gate (for example added authentication and removed the `AwsSolutions-APIG4` suppression), You SHOULD update the corresponding item in `TODO.md`'s production-readiness checklist in the same change, so the checklist stays honest
- For a release, You SHOULD follow the README "Cutting a release" recipe (`git cliff --bumped-version`, `make lock`, annotated tag), because pushing the `vX.Y.Z` tag triggers `.github/workflows/release.yml`

## Examples

### Example 1: A pure Lambda code change

**Input:**
- change_description: "Return an ISO-8601 timestamp field alongside the greeting message."
- change_type: "lambda"
- deploy_target: "none"

**Expected Behavior:**
The agent verifies both venvs (Step 1), edits `lambda/service.py` and
`lambda/models.py` (adding the field to `GreetingResponse`), notes that the
response model changed and so runs `make openapi` and commits the updated
`docs/openapi.json` (Step 4), adds unit tests to keep `lambda/` coverage at 100%,
runs `make pr` to green (Step 6), and stops without deploying. The cdk-nag gate
is not exercised because no infrastructure changed.

### Example 2: An infrastructure change deployed to an ephemeral environment

**Input:**
- change_description: "Add a DynamoDB read-capacity CloudWatch alarm to the backend monitoring."
- change_type: "infrastructure"
- deploy_target: "alice-alarm-test"
- deploy_region: "us-east-1"

**Expected Behavior:**
The agent edits `_build_monitoring` in `infrastructure/backend_app.py`, runs
`make cdk-synth` with a container runtime (Finch or Docker) up and resolves or acknowledges any new cdk-nag
finding with a scoped reason (Step 5), runs `make pr` (Step 6), deploys with
`make deploy ENV=alice-alarm-test` (Step 7), verifies the alarm exists in the
console/CLI, then tears the environment down with
`make destroy-clean ENV=alice-alarm-test REGION=us-east-1` (Step 8). It does not
enable `appconfig_monitor`.

### Example 3: A dependency bump

**Input:**
- change_description: "Bump aws-lambda-powertools to the latest patch."
- change_type: "dependencies"
- deploy_target: "none"

**Expected Behavior:**
The agent edits the pin in `pyproject.toml`'s `lambda` group, runs `make lock`
to regenerate `uv.lock` and `lambda/requirements.txt`, commits both, runs `make
pr` (which includes the `check-lock` drift gate and `pip-audit`), and stops. It
does not run a global pip install and does not touch `.venv*` directly.

## Troubleshooting

### `cdk synth` exits 0 but the stack has nag findings
This is expected — the CLI exit code is not the gate for a Python CDK app.
Always run `make cdk-synth`, which pairs the synth with
`scripts/check_validation_report.py`. If that script reports violations, fix or
acknowledge them (Step 5). If it reports a *missing* report, the packs did not
attach — check that `attach_nag_packs` still runs in `app.py`.

### A bare `AwsSolutions-IAM5` acknowledgment does not clear the finding
cdk-nag v3 matches granular IAM4/IAM5 findings individually. Read the exact
finding id from the gate's failure output and pass it as `applies_to`. If the id
contains more than one `::` (for example an IAM4 managed-policy ARN),
`acknowledge_rules` routes it through the `aws:cdk:acknowledged-rules` metadata
fallback automatically — use the helper, do not call the acknowledge API directly.

### `make cdk-synth` fails to bundle the Lambda
No container runtime is running. Start Finch (`finch vm start && export CDK_DOCKER=finch`) or Docker, or run every other gate and explicitly note
that `cdk-synth` was skipped. Do not report a full green when the synth gate did
not run.

### `pytest tests/cdk` fails on a coverage gate when run directly
Run `make test-cdk` (or `make pr`), not bare `pytest`. The global `addopts` in
`pyproject.toml` hardcodes a 100% `lambda/` coverage gate that only makes sense
for the unit suite; the make targets pass `--override-ini="addopts="` to drop it.

### The first deploy hangs and rolls back on the AppConfig deployment
`appconfig_monitor=true` was set on a cold stack. Its alarm starts
`INSUFFICIENT_DATA`, which AppConfig treats as a rollback signal. Deploy once
with the default (all-at-once, no monitor), let the metric report, then enable
the monitor via `make deploy-appconfig-monitor`.

### `cdk deploy` reports "No stack found in the main cloud assembly"
The `'**'` glob is missing. All five stacks live inside `AppStage` (a
`cdk.Stage`); bare `cdk deploy` walks only the App's direct children. Use the
`make deploy` / `make destroy` targets, which include the glob.

### A dangling log group or dashboard survived `cdk destroy`
Use `make destroy-clean` (scoped with `ENV=`/`REGION=`) for teardown — it sweeps
async-re-created log groups. If you added a service that creates supporting
resources outside CloudFormation, add a cleanup `cr.AwsCustomResource` per the
`RumLogGroupCleanup` pattern.

### CI fails on `docs/openapi.json` or `lambda/requirements.txt` drift
You edited a route/model or a dependency without regenerating the committed
artifact. Run `make openapi` (for the spec) or `make lock` (for the lockfile and
requirements export) and commit the result.

### `test-cdk` fails on `TestLogicalIdStability`
A stateful construct's logical ID changed — usually because a construct was
renamed or re-scoped. That means resource replacement and data loss on deploy.
Revert the rename, or, if the change is genuinely intended, update the expected
logical ID in the committed list in the same commit so the intent is reviewable.

### A snapshot test reports a diff
`tests/cdk/test_snapshots.py` is a tripwire, not a baseline to auto-bless. Review
the diff first: a removed resource, a flipped default, or an accidental property
change means fix the code. Only if the change is intended do you regenerate with
`UPDATE_SNAPSHOTS=1 make test-cdk` — and pair it with the matching
`test_stacks.py` assertion change (Step 4).

### You want to step-debug the Lambda handler
Do not reach for `sam local invoke` — it injects an unpinned `debugpy` that
conflicts with the project's hash-pinned (`--require-hashes`) install of
`lambda/requirements.txt`. Instead set a breakpoint in `lambda/app.py`, open a
`tests/unit/` test that exercises the path, and run the "Pytest: Current File"
F5 config — the handler runs in-process with all of Powertools' real machinery.
Runtime-only behavior (cold starts, IAM, real AWS calls) still needs the deployed
function and its observability stack.
