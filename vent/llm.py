"""The single home for every model call.

This is the only file in the codebase that talks to the network besides the
accessibility bridge, which is the point: SECURITY.md T6 promises auditors
exactly one file to read. The dependency rule from ARCHITECTURE.md section 6
holds in the other direction too; runtime.py and server.py never import this
module.

Model roles come from the architecture doc: exploration and synthesis run on
a Sonnet-class model, healing runs on a fast model. The ids live here and
nowhere else so an upgrade is a one-line change.

Untrusted content: anything read from an app's accessibility tree (labels,
values, window titles) is attacker-influenced text. Every prompt built here
wraps that content in delimiters and says what it is, so the model can tell
app content from instructions (SECURITY.md T5). Model output is parsed
against a JSON schema through the API's structured output support; anything
else is rejected, not coerced.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Optional

# Prompt builders (explorer, compiler, heal) call it as llm.wrap_untrusted.
from .untrusted import wrap_untrusted

EXPLORER_MODEL = "claude-sonnet-5"
HEALER_MODEL = "claude-haiku-4-5"

# Thinking is on by default for the explorer model and counts against this
# limit, so it needs headroom beyond the JSON itself.
MAX_OUTPUT_TOKENS = 16000


class ModelError(RuntimeError):
    """A model call failed or returned something outside its schema."""


class ModelUnavailable(ModelError):
    """No credentials, or the client cannot be constructed."""


# Test seam: tests install a fake completer here so every module above this
# one runs without network, credentials, or nondeterminism. Production code
# never touches this.
_completer_override: Optional[Callable[..., dict]] = None


def set_completer_for_tests(completer: Optional[Callable[..., dict]]) -> None:
    global _completer_override
    _completer_override = completer


def _claude_cli() -> Optional[str]:
    """Path to the Claude Code CLI, or None. A separate function so tests
    can pretend it is absent or present."""
    return shutil.which("claude")


def _cli_argv(system: str, model: str) -> list[str]:
    """The `claude` command line for one isolated completion.

    Print mode with tools disabled is a plain completion: one request in,
    one response out, nothing executed. The rest keeps the call out of the
    user's own Claude Code setup, which would otherwise start their MCP
    servers, run their hooks on app content and load their memory files:
    safe mode leaves out CLAUDE.md, skills, plugins, hooks and MCP servers;
    --strict-mcp-config, with no --mcp-config, loads no MCP servers from
    anywhere either; an empty --setting-sources reads no user, project or
    local settings files; and --no-session-persistence keeps the prompt,
    which holds app content, out of the saved sessions. --bare would do
    more, but it only accepts an API key, and this backend exists for
    machines that have a Claude Code login and no key. The flags were
    checked against `claude --help` for Claude Code 2.1.296.
    """
    return [
        _claude_cli() or "claude",
        "-p",
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--system-prompt", system,
        "--safe-mode",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
    ]


def _complete_via_cli(system: str, user_text: str, schema: dict, model: str) -> dict:
    """One model call through `claude -p`, for machines with a Claude Code
    login but no API key.

    The call runs isolated (see _cli_argv), in a fresh empty directory, so
    there is no project CLAUDE.md, settings or memory for it to find. The
    CLI cannot enforce a response schema server-side the way the API's
    structured output does, so the schema travels in the prompt and the
    response is parsed strictly. One transport allowance: chat-tuned models
    habitually wrap the object in a single markdown fence even when told
    not to (observed on the first live call), so exactly that wrapper is
    stripped before parsing. Anything else that is not the JSON object is
    rejected, never repaired.
    """
    prompt = (
        f"{user_text}\n\n"
        "Respond with a single JSON object conforming exactly to this JSON "
        "Schema. Output only the JSON object itself: no prose, no markdown, "
        "no code fences.\n"
        f"{json.dumps(schema)}"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="vent-claude-") as scratch:
            proc = subprocess.run(
                _cli_argv(system, model),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=scratch,
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ModelError(f"claude CLI call failed: {exc}") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "unknown option" in stderr:
            # Failing closed: without these flags the call would run inside
            # the user's own setup, hooks and MCP servers included.
            raise ModelUnavailable(
                f"The installed claude CLI is too old for the flags vent runs it with "
                f"({stderr[:200]}). Run `claude update`, then retry."
            )
        raise ModelError(f"claude CLI exited with {proc.returncode}: {stderr[:300]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ModelError(f"claude CLI returned a malformed envelope: {exc}") from exc

    result_text = str(envelope.get("result", ""))
    if envelope.get("is_error"):
        lowered = result_text.lower()
        if "auth" in lowered or "login" in lowered:
            raise ModelUnavailable(
                f"The claude CLI is installed but not signed in ({result_text}). "
                "Run `claude login` once, then retry."
            )
        raise ModelError(f"claude CLI reported an error: {result_text[:300]}")

    body = result_text.strip()
    if body.startswith("```"):
        first_newline = body.find("\n")
        if first_newline != -1 and body.endswith("```"):
            body = body[first_newline + 1 : -3].strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ModelError(
            f"Model returned non-JSON despite instructions: {result_text[:200]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ModelError(f"Model returned {type(parsed).__name__}, expected a JSON object")
    return parsed


def complete_json(
    system: str,
    user_text: str,
    schema: dict,
    model: str = EXPLORER_MODEL,
) -> dict:
    """One model call, returning a dict validated against the schema.

    Backends, in order: the Anthropic SDK when it has credentials (the
    API's structured output guarantees the response parses), then the
    Claude Code CLI when one is installed and signed in (subscription
    auth, no API key needed; schema held to by strict parsing). A response
    outside its schema raises ModelError rather than being repaired.
    """
    if _completer_override is not None:
        return _completer_override(system=system, user_text=user_text, schema=schema, model=model)

    try:
        import anthropic
    except ImportError as exc:
        if _claude_cli():
            return _complete_via_cli(system, user_text, schema, model)
        raise ModelUnavailable(
            "The anthropic package is not installed in vent's environment and "
            "no claude CLI was found. Reinstall vent with it "
            "(`uv tool install --with anthropic ventriloquist-mcp`, or "
            "`pipx inject ventriloquist-mcp anthropic`), or install Claude "
            "Code and run `claude login`."
        ) from exc

    try:
        # Construction raises anthropic.AnthropicError when no credential
        # resolves (env var or `ant auth login` profile), so wrap it.
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        if _claude_cli():
            return _complete_via_cli(system, user_text, schema, model)
        raise ModelUnavailable(
            "No working Anthropic credentials. Export ANTHROPIC_API_KEY, run "
            "`ant auth login`, or install Claude Code and run `claude login`."
        ) from exc

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user_text}],
        )
    except anthropic.AuthenticationError as exc:
        raise ModelUnavailable(
            "No working Anthropic credentials. Export ANTHROPIC_API_KEY or "
            "run `ant auth login`, then retry."
        ) from exc
    except anthropic.APIError as exc:
        raise ModelError(f"Model call failed: {exc}") from exc

    if response.stop_reason == "refusal":
        raise ModelError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise ModelError(
            f"The model's answer hit the {MAX_OUTPUT_TOKENS}-token output limit "
            "before the JSON was complete."
        )

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError(f"Model returned non-JSON despite schema constraint: {exc}") from exc
