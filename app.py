#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes three CloudFormation stacks:

  HelloWorldWaf      — WAF WebACL, always in us-east-1 (CloudFront constraint)
  HelloWorld         — Lambda, API Gateway, DynamoDB, SSM, AppConfig
  HelloWorldFrontend — S3, CloudFront (references WAF ARN cross-region via SSM)

The target region for the backend and frontend stacks is controlled by the
``region`` CDK context key. Defaults to us-east-1 if not specified.

Usage:
    cdk deploy --all                          # deploy to us-east-1 (default)
    cdk deploy --all -c region=eu-west-1      # deploy backend/frontend to eu-west-1
                                              # WAF always stays in us-east-1
"""

import aws_cdk as cdk

from hello_world.hello_world_frontend_stack import HelloWorldFrontendStack
from hello_world.hello_world_stack import HelloWorldStack
from hello_world.hello_world_waf_stack import HelloWorldWafStack

app = cdk.App()

# WAF WebACL must always be in us-east-1 — this is an AWS hard requirement
# for CloudFront-scoped WebACLs, regardless of where other resources live.
waf = HelloWorldWafStack(
    app,
    "HelloWorldWaf",
    env=cdk.Environment(region="us-east-1"),
)

# Backend and frontend deploy to the region specified via CDK context.
# Defaults to us-east-1 when no context value is provided.
target_region: str = app.node.try_get_context("region") or "us-east-1"
target_env = cdk.Environment(region=target_region)

backend = HelloWorldStack(app, "HelloWorld", env=target_env)

HelloWorldFrontendStack(
    app,
    "HelloWorldFrontend",
    api_url=backend.api_url,
    waf_acl_arn=waf.web_acl_arn,
    env=target_env,
    # Enables CDK's SSM-based cross-region reference bridging.
    # When target_region == us-east-1 this is a no-op.
    # When target_region differs, CDK writes the WAF ARN into SSM in us-east-1
    # and reads it back in target_region — all managed automatically.
    cross_region_references=True,
)

app.synth()
