"""CLI behavior that needs no live app. The MCP client entry matters most:
Claude Desktop and Cursor start servers without the user's shell PATH, so
the printed command must be an absolute path that survives being pasted."""

import json

from click.testing import CliRunner

from vent import __version__, cli, harness

from .fakes import on_another_space, textedit_like


def test_version_flag_reports_the_package_version():
    result = CliRunner().invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_explore_refuses_a_bundle_id_that_would_leave_its_directory(monkeypatch):
    """The trace is saved under the app's bundle id, which the app sets."""
    monkeypatch.setattr(cli.ax, "find_app", lambda name: cli.ax.RunningApp("Evil", "../../evil", 1))

    def never(app):
        raise AssertionError("explore went on to read the app")

    monkeypatch.setattr(cli.ax, "app_element", never)
    result = CliRunner().invoke(cli.main, ["explore", "Evil"])
    assert result.exit_code == 1
    assert "not reverse-DNS" in result.output


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


def test_doctor_reaches_a_window_on_another_space(monkeypatch):
    """Off its Space an app lists no window, and doctor called Outlook
    "0 elements ... No usable tree" while it was open on another desktop."""
    monkeypatch.setattr(cli.ax, "is_trusted", lambda: True)
    monkeypatch.setattr(cli.ax, "find_app", lambda name: cli.ax.RunningApp("TextEdit", "com.apple.TextEdit", 1))
    monkeypatch.setattr(cli.ax, "app_element", lambda app: on_another_space(textedit_like()))
    monkeypatch.setattr(harness, "enable_web_accessibility", lambda root: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    result = CliRunner().invoke(cli.main, ["doctor", "TextEdit"])
    assert result.exit_code == 0
    assert "TextEdit: 4 elements" in result.output
    assert "another Space" in result.output


def test_doctor_asks_for_a_window_when_the_app_has_none(monkeypatch):
    """With its mail window closed, Outlook lists no window at all, and
    doctor said it "cannot be packed yet" though it packs fine once a
    window is open."""
    tree = textedit_like()
    tree._children.clear()
    monkeypatch.setattr(cli.ax, "is_trusted", lambda: True)
    monkeypatch.setattr(cli.ax, "session_locked", lambda: False)
    monkeypatch.setattr(cli.ax, "find_app", lambda name: cli.ax.RunningApp("TextEdit", "com.apple.TextEdit", 1))
    monkeypatch.setattr(cli.ax, "app_element", lambda app: tree)
    monkeypatch.setattr(harness, "enable_web_accessibility", lambda root: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    result = CliRunner().invoke(cli.main, ["doctor", "TextEdit"])
    assert result.exit_code == 1
    assert "TextEdit is running but has no open window" in result.output
    assert "cannot be packed" not in result.output
