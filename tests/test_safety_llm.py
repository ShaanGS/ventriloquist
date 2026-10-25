"""The claude CLI fallback runs as a plain completion, outside the user's
own Claude Code setup. subprocess.run is faked: no CLI runs and no model
is called."""

import json
import os

import pytest

from vent import llm

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def run_cli(monkeypatch, proc: FakeProc) -> dict:
    """Make one CLI-backed call and return what subprocess.run was given."""
    seen: dict = {}
    monkeypatch.setattr(llm, "_claude_cli", lambda: "/usr/local/bin/claude")

    def fake_run(cmd, **kwargs):
        cwd = kwargs.get("cwd")
        seen.update(cmd=cmd, cwd=cwd, cwd_was_empty=bool(cwd) and os.path.isdir(cwd) and not os.listdir(cwd))
        return proc

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    seen["result"] = llm._complete_via_cli("system", "user text", SCHEMA, "claude-haiku-4-5")
    return seen


def ok_proc() -> FakeProc:
    return FakeProc(json.dumps({"type": "result", "is_error": False, "result": '{"ok": true}'}))


def test_the_fallback_loads_none_of_the_users_setup(monkeypatch):
    """Without these flags the call ran as a session of the user's Claude
    Code: their MCP servers started, their hooks saw the app snapshot, and
    their memory files went into the prompt."""
    cmd = run_cli(monkeypatch, ok_proc())["cmd"]
    assert cmd[:2] == ["/usr/local/bin/claude", "-p"]
    assert "--safe-mode" in cmd  # no CLAUDE.md, skills, plugins, hooks or MCP servers
    assert "--strict-mcp-config" in cmd and "--mcp-config" not in cmd  # so no MCP servers at all
    assert cmd[cmd.index("--setting-sources") + 1] == ""  # no settings files
    assert "--no-session-persistence" in cmd  # no transcript of app content
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--bare" not in cmd  # it would refuse the Claude Code login this backend exists for


def test_the_fallback_runs_in_a_fresh_empty_directory(monkeypatch):
    """Run from the user's project, the CLI would pick up its CLAUDE.md,
    .claude settings and saved memory."""
    seen = run_cli(monkeypatch, ok_proc())
    assert seen["result"] == {"ok": True}
    assert seen["cwd_was_empty"]
    assert os.path.realpath(seen["cwd"]) != os.path.realpath(os.getcwd())
    assert not os.path.exists(seen["cwd"])  # and it is cleaned up afterwards


def test_a_cli_too_old_for_the_flags_fails_closed(monkeypatch):
    """Running without the isolation flags would bring the user's hooks and
    MCP servers back, so an old CLI is reported as unavailable instead."""
    proc = FakeProc(returncode=1, stderr="error: unknown option '--safe-mode'")
    with pytest.raises(llm.ModelUnavailable, match="claude update"):
        run_cli(monkeypatch, proc)
