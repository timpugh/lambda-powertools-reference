# Lambda Powertools Reference

[![CI](https://github.com/timpugh/lambda-powertools-reference/actions/workflows/ci.yml/badge.svg)](https://github.com/timpugh/lambda-powertools-reference/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://timpugh.github.io/lambda-powertools-reference/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Docs:** https://timpugh.github.io/lambda-powertools-reference/

This project contains source code and supporting files for a serverless application that you can deploy with the AWS CDK. It includes the following files and folders.

- `app.py` - CDK entry point; instantiates the `HelloWorldStack` and calls `app.synth()`
- `lambda/` - Code for the application's Lambda function
- `hello_world/hello_world_stack.py` - The CDK stack that defines all AWS resources
- `events/event.json` - A sample API Gateway proxy event for local SAM invocation
- `tests/` - Unit and integration tests
- `tests/conftest.py` - Shared test fixtures (API Gateway event, Lambda context, mocks)
- `docs/` - Sphinx documentation source files
- `pyproject.toml` - Consolidated tool configuration (ruff, mypy, pylint, pytest, coverage)
- `.pre-commit-config.yaml` - Pre-commit hook definitions (runs on every `git commit`)
- `.bandit` - Bandit security scanner configuration (excluded directories)
- `.github/dependabot.yml` - Dependabot configuration (weekly GitHub Actions version checks)
- `.github/workflows/dependabot-auto-merge.yml` - Auto-merges Dependabot PRs when CI passes
- `Makefile` - Common development commands (`make help` to list all targets)

The application uses several AWS resources, including Lambda functions, an API Gateway API, a DynamoDB table, SSM parameters, and AppConfig. These resources are defined in the `hello_world/hello_world_stack.py` file in this project. The Lambda function uses [AWS Lambda Powertools](https://docs.powertools.aws.dev/lambda/python/latest/) extensively — see the [Lambda Powertools features](#lambda-powertools-features) section below for details. Note that Powertools Tracer currently depends on the `aws-xray-sdk`, which is approaching deprecation. There is an [open RFC](https://github.com/aws-powertools/powertools-lambda/discussions/90) to replace it with OpenTelemetry as the tracing provider. You can update the stack to add AWS resources through the same deployment process that updates your application code.

## Lambda Powertools features

The Lambda function in `lambda/app.py` uses the following Powertools utilities:

### Logger
Structured JSON logging with `@logger.inject_lambda_context`. Automatically includes Lambda context fields (function name, request ID, cold start) in every log entry. Configured via `POWERTOOLS_SERVICE_NAME` and `POWERTOOLS_LOG_LEVEL` environment variables.

### Tracer
X-Ray tracing with `@tracer.capture_lambda_handler` on the entry point and `@tracer.capture_method` on route handlers. Creates subsegments for each traced method.

### Metrics
CloudWatch Embedded Metric Format (EMF) via `@metrics.log_metrics(capture_cold_start_metric=True)`. The `/hello` route emits a `HelloRequests` count metric. Metrics are published under the `HelloWorld` namespace (set via `POWERTOOLS_METRICS_NAMESPACE`).

### Event Handler
`APIGatewayRestResolver` provides Flask-like routing with `@app.get("/hello")`. It parses the API Gateway event and routes to the correct handler based on HTTP method and path.

### Idempotency
The `@idempotent` decorator uses a DynamoDB table to prevent duplicate processing of the same request. It keys on `requestContext.requestId` and records expire after 1 hour. The CDK stack provisions the DynamoDB table with PAY_PER_REQUEST billing and a TTL attribute.

### Parameters
`get_parameter()` fetches the greeting message from SSM Parameter Store. The parameter path is set via the `GREETING_PARAM_NAME` environment variable. Values are cached automatically by Powertools to reduce API calls.

### Feature Flags
`FeatureFlags` reads from AWS AppConfig to toggle behavior at runtime. The `enhanced_greeting` flag controls whether the response includes extra text. The CDK stack provisions the AppConfig application, environment, configuration profile, and an initial hosted configuration version.

### Validation
`validate(event=response, schema=RESPONSE_SCHEMA)` checks the route handler's return value against a JSON Schema before the resolver wraps it into the API Gateway proxy response. This catches malformed responses at the source rather than after serialization.

### Event Source Data Classes
`APIGatewayProxyEvent` provides typed access to the incoming API Gateway event. Instead of raw dict access like `event["requestContext"]["identity"]["sourceIp"]`, you get `event.request_context.identity.source_ip` with IDE autocomplete and type safety. Powertools includes data classes for many event sources:

- `APIGatewayProxyEvent` / `APIGatewayProxyEventV2` — REST and HTTP API events
- `S3Event` — S3 bucket notifications
- `SQSEvent` — SQS messages
- `DynamoDBStreamEvent` — DynamoDB stream records
- `EventBridgeEvent` — EventBridge events
- `SNSEvent`, `KinesisStreamEvent`, `CloudWatchLogsEvent`, and more

These are available from `aws_lambda_powertools.utilities.data_classes` and require no extra dependencies.

## AWS resources provisioned

The CDK stack (`hello_world/hello_world_stack.py`) creates the following resources:

| Resource | Purpose |
|---|---|
| Lambda Function | Runs the hello-world handler with Powertools |
| API Gateway REST API | Exposes `GET /hello` with X-Ray tracing |
| DynamoDB Table | Stores idempotency records (TTL-enabled, PAY_PER_REQUEST) |
| SSM Parameter | Stores the greeting message (`/{stack}/greeting`) |
| AppConfig Application | Hosts feature flag configuration |
| AppConfig Environment | `production` environment for feature flags |
| AppConfig Configuration Profile | `features` profile with `AWS.AppConfig.FeatureFlags` type |
| Resource Group + Application Insights | CloudWatch Application Insights monitoring |
| CloudWatch Dashboard | Auto-generated via cdk-monitoring-constructs |

## Makefile

Common commands are available via `make`. Run `make help` to see all targets:

```bash
make install        # set up venv with all dependencies and pre-commit hooks
make test           # run unit tests with coverage
make test-integration  # run integration tests (requires deployed stack)
make lint           # run all pre-commit hooks (ruff, mypy, pylint, bandit, xenon, pip-audit)
make format         # format code with ruff
make typecheck      # run mypy type checking
make security       # run bandit + pip-audit
make docs           # build Sphinx HTML docs
make docs-open      # build and open docs in browser
make compile        # regenerate all lock files from .in sources
make upgrade        # upgrade all dependencies to latest compatible versions
make clean          # remove build artifacts, caches, and coverage files
```

## Prerequisites

To use the CDK, you need the following tools.

* AWS CDK CLI - [Install the CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html)
* AWS SAM CLI - [Install the SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html) - Required for local invocation and log tailing
* [Python 3 installed](https://www.python.org/downloads/)
* [Finch](https://runfinch.com/) - Container runtime used for bundling Lambda dependencies and local invocation

## Deploy the application

This project uses Finch as the container runtime for bundling Lambda dependencies during synthesis. Set the `CDK_DOCKER` environment variable before running CDK commands (see the [CDK GitHub issue](https://github.com/aws/aws-cdk/issues/23680#issuecomment-1741643237) where this was added):

```bash
export CDK_DOCKER=finch
```

To set up and deploy your application for the first time, run the following in your shell:

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install pip-tools first (needed for pip-sync)
pip install pip-tools

# Install dev dependencies (CDK, linting, type checking) via pip-sync
pip-sync requirements.txt

# Add test and Lambda dependencies on top (additive — does not remove dev deps)
pip install -r tests/requirements.txt -r lambda/requirements.txt

# Make sure Finch is running
finch vm start

# Set Finch as the container runtime for CDK
export CDK_DOCKER=finch

# Bootstrap CDK (first time only)
cdk bootstrap

# Deploy the stack
cdk deploy
```

The `cdk synth` and `cdk deploy` commands use Finch to build a container that installs the Lambda dependencies from `lambda/requirements.txt` into the deployment package. The first run will be slower as it pulls the SAM build image.

You can find your API Gateway Endpoint URL in the output values displayed after deployment.

## Useful CDK commands

* `cdk ls`          list all stacks in the app
* `cdk synth`       emit the synthesized CloudFormation template
* `cdk deploy`      deploy this stack to your default AWS account/region
* `cdk diff`        compare deployed stack with current state
* `cdk destroy`     destroy the deployed stack

## Use the CDK to build and test locally

Synthesize your application to verify the CloudFormation template (requires Finch running):

```bash
export CDK_DOCKER=finch
cdk synth
```

You can invoke the Lambda function locally using the SAM CLI with the synthesized template:

```bash
sam local invoke HelloWorldFunction -t cdk.out/HelloWorld.template.json --event events/event.json
```

`events/event.json` is a sample API Gateway REST proxy event that simulates a `GET /hello` request. It includes realistic headers, a `requestContext` with a unique `requestId` (used by idempotency), and placeholder CloudFront fields. Use it as a starting point for local invocation — edit the `httpMethod`, `path`, or `body` fields to test different scenarios.

You can also emulate the API locally:

```bash
sam local start-api -t cdk.out/HelloWorld.template.json
curl http://localhost:3000/hello
```

**Note:** Local invocation requires Finch to be running:

```bash
finch vm start
```

## Fetch, tail, and filter Lambda function logs

You can use the SAM CLI to fetch logs from your deployed Lambda function:

```bash
sam logs -n HelloWorldFunction --stack-name "HelloWorld" --tail
```

This works for any AWS Lambda function, not just ones deployed with SAM. See the [SAM CLI logging documentation](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-logging.html) for more on filtering and searching logs.

## Add a resource to your application

To add AWS resources, define new constructs in `hello_world/hello_world_stack.py`. The CDK provides high-level constructs for most AWS services. Browse available constructs in the [AWS CDK API Reference](https://docs.aws.amazon.com/cdk/api/v2/python/). For resources without a dedicated CDK construct, you can use [CloudFormation resource types](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-template-resource-type-ref.html) directly via `CfnResource`.

## Tests

Tests are defined in the `tests` folder in this project. Make sure dependencies are installed first (see [Deploy the application](#deploy-the-application)).

### Unit test architecture

Unit tests mock all external AWS dependencies so they run locally without credentials or a deployed stack. The key patterns used:

**Shared fixtures via conftest.py** — Reusable fixtures live in `tests/conftest.py`, including the API Gateway event, Lambda context mock, and the Lambda app module reference. The autouse mock that patches SSM Parameters and Feature Flags lives in `tests/unit/conftest.py` so it only applies to unit tests. Test files stay clean and focused on assertions.

**Environment variables** — All test env vars are centralized in `pyproject.toml` via pytest-env. This includes Powertools config, mock resource names, and the idempotency disable flag. No `os.environ` calls needed in test files.

**Idempotency disabled via env var** — `POWERTOOLS_IDEMPOTENCY_DISABLED=true` is set in `pyproject.toml` to tell Powertools to skip DynamoDB calls during tests. This is the recommended approach from Powertools docs. In production, this env var is not set, so idempotency is fully active.

**Mocking external calls with pytest-mock** — SSM Parameters and Feature Flags are mocked in `tests/unit/conftest.py` using `mocker.patch.object()`:
```python
mocker.patch.object(lambda_app, "get_parameter", return_value="hello world")
mocker.patch.object(lambda_app.feature_flags, "evaluate", return_value=False)
```

**Lambda context via pytest-mock** — A `MagicMock` provides the Lambda context object with realistic attributes (function name, ARN, request ID).

**Import path isolation** — The `lambda/` directory is added to `sys.path` in `tests/conftest.py` before the root directory to ensure `import app` resolves to the Lambda handler (`lambda/app.py`) and not the CDK entry point (`app.py`).

### Running unit tests

```bash
python -m pytest tests/unit -v
```

### Integration tests

Integration tests call the live API Gateway endpoint, so the stack must be deployed first. They verify the response body, content type headers, and response time (under 5 seconds). The stack name and other test environment variables are configured in `pyproject.toml` via pytest-env (see the `env` key under `[tool.pytest.ini_options]`).

All test environment variables are centralized in `pyproject.toml` rather than scattered across test files. Note that `POWERTOOLS_IDEMPOTENCY_DISABLED=true` is only active during test runs — in production, this env var is not set, so idempotency is fully active against the DynamoDB table.

```bash
python -m pytest tests/integration -v
```

### Timeout

Every test has a 30-second timeout enforced via `timeout = 30` in `pyproject.toml`. Tests that exceed this are terminated and marked as failed. To override for a specific test, use the `@pytest.mark.timeout(60)` decorator.

### Test randomization

pytest-randomly shuffles test execution order on every run to catch order-dependent bugs. It activates automatically when installed — no additional configuration needed. The seed is printed at the top of the output. To reproduce a specific order:

```bash
python -m pytest tests/ -p randomly -p no:randomly  # disable
python -m pytest tests/ --randomly-seed=12345        # replay a specific seed
```

### Coverage

Coverage runs automatically on every test run. Key flags set in `pyproject.toml`:

| Flag | Effect |
|---|---|
| `--cov=lambda` | Measures coverage for the `lambda/` source directory |
| `--cov-branch` | Tracks branch coverage (not just whether a line executed, but whether all conditional paths did) |
| `--cov-report=term-missing` | Prints uncovered line numbers in the terminal |
| `--cov-report=html` | Generates `htmlcov/index.html` for detailed browsing |
| `--cov-fail-under=100` | Fails the run if total coverage drops below 100% |
| `--no-cov-on-fail` | Skips the coverage report when tests fail (avoids misleading partial output) |

To open the HTML report after a test run:

```bash
open htmlcov/index.html
```

### Parallel execution

Tests run in parallel automatically via `-n auto` in `addopts` (`pyproject.toml`). pytest-xdist distributes tests across CPU cores. To disable it for debugging:

```bash
python -m pytest tests/ -n0
```

### HTML report

An HTML test report (`report.html`) is generated automatically on every test run via `--html=report.html --self-contained-html` in `addopts` (`pyproject.toml`). Open it in a browser to view detailed results.

## Linting and static analysis

This project uses several tools for code quality. Most are configured in `pyproject.toml`; bandit uses a separate `.bandit` file.

```bash
# Lint with ruff
ruff check .

# Format with ruff
ruff format .

# Type check with mypy
mypy lambda/ hello_world/

# Design and complexity checks with pylint
pylint lambda/ hello_world/

# Security scan with bandit
bandit -r lambda/ hello_world/

# Dependency vulnerability audit
pip-audit

# Code complexity with radon/xenon
radon cc lambda/ -a
xenon lambda/ -b B -m A -a A
```

### Bandit configuration (`.bandit`)

Bandit is a security-focused static analyzer that scans Python source code for common vulnerabilities. Its configuration lives in `.bandit` rather than `pyproject.toml` because the pre-commit bandit hook reads YAML config files by convention.

The `.bandit` file specifies which directories to exclude from scanning:

| Directory | Reason excluded |
|---|---|
| `tests/` | Test code uses `assert`, hardcoded strings, and other patterns that trigger false positives |
| `cdk.out/` | CDK-generated CloudFormation output — not code you write or can fix |
| `.venv/` | Third-party packages — vulnerabilities here are caught by `pip-audit` instead |

Everything outside these directories — `lambda/` and `hello_world/` — is scanned. That is the code you own and ship.

## pyproject.toml configuration

All tool configuration is consolidated in `pyproject.toml`. Here is a summary of the key settings in each section:

### `[tool.ruff]`

| Setting | Value | Purpose |
|---|---|---|
| `target-version` | `py312` | Enables Python 3.12-specific lint rules and syntax modernization |
| `line-length` | `120` | Maximum line length enforced by the formatter |
| `dummy-variable-rgx` | `^(_+\|...)$` | Allows `_`-prefixed variables to be unused without triggering a lint warning |

### `[tool.ruff.lint]`

Ruff is configured with a broad set of rule groups. Each group targets a specific class of issue:

| Code | Plugin | What it catches |
|---|---|---|
| `E` / `W` | pycodestyle | Style errors and warnings |
| `F` | pyflakes | Undefined names, unused imports |
| `I` | isort | Import ordering |
| `C` | flake8-comprehensions | Inefficient list/dict/set comprehensions |
| `B` | flake8-bugbear | Likely bugs and design issues |
| `S` | flake8-bandit | Security anti-patterns |
| `UP` | pyupgrade | Modernize syntax to the target Python version |
| `SIM` | flake8-simplify | Suggest simpler code patterns |
| `RUF` | ruff-specific | Ruff's own opinionated rules |
| `T20` | flake8-print | Catches `print()` calls — use Powertools Logger instead |
| `PT` | flake8-pytest-style | Enforces pytest conventions (fixtures, raises, etc.) |
| `N` | pep8-naming | Naming conventions (snake_case, PascalCase, SCREAMING_SNAKE) |
| `RET` | flake8-return | Unnecessary `else` after `return`, redundant return values |

### `[tool.mypy]`

| Setting | Purpose |
|---|---|
| `warn_return_any` | Warns when a typed function returns `Any`, which often masks missing type coverage |
| `warn_unused_ignores` | Warns when a `# type: ignore` comment is no longer needed, preventing stale suppression comments |
| `disallow_untyped_defs` | Every function must have complete type annotations |
| `check_untyped_defs` | Type-checks function bodies even if the function itself lacks annotations |
| `no_implicit_optional` | `f(x: str = None)` does not implicitly mean `Optional[str]` — must be explicit |
| `ignore_missing_imports` | Suppresses errors for third-party packages without type stubs (e.g. aws-lambda-powertools) |
| `show_error_codes` | Prints `[error-code]` next to each error — required to write precise `# type: ignore[code]` comments |

### `[tool.pylint.design]`

Structural complexity thresholds. Pylint fails if any function or class exceeds these limits. Complexity is also enforced by the xenon pre-commit hook (which uses radon under the hood).

| Threshold | Value | What it limits |
|---|---|---|
| `max-args` | 8 | Parameters per function |
| `max-locals` | 20 | Local variables per function |
| `max-returns` | 6 | Return statements per function |
| `max-branches` | 12 | Branches (if/for/while/try) per function |
| `max-statements` | 50 | Statements per function body |
| `max-attributes` | 10 | Instance attributes per class |

### `[tool.pytest.ini_options]`

Key flags in `addopts`:

| Flag | Purpose |
|---|---|
| `-ra` | Prints a short summary of all non-passed tests (failures, errors, skipped) at the end |
| `--cov=lambda` | Measures coverage for the `lambda/` directory |
| `--cov-branch` | Tracks branch coverage — not just whether a line ran, but whether all conditional paths did |
| `--cov-fail-under=100` | Fails the run if total coverage drops below 100% |
| `--no-cov-on-fail` | Skips coverage reporting when tests fail (avoids misleading partial results) |
| `-n auto` | Runs tests in parallel across all available CPU cores (pytest-xdist) |

`log_cli = true` and `log_cli_level = "WARNING"` stream log output in real time during the test run, showing only WARNING and above to reduce noise.

## Security

Security is enforced at three layers, each covering a different surface area:

| Layer | Tool | What it scans | When it runs |
|---|---|---|---|
| **Source code** | bandit | `lambda/` and `hello_world/` for security anti-patterns (hardcoded secrets, shell injection, unsafe deserialization, etc.) | Pre-commit hook on every commit; CI quality job |
| **Dependencies** | pip-audit | All three requirements files for packages with known CVEs | Pre-commit hook on every commit; weekly Dependency Audit workflow |
| **Infrastructure** | cdk-nag | CDK stack against AWS Solutions security rules | `cdk synth` — findings are printed and fail synthesis if unsuppressed |

These tools are complementary — no single one covers all three surfaces. Bandit catches code-level issues, pip-audit catches supply chain issues, and cdk-nag catches infrastructure misconfigurations.

## Commit message convention

This project follows [Conventional Commits](https://www.conventionalcommits.org/). Format:

```
type: short description
```

| Type | When to use |
|---|---|
| `feat` | A new feature |
| `fix` | A bug fix |
| `docs` | Documentation changes only |
| `chore` | Maintenance tasks that don't affect functionality (lock files, Makefile, LICENSE) |
| `ci` | Changes to CI/CD configuration (GitHub Actions, pre-commit) |
| `test` | Adding or updating tests |
| `refactor` | Code restructuring that neither fixes a bug nor adds a feature |
| `build` | Changes to the build system or dependencies |

## Pre-commit hooks

Pre-commit runs a chain of hooks automatically on every `git commit`. Hooks are defined in `.pre-commit-config.yaml`. Set it up once after cloning:

```bash
pre-commit install
```

To run all hooks manually without committing (useful before pushing or after changing config):

```bash
pre-commit run --all-files
```

### Hook reference

| Hook | Source | What it does |
|---|---|---|
| `ruff` | astral-sh/ruff-pre-commit | Lints and auto-fixes code (runs before formatting) |
| `ruff-format` | astral-sh/ruff-pre-commit | Formats code (equivalent to black) |
| `mypy` | mirrors-mypy | Static type checking on `hello_world/` (excludes `app.py` and `tests/`) |
| `bandit` | PyCQA/bandit | Security-focused static analysis on `lambda/` and `hello_world/` |
| `pylint` | local | Design and complexity checks on non-test, non-docs Python files |
| `trailing-whitespace` | pre-commit-hooks | Removes trailing whitespace |
| `end-of-file-fixer` | pre-commit-hooks | Ensures every file ends with a newline |
| `check-yaml` | pre-commit-hooks | Validates YAML syntax |
| `check-json` | pre-commit-hooks | Validates JSON syntax |
| `xenon` | local | Enforces cyclomatic complexity thresholds on `lambda/` (max absolute: B, module: A, average: A) |
| `pip-audit` | local | Scans all installed dependencies for known CVEs (runs on every commit) |

## GitHub Actions

Four workflows are configured:

| Workflow | Trigger | What it does |
|---|---|---|
| **CI** | Push / PR to `main` | Runs pre-commit hooks (quality job) and pytest unit tests (test job) |
| **Docs** | Push to `main` | Builds Sphinx docs and deploys to GitHub Pages |
| **Dependency Audit** | Every Monday 9am UTC | Runs `pip-audit` across all requirements files |
| **Dependabot Auto-merge** | Dependabot PRs | Approves and auto-merges GitHub Actions version updates when CI passes |

Both the `quality` and `test` CI jobs must pass before anything can merge to `main` (branch protection).

The CI installs dependencies with `pip-sync` to match the local dev workflow exactly:
- `quality` job: `pip-sync requirements.txt`
- `test` job: `pip-sync tests/requirements.txt lambda/requirements.txt`

### Dependabot

Dependabot is configured in `.github/dependabot.yml` to check for GitHub Actions version updates every Monday. When a newer version of an action is available (e.g. `actions/checkout@v4` → a newer release), Dependabot opens a PR automatically.

The `dependabot-auto-merge` workflow then:
1. Confirms the PR is a GitHub Actions ecosystem update (not pip)
2. Approves it
3. Enables auto-merge — GitHub merges it automatically once CI passes

This keeps workflow action versions current without any manual intervention. If CI fails on a Dependabot PR, it stays open for investigation rather than merging.

## CDK security checks

The CDK stack uses [cdk-nag](https://github.com/cdklabs/cdk-nag) with AWS Solutions checks enabled. Security findings are surfaced during `cdk synth`. Suppressions for this sample app are documented inline in `hello_world/hello_world_stack.py`.

## Monitoring

The stack includes a [cdk-monitoring-constructs](https://github.com/cdklabs/cdk-monitoring-constructs) MonitoringFacade that creates a CloudWatch dashboard with Lambda, API Gateway, and DynamoDB metrics out of the box.

## Documentation

Project documentation is generated from docstrings and markdown files using Sphinx with MyST-Parser. Source files are in `docs/`. Doc builds are best run in CI/CD pipelines or manually before publishing, rather than on every commit.

```bash
# Build HTML docs
PYTHONPATH=lambda:. sphinx-build -b html docs docs/_build

# Open in browser
open docs/_build/index.html
```

## Project dependencies

Dependencies are managed with [pip-tools](https://pip-tools.readthedocs.io/). Each dependency group has a `.in` file (direct dependencies you maintain) and a `.txt` file (fully resolved with transitive dependencies and hashes, generated by `pip-compile`).

- `requirements.in` / `requirements.txt` — CDK, linting, static analysis, and dev tooling (constrained by `tests/requirements.txt`)
- `tests/requirements.in` / `tests/requirements.txt` — pytest and test plugins (constrained by `lambda/requirements.txt`)
- `lambda/requirements.in` / `lambda/requirements.txt` — Lambda runtime dependencies (packaged with the function at deploy time)

Constraint files (`-c`) ensure shared packages like `boto3` resolve to the same version across all environments, preventing drift.

To regenerate the lock files after editing a `.in` file, compile in order (lambda → tests → dev):

```bash
pip-compile --generate-hashes lambda/requirements.in -o lambda/requirements.txt
pip-compile --generate-hashes --allow-unsafe tests/requirements.in -o tests/requirements.txt
pip-compile --generate-hashes --allow-unsafe requirements.in -o requirements.txt
```

To upgrade all dependencies:

```bash
pip-compile --upgrade --generate-hashes --allow-unsafe requirements.in -o requirements.txt
```

To install and keep your venv in sync with dev dependencies:

```bash
pip-sync requirements.txt
pip install -r tests/requirements.txt -r lambda/requirements.txt
```

`pip-sync` is used for the dev context because it removes stale packages not in the lock file. Test deps are added with `pip install -r` instead — using `pip-sync` for both contexts would remove dev packages, corrupting the venv. CI uses separate jobs so each runs `pip-sync` against a single context cleanly.

### `lambda/requirements.txt` — Lambda runtime

| Library | Purpose |
|---|---|
| `aws-lambda-powertools[all]` | Full Powertools suite: Logger, Tracer, Metrics, Event Handler, Idempotency, Parameters, Feature Flags, Validation, and Event Source Data Classes |
| `aws-xray-sdk` | Required by Powertools Tracer for X-Ray instrumentation |
| `boto3` | AWS SDK, version-locked in the deployment package to avoid depending on the Lambda runtime's bundled version |

### `requirements.txt` — CDK and dev tooling

| Library | Purpose |
|---|---|
| `aws-cdk-lib` | Core CDK framework for defining AWS infrastructure |
| `constructs` | Base construct library used by CDK |
| `aws-cdk-aws-lambda-python-alpha` | `PythonFunction` construct that bundles Lambda dependencies in a container |
| `cdk-monitoring-constructs` | Auto-generates CloudWatch dashboards and alarms for Lambda and API Gateway |
| `cdk-nag` | Runs AWS Solutions security checks against the CDK stack during synthesis |
| `ruff` | Fast Python linter and formatter (configured in `pyproject.toml`) |
| `mypy` | Static type checker (configured in `pyproject.toml`) |
| `pylint` | Design and complexity checks complementing ruff (configured in `pyproject.toml`) |
| `bandit` | Security-focused static analysis (configured in `.bandit`) |
| `radon` | Computes code complexity metrics (cyclomatic complexity, maintainability index) |
| `xenon` | Enforces complexity thresholds, fails if code exceeds limits |
| `pip-audit` | Scans installed dependencies for known vulnerabilities |
| `pre-commit` | Git hook framework that runs linters and formatters on each commit |
| `boto3-stubs` | Type stubs for boto3, enables mypy to type-check AWS SDK calls |
| `sphinx` | Documentation generator, builds HTML docs from docstrings and markdown (configured in `docs/conf.py`) |
| `myst-parser` | Enables Sphinx to use Markdown files alongside reStructuredText |
| `pip-tools` | Generates fully resolved, hash-verified `requirements.txt` files from `.in` source files |

### `tests/requirements.txt` — Testing

| Library | Purpose |
|---|---|
| `pytest` | Test framework |
| `pytest-env` | Sets environment variables in `pyproject.toml` (e.g. `AWS_SAM_STACK_NAME`) |
| `pytest-cov` | Code coverage reporting |
| `pytest-xdist` | Parallel test execution with `-n auto` |
| `pytest-mock` | Provides `mocker` fixture for mocking (used for Lambda context in unit tests) |
| `pytest-html` | Generates HTML test reports |
| `pytest-timeout` | Enforces per-test time limits (configured in `pyproject.toml`) |
| `pytest-randomly` | Randomizes test execution order to catch order-dependent bugs |
| `boto3` | AWS SDK, used by integration tests to query CloudFormation stack outputs |
| `requests` | HTTP client, used by integration tests to call the live API Gateway endpoint |

## Cleanup

To delete the application that you created, run:

```bash
cdk destroy
```

## Resources

See the [AWS CDK Developer Guide](https://docs.aws.amazon.com/cdk/v2/guide/home.html) for an introduction to CDK concepts and the CDK CLI.
