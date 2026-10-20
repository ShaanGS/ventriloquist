"""The Anthropic SDK model backend: the request it sends, and the errors a
user sees when the SDK is missing or the answer is cut short. A fake
anthropic module stands in for the real one, so these run with no network
and no credentials."""

import sys
import types

import pytest

from vent import llm

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def fake_anthropic(stop_reason: str, text: str, captured: dict) -> types.ModuleType:
    module = types.ModuleType("anthropic")

    class AnthropicError(Exception):
        pass

    class APIError(AnthropicError):
        pass

    class AuthenticationError(APIError):
        pass

    class Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            block = types.SimpleNamespace(type="text", text=text)
            return types.SimpleNamespace(stop_reason=stop_reason, content=[block])

    class Anthropic:
        def __init__(self):
            self.messages = Messages()

    module.AnthropicError = AnthropicError
    module.APIError = APIError
    module.AuthenticationError = AuthenticationError
    module.Anthropic = Anthropic
    return module


def test_sdk_backend_parses_structured_output(monkeypatch):
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic("end_turn", '{"ok": true}', captured))
    assert llm.complete_json("system", "user text", SCHEMA) == {"ok": True}
    assert captured["max_tokens"] == llm.MAX_OUTPUT_TOKENS
    assert captured["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}


def test_sdk_backend_reports_a_truncated_answer(monkeypatch):
    """A cut-off answer is half a JSON object. Saying it hit the output
    limit is more useful than a parse error about invalid JSON."""
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic("max_tokens", '{"ok": tr', {}))
    with pytest.raises(llm.ModelError, match="output limit"):
        llm.complete_json("system", "user text", SCHEMA)


def test_missing_sdk_hint_works_for_tool_installs(monkeypatch):
    """vent is usually installed with `uv tool install` or `pipx`, whose
    environments `pip install` never reaches, so the hint has to name the
    commands that add the SDK to vent's own environment."""
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setattr(llm, "_claude_cli", lambda: None)
    with pytest.raises(llm.ModelUnavailable) as excinfo:
        llm.complete_json("system", "user text", SCHEMA)
    message = str(excinfo.value)
    assert "uv tool install --with anthropic ventriloquist-mcp" in message
    assert "pipx inject ventriloquist-mcp anthropic" in message
    assert "claude login" in message
