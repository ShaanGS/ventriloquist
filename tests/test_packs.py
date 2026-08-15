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
