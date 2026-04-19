"""Generate the OpenAPI spec for the Hello World API.

Imports the Lambda resolver, calls get_openapi_json_schema() on it, and writes
the result to docs/openapi.json. Runs as a pre-build step for Zensical via
the ``docs`` Make target, so the rendered API reference always reflects the
routes and Pydantic models currently in the code.

After Powertools generates the vanilla OpenAPI 3 spec, a small post-processor
injects a uniform ``x-amazon-apigateway-integration`` block onto every
operation so readers can see the AWS wiring in the published spec, not just
the HTTP interface. The integration URI uses literal placeholders
(``{region}``, ``{lambdaArn}``) — callers who want to import the spec into
their own API Gateway substitute those for real values first. The actual
deployed API is built by CDK in ``HelloWorldApp``; this spec is
documentation-only.

The spec is intentionally generated at build time rather than served at
runtime: exposing it via API Gateway would publish the full API surface to
any caller, which we do not want for a reference service.
"""

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lambda"))

# Importing app.py instantiates a DynamoDB client for the idempotency layer,
# which requires a region. We never make a real AWS call here — we only
# introspect the resolver — so a dummy region satisfies botocore without
# touching any real environment.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Import must follow sys.path mutation so the lambda/ directory is importable.
# pylint: disable=wrong-import-position
from aws_lambda_powertools.event_handler.openapi.models import Server, Tag  # noqa: E402

from app import app  # noqa: E402

# Write into docs/ so Zensical (which treats the docs/ tree as input and
# copies non-markdown assets verbatim into the site) picks it up alongside
# docs/api.html, which references it as a sibling.
OUTPUT_PATH = REPO_ROOT / "docs" / "openapi.json"

DESCRIPTION = """\
Reference serverless API built on AWS Lambda Powertools, deployed behind
API Gateway, CloudFront, and AWS WAF.

The spec on this page is generated at documentation-build time from the
live Pydantic models and route decorators in `lambda/app.py`. Any change
to a route, a request body model, or a return-type annotation appears
here on the next `make docs` run.

Each operation carries an `x-amazon-apigateway-integration` block showing
the AWS_PROXY integration with Lambda that the CDK stack provisions. The
URI uses literal `{region}` and `{lambdaArn}` placeholders — the deployed
API is built by CDK, not imported from this file, so readers can substitute
those if they want to `aws apigateway import-rest-api` the spec elsewhere.
"""

# Standard HTTP verbs recognised as OpenAPI operations. Anything else on a
# path-item (``parameters``, ``summary``, vendor extensions) is skipped.
_HTTP_VERBS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# Uniform AWS_PROXY integration applied to every operation. Matches how
# ``HelloWorldApp`` wires its single Lambda to API Gateway — AWS_PROXY means
# the entire request/response round-trips through the Lambda unchanged. The
# HTTP method in ``httpMethod`` is always ``POST`` for Lambda integrations
# regardless of the caller's verb; that is an API Gateway quirk, not a typo.
_LAMBDA_PROXY_INTEGRATION: dict[str, Any] = {
    "type": "aws_proxy",
    "httpMethod": "POST",
    "uri": "arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/{lambdaArn}/invocations",
    "passthroughBehavior": "when_no_match",
}


def _inject_apigateway_extensions(spec: dict[str, Any]) -> dict[str, Any]:
    """Attach a uniform AWS_PROXY integration block to every operation.

    Intentionally undiscriminating: every path + verb gets the same block.
    Per-route customisation would drift from the CDK stack, since CDK — not
    this post-processor — owns the real API. Keeping it uniform means the
    documented shape can only be wrong in one way at a time (if the stack's
    integration type ever diverges from AWS_PROXY), which is easy to catch.
    """
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for verb, operation in path_item.items():
            if verb.lower() not in _HTTP_VERBS or not isinstance(operation, dict):
                continue
            operation["x-amazon-apigateway-integration"] = copy.deepcopy(_LAMBDA_PROXY_INTEGRATION)
    return spec


def main() -> None:
    # Powertools' ApiGatewayResolver exposes get_openapi_json_schema at runtime
    # but the method is not declared in the current type stubs.
    spec = app.get_openapi_json_schema(  # type: ignore[attr-defined]
        title="Hello World API",
        version="1.0.0",
        description=DESCRIPTION,
        servers=[
            Server(
                url="https://{apiId}.execute-api.{region}.amazonaws.com/prod",
                description="API Gateway stage (substitute your deployed apiId and region)",
            ),
        ],
        tags=[
            Tag(
                name="Greeting",
                description="Endpoints that return the configured greeting.",
            ),
        ],
    )
    # Re-serialize through json to get stable, human-readable formatting that
    # diffs cleanly in PRs if the spec is ever committed.
    spec_dict = _inject_apigateway_extensions(json.loads(spec))
    OUTPUT_PATH.write_text(json.dumps(spec_dict, indent=2) + "\n")


if __name__ == "__main__":
    main()
