#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes the HelloWorld CloudFormation stack.
"""

import aws_cdk as cdk

from hello_world.hello_world_stack import HelloWorldStack

app = cdk.App()
HelloWorldStack(app, "HelloWorld")
app.synth()
