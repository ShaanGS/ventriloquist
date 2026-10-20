"""Release metadata the official MCP registry checks. It rejects a server
whose version differs from the package it points at, a description over
100 characters, and a PyPI package whose description (this README) lacks
the server's mcp-name line."""

import json
from pathlib import Path

import vent

REPO = Path(__file__).resolve().parent.parent


def server_json() -> dict:
    return json.loads((REPO / "server.json").read_text())


def test_server_json_versions_match_the_package():
    server = server_json()
    assert server["version"] == vent.__version__
    assert [package["version"] for package in server["packages"]] == [vent.__version__]


def test_server_json_description_fits_the_registry_limit():
    assert len(server_json()["description"]) <= 100


def test_readme_names_the_server_for_pypi_ownership_checks():
    readme = (REPO / "README.md").read_text()
    assert f"<!-- mcp-name: {server_json()['name']} -->" in readme
