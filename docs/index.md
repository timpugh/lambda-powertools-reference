---
icon: lucide/book-open
---

# Lambda Powertools Reference

A reference serverless API built on AWS Lambda Powertools, deployed behind
API Gateway, CloudFront, and AWS WAF. This site covers two audiences:

## Code reference (for developers)

Autodoc-rendered pages for every Python module in the project, generated
from the Google-style docstrings in the source via
[mkdocstrings](https://mkdocstrings.github.io/):

- [Lambda handler](lambda_handler.md) — the Powertools route handler, Pydantic models, and cross-cutting concerns.
- [Backend stack](cdk_stack.md) — API Gateway, Lambda, DynamoDB, SSM, AppConfig.
- [WAF stack](hello_world_waf_stack.md) — us-east-1 WebACL attached to CloudFront.
- [Frontend stack](hello_world_frontend_stack.md) — CloudFront, S3 access logs, Glue + Athena analytics.
- [NAG utilities](nag_utils.md) — cdk-nag suppression helpers shared across stacks.

## API reference (for callers)

A standalone [Scalar](https://github.com/scalar/scalar) API Reference page
that renders the OpenAPI spec in the browser:

- [**HTTP API Reference**](api.html) — paths, request / response schemas, status codes, and an interactive request sandbox.

The OpenAPI spec itself is published as [openapi.json](openapi.json) if a
caller wants to point their own tooling at it. Both files are regenerated
from the live Pydantic models in `lambda/app.py` on every docs build, so
what you see here always reflects the code currently on `main`.
