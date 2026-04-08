# Lambda Powertools Reference

This project contains source code and supporting files for a serverless application that you can deploy with the AWS CDK. It includes the following files and folders.

- lambda - Code for the application's Lambda function.
- events - Invocation events that you can use to invoke the function.
- tests - Unit tests for the application code.
- tests/conftest.py - Shared test fixtures (API Gateway event, Lambda context, mocks).
- docs - Sphinx documentation source files.
- hello_world/hello_world_stack.py - The CDK stack that defines the application's AWS resources.
- pyproject.toml - Consolidated tool configuration (ruff, mypy, pylint, pytest).

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

# Install dev/CDK dependencies and test dependencies
pip-sync requirements.txt tests/requirements.txt

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

Integration tests call the live API Gateway endpoint, so the stack must be deployed first. They verify the response body, content type headers, and response time (under 5 seconds). The stack name and other test environment variables are configured in `pyproject.toml` via pytest-env:

```toml
[tool.pytest.ini_options]
timeout = 30
addopts = "--cov=lambda --cov-report=term-missing -n auto --html=report.html --self-contained-html"
env = [
    "AWS_SAM_STACK_NAME=HelloWorld",
    "POWERTOOLS_IDEMPOTENCY_DISABLED=true",
    "POWERTOOLS_SERVICE_NAME=hello-world",
    "POWERTOOLS_METRICS_NAMESPACE=HelloWorld",
    "POWERTOOLS_LOG_LEVEL=INFO",
    "LOG_LEVEL=INFO",
    "IDEMPOTENCY_TABLE_NAME=test-idempotency",
    "GREETING_PARAM_NAME=/test/greeting",
    "APPCONFIG_APP_NAME=test-app",
    "APPCONFIG_ENV_NAME=test-env",
    "APPCONFIG_PROFILE_NAME=test-profile",
]
```

All test environment variables are centralized here rather than scattered across test files. Note that `POWERTOOLS_IDEMPOTENCY_DISABLED=true` is only active during test runs — in production, this env var is not set, so idempotency is fully active against the DynamoDB table.

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

Coverage runs automatically on every test run via `addopts = --cov=lambda --cov-report=term-missing` in `pyproject.toml`. To generate an HTML report instead:

```bash
python -m pytest tests/unit --cov-report=html
```

### Parallel execution

Tests run in parallel automatically via `addopts = -n auto` in `pyproject.toml`. pytest-xdist distributes tests across CPU cores. To disable it for debugging:

```bash
python -m pytest tests/ -n0
```

### HTML report

An HTML test report (`report.html`) is generated automatically on every test run via `addopts = --html=report.html --self-contained-html` in `pyproject.toml`. Open it in a browser to view detailed results.

## Linting and static analysis

This project uses several tools for code quality, all configured in `pyproject.toml` (except bandit which uses `.bandit`):

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

## Pre-commit hooks

Pre-commit is configured in `.pre-commit-config.yaml` to run ruff, mypy, pylint, bandit, xenon (complexity threshold enforcement), and pip-audit (dependency vulnerability scanning) automatically on each commit. Set it up with:

```bash
pre-commit install
```

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

To install and keep your venv in sync (removes stale packages that aren't in the lock files):

```bash
pip-sync requirements.txt tests/requirements.txt
```

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
