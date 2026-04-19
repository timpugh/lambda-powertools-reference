"""HelloWorldStage — groups the WAF, backend, and frontend stacks as one deploy unit.

The three stacks are always deployed together for a given region, so modelling
them as a :class:`cdk.Stage` makes that relationship structural rather than
conventional. A Stage also scopes the synthesised cloud assembly under its
own subdirectory (``cdk.out/assembly-{stage}/``), which keeps multi-region
synths from mixing their templates in the root of ``cdk.out/``.

This change also paves the way for CDK Pipelines (each Stage is the natural
deployment unit) and for a future multi-environment layout (dev/staging/prod
as separate Stage instances under the same App).

Stack names are set explicitly via ``stack_name=`` so the CloudFormation
names stay unchanged (``HelloWorld-{region}`` etc.). Without the override,
wrapping in a Stage would prefix each stack name with the Stage ID, which
would orphan any currently deployed stacks.
"""

from typing import Any

import aws_cdk as cdk
from constructs import Construct

from hello_world.hello_world_frontend_stack import HelloWorldFrontendStack
from hello_world.hello_world_stack import HelloWorldStack
from hello_world.hello_world_waf_stack import HelloWorldWafStack


class HelloWorldStage(cdk.Stage):
    """All three stacks (WAF, backend, frontend) for a single regional deployment.

    The WAF stack is always pinned to ``us-east-1`` (CloudFront-scoped WebACLs
    must live there). The backend and frontend deploy to ``region``. When
    ``region`` differs from ``us-east-1``, ``cross_region_references=True``
    on the frontend stack bridges the WAF ARN through SSM automatically.
    """

    def __init__(self, scope: Construct, construct_id: str, *, region: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        waf_env = cdk.Environment(region="us-east-1")
        target_env = cdk.Environment(region=region)

        waf_stack_name = f"HelloWorldWaf-{region}"
        backend_stack_name = f"HelloWorld-{region}"
        frontend_stack_name = f"HelloWorldFrontend-{region}"

        self.waf = HelloWorldWafStack(
            self,
            waf_stack_name,
            stack_name=waf_stack_name,
            env=waf_env,
        )

        self.backend = HelloWorldStack(
            self,
            backend_stack_name,
            stack_name=backend_stack_name,
            env=target_env,
        )

        self.frontend = HelloWorldFrontendStack(
            self,
            frontend_stack_name,
            stack_name=frontend_stack_name,
            api_url=self.backend.api_url,
            waf_acl_arn=self.waf.web_acl_arn,
            env=target_env,
            # Enables CDK's SSM-based cross-region reference bridging.
            # When region == us-east-1 this is a no-op.
            # When region differs, CDK writes the WAF ARN into SSM in us-east-1
            # and reads it back in the target region — all managed automatically.
            cross_region_references=True,
        )
