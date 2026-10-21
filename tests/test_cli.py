"""CLI behavior that needs no live app. The MCP client entry matters most:
Claude Desktop and Cursor start servers without the user's shell PATH, so
the printed command must be an absolute path that survives being pasted."""

import json

from click.testing import CliRunner

from vent import __version__, cli


def test_version_flag_reports_the_package_version():
    result = CliRunner().invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_mcp_config_names_the_running_vent_by_absolute_path(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["/opt/tools/bin/vent", "mcp-config"])
    result = CliRunner().invoke(cli.main, ["mcp-config"])
    assert result.exit_code == 0
    entry = json.loads(result.stdout)["mcpServers"]["ventriloquist"]
    assert entry == {"command": "/opt/tools/bin/vent", "args": ["serve"]}


def test_mcp_config_shell_quotes_a_path_with_spaces(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["/Users/a b/tools/vent", "mcp-config"])
    result = CliRunner().invoke(cli.main, ["mcp-config"])
    assert json.loads(result.stdout)["mcpServers"]["ventriloquist"]["command"] == "/Users/a b/tools/vent"
    assert "-- '/Users/a b/tools/vent' serve" in result.stderr


def test_doctor_without_permission_names_the_app_to_trust(monkeypatch):
    """macOS trusts the app that launched vent, so for an MCP client that
    is the client itself, not the terminal."""
    monkeypatch.setattr(cli.ax, "is_trusted", lambda: False)
    monkeypatch.setattr(cli.ax, "_process_chain", lambda: [])
    result = CliRunner().invoke(cli.main, ["doctor"])
    assert result.exit_code == 1
    assert "MCP client" in result.output


def test_doctor_points_claude_desktop_users_at_vents_python(monkeypatch):
    """A terminal can pass doctor while Claude Desktop still fails: it
    checks vent's Python, not the terminal."""
    monkeypatch.setattr(cli.ax, "is_trusted", lambda: True)
    monkeypatch.setattr(cli, "_vent_python", lambda: "/opt/tools/python/bin/python3.14")
    result = CliRunner().invoke(cli.main, ["doctor"])
    assert result.exit_code == 0
    assert "Claude Desktop" in result.output
    assert "/opt/tools/python/bin/python3.14" in result.output


def test_mcp_config_tells_claude_desktop_users_what_differs(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["/opt/tools/bin/vent", "mcp-config"])
    monkeypatch.setattr(cli, "_vent_python", lambda: "/opt/tools/python/bin/python3.14")
    result = CliRunner().invoke(cli.main, ["mcp-config"])
    assert "Claude Desktop works differently" in result.stderr
    assert "while it is closed" in result.stderr
    assert "/opt/tools/python/bin/python3.14" in result.stderr
