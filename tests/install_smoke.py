"""Install check: start an installed vent's MCP server the way a client
does, and confirm the bundled packs are served.

Run it against a wheel installed into a clean environment, never an
editable checkout, so packaging mistakes (bundled packs left out of the
wheel, a broken entry point) fail here instead of on a user's machine:

    python tests/install_smoke.py /path/to/env/bin

It lists tools and never calls one, so it needs no Accessibility
permission and no running app. CI runs it on Linux and macOS.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {"textedit_write_document", "textedit_read_document"}


async def served_tools(command: Path, args: list[str], vent_home: str) -> set[str]:
    params = StdioServerParameters(
        command=str(command), args=args, env={**os.environ, "VENT_HOME": vent_home}
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return {tool.name for tool in (await session.list_tools()).tools}


def main() -> int:
    bin_dir = Path(sys.argv[1])
    failed = False
    # An empty VENT_HOME leaves the bundled packs as the only source.
    with tempfile.TemporaryDirectory() as vent_home:
        for command, args in ((bin_dir / "vent", ["serve"]), (bin_dir / "ventriloquist-mcp", [])):
            tools = asyncio.run(served_tools(command, args, vent_home))
            missing = EXPECTED - tools
            print(f"{command.name} {' '.join(args)}".strip() + f": {len(tools)} tools served")
            if missing:
                print(f"  missing: {sorted(missing)}")
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
