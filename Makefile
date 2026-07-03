.DEFAULT_GOAL := help

# =============================================================================
# Two-environment model
# =============================================================================
# CDK and Lambda Powertools require incompatible `attrs` versions (CDK pulls
# attrs<26 via jsii; Powertools pulls attrs>=26). uv locks both resolutions
# in a single uv.lock via `[tool.uv.conflicts]`, but each resolution must
# install into its own venv.
#
# Both venvs live at the PROJECT ROOT (relative paths, gitignored):
#
#   .venv         — CDK workstation: cdk + test + lint + docs groups
#   .venv-lambda  — Lambda runtime:  lambda + test + lint groups (unit tests, OpenAPI gen)
#
# Both are created automatically by `make install` (which runs `uv sync` for
# each group set into the right env). You do not pick the location — uv does,
# based on the directory `make` is invoked from. Each clone of this repo gets
# its own pair; nothing is shared across projects on disk.
#
# Check status with `make doctor`. Nuke and rebuild with `make clean-venvs &&
# make install`.
#
# The venv selector uses the UV_PROJECT_ENVIRONMENT env var that uv honours
# natively — no activation dance, no symlink juggling.
LAMBDA_ENV := UV_PROJECT_ENVIRONMENT=.venv-lambda
LAMBDA_RUN := $(LAMBDA_ENV) uv run

# CDK CLI comes from package.json via npx (installed by `npm ci` in `make
# install`), not a global `npm install -g`. The global route left the CLI as
# the one un-pinned supply-chain input in an otherwise fully-locked repo, and
# let local and CI versions drift apart. Dependabot's npm ecosystem tracks the
# pin in package.json like every Python dependency.
CDK := npx cdk

# Deployment environment for the env-aware targets below. Empty (the default)
# targets the long-lived prod stacks with their legacy names. Set ENV to spin
# up/tear down a namespaced, collision-free copy of all five stacks — e.g.
# `make deploy ENV=alice-feature-x` — for per-developer or per-branch work in
# a shared account. Non-prod environments keep dashboards and alarms but skip
# the SNS alarm topic so an ephemeral stack never pages anyone. See app.py.
ENV ?=
ENVSEG := $(if $(ENV),-$(ENV))
CDK_ENV_ARG := $(if $(ENV),-c env=$(ENV))

.PHONY: help install install-cdk install-lambda doctor test test-cdk test-integration coverage \
	lint lint-docs format typecheck security check-lock pr \
	cdk-synth cdk-notices cdk-deprecations \
	cdk-ls cdk-diff cdk-drift cdk-revert-drift cdk-diagnose cdk-gc cdk-rollback \
	deploy deploy-appconfig-monitor destroy destroy-clean _empty-frontend-buckets _delete-straggler-log-groups \
	docs docs-open docs-serve openapi compare-openapi coverage coverage-badge lock upgrade deps-merge clean clean-venvs

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# =============================================================================
# Environment setup
# =============================================================================

install: install-cdk install-lambda ## Install both environments, node tooling (CDK CLI), and pre-commit hooks
	npm ci
	.venv/bin/pre-commit install

# --locked mirrors CI (.github/workflows/ci.yml). Without it, a stale uv.lock
# would silently install whatever the resolver picks today, which can drift
# from CI's pinned set. With it, `make install` after a pyproject.toml edit
# will fail until `make lock` is run, which is the desired contract.
install-cdk: ## Install the CDK workstation env into .venv (cdk + test + lint + docs)
	uv sync --locked --group cdk --group test --group lint --group docs

# The lint group rides into .venv-lambda alongside the runtime so `make
# typecheck` can run mypy in this env over lambda/ and scripts/ (where
# Powertools is importable but aws-cdk-lib is not). Without lint here,
# mypy is missing entirely and the typecheck target falls back to the
# weaker .venv-side check that treats Powertools as Any.
install-lambda: ## Install the Lambda runtime env into .venv-lambda (lambda + test + lint)
	$(LAMBDA_ENV) uv sync --locked --only-group lambda --only-group test --only-group lint

# =============================================================================
# Diagnostics
# =============================================================================

doctor: ## Diagnostic snapshot — uv/cdk/drawio versions, venv state, pre-commit wiring
	@echo "=== Toolchain ==="
	@command -v uv >/dev/null 2>&1 && printf "uv:          %s\n" "$$(uv --version)" || echo "uv:          MISSING — install from https://docs.astral.sh/uv/"
	@command -v npm >/dev/null 2>&1 && printf "npm:         %s\n" "$$(npm --version)" || echo "npm:         MISSING — install Node.js (the CDK CLI is an npm package)"
	@npx --no-install cdk --version >/dev/null 2>&1 && printf "cdk CLI:     %s (pinned via package.json)\n" "$$(npx --no-install cdk --version)" || echo "cdk CLI:     MISSING — run 'npm ci' (or 'make install')"
	@command -v drawio >/dev/null 2>&1 && printf "drawio:      %s\n" "$$(drawio --version)" || echo "drawio:      MISSING (optional) — 'brew install --cask drawio' for diagram exports"
	@echo
	@echo "=== Virtual environments (project-local, gitignored) ==="
	@if [ -x .venv/bin/python ]; then \
		printf ".venv:        %s\n" "$$(.venv/bin/python --version)"; \
		.venv/bin/python -c "import aws_cdk" 2>/dev/null && echo "              [OK] CDK group installed" || echo "              [X]  CDK group missing — run 'make install-cdk'"; \
	else \
		echo ".venv:        NOT CREATED — run 'make install-cdk' or 'make install'"; \
	fi
	@if [ -x .venv-lambda/bin/python ]; then \
		printf ".venv-lambda: %s\n" "$$(.venv-lambda/bin/python --version)"; \
		.venv-lambda/bin/python -c "import aws_lambda_powertools" 2>/dev/null && echo "              [OK] Lambda runtime group installed" || echo "              [X]  Lambda runtime group missing — run 'make install-lambda'"; \
	else \
		echo ".venv-lambda: NOT CREATED — run 'make install-lambda' or 'make install'"; \
	fi
	@echo
	@echo "=== Pre-commit hooks ==="
	@if [ -f .git/hooks/pre-commit ]; then \
		echo "Installed:   [OK] .git/hooks/pre-commit present"; \
	else \
		echo "Installed:   [X]  not wired — run 'make install' (or '.venv/bin/pre-commit install')"; \
	fi

# =============================================================================
# Testing
# =============================================================================

test: ## Run unit tests with coverage (uses .venv-lambda — needs Powertools)
	$(LAMBDA_RUN) pytest tests/unit -v

test-cdk: ## Run CDK stack assertion tests (uses .venv — needs CDK)
	uv run pytest tests/cdk -v --override-ini="addopts=" --timeout=120

test-integration: ## Run integration tests against a deployed stack (uses .venv-lambda)
	# --override-ini drops the project-wide --cov-fail-under=100 gate (which
	# only makes sense for unit tests over lambda/) so integration tests don't
	# fail the run on coverage instead of behavior. Mirrors test-cdk's pattern.
	# --timeout=120 lifts the 30s per-test cap from pyproject (an ini option,
	# NOT part of addopts, so the override above does not touch it): the
	# warm-latency test makes 4 sequential HTTP calls with 10s client timeouts
	# and can exceed 30s on a degraded network without anything being wrong.
	$(LAMBDA_RUN) pytest tests/integration -v --override-ini="addopts=" --timeout=120

# Combined coverage across BOTH venvs in one report. The editor's Test panel
# can't produce a single cross-venv number on its own — each "Run with
# Coverage" loads one .coverage data file, and the multi-root global run's
# per-folder line highlighting is subject to an upstream bug
# (microsoft/vscode-python#25643). This target sidesteps both: it runs the CDK
# suite under .venv and the unit suite under .venv-lambda, each appending into
# ONE shared .coverage (--cov-append, no erase between runs), then renders a
# single HTML report spanning infrastructure/ (covered by the CDK tests) and
# lambda/ (covered by the unit tests). --override-ini=addopts= drops the global
# unit-only flags (--cov=lambda, the 100% gate, -n auto) so this run sets its
# own --cov targets and is NOT gated — the combined total is informational
# (infrastructure/ carries intentional uncovered defensive lines), while the 100%
# lambda/ gate stays enforced by `make test` and CI. Integration tests are
# excluded: they need a live stack. coverage is invoked via `python -m` so it
# resolves from each venv's pytest-cov install without relying on a console
# script on PATH.
# Two validation-aspect tests read a *populated* error annotation
# (Annotations.find_error returning a hit). That trips a known jsii bug, but ONLY
# under coverage instrumentation: MetadataEntry is a struct the kernel can return
# by-reference, and jsii's interface registry then can't resolve it
# (KeyError: aws-cdk-lib.cloud_assembly_schema.MetadataEntry). Under --cov it is
# effectively deterministic (all retry attempts failed identically), so those two
# tests are DESELECTED from the coverage run only. They still run — and gate
# correctness — in the cdk-check CI job, which runs the full tests/cdk suite
# without --cov and never hits the bug. Full write-up: README "Design decisions
# and known limitations". The tradeoff is a negligible dip in the informational
# badge (the aspect's error-emitting branch isn't exercised under --cov); that
# branch is still run by cdk-check's full no-coverage suite.
_CDK_COV_DESELECT = --deselect "tests/cdk/test_validation_aspects.py::TestRemovalPolicyInvariant::test_raw_l1_bucket_without_removal_policy_errors" --deselect "tests/cdk/test_validation_aspects.py::TestLogRetentionInvariant::test_log_group_without_retention_errors"
_CDK_COV = uv run pytest tests/cdk --override-ini="addopts=" --cov=infrastructure --cov=lambda --cov-branch --cov-append -q $(_CDK_COV_DESELECT)

coverage: ## Combined coverage report across both venvs (infrastructure/ + lambda/), opens HTML
	rm -f .coverage
	$(_CDK_COV)
	$(LAMBDA_RUN) pytest tests/unit --override-ini="addopts=" --cov=infrastructure --cov=lambda --cov-branch --cov-append -q
	uv run python -m coverage report
	uv run python -m coverage html
	open htmlcov/index.html

# In-house coverage badge — the deliberate alternative to Codecov (a third-party
# SaaS this repo skips). Runs the same combined cross-venv coverage as `make
# coverage`, then writes a shields.io "endpoint" JSON ({label, message, color}).
# The docs workflow publishes that JSON to our own GitHub Pages, and the README
# badge points img.shields.io/endpoint at it — so coverage DATA never leaves our
# infrastructure; shields only renders the three fields, exactly as it already
# does for the Python/Docs/License badges. No new dependency: the percentage
# comes from coverage.py's built-in JSON report. --override-ini drops the global
# unit-only flags so this run sets its own --cov targets and isn't gated (the
# combined ~96% is informational; the 100% lambda/ gate stays in `make test`/CI).
COVERAGE_BADGE_JSON ?= coverage-badge.json
coverage-badge: ## Generate the shields-endpoint coverage badge JSON (whole repo: infrastructure/ + lambda/)
	rm -f .coverage .coverage.json
	$(_CDK_COV)
	$(LAMBDA_RUN) pytest tests/unit --override-ini="addopts=" --cov=infrastructure --cov=lambda --cov-branch --cov-append -q
	uv run python -m coverage json -o .coverage.json
	uv run python -c 'import json; t=round(json.load(open(".coverage.json"))["totals"]["percent_covered"]); c="brightgreen" if t>=95 else "green" if t>=90 else "yellow" if t>=75 else "red"; json.dump({"schemaVersion":1,"label":"coverage","message":str(t)+"%","color":c}, open("$(COVERAGE_BADGE_JSON)","w"))'
	@echo "Wrote $(COVERAGE_BADGE_JSON): $$(cat $(COVERAGE_BADGE_JSON))"

# =============================================================================
# Code quality
# =============================================================================

cdk-synth: ## Synthesize all CDK stacks and validate cdk-nag rules (CDK CLI via `npm ci` / `make install`)
	# The '**' glob descends into Stage-nested stacks. Without it, `cdk synth`
	# stops at the Stage manifest, the five nested stacks never synthesize,
	# and asset bundling silently doesn't run on them.
	#
	# The explicit report check is the cdk-nag v3 hard gate: CDK signals a
	# failed policy validation by setting process.exitCode in the NODE process,
	# which for a Python app is jsii's throwaway kernel — so `cdk synth` exits 0
	# even with findings (verified live; see scripts/check_validation_report.py).
	# The checker fails on any violation AND on a missing report (packs not
	# attached = broken gate, not a pass).
	$(CDK) synth '**' $(CDK_ENV_ARG)
	uv run python scripts/check_validation_report.py cdk.out

cdk-notices: ## Show AWS-published CDK notices (CVEs, deprecated CDK versions, upcoming breaking changes)
	$(CDK) notices

cdk-deprecations: ## List every deprecated CDK API used by any stack (synth output filtered for "deprecated")
	$(CDK) synth '**' $(CDK_ENV_ARG) 2>&1 | grep -i deprecat || echo "No deprecated CDK APIs in use"

cdk-ls: ## List all CDK stacks (uses '**' to descend into Stage-nested stacks)
	# Without '**', `cdk ls` stops at the top-level Stage manifest and
	# prints nothing useful. With it, the five nested stacks (Data, WAF,
	# Backend, Frontend, Audit) are listed — handy as a sanity check after
	# stack-graph refactors or when verifying the Stage wiring is intact.
	$(CDK) ls '**' $(CDK_ENV_ARG)

cdk-diff: ## Preview infra changes against deployed stacks (requires AWS credentials)
	# Same Stage-nesting trap as cdk-synth and deploy: bare `cdk diff`
	# walks only the App's direct children and reports no changes for the
	# five real stacks. Use this as the pre-PR companion to cdk-synth —
	# synth tells you cdk-nag is happy, diff tells you what would deploy.
	$(CDK) diff '**' $(CDK_ENV_ARG)

cdk-drift: ## Detect drift between deployed resources and what CDK last shipped (requires AWS credentials)
	# Surfaces resources mutated outside CDK — console edits, manual SDK
	# calls, neighbor-stack collisions. Load-bearing for this template's
	# encryption posture: CMK key policies, IAM grants, and CloudTrail
	# trail config are easy to silently drift and easy to miss.
	$(CDK) drift '**' $(CDK_ENV_ARG)

cdk-revert-drift: ## Deploy AND auto-revert out-of-band drift back to code (requires CDK CLI 2.1110.0+)
	# The remediation half of cdk-drift: where `cdk drift` only reports
	# resources mutated outside CDK, --revert-drift rolls them back to what
	# the code last shipped — in the same operation as any pending template
	# changes (CloudFormation's REVERT_DRIFT deployment mode). Self-healing
	# posture for this template's encryption invariants: a console-edited CMK
	# key policy or IAM grant snaps back to the committed state on deploy.
	#
	# Deliberately a separate, opt-in target rather than folded into `deploy`:
	# --revert-drift assumes code is always the source of truth, so it would
	# also undo a legitimate emergency console change made during an incident.
	# Keep the default `deploy` predictable; reach for this consciously after
	# `make cdk-drift` shows what would be reverted.
	$(CDK) deploy '**' $(CDK_ENV_ARG) --revert-drift --require-approval never

cdk-diagnose: ## Root-cause CloudFormation failures with construct paths and source locations (CDK 2.1120.0+)
	# The --unstable=diagnose flag gates the command while it's behind the
	# unstable feature flag; drop the flag once it graduates to stable.
	# Output maps CFN errors back to the construct and the file:line where
	# it was defined — designed to be parseable by AI agents as well as
	# humans. Substitute a specific stack name for '**' to narrow scope.
	$(CDK) --unstable=diagnose diagnose '**' $(CDK_ENV_ARG)

cdk-gc: ## Inspect (dry-run) unused Lambda/Docker assets in the CDK bootstrap S3/ECR repos
	# Every `cdk deploy` adds new Lambda zips and container images to the
	# CDKToolkit bootstrap bucket and ECR repo, but older revisions
	# accumulate forever. --action=print is dry-run only — it tags isolated
	# assets and reports what *would* be deleted on a subsequent run, but
	# deletes nothing. To actually GC, run `npx cdk --unstable=gc gc` directly:
	# the default (--action=full, --confirm=true) prompts interactively
	# before each deletion. --created-buffer-days=1 (default) skips assets
	# younger than a day; tune via --created-buffer-days=N for tighter
	# windows. The --unstable=gc flag gates the command while gc is behind
	# the unstable feature flag; drop it once gc graduates to stable.
	$(CDK) --unstable=gc gc --action=print

cdk-rollback: ## Roll deployed stacks back to their last stable state (use after a partial deploy failure)
	# Pairs with cdk-diagnose: when a deploy half-fails and CloudFormation
	# parks a stack in UPDATE_ROLLBACK_FAILED, this returns it to the
	# last good state without manual console intervention. Same '**' trap
	# as cdk-synth and friends — bare `cdk rollback` only sees the empty
	# Stage manifest.
	$(CDK) rollback '**' $(CDK_ENV_ARG)

# The '**' glob is required so CDK descends into the Stage-nested stacks —
# without it `cdk deploy` only sees the empty Stage manifest and exits with
# "No stack found in the main cloud assembly". --require-approval never
# skips the interactive IAM-change prompt; cdk-nag has already gated the
# change at synth time. Drop the flag for a manual review of every IAM diff.
deploy: ## Deploy all stacks to us-east-1 (ENV=<name> for an ephemeral env, -c region=X for other regions)
	$(CDK) deploy '**' $(CDK_ENV_ARG) --require-approval never

# Enable the opt-in AppConfig gradual rollout + alarm rollback monitor
# (-c appconfig_monitor=true). This is deliberately a SECOND-deploy operation:
# the monitor cannot create a cold stack — a fresh alarm starts INSUFFICIENT_DATA,
# which AppConfig treats as a rollback signal, so it aborts the first deploy (see
# README "Deployment safety"). The guard below queries CloudFormation and refuses
# unless the backend stack already exists in an updatable *_COMPLETE state, so
# this target can never BE the cold deploy. To turn the monitor back off, run a
# plain `make deploy` (reverts to all-at-once and removes the monitor).
# ENV=<name> / REGION=<region> select an ephemeral env / non-default region.
deploy-appconfig-monitor: ## Redeploy with the AppConfig gradual rollout + alarm rollback monitor (run only AFTER a first `make deploy`)
	@stack="ServerlessAppBackend$(if $(ENV),-$(ENV))-$(REGION)"; \
	status=$$(aws cloudformation describe-stacks --region $(REGION) --stack-name "$$stack" \
		--query 'Stacks[0].StackStatus' --output text 2>/dev/null); \
	case "$$status" in \
		CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) \
			echo "Backend stack $$stack is $$status — enabling the AppConfig gradual rollout + monitor."; ;; \
		"") \
			echo "ERROR: backend stack $$stack not found in $(REGION). Run 'make deploy' first — the AppConfig monitor cannot create a cold stack (see README 'Deployment safety')."; exit 1; ;; \
		ROLLBACK_COMPLETE) \
			echo "ERROR: backend stack $$stack is ROLLBACK_COMPLETE (a failed create). Delete it and run 'make deploy' before enabling the monitor."; exit 1; ;; \
		*) \
			echo "ERROR: backend stack $$stack is $$status, not an updatable *_COMPLETE state. Wait for a clean deploy before enabling the monitor."; exit 1; ;; \
	esac
	$(CDK) deploy '**' $(CDK_ENV_ARG) -c region=$(REGION) -c appconfig_monitor=true --require-approval never

# --force skips the interactive "are you sure?" prompt, mirroring how
# the deploy target uses --require-approval never. Without --force, the
# command fails outright in non-TTY contexts (CI, background shells)
# with "terminal is not attached so we are unable to get a confirmation".
# If you want the confirmation back for a one-off run, invoke cdk
# directly: `npx cdk destroy '**'`. Five stacks are destroyed independently
# — audit first (depends on the frontend buckets it audits), then frontend
# (consumes the WAF ARN), then backend, then data and WAF.
destroy: ## Destroy all stacks in us-east-1 (ENV=<name> for an ephemeral env, -c region=X for other regions)
	$(CDK) destroy '**' $(CDK_ENV_ARG) --force

# Region the frontend stack (and its log buckets) live in. Override to match a
# non-default deploy: `make destroy-clean REGION=ap-southeast-1`.
REGION ?= us-east-1

# Resolve every S3 bucket in the frontend AND audit stacks by type (names are
# CDK-generated, so we can't hardcode them) and empty each. Idempotent: a missing
# stack or empty bucket is a no-op. Used by destroy-clean below. ENVSEG folds the
# deployment environment into the stack name (ServerlessAppFrontend-<env>-<region>,
# ServerlessAppAudit-<env>-<region>) so an ephemeral env's teardown empties its own
# buckets, not prod's. (auto_delete_objects normally empties these on destroy;
# this is the belt-and-suspenders for a prior failed deploy that left one full.)
_empty-frontend-buckets:
	@echo "Emptying frontend- and audit-stack S3 buckets in $(REGION)..."
	@for s in "ServerlessAppFrontend$(ENVSEG)-$(REGION)" "ServerlessAppAudit$(ENVSEG)-$(REGION)"; do \
		for b in $$(aws cloudformation list-stack-resources \
			--stack-name "$$s" --region $(REGION) \
			--query "StackResourceSummaries[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
			--output text 2>/dev/null); do \
			echo "  emptying s3://$$b"; \
			aws s3 rm "s3://$$b" --recursive --region $(REGION) >/dev/null 2>&1 || true; \
		done; \
	done

# CloudWatch log delivery is asynchronous in the same way: the custom-resource
# provider and BucketDeployment Lambdas flush their final teardown logs AFTER
# CloudFormation deleted their (CMK-encrypted) log groups, and the Lambda service
# re-creates the configured group on delivery — leaving unencrypted,
# retention-less groups dangling after an otherwise-clean destroy (observed on a
# live teardown). Prefixes are scoped to the FULL stack names of the deployment
# being torn down — "ServerlessAppBackend$(ENVSEG)-$(REGION)" etc. for stack-named groups,
# "/aws/lambda/<stack-name>" for function groups, and "aws-waf-logs-<stack-name>"
# for WAF groups. The env segment in the prefix is what keeps multi-environment
# accounts safe: a bare "ServerlessApp" prefix would also sweep the log groups of
# every OTHER deployment environment still running in the account.
# WAF-stack-derived groups are swept in us-east-1 too because the WAF stack
# always lives there regardless of REGION. Idempotent; missing groups are no-ops.
#
# KNOWN GAP these prefixes cannot close (handled by the snapshot pass below):
# CloudFormation composes Lambda physical names as {stack-name}-{logical-id}-
# {suffix} truncated to 64 chars, and the truncation cuts the STACK-NAME
# PORTION mid-word — a live teardown left
# "/aws/lambda/ServerlessAppFrontend-us-eas-CustomS3AutoDeleteObject-…" behind
# ("us-eas", not "us-east-1"), which no full-stack-name prefix can match.
_delete-straggler-log-groups:
	@echo "Sweeping straggler CloudWatch log groups..."
	@for base in "ServerlessAppBackend$(ENVSEG)-$(REGION)" "ServerlessAppFrontend$(ENVSEG)-$(REGION)" "ServerlessAppAudit$(ENVSEG)-$(REGION)"; do \
		for prefix in "$$base" "/aws/lambda/$$base" "aws-waf-logs-$$base"; do \
			for lg in $$(aws logs describe-log-groups --log-group-name-prefix "$$prefix" \
				--region $(REGION) --query "logGroups[].logGroupName" --output text 2>/dev/null); do \
				echo "  deleting $$lg ($(REGION))"; \
				aws logs delete-log-group --log-group-name "$$lg" --region $(REGION) 2>/dev/null || true; \
			done; \
		done; \
	done
	@for prefix in "ServerlessAppWaf$(ENVSEG)-$(REGION)" "/aws/lambda/ServerlessAppWaf$(ENVSEG)-$(REGION)" "aws-waf-logs-ServerlessAppWaf$(ENVSEG)-$(REGION)"; do \
		for lg in $$(aws logs describe-log-groups --log-group-name-prefix "$$prefix" \
			--region us-east-1 --query "logGroups[].logGroupName" --output text 2>/dev/null); do \
			echo "  deleting $$lg (us-east-1)"; \
			aws logs delete-log-group --log-group-name "$$lg" --region us-east-1 2>/dev/null || true; \
		done; \
	done

# Where the pre-destroy log-group snapshot is written ("<region> <name>" lines).
# Env+region-scoped filename so concurrent teardowns of different deployments
# never clobber each other's snapshots.
LOG_GROUP_SNAPSHOT := /tmp/log-group-snapshot$(ENVSEG)-$(REGION).txt

# Records the exact physical names of every CFN-owned log group in the
# target-region stacks BEFORE destroy. This is what makes the truncated-name gap
# above closeable: prefixes can't reconstruct a mid-word-truncated function name,
# but CloudFormation knows each group's exact physical ID while the stack
# still exists. Missing stacks contribute nothing (fresh teardown re-runs are
# no-ops). The WAF stack is queried in us-east-1 (it always lives there).
_snapshot-log-groups:
	@echo "Snapshotting CFN-owned log groups (for the post-destroy exact-name sweep)..."
	@: > $(LOG_GROUP_SNAPSHOT)
	@for s in "ServerlessAppBackend$(ENVSEG)-$(REGION)" "ServerlessAppFrontend$(ENVSEG)-$(REGION)" "ServerlessAppAudit$(ENVSEG)-$(REGION)"; do \
		aws cloudformation list-stack-resources --stack-name "$$s" --region $(REGION) \
			--query "StackResourceSummaries[?ResourceType=='AWS::Logs::LogGroup'].PhysicalResourceId" \
			--output text 2>/dev/null | tr '\t' '\n' | sed "s/^/$(REGION) /" >> $(LOG_GROUP_SNAPSHOT) || true; \
	done
	@aws cloudformation list-stack-resources --stack-name "ServerlessAppWaf$(ENVSEG)-$(REGION)" --region us-east-1 \
		--query "StackResourceSummaries[?ResourceType=='AWS::Logs::LogGroup'].PhysicalResourceId" \
		--output text 2>/dev/null | tr '\t' '\n' | sed "s/^/us-east-1 /" >> $(LOG_GROUP_SNAPSHOT) || true
	@echo "  $$(wc -l < $(LOG_GROUP_SNAPSHOT) | tr -d ' ') log group(s) snapshotted"

# Deletes any snapshotted group that exists again after destroy — i.e. the
# groups async log delivery re-created under their exact pre-destroy names,
# including the truncated-function-name ones the prefix sweep can't see.
# Exact names only; cannot touch any other deployment's groups by construction.
_delete-snapshotted-log-groups:
	@echo "Sweeping re-created CFN-owned log groups by exact name..."
	@if [ -s $(LOG_GROUP_SNAPSHOT) ]; then \
		while read -r region lg; do \
			[ -n "$$lg" ] || continue; \
			aws logs delete-log-group --log-group-name "$$lg" --region "$$region" 2>/dev/null \
				&& echo "  deleting $$lg ($$region)" || true; \
		done < $(LOG_GROUP_SNAPSHOT); \
	else \
		echo "  no snapshot found ($(LOG_GROUP_SNAPSHOT)) — skipping"; \
	fi

# CloudFront / S3 / CloudTrail log delivery is ASYNCHRONOUS, so a log file can land
# in the access-log (or CloudTrail) bucket AFTER cdk's auto_delete_objects empties
# it during teardown — leaving DeleteBucket with a 409 "bucket not empty" and the
# stack in DELETE_FAILED. This target empties the frontend log buckets first to
# shrink that window, then destroys; if a straggler log still lands while the
# CloudFront distribution is deleting (which takes minutes), it empties once more
# and retries. After destroy, straggler CloudWatch log groups (re-created by
# late async log delivery — see _delete-straggler-log-groups) are swept.
# Re-running the whole target is always safe — every step is idempotent.
# The retry block invokes make via the shell's $$MAKE (exported by make into
# every recipe environment), NOT the literal $(MAKE) variable reference. The
# distinction is load-bearing: make executes any recipe line containing
# $(MAKE)/$ {MAKE} even under -n, so with the literal form a "dry-run"
# `make -n destroy-clean` would have REALLY run `cdk destroy` against the
# live stacks (observed; the recipe line is one shell command, so the destroy
# rides along with the recursive call). $$MAKE escapes make's recursive-line
# scan, making -n print this line instead of executing it.
destroy-clean: ## Empty async-log buckets, destroy all stacks, sweep straggler log groups. REGION=us-east-1 default.
	@$(MAKE) _snapshot-log-groups REGION=$(REGION) ENV=$(ENV)
	@$(MAKE) _empty-frontend-buckets REGION=$(REGION) ENV=$(ENV)
	$(CDK) destroy '**' $(CDK_ENV_ARG) --force -c region=$(REGION) || { \
		echo "destroy hit a late-arriving log straggler — emptying again and retrying once..."; \
		"$$MAKE" _empty-frontend-buckets REGION=$(REGION) ENV=$(ENV); \
		$(CDK) destroy '**' $(CDK_ENV_ARG) --force -c region=$(REGION); \
	}
	@$(MAKE) _delete-straggler-log-groups REGION=$(REGION) ENV=$(ENV)
	@$(MAKE) _delete-snapshotted-log-groups REGION=$(REGION) ENV=$(ENV)

lint: ## Run all pre-commit hooks (ruff, mypy, pylint, bandit, xenon, pip-audit)
	uv run pre-commit run --all-files

lint-docs: ## Lint Markdown files (README, TODO, docs/) with markdownlint
	# Rules live in .markdownlint.yaml. CHANGELOG.md is excluded — it is
	# generated by git-cliff, so style nits there are fixed in cliff.toml,
	# not by hand-editing generated output.
	npx markdownlint --config .markdownlint.yaml "*.md" "docs/**/*.md" --ignore CHANGELOG.md

format: ## Format code with ruff
	uv run ruff format .

# Mirrors the lambda/requirements.txt drift gate in .github/workflows/ci.yml:
# Dependabot's uv ecosystem regenerates pyproject.toml + uv.lock but does not
# know about the exported requirements file that PythonFunction bundles into
# the deployed Lambda. Run locally before pushing a dependency change.
check-lock: ## Verify lambda/requirements.txt is in sync with uv.lock (fix with `make lock`)
	@uv export --only-group lambda --no-emit-project --no-header --format requirements.txt -o /tmp/expected-requirements.txt
	@diff -q /tmp/expected-requirements.txt lambda/requirements.txt >/dev/null \
		&& echo "lambda/requirements.txt is in sync with uv.lock" \
		|| { echo "lambda/requirements.txt is OUT OF SYNC with uv.lock — run 'make lock' and commit the result"; \
			diff /tmp/expected-requirements.txt lambda/requirements.txt || true; exit 1; }

# One-shot local mirror of everything CI gates on, in CI's order: the
# requirements drift check, every pre-commit hook (ruff/mypy/pylint/bandit/
# xenon/pip-audit), both-venv typechecking, markdown lint, unit tests with the
# 100% coverage gate, the CDK assertion suite (including the in-process
# cdk-nag annotations gate), the authoritative CLI synth (needs Docker for
# Lambda bundling), and the committed-OpenAPI drift check. Run before pushing;
# a clean `make pr` should mean a green CI run.
pr: check-lock lint typecheck lint-docs test test-cdk cdk-synth compare-openapi ## Run every CI gate locally (lint, typecheck, tests, synth, OpenAPI drift)
	@echo "All local CI gates passed."

typecheck: ## Run mypy type checking (CDK side in .venv, Lambda runtime + scripts in .venv-lambda)
	# .venv has aws-cdk-lib + boto3-stubs but not Powertools (attrs conflict),
	# so it checks the CDK construct code only. .venv-lambda has Powertools
	# and lint tooling, so it checks the Lambda handler and the scripts/
	# helpers that import from it (notably scripts/generate_openapi.py).
	# The pre-commit mypy hook holds the CDK side and excludes scripts/ for
	# the same reason — see .pre-commit-config.yaml.
	uv run mypy infrastructure/
	$(LAMBDA_RUN) mypy lambda/ scripts/

security: ## Run bandit security scan and pip-audit vulnerability check
	# scripts/ is included to match the typecheck target and the pre-commit bandit
	# hook; bandit only scans .py files, so the shell scripts are harmlessly ignored.
	uv run bandit -r lambda/ infrastructure/ scripts/
	# pip-audit goes through the pre-commit hook so the --ignore-vuln list
	# (currently CVE-2026-3219 — pip 26.0.1, no upstream fix) is sourced
	# from .pre-commit-config.yaml. Invoking pip-audit directly here would
	# duplicate the suppression list and silently drift when the upstream
	# fix lands.
	uv run pre-commit run pip-audit --all-files

# =============================================================================
# Documentation
# =============================================================================
#
# The OpenAPI generator imports lambda/app.py, which requires Powertools —
# so it runs in .venv-lambda. Zensical itself is only installed in .venv
# (the docs group), so the build step runs in .venv.

docs: ## Build Zensical HTML documentation (regenerates the OpenAPI spec first)
	$(LAMBDA_RUN) python scripts/generate_openapi.py
	uv run zensical build

docs-open: docs ## Build and open documentation in browser
	open site/index.html

docs-serve: ## Regenerate OpenAPI spec and start the Zensical dev server with hot reload
	$(LAMBDA_RUN) python scripts/generate_openapi.py
	uv run zensical serve

openapi: ## Regenerate the committed OpenAPI spec (docs/openapi.json) from lambda/app.py
	# The spec is COMMITTED (not just a docs-build artifact) so PR diffs show
	# API-contract changes and CI can gate on drift and breaking changes.
	# Run this after touching routes, models, or response metadata.
	$(LAMBDA_RUN) python scripts/generate_openapi.py

compare-openapi: ## Fail if the committed docs/openapi.json is stale (regenerate with `make openapi`)
	# Mirrors the CI drift gate: regenerate into a temp location and compare
	# byte-for-byte with the committed spec. Generation is hermetic (the
	# generator pins its own env vars), so any diff means the code changed
	# without `make openapi` being run.
	$(LAMBDA_RUN) python scripts/generate_openapi.py --out-path /tmp/openapi-latest.json
	@cmp --silent /tmp/openapi-latest.json docs/openapi.json \
		&& echo "docs/openapi.json is up to date" \
		|| { echo "docs/openapi.json is STALE — run 'make openapi' and commit the result"; \
			diff /tmp/openapi-latest.json docs/openapi.json || true; exit 1; }

# =============================================================================
# Dependency management
# =============================================================================
#
# COOLDOWN_DAYS gates `make upgrade` against PyPI versions uploaded in the last
# N days. This is the local mirror of the Dependabot cooldown — it defends
# laptop-side dependency upgrades against fresh malicious releases (xz-utils /
# nx / tj-actions class incidents). The cooldown only applies to `upgrade`,
# not `lock`: `lock` reproduces decisions already encoded in pyproject.toml
# and the existing uv.lock, while `upgrade` is where brand-new versions
# enter the project and is the only place a fresh malicious release can land.
#
# Override at the command line: `make upgrade COOLDOWN_DAYS=14`.
COOLDOWN_DAYS ?= 7
# Lazy ('=' not ':=') so the python3 subshell only runs when the recipe that
# expands $(COOLDOWN_CUTOFF) actually fires. Otherwise every `make help` /
# `make test` invocation pays the python startup cost up-front.
# Bare `python3` (not `uv run python`) is intentional: this is a stdlib-only date
# calc, system python3 is already a documented prerequisite, and avoiding `uv run`
# preserves the lazy-eval startup savings above. python3's datetime is also more
# portable than shelling out to `date`, whose flags differ between macOS (-v) and
# GNU/Linux (-d) — this repo is developed on both.
COOLDOWN_CUTOFF = $(shell python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=$(COOLDOWN_DAYS))).strftime("%Y-%m-%dT00:00:00Z"))')

lock: ## Regenerate uv.lock and lambda/requirements.txt from pyproject.toml
	uv lock
	uv export --only-group lambda --no-emit-project --no-header --format requirements.txt -o lambda/requirements.txt

upgrade: ## Upgrade all dependencies to latest versions older than COOLDOWN_DAYS days
	uv lock --upgrade --exclude-newer $(COOLDOWN_CUTOFF)
	uv export --only-group lambda --no-emit-project --no-header --format requirements.txt -o lambda/requirements.txt
	# pre-commit hook revs and the npm-side pins (CDK CLI, markdownlint) ride
	# along so one command refreshes every dependency surface. NOTE: neither
	# pre-commit autoupdate nor npm honours the PyPI cooldown above — those
	# bumps land at whatever upstream just released. Dependabot's cooldown
	# still applies to its own PRs; for a cooldown-conscious local refresh,
	# review these two diffs (release dates) before committing.
	uv run pre-commit autoupdate
	npm update --save-dev
	@echo "Upgraded: uv.lock, lambda/requirements.txt, .pre-commit-config.yaml revs, package(-lock).json"

# Wrapper around scripts/deps_merge.sh — see the file header for the full
# step list. Pass PR=N to handle a single PR; omit to process every open
# Dependabot PR sequentially. Sequential is required because each `make lock`
# regenerates uv.lock, and concurrent processing would have later PRs clobber
# earlier ones during squash-merge.
deps-merge: ## Process Dependabot PRs (rebase + lock + push + arm auto-merge). Use PR=N for one, omit for all open.
	@bash scripts/deps_merge.sh $(PR)

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Remove build artifacts, caches, and coverage files (preserves venvs)
	# .coverage* (glob, not bare .coverage) also catches the ".coverage 2"-style
	# suffixed files pytest-cov leaves behind when parallel runs race on the name.
	rm -rf site htmlcov .coverage* report.html coverage-badge.json .pytest_cache .mypy_cache .ruff_cache cdk.out
	find . -type d -name __pycache__ -exec rm -rf {} +

# Separate from `clean` because re-installing both venvs takes minutes (CDK
# bundle, all groups) and is not something you want in a routine cache reset.
# When you DO need a fresh install (lockfile changes that uv refuses to
# reconcile, corrupted venv, switching Python versions), run this then
# `make install`.
clean-venvs: ## Wipe .venv and .venv-lambda (separate from `clean` which preserves them)
	rm -rf .venv .venv-lambda
	@echo "Venvs removed. Run 'make install' to recreate."
