.DEFAULT_GOAL := help

# =============================================================================
# Two-environment model
# =============================================================================
# CDK and Lambda Powertools require incompatible `attrs` versions (CDK pulls
# attrs<26 via jsii; Powertools pulls attrs>=26). uv locks both resolutions
# in a single uv.lock via `[tool.uv.conflicts]`, but each resolution must
# install into its own venv.
#
#   .venv         — CDK workstation: cdk + test + lint + docs groups
#   .venv-lambda  — Lambda runtime:  lambda + test groups (unit tests, OpenAPI gen)
#
# The venv selector uses the UV_PROJECT_ENVIRONMENT env var that uv honours
# natively — no activation dance, no symlink juggling.
LAMBDA_ENV := UV_PROJECT_ENVIRONMENT=.venv-lambda
LAMBDA_RUN := $(LAMBDA_ENV) uv run

.PHONY: help install install-cdk install-lambda test test-cdk test-integration \
	lint format typecheck security cdk-synth cdk-notices cdk-deprecations \
	docs docs-open docs-serve lock upgrade clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# =============================================================================
# Environment setup
# =============================================================================

install: install-cdk install-lambda ## Install both environments and pre-commit hooks
	.venv/bin/pre-commit install

install-cdk: ## Install the CDK workstation env into .venv (cdk + test + lint + docs)
	uv sync --group cdk --group test --group lint --group docs

install-lambda: ## Install the Lambda runtime env into .venv-lambda (lambda + test)
	$(LAMBDA_ENV) uv sync --only-group lambda --only-group test

# =============================================================================
# Testing
# =============================================================================

test: ## Run unit tests with coverage (uses .venv-lambda — needs Powertools)
	$(LAMBDA_RUN) pytest tests/unit -v

test-cdk: ## Run CDK stack assertion tests (uses .venv — needs CDK)
	uv run pytest tests/cdk -v --override-ini="addopts=" --timeout=120

test-integration: ## Run integration tests against a deployed stack (uses .venv-lambda)
	$(LAMBDA_RUN) pytest tests/integration -v

# =============================================================================
# Code quality
# =============================================================================

cdk-synth: ## Synthesize all CDK stacks and validate cdk-nag rules (requires CDK CLI: npm install -g aws-cdk)
	cdk synth

cdk-notices: ## Show AWS-published CDK notices (CVEs, deprecated CDK versions, upcoming breaking changes)
	cdk notices

cdk-deprecations: ## List every deprecated CDK API used by any stack (synth output filtered for "deprecated")
	cdk synth 2>&1 | grep -i deprecat || echo "No deprecated CDK APIs in use"

lint: ## Run all pre-commit hooks (ruff, mypy, pylint, bandit, xenon, pip-audit)
	uv run pre-commit run --all-files

format: ## Format code with ruff
	uv run ruff format .

typecheck: ## Run mypy type checking
	uv run mypy lambda/ hello_world/

security: ## Run bandit security scan and pip-audit vulnerability check
	uv run bandit -r lambda/ hello_world/
	uv run pip-audit

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
COOLDOWN_CUTOFF := $(shell python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=$(COOLDOWN_DAYS))).strftime("%Y-%m-%dT00:00:00Z"))')

lock: ## Regenerate uv.lock and lambda/requirements.txt from pyproject.toml
	uv lock
	uv export --only-group lambda --no-emit-project --format requirements.txt -o lambda/requirements.txt

upgrade: ## Upgrade all dependencies to latest versions older than COOLDOWN_DAYS days
	uv lock --upgrade --exclude-newer $(COOLDOWN_CUTOFF)
	uv export --only-group lambda --no-emit-project --format requirements.txt -o lambda/requirements.txt

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Remove build artifacts, caches, and coverage files
	rm -rf site htmlcov .coverage report.html .pytest_cache .mypy_cache .ruff_cache cdk.out
	find . -type d -name __pycache__ -exec rm -rf {} +
