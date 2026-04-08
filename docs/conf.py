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

# Output
html_theme = "alabaster"
