"""Shared cdk-nag suppression helpers for CDK-managed singleton Lambdas.

CDK creates several singleton Lambda functions internally (AwsCustomResource
provider, LogRetention, BucketDeployment, S3AutoDeleteObjects). These Lambdas
share the same limitations: their runtime, memory, tracing, DLQ, VPC, and IAM
policies are all managed by CDK and cannot be configured by the caller.

``CDK_LAMBDA_SUPPRESSIONS`` is the canonical suppression list for these
singletons. Import it and pass it to
``NagSuppressions.add_resource_suppressions_by_path`` or
``NagSuppressions.add_resource_suppressions`` with ``apply_to_children=True``.
"""

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
]
