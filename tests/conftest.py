"""Shared test fixtures for the Hello World project."""

import os
import sys

import pytest

# Add the lambda directory to the FRONT of the path so it takes priority
# over the root app.py (CDK entry point)
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

import app as lambda_app  # noqa: E402


@pytest.fixture()
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


@pytest.fixture()
def lambda_context(mocker):
    """Mock Lambda context using pytest-mock."""
    context = mocker.MagicMock()
    context.function_name = "HelloWorldFunction"
    context.memory_limit_in_mb = 128
    context.invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:HelloWorldFunction"
    )
    context.aws_request_id = "test-request-id"
    return context


@pytest.fixture()
def lambda_app_module():
    """Provide the Lambda app module for direct access in tests."""
    return lambda_app
