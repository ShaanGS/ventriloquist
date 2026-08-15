"""End-to-end MCP smoke test: spawn `vent serve` over stdio the way a real
client would, list its tools, call one, and print what came back.

This is a live test: it needs macOS, Accessibility trust, and TextEdit
running with a document open. It is a script rather than a pytest test for
that reason. Run it with:

    .venv/bin/python tests/mcp_smoke.py
"""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent


async def main() -> None:
    params = StdioServerParameters(
        command=str(REPO / ".venv" / "bin" / "vent"),
        args=["serve"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools served:")
            for tool in tools.tools:
                print(f"  {tool.name}: {tool.description}")

            print("\ncalling textedit_write_document ...")
            result = await session.call_tool(
                "textedit_write_document",
                {"text": "Written over MCP: a client asked, the pack replayed, no model was consulted."},
            )
            for block in result.content:
                print(" ", block.text)

            print("\ncalling textedit_read_document ...")
            result = await session.call_tool("textedit_read_document", {})
            for block in result.content:
                print(" ", block.text)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
