.DEFAULT_GOAL := help
PYTHON := python3
VENV := .venv
PIP := pip

.PHONY: help install install-dev test test-cdk test-integration lint format typecheck security cdk-synth docs docs-open compile upgrade clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# =============================================================================
# Environment setup
# =============================================================================

install: ## Install dev dependencies (CDK, linting, type checking)
	$(PIP) install pip-tools
	pip-sync requirements.txt
	$(PIP) install -r tests/requirements.txt -r lambda/requirements.txt
	pre-commit install

install-dev: ## Install dev dependencies only (no test/lambda deps)
	$(PIP) install pip-tools
	pip-sync requirements.txt
	pre-commit install

# =============================================================================
# Testing
# =============================================================================

test: ## Run unit tests with coverage
	$(PYTHON) -m pytest tests/unit -v

test-cdk: ## Run CDK stack assertion tests (requires aws_cdk — use make install, not make install-dev)
	$(PYTHON) -m pytest tests/unit/test_stacks.py -v --override-ini="addopts=" --timeout=120

test-integration: ## Run integration tests (requires deployed stack)
	$(PYTHON) -m pytest tests/integration -v

# =============================================================================
# Code quality
# =============================================================================

cdk-synth: ## Synthesize all CDK stacks and validate cdk-nag rules (requires CDK CLI: npm install -g aws-cdk)
	cdk synth --no-notices

lint: ## Run all pre-commit hooks (ruff, mypy, pylint, bandit, xenon, pip-audit)
	pre-commit run --all-files

format: ## Format code with ruff
	ruff format .

typecheck: ## Run mypy type checking
	mypy lambda/ hello_world/

security: ## Run bandit security scan and pip-audit vulnerability check
	bandit -r lambda/ hello_world/
	pip-audit

# =============================================================================
# Documentation
# =============================================================================

docs: ## Build Sphinx HTML documentation
	PYTHONPATH=lambda:. sphinx-build -b html docs docs/_build

docs-open: docs ## Build and open documentation in browser
	open docs/_build/index.html

# =============================================================================
# Dependency management
# =============================================================================

compile: ## Regenerate all lock files from .in sources (lambda -> tests -> dev)
	pip-compile --generate-hashes lambda/requirements.in -o lambda/requirements.txt
	pip-compile --generate-hashes --allow-unsafe tests/requirements.in -o tests/requirements.txt
	pip-compile --generate-hashes --allow-unsafe requirements.in -o requirements.txt

upgrade: ## Upgrade all dependencies to latest compatible versions
	pip-compile --upgrade --generate-hashes lambda/requirements.in -o lambda/requirements.txt
	pip-compile --upgrade --generate-hashes --allow-unsafe tests/requirements.in -o tests/requirements.txt
	pip-compile --upgrade --generate-hashes --allow-unsafe requirements.in -o requirements.txt

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Remove build artifacts, caches, and coverage files
	rm -rf docs/_build htmlcov .coverage report.html .pytest_cache .mypy_cache .ruff_cache cdk.out
	find . -type d -name __pycache__ -exec rm -rf {} +
