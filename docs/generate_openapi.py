"""Generate the OpenAPI spec for the Hello World API.

Imports the Lambda resolver, calls get_openapi_json_schema() on it, and writes
the result to docs/openapi.json. Runs as a pre-build step for Sphinx via the
``docs`` Make target, so the rendered API reference always reflects the
routes and Pydantic models currently in the code.

The spec is intentionally generated at build time rather than served at
runtime: exposing it via API Gateway would publish the full API surface to
any caller, which we do not want for a reference service.
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lambda"))

# Importing app.py instantiates a DynamoDB client for the idempotency layer,
# which requires a region. We never make a real AWS call here — we only
# introspect the resolver — so a dummy region satisfies botocore without
# touching any real environment.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Import must follow sys.path mutation so the lambda/ directory is importable.
from app import app  # noqa: E402
from aws_lambda_powertools.event_handler.openapi.models import Server, Tag  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent / "openapi.json"

DESCRIPTION = """\
Reference serverless API built on AWS Lambda Powertools, deployed behind
API Gateway, CloudFront, and AWS WAF.

The spec on this page is generated at documentation-build time from the
live Pydantic models and route decorators in `lambda/app.py`. Any change
to a route, a request body model, or a return-type annotation appears
here on the next `make docs` run.
"""


def main() -> None:
    spec = app.get_openapi_json_schema(
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
    OUTPUT_PATH.write_text(json.dumps(json.loads(spec), indent=2) + "\n")


if __name__ == "__main__":
    main()
