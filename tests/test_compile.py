"""`vent compile` adds to the pack already there instead of replacing it, so
a pack can grow over several explore runs without losing earlier approvals."""

import pytest
from click.testing import CliRunner

from vent import anchors, cli, compiler, explorer, packs
from vent.packs import Pack, Step, ToolSpec
from vent.snapshot import snapshot

from .fakes import textedit_like

BUNDLE = "com.apple.TextEdit"


def tool(name: str, description: str = "Replace the document text.") -> ToolSpec:
    body = next(n for n in snapshot(textedit_like()).nodes if n.identifier == "doc-body")
    return ToolSpec(
        name=name,
        description=description,
        risk="mutating",
        params={"text": {"type": "string", "description": "Content."}},
        steps=[Step(op="set_value", anchor=anchors.build(body), value={"param": "text"})],
    )


def compile_with(monkeypatch, tmp_path, proposed, answers, bundled_dir=None):
    """Run `vent compile TextEdit` with the model's proposals and the
    user's y/n answers scripted, against a VENT_HOME under tmp_path."""
    monkeypatch.setenv("VENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.paths, "BUNDLED_PACKS_DIR", bundled_dir or tmp_path / "no-bundled-packs")
    monkeypatch.setattr(cli.ax, "find_app", lambda name: cli.ax.RunningApp("TextEdit", BUNDLE, 1))
    monkeypatch.setattr(explorer, "load_trace", lambda traces, bundle: explorer.Trace(BUNDLE, "TextEdit", "1.19"))
    monkeypatch.setattr(compiler, "propose", lambda trace: proposed)
    monkeypatch.setattr(compiler, "build_spec", lambda proposal: proposal)
    monkeypatch.setattr(compiler, "deterministic_summary", lambda spec, app: "  1. set the document text")
    monkeypatch.setattr(compiler, "description_mismatch", lambda spec, app: [])
    return CliRunner().invoke(cli.main, ["compile", "TextEdit"], input="".join(a + "\n" for a in answers))


def pack_file(tmp_path):
    return tmp_path / "home" / "packs" / BUNDLE / "pack.json"


def test_a_second_compile_keeps_the_tools_from_the_first(monkeypatch, tmp_path):
    first = compile_with(monkeypatch, tmp_path, [tool("write_document")], ["y"])
    assert first.exit_code == 0, first.output
    second = compile_with(monkeypatch, tmp_path, [tool("append_text", "Add text at the end.")], ["y"])
    assert second.exit_code == 0, second.output
    assert [t.name for t in packs.load(pack_file(tmp_path)).tools] == ["write_document", "append_text"]
    assert "2 in the pack" in second.output


def test_a_same_name_tool_is_only_replaced_on_a_yes(monkeypatch, tmp_path):
    compile_with(monkeypatch, tmp_path, [tool("write_document", "Old.")], ["y"])
    declined = compile_with(monkeypatch, tmp_path, [tool("write_document", "New.")], ["y", "n"])
    assert "already has write_document" in declined.output
    assert packs.load(pack_file(tmp_path)).tool("write_document").description == "Old."

    accepted = compile_with(monkeypatch, tmp_path, [tool("write_document", "New.")], ["y", "y"])
    assert accepted.exit_code == 0, accepted.output
    pack = packs.load(pack_file(tmp_path))
    assert [t.name for t in pack.tools] == ["write_document"]
    assert pack.tool("write_document").description == "New."


def test_quarantined_heals_survive_a_compile(monkeypatch, tmp_path):
    compile_with(monkeypatch, tmp_path, [tool("write_document")], ["y"])
    pack = packs.load(pack_file(tmp_path))
    pack.healed_pending = [{"original": {"role": "AXTextArea"}, "healed": {"role": "AXTextArea"}, "target_label": "Body"}]
    packs.save(pack, pack_file(tmp_path))

    compile_with(monkeypatch, tmp_path, [tool("append_text")], ["y"])
    assert packs.load(pack_file(tmp_path)).healed_pending == pack.healed_pending


def test_an_unreadable_pack_is_not_replaced_without_a_yes(monkeypatch, tmp_path):
    pack_file(tmp_path).parent.mkdir(parents=True)
    pack_file(tmp_path).write_text("{ not json")
    result = compile_with(monkeypatch, tmp_path, [tool("write_document")], ["n"])
    assert result.exit_code == 1
    assert pack_file(tmp_path).read_text() == "{ not json"


def test_the_bundled_pack_is_the_start_when_the_user_has_none(monkeypatch, tmp_path):
    """The user's pack shadows the bundled one, so starting empty would hide
    the bundled tools from the server."""
    bundled = tmp_path / "bundled"
    packs.save(Pack(bundle_id=BUNDLE, app_name="TextEdit", tools=[tool("read_document", "Read the text.")]), bundled / BUNDLE / "pack.json")
    result = compile_with(monkeypatch, tmp_path, [tool("write_document")], ["y"], bundled_dir=bundled)
    assert result.exit_code == 0, result.output
    assert "Starting from the bundled TextEdit pack" in result.output
    assert [t.name for t in packs.load(pack_file(tmp_path)).tools] == ["read_document", "write_document"]


def test_merge_keeps_the_versions_the_older_tools_were_recorded_on():
    """Older tools still date from the older versions, and a stale pack
    loads its anchors cautiously."""
    base = Pack(bundle_id=BUNDLE, app_name="TextEdit", app_version="1.18", os_version="15.6", tools=[tool("write_document")])
    added = Pack(bundle_id=BUNDLE, app_name="TextEdit", app_version="1.19", os_version="26.0", tools=[tool("append_text")])
    merged = packs.merge(base, added)
    assert (merged.app_version, merged.os_version) == ("1.18", "15.6")
    assert [t.name for t in merged.tools] == ["write_document", "append_text"]


def test_merge_takes_the_new_versions_once_no_older_tool_survives():
    """Kept forever, the old versions made "re-record those tools" a hint
    nothing could satisfy."""
    base = Pack(bundle_id=BUNDLE, app_name="TextEdit", app_version="1.18", os_version="15.6", tools=[tool("write_document")])
    added = Pack(bundle_id=BUNDLE, app_name="TextEdit", app_version="1.19", os_version="26.0", tools=[tool("write_document", "New.")])
    merged = packs.merge(base, added)
    assert (merged.app_version, merged.os_version) == ("1.19", "26.0")
    assert merged.tool("write_document").description == "New."


def test_two_proposals_with_one_name_keep_the_first_unless_told_otherwise(monkeypatch, tmp_path):
    """Both used to go into the pack, which then failed to load."""
    proposed = [tool("write_document", "First."), tool("write_document", "Second.")]
    result = compile_with(monkeypatch, tmp_path, proposed, ["y", "y", "n"])
    assert result.exit_code == 0, result.output
    assert "You already approved write_document in this run" in result.output
    pack = packs.load(pack_file(tmp_path))
    assert [(t.name, t.description) for t in pack.tools] == [("write_document", "First.")]


def test_two_proposals_with_one_name_keep_the_second_on_a_yes(monkeypatch, tmp_path):
    proposed = [tool("write_document", "First."), tool("write_document", "Second.")]
    result = compile_with(monkeypatch, tmp_path, proposed, ["y", "y", "y"])
    assert result.exit_code == 0, result.output
    pack = packs.load(pack_file(tmp_path))
    assert [(t.name, t.description) for t in pack.tools] == [("write_document", "Second.")]


def old_pack(where, *tools):
    path = where / BUNDLE / "pack.json"
    packs.save(Pack(bundle_id=BUNDLE, app_name="TextEdit", app_version="1.18", os_version="15.6", tools=list(tools)), path)
    return path


@pytest.fixture
def on_macos_26(monkeypatch):
    monkeypatch.setattr("platform.mac_ver", lambda: ("26.3.1", ("", "", ""), "arm64"))


def test_adding_to_older_tools_says_the_pack_keeps_their_version(monkeypatch, tmp_path, on_macos_26):
    old_pack(tmp_path / "home" / "packs", tool("write_document"))
    result = compile_with(monkeypatch, tmp_path, [tool("append_text")], ["y"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert (
        "Your pack's tools from before this run (write_document) were recorded on TextEdit 1.18 on macOS 15.6, "
        "and this run on TextEdit 1.19 on macOS 26.3.1." in flat
    )
    assert "It takes this Mac's version in a compile that replaces every tool it holds." in flat
    pack = packs.load(pack_file(tmp_path))
    assert (pack.app_version, pack.os_version) == ("1.18", "15.6")


def test_replacing_every_older_tool_says_the_pack_now_records_this_mac(monkeypatch, tmp_path, on_macos_26):
    old_pack(tmp_path / "home" / "packs", tool("write_document"))
    result = compile_with(monkeypatch, tmp_path, [tool("write_document", "New.")], ["y", "y"])
    assert result.exit_code == 0, result.output
    assert "Every tool from before this run was replaced, so the pack now records TextEdit 1.19 on macOS 26.3.1." in result.output
    pack = packs.load(pack_file(tmp_path))
    assert (pack.app_version, pack.os_version) == ("1.19", "26.3.1")


def test_adding_to_older_bundled_tools_names_the_bundled_pack(monkeypatch, tmp_path, on_macos_26):
    old_pack(tmp_path / "bundled", tool("read_document", "Read the text."))
    result = compile_with(monkeypatch, tmp_path, [tool("write_document")], ["y"], bundled_dir=tmp_path / "bundled")
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "The bundled TextEdit pack's tools (read_document) were recorded on TextEdit 1.18" in flat


def test_a_proposal_named_like_a_bundled_tool_asks_about_the_bundled_pack(monkeypatch, tmp_path, on_macos_26):
    old_pack(tmp_path / "bundled", tool("write_document", "Bundled."))
    result = compile_with(monkeypatch, tmp_path, [tool("write_document", "Mine.")], ["y", "n"], bundled_dir=tmp_path / "bundled")
    assert "The bundled TextEdit pack already has write_document" in result.output


def test_no_version_note_when_the_pack_was_recorded_here(monkeypatch, tmp_path, on_macos_26):
    path = tmp_path / "home" / "packs" / BUNDLE / "pack.json"
    packs.save(Pack(bundle_id=BUNDLE, app_name="TextEdit", app_version="1.19", os_version="26.3", tools=[tool("write_document")]), path)
    result = compile_with(monkeypatch, tmp_path, [tool("append_text")], ["y"])
    assert result.exit_code == 0, result.output
    assert "recorded on" not in result.output
    assert "now records" not in result.output


def test_compile_refuses_a_bundle_id_that_would_leave_its_directory(monkeypatch, tmp_path):
    """An app sets its own bundle id, and vent builds paths from it."""
    monkeypatch.setenv("VENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.ax, "find_app", lambda name: cli.ax.RunningApp("Evil", "../../evil", 1))
    result = CliRunner().invoke(cli.main, ["compile", "Evil"])
    assert result.exit_code == 1
    assert "not reverse-DNS" in result.output
    assert not (tmp_path / "evil").exists()
