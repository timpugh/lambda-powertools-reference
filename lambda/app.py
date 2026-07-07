"""Serverless App Lambda handler — the handler layer.

This module initializes the AWS Powertools resolver and the AWS provider clients
(SSM, AppConfig feature flags, the DynamoDB idempotency layer) with a shared
retry config, validates the environment at cold start, routes the API Gateway
event, and translates the service layer's domain errors into HTTP responses. The
business logic lives in :mod:`service` and the data contracts in :mod:`models`,
so this file stays focused on wiring and the HTTP boundary.

It demonstrates the Powertools utilities a production handler leans on:
structured logging, X-Ray tracing, CloudWatch metrics, idempotency, SSM
parameters, feature flags, Pydantic-backed request/response validation (with an
OpenAPI spec generated at documentation-build time — see
scripts/generate_openapi.py), and Event Source Data Classes.
"""

import os
from typing import Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.api_gateway import CORSConfig
from aws_lambda_powertools.event_handler.exceptions import InternalServerError
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEvent
from aws_lambda_powertools.utilities.feature_flags import AppConfigStore, FeatureFlags
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    idempotent,
)
from aws_lambda_powertools.utilities.idempotency.config import IdempotencyConfig
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyKeyError,
)
from aws_lambda_powertools.utilities.parameters import SSMProvider
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config
from models import (
    EnvVars,
    GreetingResponse,
    IdempotencyInProgressResponse,
    InternalErrorResponse,
    MissingIdempotencyKeyResponse,
)
from service import GreetingUnavailableError, build_greeting

# Module-level so a bad deployment fails the cold start, not the Nth request.
# Extra keys in os.environ are ignored by pydantic's default model config.
_ENV = EnvVars.model_validate(dict(os.environ))


logger = Logger()
tracer = Tracer()
metrics = Metrics()

# Shared botocore retry config applied to every AWS SDK client this handler uses
# (SSM, AppConfig, DynamoDB). botocore retries transient failures by default, but
# pinning the policy here makes the posture explicit and tunable rather than
# implicit in the SDK default — the same "visible in code, not implicit in the
# runtime default" rationale behind setting recursive_loop="Terminate" on the
# function in CDK. "adaptive" mode adds client-side rate limiting on top of
# exponential backoff with jitter, which backs off proactively when a dependency
# starts returning throttling (429) responses; its token-bucket state is useful
# here because the clients are module-scoped (constructed once below) and reused
# across every warm invocation, so the limiter persists for the container's life.
# total_max_attempts (NOT max_attempts) on purpose: botocore's Config retries
# key "max_attempts" counts retries-after-initial and is stored as
# total_max_attempts = max_attempts + 1 — an off-by-one that silently inflates
# the budget (this codebase shipped 5 total attempts while the comment claimed
# 4). total_max_attempts pins the total unambiguously: 1 initial + 2 retries.
# Retrying is
# safe because the write path (DynamoDB via @idempotent) is idempotent on the
# client-supplied Idempotency-Key, and the SSM/AppConfig calls are reads. This
# implements the AWS "retry with backoff" prescriptive-guidance pattern without
# hand-rolling a retry loop — see README "Patterns deliberately not used".
# connect/read timeouts bound each ATTEMPT: the retry budget above only helps
# against fast-fail errors — botocore's defaults are 60s connect + 60s read,
# six times the function's own 10s timeout, so a single hung connection would
# otherwise consume the whole invocation (502 to the caller) before the first
# retry ever fired. Bounding the attempt turns a hang into a fast, retryable
# error; 0.5s/1s is generous for the small same-region SSM/AppConfig/DynamoDB
# calls this handler makes.
# Budget math (keep it under the function timeout when tuning). The @idempotent
# layer makes TWO sequential DynamoDB writes per request — save_inprogress
# (conditional PutItem) before the handler body, save_success (UpdateItem)
# after — so a DynamoDB brownout hits this retry budget TWICE, not once. Size it
# per call so even two serial browned-out DynamoDB calls stay under the 10s
# function timeout: total_max_attempts=2 bounds ONE call at 2 x (0.5 + 1)
# attempt-timeouts + ~1s of backoff ≈ 4s, so the two writes together reach ~8s
# < 10s, with headroom for the SSM/AppConfig reads in between. (History:
# total_max_attempts=3 put one call at ~7.5s, so the two writes reached ~15s and
# timed out on a DynamoDB brownout — an API Gateway 502, off the OpenAPI
# contract with no CORS header, feeding the canary rollback alarm during
# deploys; the earlier max_attempts=4 was worse still.)
boto_config = Config(
    retries={"mode": "adaptive", "total_max_attempts": 2},
    connect_timeout=0.5,
    read_timeout=1,
)

# enable_validation=True wires Pydantic into the resolver. Request bodies and
# response return types are validated against their model annotations, and
# those same models drive the OpenAPI schema read by scripts/generate_openapi.py.
# We deliberately do NOT call app.enable_swagger() here — exposing the spec at
# runtime would publish the full API surface to any caller. The spec is
# instead rendered into Zensical at documentation-build time.
app = APIGatewayRestResolver(
    # allow_headers is only relevant for the response-side CORS Access-Control-
    # Allow-Headers value, but for completeness we list Idempotency-Key here
    # too — keeps the Powertools CORSConfig in sync with API Gateway's preflight
    # configuration declared in CDK.
    cors=CORSConfig(
        allow_origin="*",
        max_age=300,
        allow_headers=[
            "Content-Type",
            "X-Amz-Date",
            "Authorization",
            "X-Api-Key",
            "X-Amzn-Trace-Id",
            "Idempotency-Key",
        ],
    ),
    enable_validation=True,
)

# Idempotency setup.
# Key on the client-supplied "Idempotency-Key" header. HTTP header names are
# case-insensitive (RFC 9110) but JMESPath lookups are exact-match, so
# lambda_handler lowercases all header keys before the @idempotent layer sees
# the event — the JMESPath then only needs the single lowercase form instead of
# enumerating casings. raise_on_no_idempotency_key turns a missing header into
# Powertools' IdempotencyKeyError, which the resolver below converts into a 400
# BadRequest — making the requirement enforced rather than implicit. Keying on a
# client-controlled value (instead of the server-generated
# requestContext.requestId, which changes on every retry) is what actually makes
# the layer deduplicate.
persistence_layer = DynamoDBPersistenceLayer(
    table_name=_ENV.IDEMPOTENCY_TABLE_NAME,
    boto_config=boto_config,
)
idempotency_config = IdempotencyConfig(
    event_key_jmespath='headers."idempotency-key"',
    raise_on_no_idempotency_key=True,
    expires_after_seconds=3600,
)

# Feature Flags setup.
# AppConfigStore is given an explicit appconfigdata client built with the shared
# retry config. Passing boto_config= (or sdk_config=) to AppConfigStore instead
# routes through a parameter that Powertools v3 deprecated and emits a warning,
# so an explicit client is the clean way to apply the retry policy here.
# max_age lifts the fetched-configuration TTL from the Powertools default of
# 5 seconds — which would re-poll the AppConfig data plane every 5s per warm
# container — to the same 300s posture as the SSM read in service.py. Tunable per
# environment via APPCONFIG_MAX_AGE_SECONDS.
app_config_store = AppConfigStore(
    environment=_ENV.APPCONFIG_ENV_NAME,
    application=_ENV.APPCONFIG_APP_NAME,
    name=_ENV.APPCONFIG_PROFILE_NAME,
    max_age=_ENV.APPCONFIG_MAX_AGE_SECONDS,
    boto3_client=boto3.client("appconfigdata", config=boto_config),
)
feature_flags = FeatureFlags(store=app_config_store)

# SSM provider for the greeting parameter. An explicit SSMProvider (rather than
# the module-level get_parameter helper) is used so the shared retry config can
# be injected — the free helper builds its own client and takes no boto_config.
# The in-memory cache is per-provider, so reuse this one instance across
# invocations to preserve warm-container caching (max_age is set per get() call).
ssm_provider = SSMProvider(boto_config=boto_config)

# Greeting parameter name resolved at module load — validated as non-empty by
# the EnvVars model rather than letting boto3 reject an empty key at runtime.
GREETING_PARAM_NAME = _ENV.GREETING_PARAM_NAME


def _resolve_tenant_id(event: APIGatewayProxyEvent) -> str:
    """Resolve the tenant identifier for the current request.

    This is the single place tenant context enters the application. There is no
    authentication in this reference today, so there is no real tenant: the
    function reads an optional ``tenantId`` claim from the API Gateway authorizer
    context and falls back to ``"anonymous"`` — the value every request actually
    hits right now. It exists so that logs, metrics, and traces are dimensioned
    by tenant from day one (see ``get_greeting``), rather than needing a retrofit
    across every dashboard and saved query the day multi-tenancy arrives.

    When a fork adds authentication (a Cognito/JWT authorizer or a custom Lambda
    authorizer), the tenant claim lands in ``requestContext.authorizer`` and this
    function starts returning real values. Derive tenant from the *authenticated*
    identity, never from a client-supplied header — a header the caller controls
    can be spoofed to read another tenant's telemetry.
    """
    # Both authorizer shapes are handled: a custom Lambda authorizer's context
    # keys land directly on the authorizer object, while a Cognito user-pool
    # authorizer nests its JWT claims one level deeper under "claims" — reading
    # only the flat shape would silently keep resolving "anonymous" for
    # authenticated Cognito users.
    authorizer = event.request_context.authorizer
    tenant_id = authorizer.get("tenantId") or (authorizer.get("claims") or {}).get("tenantId")
    return str(tenant_id) if tenant_id else "anonymous"


@app.get(
    "/greeting",
    summary="Return a greeting",
    description=(
        "Returns the greeting string configured in SSM Parameter Store. "
        "When the `enhanced_greeting` AppConfig feature flag is enabled for "
        "the caller's source IP, the response includes the feature flag's "
        "configured suffix. Requires an `Idempotency-Key` header; requests "
        "without one are rejected with 400 before this route runs."
    ),
    response_description="A JSON object containing the resolved greeting.",
    tags=["Greeting"],
    # Error responses documented explicitly — the generated OpenAPI spec
    # otherwise covers only the happy path, leaving the 400 (missing
    # Idempotency-Key, enforced outside the resolver) and the 500 (SSM
    # failure) invisible to spec consumers and breaking-change tooling.
    responses={
        200: {
            "description": "The resolved greeting.",
            "content": {"application/json": {"model": GreetingResponse}},
        },
        400: {
            "description": "Missing Idempotency-Key header.",
            "content": {"application/json": {"model": MissingIdempotencyKeyResponse}},
        },
        409: {
            "description": "A request with the same Idempotency-Key is still in progress.",
            "content": {"application/json": {"model": IdempotencyInProgressResponse}},
        },
        500: {
            "description": "Greeting could not be fetched from SSM Parameter Store.",
            "content": {"application/json": {"model": InternalErrorResponse}},
        },
    },
)
@tracer.capture_method(capture_response=False)
def get_greeting() -> GreetingResponse:
    """Handle GET /greeting requests.

    Extracts request metadata from the typed API Gateway event, delegates the
    business logic to :func:`service.build_greeting`, and maps a service-layer
    :class:`service.GreetingUnavailableError` to an HTTP 500.

    Returns:
        GreetingResponse: Validated response model with a ``message`` field.
    """
    # Access typed event data via Event Source Data Classes
    event: APIGatewayProxyEvent = app.current_event
    source_ip = event.request_context.identity.source_ip
    user_agent = event.request_context.identity.user_agent
    request_id = event.request_context.request_id

    # Tenant context as a first-class observability signal. Today there is no
    # auth, so this is always "anonymous" (see _resolve_tenant_id); the value
    # goes live the day a fork puts a tenant claim on the request. Tagging all
    # three signals here means logs, EMF records, and the X-Ray trace can be
    # sliced per tenant without a retrofit. Powertools propagates appended log
    # keys across Logger instances of the same service, and shares one metric
    # store, so the service layer's records and metrics inherit tenant_id too —
    # no need to thread it through build_greeting.
    # Metadata, NOT add_dimension: a dimension changes the EMF dimension set to
    # {service, tenant_id}, and CloudWatch matches custom metrics on the EXACT
    # dimension set — every consumer that addresses these metrics by {service}
    # alone (the GreetingRequests dashboard widget and the
    # FeatureFlagEvaluationFailure AppConfig rollback alarm in
    # infrastructure/backend_app.py) silently stops seeing data. Metadata rides
    # the same EMF blob, so per-tenant queries still work in Logs Insights
    # (`filter tenant_id = ...`), without forking the metric identity or paying
    # the per-distinct-value custom-metric billing a dimension incurs. A fork
    # that wants true per-tenant metric *streams* must add tenant_id to those
    # consumers' dimensions_map in the same commit — both sides of the
    # telemetry contract move together (pinned by the EMF dimension-set unit
    # test in tests/unit/test_handler.py).
    tenant_id = _resolve_tenant_id(event)
    logger.append_keys(tenant_id=tenant_id)
    tracer.put_annotation(key="tenant_id", value=tenant_id)
    metrics.add_metadata(key="tenant_id", value=tenant_id)

    logger.info(
        "Request received",
        source_ip=source_ip,
        user_agent=user_agent,
        request_id=request_id,
    )

    # Pass source_ip + user_agent as flag-evaluation context so AppConfig rules
    # can match on them (the route's description promises IP-based gating). A
    # GetParameterError on the SSM read surfaces from the service as a domain
    # GreetingUnavailableError; map it to the 500 the OpenAPI spec documents.
    try:
        message = build_greeting(
            ssm_provider=ssm_provider,
            feature_flags=feature_flags,
            param_name=GREETING_PARAM_NAME,
            flag_context={"source_ip": source_ip, "user_agent": user_agent},
        )
    except GreetingUnavailableError as exc:
        raise InternalServerError("Failed to fetch greeting") from exc

    return GreetingResponse(message=message)


@idempotent(config=idempotency_config, persistence_store=persistence_layer)
def _resolve_with_idempotency(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Inner handler wrapped by @idempotent.

    Split out so the outer handler can catch IdempotencyKeyError (raised by
    @idempotent before this body runs when the request has no Idempotency-Key
    header) and return a 400 instead of letting Lambda surface a 500.

    Caching caveat: @idempotent wraps the whole resolver, and Powertools persists
    whatever this function *returns* (only raised exceptions are not cached). The
    APIGatewayRestResolver returns non-2xx outcomes — 404 (unknown route), 422
    (validation), and the 500 produced when ``get_greeting`` raises InternalServerError —
    as response dicts rather than exceptions, so those are cached under the
    client's Idempotency-Key for ``expires_after_seconds`` (1 hour). A client that
    reuses the same key after fixing the route/payload would get the stale error
    replayed. This is acceptable for this single-GET reference (the documented
    contract is one key per logical request — see README "Idempotency keys"); a
    fork that wants transient errors retried under the same key should move
    idempotency onto ``service.build_greeting`` (the success-bearing function) instead
    of the resolver — Powertools supports this directly via
    ``@idempotent_function(data_keyword_argument=..., ...)`` with a
    ``PydanticSerializer`` output serializer, so the cached record stores the
    validated response model rather than a raw dict — accepting that the
    missing-key→400 path then needs a separate guard.
    """
    return cast("dict[str, Any]", app.resolve(event, context))


# correlation_id_path lifts the API Gateway request id into a correlation_id
# field on every log record of the invocation, so one request's records can be
# joined in Logs Insights without relying on the single line that logs
# request_id by hand. capture_response=False keeps response payloads out of
# X-Ray segments — traces record timing and metadata, not body content.
# clear_state=True wipes append_keys state at the start of each invocation:
# tenant_id is appended only once routing succeeds, so without it a request
# rejected *before* the route runs (400/404/422) on a warm container would log
# the previous request's tenant_id — misattributing one tenant's malformed
# traffic to another in per-tenant Logs Insights queries. Keys appended during
# an invocation still propagate to the service layer for that request.
@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST, clear_state=True)
@tracer.capture_lambda_handler(capture_response=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Lambda entry point.

    Resolves the API Gateway event through the router and returns the result.
    Decorated with Powertools Logger, Tracer, Metrics; the inner function
    handles Idempotency so the idempotency layer's caller-caused rejections map
    to meaningful HTTP responses — a missing Idempotency-Key header to a 400, a
    duplicate request whose original is still executing to a 409 — instead of
    unhandled 5xx errors. Infrastructure faults (e.g. the persistence layer's
    DynamoDB errors) deliberately propagate so they surface in the Errors
    metric and X-Ray rather than being flattened into a client-error shape.

    Args:
        event: API Gateway Lambda proxy event.
        context: Lambda runtime context.

    Returns:
        dict: API Gateway Lambda proxy response.
    """
    # Header names are case-insensitive on the wire but the idempotency layer's
    # JMESPath is exact-match, so normalize keys to lowercase once here. The
    # resolver is unaffected — Powertools' event data classes already do
    # case-insensitive header lookups. A copy (not in-place mutation) keeps the
    # caller's event untouched. `or {}` covers manual invocations (console test
    # events, aws lambda invoke) that omit the headers map entirely.
    event = {**event, "headers": {k.lower(): v for k, v in (event.get("headers") or {}).items()}}

    # cast() restores the return type after @idempotent erases it. Powertools'
    # app.resolve() is well-typed in .venv-lambda, but the @idempotent wrapper
    # passes return values through as Any; .venv (CDK side, no Powertools)
    # already sees the function as Any. The cast is a no-op at runtime.
    try:
        return cast("dict[str, Any]", _resolve_with_idempotency(event, context))
    except IdempotencyKeyError:
        logger.warning("Request rejected: missing Idempotency-Key header")
        return {
            "statusCode": 400,
            # This response is built outside the Powertools resolver, so it must
            # carry its own CORS header (the resolver adds one to every response
            # it builds) — without it, cross-origin browsers can't read the 400
            # body at all. Keep in sync with CORSConfig.allow_origin above.
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": '{"message":"Idempotency-Key header is required"}',
            "isBase64Encoded": False,
        }
    except IdempotencyAlreadyInProgressError:
        # Raised when the same Idempotency-Key arrives while the first
        # execution's record is still INPROGRESS — ordinary client behavior
        # (double-click, timeout-retry during a cold start), not a fault.
        # Uncaught it would become an API Gateway 502 with no CORS header and
        # off the OpenAPI contract, and each occurrence would feed the canary
        # alias-errors rollback alarm.
        logger.warning("Request rejected: duplicate request while the original is still in progress")
        return {
            "statusCode": 409,
            # Built outside the resolver — carries its own CORS header, same
            # rule as the 400 above. Keep in sync with CORSConfig.allow_origin.
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": '{"message":"A request with this Idempotency-Key is still in progress; retry shortly"}',
            "isBase64Encoded": False,
        }
