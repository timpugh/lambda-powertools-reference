# Lambda Powertools Reference Documentation

Welcome to the Lambda Powertools Reference project documentation.

```{toctree}
:maxdepth: 2
:caption: Code reference (for developers)

lambda_handler
cdk_stack
hello_world_waf_stack
hello_world_frontend_stack
nag_utils
```

## API reference (for callers)

The HTTP API surface — paths, request/response schemas, status codes — is
rendered by [Redoc](https://github.com/Redocly/redoc) from the OpenAPI spec
that `docs/generate_openapi.py` produces at documentation-build time:

- **[HTTP API Reference](api.html)** — interactive three-panel Redoc page

The spec itself is also published as [openapi.json](openapi.json) if a caller
wants to point their own tooling at it.
