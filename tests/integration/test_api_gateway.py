"""Integration tests for the API Gateway endpoint."""

import os

import boto3
import pytest
import requests


class TestApiGateway:
    @pytest.fixture
    def api_gateway_url(self):
        """Get the API Gateway URL from Cloudformation Stack outputs"""
        stack_name = os.environ.get("AWS_SAM_STACK_NAME")

        if stack_name is None:
            raise ValueError("Please set the AWS_SAM_STACK_NAME environment variable to the name of your stack")

        client = boto3.client("cloudformation")

        try:
            response = client.describe_stacks(StackName=stack_name)
        except Exception as e:
            raise Exception(
                f'Cannot find stack {stack_name} \nPlease make sure a stack with the name "{stack_name}" exists'
            ) from e

        stacks = response["Stacks"]
        stack_outputs = stacks[0]["Outputs"]
        api_outputs = [output for output in stack_outputs if output["OutputKey"] == "HelloWorldApiOutput"]

        if not api_outputs:
            raise KeyError(f"HelloWorldApiOutput not found in stack {stack_name}")

        return api_outputs[0]["OutputValue"]

    def test_api_gateway(self, api_gateway_url):
        """Call the API Gateway endpoint and check the response"""
        response = requests.get(api_gateway_url, timeout=10)

        assert response.status_code == 200
        assert response.json() == {"message": "hello world"}

    def test_api_gateway_response_headers(self, api_gateway_url):
        """Verify the response returns correct content type"""
        response = requests.get(api_gateway_url, timeout=10)

        assert response.headers["Content-Type"] == "application/json"

    def test_api_gateway_response_time(self, api_gateway_url):
        """Verify the API responds within a reasonable time"""
        response = requests.get(api_gateway_url, timeout=10)

        assert response.elapsed.total_seconds() < 5.0
