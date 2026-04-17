HTTP API Reference
==================

The reference below documents the HTTP contract of the Hello World API —
paths, methods, request and response schemas, and status codes. It is the
companion to the Python module documentation and is intended for **callers
of the API**, not for developers extending the codebase.

The contents are generated from the live Pydantic models and route decorators
in ``lambda/app.py`` at documentation-build time by
``docs/generate_openapi.py``. The spec therefore stays in sync with the code
automatically — any change to a route, a request body model, or a return-type
annotation appears here on the next ``make docs`` run.

The spec is **not** served at runtime by the deployed API. Publishing the
full API surface via a public ``/openapi.json`` endpoint would expose every
path and field name to unauthenticated callers; the build-time approach
keeps the reference available for internal documentation without leaking
it to the open internet.

.. openapi:: openapi.json
