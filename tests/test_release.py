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


def test_the_plugin_ships_the_same_skill_and_version():
    plugin = REPO / "integrations" / "claude-plugin"
    assert (plugin / "skills" / "ventriloquist" / "SKILL.md").read_text() == (REPO / "SKILL.md").read_text()
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "ventriloquist" and manifest["version"] == vent.__version__
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert market["plugins"][0]["source"] == "./integrations/claude-plugin"
    server = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["ventriloquist"]
    assert f"ventriloquist-mcp>={vent.__version__}" in server["args"]
