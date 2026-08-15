"""The claude CLI model backend: engaged only when the SDK path is
unavailable, strict about the envelope and the JSON inside it, and clear
about the difference between "not signed in" and "the model failed"."""

import json
import subprocess

import pytest

from vent import llm


class FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def envelope(result: str, is_error: bool = False) -> str:
    return json.dumps({"result": result, "is_error": is_error, "type": "result"})


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def call(monkeypatch, proc: FakeProc, captured: dict | None = None) -> dict:
    monkeypatch.setattr(llm, "_claude_cli", lambda: "/usr/local/bin/claude")

    def fake_run(cmd, **kwargs):
        if captured is not None:
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input", "")
        return proc

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    return llm._complete_via_cli("system", "user text", SCHEMA, "claude-haiku-4-5")


def test_cli_backend_parses_json_result(monkeypatch):
    captured: dict = {}
    out = call(monkeypatch, FakeProc(envelope('{"ok": true}')), captured)
    assert out == {"ok": True}
    # Tools are disabled and print mode is used; this is a completion,
    # not an agent run.
    assert "-p" in captured["cmd"]
    tools_flag = captured["cmd"][captured["cmd"].index("--tools") + 1]
    assert tools_flag == ""
    # The schema travels in the prompt since the CLI cannot enforce it.
    assert '"properties"' in captured["input"]


def test_cli_backend_rejects_non_json_result(monkeypatch):
    with pytest.raises(llm.ModelError, match="non-JSON"):
        call(monkeypatch, FakeProc(envelope("Sure! Here is the JSON you asked for")))


def test_cli_backend_unwraps_a_single_fence(monkeypatch):
    """Chat-tuned models habitually fence their JSON even when told not
    to (observed on the first live call). Exactly one wrapping fence is
    transport framing and is stripped; the content is still parsed
    strictly."""
    fenced = '```json\n{"ok": true}\n```'
    assert call(monkeypatch, FakeProc(envelope(fenced))) == {"ok": True}


def test_cli_backend_rejects_prose_around_fence(monkeypatch):
    noisy = 'Here you go!\n```json\n{"ok": true}\n```'
    with pytest.raises(llm.ModelError, match="non-JSON"):
        call(monkeypatch, FakeProc(envelope(noisy)))


def test_cli_backend_auth_failure_is_unavailable_not_error(monkeypatch):
    proc = FakeProc(envelope("Failed to authenticate: OAuth session expired", is_error=True))
    with pytest.raises(llm.ModelUnavailable, match="claude login"):
        call(monkeypatch, proc)


def test_cli_backend_other_cli_error(monkeypatch):
    proc = FakeProc(envelope("model overloaded", is_error=True))
    with pytest.raises(llm.ModelError, match="overloaded"):
        call(monkeypatch, proc)


def test_cli_backend_nonzero_exit(monkeypatch):
    with pytest.raises(llm.ModelError, match="exited with 2"):
        call(monkeypatch, FakeProc("", returncode=2, stderr="boom"))


def test_complete_json_falls_back_to_cli_without_sdk(monkeypatch):
    """With no anthropic package installed (true in this venv and in CI),
    complete_json must route to the CLI backend when one exists."""
    monkeypatch.setattr(llm, "_claude_cli", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(
        llm.subprocess, "run", lambda cmd, **kw: FakeProc(envelope('{"ok": true}'))
    )
    assert llm.complete_json("s", "u", SCHEMA) == {"ok": True}


def test_complete_json_without_sdk_or_cli_is_unavailable(monkeypatch):
    monkeypatch.setattr(llm, "_claude_cli", lambda: None)
    with pytest.raises(llm.ModelUnavailable, match="claude login"):
        llm.complete_json("s", "u", SCHEMA)
