"""Hello World Lambda function using AWS Lambda Powertools.

This module implements a serverless API endpoint that returns a greeting message.
It demonstrates the use of Powertools utilities including structured logging,
X-Ray tracing, CloudWatch metrics, idempotency, SSM parameters, feature flags,
Pydantic-backed request/response validation (with an OpenAPI spec generated
at documentation-build time — see docs/generate_openapi.py), and Event Source
Data Classes.
"""

import os

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.api_gateway import CORSConfig
from aws_lambda_powertools.event_handler.exceptions import InternalServerError
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent
from aws_lambda_powertools.utilities.feature_flags import AppConfigStore, FeatureFlags
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    idempotent,
)
from aws_lambda_powertools.utilities.idempotency.config import IdempotencyConfig
from aws_lambda_powertools.utilities.parameters import get_parameter
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel

logger = Logger()
tracer = Tracer()
metrics = Metrics()

# enable_validation=True wires Pydantic into the resolver. Request bodies and
# response return types are validated against their model annotations, and
# those same models drive the OpenAPI schema read by docs/generate_openapi.py.
# We deliberately do NOT call app.enable_swagger() here — exposing the spec at
# runtime would publish the full API surface to any caller. The spec is
# instead rendered into Sphinx at documentation-build time.
app = APIGatewayRestResolver(
    cors=CORSConfig(allow_origin="*", max_age=300),
    enable_validation=True,
)

# Idempotency setup
persistence_layer = DynamoDBPersistenceLayer(
    table_name=os.environ.get("IDEMPOTENCY_TABLE_NAME", ""),
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath="requestContext.requestId",
    expires_after_seconds=3600,
)

# Feature Flags setup
app_config_store = AppConfigStore(
    environment=os.environ.get("APPCONFIG_ENV_NAME", ""),
    application=os.environ.get("APPCONFIG_APP_NAME", ""),
    name=os.environ.get("APPCONFIG_PROFILE_NAME", ""),
)
feature_flags = FeatureFlags(store=app_config_store)


class HelloResponse(BaseModel):
    """Response body for GET /hello."""

    message: str


@app.get("/hello")
@tracer.capture_method
def hello() -> HelloResponse:
    """Handle GET /hello requests.

    Fetches the greeting from SSM Parameter Store, checks the enhanced_greeting
    feature flag, emits a CloudWatch metric, and logs request metadata from
    the API Gateway event.

    Returns:
        HelloResponse: Validated response model with a ``message`` field.
    """
    metrics.add_metric(name="HelloRequests", unit=MetricUnit.Count, value=1)

    # Access typed event data via Event Source Data Classes
    event: APIGatewayProxyEvent = app.current_event
    source_ip = event.request_context.identity.source_ip
    user_agent = event.request_context.identity.user_agent
    request_id = event.request_context.request_id

    logger.info(
        "Request received",
        source_ip=source_ip,
        user_agent=user_agent,
        request_id=request_id,
    )

    # Fetch greeting from SSM Parameter Store — required, raise 500 on failure
    param_name = os.environ.get("GREETING_PARAM_NAME", "/HelloWorld/greeting")
    try:
        greeting = get_parameter(param_name)
    except Exception as exc:
        logger.exception("Failed to fetch greeting from SSM", param_name=param_name)
        raise InternalServerError("Failed to fetch greeting") from exc
    logger.info("Greeting fetched from parameter store", greeting=greeting)

    # Check feature flag — non-critical, fall back to default on failure
    try:
        enhanced = feature_flags.evaluate(name="enhanced_greeting", default=False)
    except Exception:
        logger.warning("Feature flag evaluation failed, falling back to default")
        enhanced = False

    if enhanced:
        message = f"{greeting} - enhanced mode enabled"
        logger.info("Enhanced greeting enabled")
    else:
        message = greeting

    return HelloResponse(message=message)


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
@idempotent(config=idempotency_config, persistence_store=persistence_layer)
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    """Lambda entry point.

    Resolves the API Gateway event through the router and returns the result.
    Decorated with Powertools Logger, Tracer, Metrics, and Idempotency.

    Args:
        event: API Gateway Lambda proxy event.
        context: Lambda runtime context.

    Returns:
        dict: API Gateway Lambda proxy response.
    """
    return app.resolve(event, context)  # type: ignore[no-any-return]
