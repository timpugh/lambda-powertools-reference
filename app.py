#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes three CloudFormation stacks per deployment, all scoped by region:

  HelloWorldWaf-{region}      — WAF WebACL, physically in us-east-1 (CloudFront
                                constraint), but named per region so each deployment
                                is fully independent and can be destroyed separately.
  HelloWorld-{region}         — Lambda, API Gateway, DynamoDB, SSM, AppConfig
  HelloWorldFrontend-{region} — S3, CloudFront (references WAF ARN cross-region
                                via SSM when target region differs from us-east-1)

The target region is controlled by the ``region`` CDK context key.
Defaults to us-east-1 if not specified.

Usage:
    cdk deploy --all                            # deploy to us-east-1 (default)
    cdk deploy --all -c region=ap-southeast-1   # deploy a separate Singapore stack set

Each regional deployment is fully independent — destroying one does not affect
any other. All three stacks for a given region are destroyed together:

    cdk destroy --all -c region=ap-southeast-1
"""

import aws_cdk as cdk

from hello_world.hello_world_frontend_stack import HelloWorldFrontendStack
from hello_world.hello_world_stack import HelloWorldStack
from hello_world.hello_world_waf_stack import HelloWorldWafStack

app = cdk.App()

# Backend and frontend deploy to the region specified via CDK context.
# Defaults to us-east-1 when no context value is provided.
target_region: str = app.node.try_get_context("region") or "us-east-1"
target_env = cdk.Environment(region=target_region)

# Each regional deployment gets its own WAF WebACL, named by region so stack
# sets are fully independent. The WebACL is always physically in us-east-1
# (CloudFront hard requirement) regardless of the target region.
waf = HelloWorldWafStack(
    app,
    f"HelloWorldWaf-{target_region}",
    env=cdk.Environment(region="us-east-1"),
)

backend = HelloWorldStack(app, f"HelloWorld-{target_region}", env=target_env)

HelloWorldFrontendStack(
    app,
    f"HelloWorldFrontend-{target_region}",
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
