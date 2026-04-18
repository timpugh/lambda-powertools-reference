# Lambda Handler

The `app` module in `lambda/app.py` is the Powertools-based request handler
wired into API Gateway. It owns the `/hello` route, Pydantic validation, and
the cross-cutting concerns (idempotency, feature flags, metrics, tracing).

## API reference

::: app
