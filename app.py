#!/usr/bin/env python3
"""CDK application entry point.

Synthesizes a :class:`HelloWorldStage` per target region. Each Stage groups
the three stacks that make up one regional deployment (WAF, backend,
frontend) so ``cdk deploy`` treats them as a single unit:

  HelloWorldWaf-{region}      — WAF WebACL, physically in us-east-1
                                (CloudFront constraint), but named per region
                                so each Stage is fully independent and can
                                be destroyed separately.
  HelloWorld-{region}         — Lambda, API Gateway, DynamoDB, SSM, AppConfig
  HelloWorldFrontend-{region} — S3, CloudFront (references WAF ARN cross-region
                                via SSM when target region differs from us-east-1)

The target region is controlled by the ``region`` CDK context key.
Defaults to us-east-1 if not specified.

Usage:
    cdk deploy --all                            # deploy to us-east-1 (default)
    cdk deploy --all -c region=ap-southeast-1   # deploy a separate Singapore Stage

Each regional Stage is fully independent — destroying one does not affect
any other. All three stacks for a given region are destroyed together:

    cdk destroy --all -c region=ap-southeast-1
"""

import aws_cdk as cdk

from hello_world.hello_world_stage import HelloWorldStage

app = cdk.App()

# Target region for the backend and frontend stacks. Defaults to us-east-1
# when no context value is provided. WAF is always pinned to us-east-1
# inside the Stage regardless of this value.
target_region: str = app.node.try_get_context("region") or "us-east-1"

HelloWorldStage(app, f"HelloWorld-{target_region}-stage", region=target_region)

app.synth()
