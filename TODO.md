# TODO

Items that would improve this project for production use but are not yet implemented.

## Infrastructure

- [ ] **Multi-environment CDK stacks** — separate dev/staging/prod stacks with environment-specific config (SSM paths, AppConfig environments, DynamoDB table names)
- [ ] **API Gateway throttling** — add rate limiting and burst limits to prevent abuse
- [ ] **WAF** — attach AWS WAF to the API Gateway to block common attack patterns
- [ ] **SSM SecureString** — store the greeting parameter as a `SecureString` (KMS-encrypted) rather than plaintext
- [ ] **Parameterise the SSM path** — pass the parameter path through CDK context rather than hardcoding `/HelloWorld/greeting`
- [ ] **AppConfig initial value management** — manage the feature flag hosted configuration outside the CDK stack so it can be updated independently of a deployment

## Observability

- [ ] **CloudWatch alarms** — add alarms for Lambda error rate, p99 latency, and DynamoDB throttles, with SNS notifications
- [ ] **Dead letter queue (DLQ)** — configure a DLQ on the Lambda function to capture failed invocations
- [ ] **Structured error reporting** — integrate with an error tracking service (e.g. Sentry) for aggregated error visibility

## CI/CD

- [ ] **Deploy workflow** — GitHub Actions workflow to run `cdk deploy` on merge to `main` (deliberately deferred)
- [ ] **CDK diff on PRs** — run `cdk diff` in CI on pull requests to surface infrastructure changes before merge
- [ ] **Integration tests in CI** — run integration tests against a deployed dev stack as part of the CI pipeline

## Security

- [ ] **API Gateway authentication** — add an API key, IAM auth, or Cognito authorizer to restrict access
- [ ] **Lambda least-privilege IAM** — tighten the Lambda execution role to the minimum required permissions per resource
- [ ] **VPC placement** — place the Lambda function inside a VPC if it needs to access private resources

## Code

- [ ] **Input validation** — validate and sanitise query string parameters and request body on the `/hello` route
- [ ] **Contributing guide** — `CONTRIBUTING.md` with fork/branch/PR workflow and pre-commit setup instructions
- [ ] **Changelog** — auto-generated `CHANGELOG.md` from conventional commit history using `conventional-changelog`
