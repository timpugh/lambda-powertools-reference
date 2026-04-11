"""Integration tests for the CloudFront / S3 frontend distribution.

These tests require a deployed HelloWorldFrontend stack. They are skipped
automatically when the stack cannot be found, so the standard ``pytest`` run
(unit tests) stays green without a live deployment.

To run frontend integration tests explicitly:

    AWS_FRONTEND_STACK_NAME=HelloWorldFrontend-us-east-1 pytest tests/integration/

Override for a different region:

    AWS_FRONTEND_STACK_NAME=HelloWorldFrontend-ap-southeast-1 pytest tests/integration/
"""

import os

import boto3
import pytest
import requests


class TestFrontend:
    @pytest.fixture
    def cloudfront_url(self):
        """Resolve the CloudFront distribution URL from the frontend stack outputs.

        Skips the test if the stack is not deployed rather than failing, so the
        test suite stays green in environments without a live deployment.
        """
        stack_name = os.environ.get("AWS_FRONTEND_STACK_NAME")

        if stack_name is None:
            pytest.skip("AWS_FRONTEND_STACK_NAME not set — skipping frontend integration tests")

        client = boto3.client("cloudformation")

        try:
            response = client.describe_stacks(StackName=stack_name)
        except Exception:
            pytest.skip(f"Stack '{stack_name}' not found — skipping frontend integration tests")

        outputs = response["Stacks"][0].get("Outputs", [])
        url_outputs = [o for o in outputs if o["OutputKey"] == "CloudFrontDomainName"]

        if not url_outputs:
            pytest.skip(f"CloudFrontDomainName not found in stack '{stack_name}' — skipping frontend integration tests")

        return url_outputs[0]["OutputValue"]

    def test_index_html_serves_successfully(self, cloudfront_url):
        """CloudFront should return 200 with HTML for the root path."""
        response = requests.get(cloudfront_url, timeout=15)

        assert response.status_code == 200
        assert "text/html" in response.headers.get("Content-Type", "")

    def test_config_json_contains_api_url(self, cloudfront_url):
        """config.json is generated at deploy time with the injected API Gateway URL."""
        response = requests.get(f"{cloudfront_url}/config.json", timeout=15)

        assert response.status_code == 200
        data = response.json()
        assert "apiUrl" in data
        assert data["apiUrl"].startswith("https://")

    def test_distribution_url_is_https(self, cloudfront_url):
        """The CloudFront domain name output should already be an HTTPS URL."""
        assert cloudfront_url.startswith("https://")

    def test_security_headers_present(self, cloudfront_url):
        """CloudFront ResponseHeadersPolicy.SECURITY_HEADERS adds standard headers."""
        response = requests.get(cloudfront_url, timeout=15)
        headers = {k.lower(): v for k, v in response.headers.items()}

        assert "x-content-type-options" in headers
        assert "x-frame-options" in headers

    def test_unknown_path_returns_spa_fallback(self, cloudfront_url):
        """CloudFront error responses return index.html (200) for SPA client-side routing."""
        response = requests.get(f"{cloudfront_url}/some/deep/client/route", timeout=15)

        assert response.status_code == 200
        assert "text/html" in response.headers.get("Content-Type", "")
