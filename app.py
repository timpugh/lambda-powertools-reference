#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes the HelloWorld backend and frontend CloudFormation stacks.
"""

import aws_cdk as cdk

from hello_world.hello_world_frontend_stack import HelloWorldFrontendStack
from hello_world.hello_world_stack import HelloWorldStack

app = cdk.App()

backend = HelloWorldStack(app, "HelloWorld")

HelloWorldFrontendStack(
    app,
    "HelloWorldFrontend",
    api_url=backend.api_url,
)

app.synth()
