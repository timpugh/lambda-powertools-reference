"""Unit tests for the Lambda handler."""

import json

import pytest


def test_lambda_handler(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    assert ret["statusCode"] == 200
    assert "message" in ret["body"]
    assert data["message"] == "hello world"


def test_lambda_handler_returns_valid_json(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    body = json.loads(ret["body"])
    assert isinstance(body, dict)


def test_lambda_handler_status_code(apigw_event, lambda_context, lambda_app_module):
    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    assert ret["statusCode"] == 200


def test_enhanced_greeting_feature_flag(apigw_event, lambda_context, lambda_app_module, mocker):
    """Test that enhanced greeting feature flag changes the response."""
    mocker.patch.object(lambda_app_module.feature_flags, "evaluate", return_value=True)

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)
    data = json.loads(ret["body"])

    assert "enhanced mode enabled" in data["message"]


def test_ssm_failure_propagates_exception(apigw_event, lambda_context, lambda_app_module, mocker):
    """Test that an SSM parameter fetch failure raises an unhandled exception.

    Powertools decorators do not suppress exceptions from route handlers,
    so a downstream failure propagates to the Lambda runtime, which returns
    a 502. Production code should add explicit error handling in the handler.
    """
    mocker.patch.object(
        lambda_app_module,
        "get_parameter",
        side_effect=Exception("SSM unavailable"),
    )

    with pytest.raises(Exception, match="SSM unavailable"):
        lambda_app_module.lambda_handler(apigw_event, lambda_context)


def test_unknown_route_returns_404(apigw_event, lambda_context, lambda_app_module):
    """Test that a request to an unknown route returns 404."""
    apigw_event["path"] = "/unknown"
    apigw_event["resource"] = "/unknown"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 404


def test_unsupported_method_returns_404(apigw_event, lambda_context, lambda_app_module):
    """Test that an unsupported HTTP method returns 404.

    Powertools APIGatewayRestResolver returns 404 (not 405) for method+path
    combinations that have no registered route handler.
    """
    apigw_event["httpMethod"] = "POST"

    ret = lambda_app_module.lambda_handler(apigw_event, lambda_context)

    assert ret["statusCode"] == 404
