"""The MCP server as a client sees it: which tools get listed from which
packs, and which calls are refused before any app is touched. The client
connects in-process, so no app, permission, or subprocess is involved."""

from dataclasses import replace

import anyio
import pytest
from mcp import Client

from vent.packs import save
from vent.server import build_server

from .test_packs import sample_pack


def connect(server, action):
    async def main():
        async with Client(server) as client:
            return await action(client)

    return anyio.run(main)


def tool_names(server) -> list[str]:
    async def action(client):
        return [tool.name for tool in (await client.list_tools()).tools]

    return connect(server, action)


def call(server, name: str, arguments: dict):
    async def action(client):
        return await client.call_tool(name, arguments)

    return connect(server, action)


def test_lists_user_and_bundled_tools_with_user_copy_winning(tmp_path):
    bundled = sample_pack()
    save(bundled, tmp_path / "bundled" / bundled.bundle_id / "pack.json")
    other = sample_pack()
    other.bundle_id, other.app_name = "com.example.Other", "Other"
    save(other, tmp_path / "bundled" / other.bundle_id / "pack.json")
    mine = sample_pack()
    mine.tools.append(replace(mine.tools[0], name="append_note"))
    save(mine, tmp_path / "user" / mine.bundle_id / "pack.json")

    names = tool_names(build_server([tmp_path / "user", tmp_path / "bundled"]))
    assert sorted(names) == ["other_write_document", "textedit_append_note", "textedit_write_document"]


def test_no_packs_anywhere_names_every_directory_searched(tmp_path):
    with pytest.raises(SystemExit) as raised:
        build_server([tmp_path / "user", tmp_path / "bundled"])
    assert str(tmp_path / "user") in str(raised.value)
    assert str(tmp_path / "bundled") in str(raised.value)


def test_unknown_tool_is_an_error_result_not_a_crash(tmp_path):
    pack = sample_pack()
    save(pack, tmp_path / pack.bundle_id / "pack.json")
    result = call(build_server([tmp_path]), "textedit_delete_everything", {})
    assert result.is_error
    assert "Unknown tool" in result.content[0].text


def test_high_risk_tool_is_refused_over_mcp_without_the_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("VENT_ALLOW_HIGH", raising=False)
    pack = sample_pack()
    pack.tools[0].risk = "high"
    save(pack, tmp_path / pack.bundle_id / "pack.json")
    result = call(build_server([tmp_path]), "textedit_write_document", {"text": "x"})
    assert result.is_error
    assert "VENT_ALLOW_HIGH=1" in result.content[0].text
