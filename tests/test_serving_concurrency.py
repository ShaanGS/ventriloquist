"""How vent serve queues tool calls, with execution stubbed so no app is
touched: calls to one app run one at a time, tools that bring their app to
the front run one at a time across apps, a call cancelled while it waits
never runs, and tool work never borrows the threads the stdio transport
reads and writes with."""

import threading
import time
from dataclasses import replace

import anyio
import pytest
from mcp import Client

from vent import server as server_mod
from vent.packs import save
from vent.runtime import ToolResult
from vent.server import build_server

from .test_packs import sample_pack


class Runs:
    """Stands in for server._run_tool. Records when each call starts and
    ends, and holds a call open while its gate is shut."""

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.gates = {}
        self.started = {}

    def hold(self, key):
        self.gates[key] = threading.Event()
        self.started[key] = threading.Event()

    def __call__(self, pack, spec, arguments, writable_dir, allow_heal):
        key = f"{pack.app_name}.{spec.name}"
        with self.lock:
            self.events.append(("start", key, arguments.get("text")))
        if key in self.started:
            self.started[key].set()
        if key in self.gates:
            assert self.gates[key].wait(10), f"{key} was never released"
        else:
            time.sleep(0.05)
        with self.lock:
            self.events.append(("end", key, arguments.get("text")))
        return ToolResult(ok=True, tool=spec.name, detail="1 step")

    def order(self):
        return [(event, key) for event, key, _ in self.events]


@pytest.fixture
def runs(monkeypatch):
    stub = Runs()
    monkeypatch.setattr(server_mod, "_run_tool", stub)
    return stub


def served(tmp_path, *pack_list):
    for pack in pack_list:
        save(pack, tmp_path / pack.bundle_id / "pack.json")
    return build_server([tmp_path])


def two_tools(**changes):
    pack = sample_pack()
    pack.tools[0] = replace(pack.tools[0], **changes)
    pack.tools.append(replace(pack.tools[0], name="append_text"))
    return pack


def other_app(**changes):
    pack = sample_pack()
    pack.bundle_id, pack.app_name = "com.example.Other", "Other"
    pack.tools[0] = replace(pack.tools[0], **changes)
    return pack


def call_together(server, *calls, **client_options):
    async def main():
        async with Client(server, **client_options) as client:
            async with anyio.create_task_group() as group:
                for name, text in calls:
                    group.start_soon(client.call_tool, name, {"text": text})

    anyio.run(main)


def wait_for(event):
    return anyio.to_thread.run_sync(event.wait, 5)


def test_calls_to_one_app_run_one_at_a_time(tmp_path, runs):
    server = served(tmp_path, two_tools())
    call_together(server, ("textedit_write_document", "a"), ("textedit_append_text", "b"))
    assert [event for event, _ in runs.order()] == ["start", "end", "start", "end"]


def test_tools_that_need_the_front_run_one_at_a_time_across_apps(tmp_path, runs):
    """There is only one front: two apps activating each other mid-call
    would each restore the wrong app afterwards."""
    server = served(tmp_path, two_tools(requires_frontmost=True), other_app(requires_frontmost=True))
    call_together(server, ("textedit_write_document", "a"), ("other_write_document", "b"))
    assert [event for event, _ in runs.order()] == ["start", "end", "start", "end"]


def test_calls_to_different_apps_run_side_by_side(tmp_path, runs):
    server = served(tmp_path, two_tools(), other_app())
    runs.hold("TextEdit.write_document")

    async def main():
        async with Client(server) as client:
            async with anyio.create_task_group() as group:
                group.start_soon(client.call_tool, "textedit_write_document", {"text": "a"})
                await wait_for(runs.started["TextEdit.write_document"])
                result = await client.call_tool("other_write_document", {"text": "b"})
                runs.gates["TextEdit.write_document"].set()
        return result

    assert not anyio.run(main).is_error
    assert runs.order() == [
        ("start", "TextEdit.write_document"),
        ("start", "Other.write_document"),
        ("end", "Other.write_document"),
        ("end", "TextEdit.write_document"),
    ]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_a_call_cancelled_while_queued_never_runs(tmp_path, runs, mode):
    server = served(tmp_path, two_tools())
    runs.hold("TextEdit.write_document")

    async def main():
        async with Client(server, mode=mode) as client:
            async with anyio.create_task_group() as group:
                group.start_soon(client.call_tool, "textedit_write_document", {"text": "first"})
                await wait_for(runs.started["TextEdit.write_document"])
                async with anyio.create_task_group() as queued:
                    queued.start_soon(client.call_tool, "textedit_append_text", {"text": "cancelled"})
                    await anyio.sleep(0.2)
                    queued.cancel_scope.cancel()
                await anyio.sleep(0.3)  # the cancel notification's trip, on the JSON-RPC path
                runs.gates["TextEdit.write_document"].set()
            # The cancelled call left nothing held: the next one still runs.
            await client.call_tool("textedit_append_text", {"text": "after"})

    anyio.run(main)
    assert [text for event, _, text in runs.events if event == "start"] == ["first", "after"]


def test_tool_work_does_not_need_the_transports_threads(tmp_path, runs):
    """stdio reads and writes run on anyio's default thread limiter. Tool
    work has a limiter of its own, so calls that are running or queued can
    never leave the transport without a thread."""
    server = served(tmp_path, sample_pack())

    async def main():
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        busy = threading.Event()
        async with anyio.create_task_group() as group:
            group.start_soon(anyio.to_thread.run_sync, busy.wait, 10)  # takes the only default thread
            await anyio.sleep(0.05)
            try:
                async with Client(server) as client:
                    with anyio.fail_after(5):
                        return await client.call_tool("textedit_write_document", {"text": "a"})
            finally:
                busy.set()

    assert not anyio.run(main).is_error


@pytest.mark.parametrize(
    "frontmost, waiting_for",
    [(False, "earlier calls to TextEdit"), (True, "earlier calls that bring an app to the front")],
)
def test_a_call_that_waits_too_long_gives_up(tmp_path, runs, monkeypatch, frontmost, waiting_for):
    monkeypatch.setattr(server_mod, "LOCK_TIMEOUT_S", 0.2)
    if frontmost:
        server = served(tmp_path, two_tools(requires_frontmost=True), other_app(requires_frontmost=True))
        held, queued = "TextEdit.write_document", "other_write_document"
    else:
        server = served(tmp_path, two_tools())
        held, queued = "TextEdit.write_document", "textedit_append_text"
    runs.hold(held)

    async def main():
        async with Client(server) as client:
            async with anyio.create_task_group() as group:
                group.start_soon(client.call_tool, "textedit_write_document", {"text": "a"})
                await wait_for(runs.started[held])
                result = await client.call_tool(queued, {"text": "b"})
                runs.gates[held].set()
        return result

    result = anyio.run(main)
    assert result.is_error
    assert f"waited 0.2s for {waiting_for} to finish. Giving up on the queue." in result.content[0].text
    assert [key for event, key in runs.order() if event == "start"] == [held]
