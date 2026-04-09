#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes the HelloWorld backend and frontend CloudFormation stacks.
"""

import aws_cdk as cdk

from hello_world.hello_world_frontend_stack import HelloWorldFrontendStack
from hello_world.hello_world_stack import HelloWorldStack

app = cdk.App()

# Both stacks are pinned to us-east-1. CloudFront WAF WebACLs (scope=CLOUDFRONT)
# must be deployed in us-east-1 regardless of where other resources live —
# this is an AWS service constraint, not a project preference.
env = cdk.Environment(region="us-east-1")

backend = HelloWorldStack(app, "HelloWorld", env=env)

HelloWorldFrontendStack(
    app,
    "HelloWorldFrontend",
    api_url=backend.api_url,
    env=env,
)

app.synth()
