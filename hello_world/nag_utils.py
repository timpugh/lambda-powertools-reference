"""Shared cdk-nag helpers.

``apply_compliance_aspects`` applies the full available rule-pack set to a
stack so every stack exercises the same compliance gauntlet. NIST 800-53 R4
is intentionally omitted — R5 supersedes it and running both would duplicate
findings on overlapping controls.

``CDK_LAMBDA_SUPPRESSIONS`` is the canonical suppression list for CDK-managed
singleton Lambdas (AwsCustomResource provider, LogRetention, BucketDeployment,
S3AutoDeleteObjects). Their runtime, memory, tracing, DLQ, VPC, and IAM
policies are all managed by CDK and cannot be configured by the caller.
Import it and pass it to ``NagSuppressions.add_resource_suppressions_by_path``
or ``NagSuppressions.add_resource_suppressions`` with ``apply_to_children=True``.
"""

from aws_cdk import Aspects, Stack
from cdk_nag import (
    AwsSolutionsChecks,
    HIPAASecurityChecks,
    NIST80053R5Checks,
    PCIDSS321Checks,
    ServerlessChecks,
)


def apply_compliance_aspects(stack: Stack) -> None:
    """Attach every cdk-nag rule pack this project runs to ``stack``."""
    Aspects.of(stack).add(AwsSolutionsChecks(verbose=True))
    Aspects.of(stack).add(ServerlessChecks(verbose=True))
    Aspects.of(stack).add(NIST80053R5Checks(verbose=True))
    Aspects.of(stack).add(HIPAASecurityChecks(verbose=True))
    Aspects.of(stack).add(PCIDSS321Checks(verbose=True))


CDK_LAMBDA_SUPPRESSIONS = [
    {"id": "AwsSolutions-IAM4", "reason": "CDK-managed singleton Lambda uses AWS managed execution role"},
    {"id": "AwsSolutions-IAM5", "reason": "CDK-managed singleton Lambda uses wildcard in auto-generated policy"},
    {"id": "AwsSolutions-L1", "reason": "CDK-managed singleton Lambda runtime is not configurable"},
    {"id": "Serverless-LambdaTracing", "reason": "CDK-managed singleton Lambda — tracing is not configurable"},
    {"id": "Serverless-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {"id": "Serverless-LambdaDefaultMemorySize", "reason": "CDK-managed singleton Lambda — memory is not configurable"},
    {"id": "Serverless-LambdaLatestVersion", "reason": "CDK-managed singleton Lambda runtime is not configurable"},
    {"id": "NIST.800.53.R5-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "NIST.800.53.R5-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {
        "id": "NIST.800.53.R5-LambdaConcurrency",
        "reason": "CDK-managed singleton Lambda — concurrency is not configurable",
    },
    {"id": "NIST.800.53.R5-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
    {"id": "HIPAA.Security-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "HIPAA.Security-LambdaDLQ", "reason": "CDK-managed singleton Lambda — DLQ is not configurable"},
    {
        "id": "HIPAA.Security-LambdaConcurrency",
        "reason": "CDK-managed singleton Lambda — concurrency is not configurable",
    },
    {"id": "HIPAA.Security-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
    {"id": "PCI.DSS.321-IAMNoInlinePolicy", "reason": "CDK-generated inline policy on singleton service role"},
    {"id": "PCI.DSS.321-LambdaInsideVPC", "reason": "CDK-managed singleton Lambda — VPC is not configurable"},
]
