"""Shared test fixtures for the Hello World project."""

import importlib.util
import os
import sys

import pytest

# Path to the Lambda handler module. We load it by absolute file path inside
# the lambda_app_module fixture rather than relying on sys.path, because
# pytest re-prepends the project rootdir to sys.path after conftest.py runs,
# which would shadow lambda/app.py with the root-level CDK entry point app.py.
# Loading by file path is unambiguous and avoids that collision entirely.
LAMBDA_APP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lambda", "app.py"))


@pytest.fixture
def apigw_event():
    """Generates API GW Event for GET /hello."""
    return {
        "body": None,
        "resource": "/hello",
        "path": "/hello",
        "httpMethod": "GET",
        "isBase64Encoded": False,
        "queryStringParameters": {"foo": "bar"},
        "requestContext": {
            "resourceId": "123456",
            "apiId": "1234567890",
            "resourcePath": "/hello",
            "httpMethod": "GET",
            "requestId": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef",
            "accountId": "123456789012",
            "identity": {
                "sourceIp": "127.0.0.1",
                "userAgent": "Custom User Agent String",
            },
            "stage": "prod",
        },
        "headers": {
            "Host": "1234567890.execute-api.us-east-1.amazonaws.com",
            "User-Agent": "Custom User Agent String",
        },
        "pathParameters": None,
        "stageVariables": None,
    }


@pytest.fixture
def lambda_context(mocker):
    """Mock Lambda context using pytest-mock."""
    context = mocker.MagicMock()
    context.function_name = "HelloWorldFunction"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:HelloWorldFunction"
    context.aws_request_id = "test-request-id"
    return context


@pytest.fixture
def lambda_app_module():
    """Provide the Lambda app module for direct access in tests.

    Loaded lazily by absolute file path so test environments without
    aws_lambda_powertools installed (e.g. the cdk-check CI job) can still
    collect tests that don't depend on this fixture without an ImportError
    at conftest load time. Cached in sys.modules under "lambda_app" so that
    mocker.patch.object() sees a consistent module identity across fixtures.
    """
    if "lambda_app" in sys.modules:
        return sys.modules["lambda_app"]
    spec = importlib.util.spec_from_file_location("lambda_app", LAMBDA_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["lambda_app"] = module
    spec.loader.exec_module(module)
    return module
