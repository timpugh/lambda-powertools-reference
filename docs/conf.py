"""Sphinx configuration for Lambda Powertools Reference documentation."""

project = "Lambda Powertools Reference"
author = "timpugh"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "myst_parser",
]

# Support both .rst and .md
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# MyST markdown settings
myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

# Autodoc settings
autodoc_member_order = "bysource"
autodoc_typehints = "description"

# Mock heavy Lambda deps so Sphinx can import lambda/app.py without installing them
autodoc_mock_imports = ["aws_lambda_powertools", "aws_xray_sdk", "boto3", "botocore"]

# Output
html_theme = "alabaster"
