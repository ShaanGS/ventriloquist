"""MCP server: expose compiled packs as tools over stdio.

This is the deterministic half's front door. The dependency rule from
ARCHITECTURE.md section 6 applies here as strictly as in runtime.py: this
module never imports llm.py.

Design choices that came out of the security review (SECURITY.md T9, T10):

- Transport is stdio only, launched by the MCP client. No network port.
- Tool calls against the same app are serialized with a per-bundle lock.
  A GUI is one global mutable resource; parallel calls against it produce
  interleaved nonsense, so the second call waits.
- Every tool carries the risk level assigned at the approval gate. High
  risk tools are refused unless the operator started the server with
  VENT_ALLOW_HIGH=1. A call-time confirmation round trip (MCP elicitation)
  is the planned replacement; the environment flag is the honest interim,
  and it is opt-in per server process, never per call.

Written against MCP SDK 2.x, which takes handlers as constructor arguments.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import ax, packs, runtime


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


def _tool_schema(spec: packs.ToolSpec) -> dict:
    properties = {}
    for param, meta in spec.params.items():
        properties[param] = {
            "type": meta.get("type", "string"),
            "description": meta.get("description", ""),
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(spec.params.keys()),
    }


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=True,
    )


def build_server(packs_dir: Path) -> Server:
    loaded = packs.load_all(packs_dir)
    if not loaded:
        raise SystemExit(f"No packs found under {packs_dir}. Author or compile one first.")

    # (mcp tool name) -> (pack, spec)
    registry: dict[str, tuple[packs.Pack, packs.ToolSpec]] = {}
    for pack in loaded:
        for spec in pack.tools:
            registry[f"{_slug(pack.app_name)}_{spec.name}"] = (pack, spec)

    locks: dict[str, threading.Lock] = {pack.bundle_id: threading.Lock() for pack in loaded}
    allow_high = os.getenv("VENT_ALLOW_HIGH") == "1"

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        tools = []
        for name, (pack, spec) in registry.items():
            description = spec.description
            if spec.risk != "read_only":
                description += f" (risk: {spec.risk})"
            tools.append(
                types.Tool(name=name, description=description, inputSchema=_tool_schema(spec))
            )
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        if name not in registry:
            return _error(f"Unknown tool {name!r}")
        pack, spec = registry[name]

        if spec.risk == "high" and not allow_high:
            return _error(
                f"{name} is a high risk tool and this server was not started with "
                "VENT_ALLOW_HIGH=1. Refusing."
            )

        def run_locked() -> runtime.ToolResult:
            # One tool call per app at a time, held for the whole call.
            with locks[pack.bundle_id]:
                app = ax.find_app(pack.app_name)
                root = ax.app_element(app)
                low_confidence = packs.is_stale(pack, app_version="")
                return runtime.execute(
                    pack, spec.name, arguments, root, low_confidence=low_confidence
                )

        try:
            result = await anyio.to_thread.run_sync(run_locked)
        except (runtime.ToolExecutionError, ax.AXError, packs.PackError) as exc:
            # The message names the app, tool, and step, which is what
            # "fail loudly" means in SECURITY.md.
            return _error(str(exc))

        text = f"{pack.app_name}.{spec.name} succeeded: {result.detail}"
        if result.values:
            text += "\n" + "\n".join(result.values)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return Server(
        "ventriloquist",
        version="0.1.0",
        instructions=(
            "Tools compiled from Mac apps by Ventriloquist. Each call replays "
            "a recorded accessibility workflow deterministically."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def serve_stdio(packs_dir: Path) -> None:
    server = build_server(packs_dir)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
