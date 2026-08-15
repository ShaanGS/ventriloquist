"""Pack schema validation: the closed op set is enforced at load, and
packs survive a save/load round trip with anchors intact."""

import pytest

from vent import anchors
from vent.packs import Pack, PackError, Step, ToolSpec, Verify, load, save
from vent.snapshot import snapshot

from .fakes import textedit_like


def sample_pack() -> Pack:
    snap = snapshot(textedit_like())
    body = next(n for n in snap.nodes if n.identifier == "doc-body")
    anchor = anchors.build(body)
    return Pack(
        bundle_id="com.apple.TextEdit",
        app_name="TextEdit",
        app_version="1.19",
        tools=[
            ToolSpec(
                name="write_document",
                description="Replace the document text.",
                risk="mutating",
                params={"text": {"type": "string", "description": "Content."}},
                steps=[Step(op="set_value", anchor=anchor, value={"param": "text"})],
                verify=[Verify(kind="value_contains", anchor=anchor, param="text")],
            )
        ],
    )


def test_round_trip(tmp_path):
    pack = sample_pack()
    path = tmp_path / "com.apple.TextEdit" / "pack.json"
    save(pack, path)
    loaded = load(path)
    assert loaded.bundle_id == pack.bundle_id
    tool = loaded.tool("write_document")
    assert tool.risk == "mutating"
    assert tool.steps[0].anchor.identifier == "doc-body"


def test_unknown_op_rejected(tmp_path):
    pack = sample_pack()
    path = tmp_path / "p" / "pack.json"
    save(pack, path)
    text = path.read_text().replace('"op": "set_value"', '"op": "shell"')
    path.write_text(text)
    with pytest.raises(PackError, match="unknown op"):
        load(path)


def test_undeclared_param_rejected():
    pack_dict = sample_pack().to_dict()
    pack_dict["tools"][0]["steps"][0]["value"] = {"param": "ghost"}
    with pytest.raises(PackError, match="not declared"):
        Pack.from_dict(pack_dict)


def test_risk_level_required():
    pack_dict = sample_pack().to_dict()
    pack_dict["tools"][0]["risk"] = "yolo"
    with pytest.raises(PackError, match="risk"):
        Pack.from_dict(pack_dict)


def test_duplicate_tool_names_rejected():
    pack_dict = sample_pack().to_dict()
    pack_dict["tools"].append(pack_dict["tools"][0])
    with pytest.raises(PackError, match="duplicate"):
        Pack.from_dict(pack_dict)


def test_future_chain_keys_are_dropped_not_fatal(tmp_path):
    pack = sample_pack()
    path = tmp_path / "p" / "pack.json"
    save(pack, path)
    import json
    data = json.loads(path.read_text())
    data["tools"][0]["steps"][0]["anchor"]["chain"][0]["hologram"] = "future-field"
    path.write_text(json.dumps(data))
    loaded = load(path)  # must not raise TypeError
    assert loaded.tool("write_document").steps[0].anchor is not None


def test_malformed_structure_is_packerror_not_traceback(tmp_path):
    pack = sample_pack()
    path = tmp_path / "p" / "pack.json"
    save(pack, path)
    import json
    data = json.loads(path.read_text())
    data["tools"][0]["steps"][0]["anchor"]["chain"] = [{"only": "garbage"}]
    path.write_text(json.dumps(data))
    with pytest.raises(PackError):
        load(path)


def test_load_all_skips_broken_pack(tmp_path, capsys):
    good = sample_pack()
    save(good, tmp_path / "good" / "pack.json")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "pack.json").write_text("{not json")
    from vent.packs import load_all
    loaded = load_all(tmp_path)
    assert len(loaded) == 1
    assert "skipping" in capsys.readouterr().err


def test_settle_opt_out_round_trips(tmp_path):
    pack = sample_pack()
    pack.tools[0].steps[0].settle = False
    path = tmp_path / "p" / "pack.json"
    save(pack, path)
    assert load(path).tool("write_document").steps[0].settle is False


def test_shipped_packs_all_load():
    """Every pack committed under packs/ must validate. A pack that fails
    here would be silently skipped by the server at startup."""
    from pathlib import Path

    from vent.packs import load

    packs_dir = Path(__file__).resolve().parent.parent / "packs"
    pack_files = sorted(packs_dir.glob("*/pack.json"))
    assert pack_files, "no shipped packs found; did the directory move?"
    for path in pack_files:
        pack = load(path)
        assert pack.tools, f"{path} loaded but has no tools"
