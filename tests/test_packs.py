"""Pack schema validation: the closed op set is enforced at load, and
packs survive a save/load round trip with anchors intact."""

import json

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
    """Every pack committed under vent/bundled_packs/ must validate. A pack
    that fails here would be silently skipped by the server at startup."""
    from vent.paths import BUNDLED_PACKS_DIR

    pack_files = sorted(BUNDLED_PACKS_DIR.glob("*/pack.json"))
    assert pack_files, "no shipped packs found; did the directory move?"
    for path in pack_files:
        pack = load(path)
        assert pack.tools, f"{path} loaded but has no tools"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def edited(edit):
    """sample_pack() as JSON, changed in place by edit(data), or replaced by
    what edit returns."""
    data = sample_pack().to_dict()
    replacement = edit(data)
    return data if replacement is None else replacement


def first_step(data):
    return data["tools"][0]["steps"][0]


def set_in(target, key, value):
    target[key] = value


def drop(target, key):
    del target[key]


MALFORMED = {
    "a list at the top": lambda data: [data],
    "a tool given as a string": lambda data: set_in(data, "tools", ["write_document"]),
    "tools given as an object": lambda data: set_in(data, "tools", {"write_document": {}}),
    "params given as a list": lambda data: set_in(data["tools"][0], "params", ["text"]),
    "a param given as a string": lambda data: set_in(data["tools"][0], "params", {"text": "Content."}),
    "a param with an unknown type": lambda data: set_in(data["tools"][0], "params", {"text": {"type": "text"}}),
    "a param typed as a list of types": lambda data: set_in(
        data["tools"][0], "params", {"text": {"type": ["string", "null"]}}
    ),
    "a param name with a space": lambda data: set_in(
        data["tools"][0], "params", {"the text": {"type": "string"}}
    ),
    "a step given as a string": lambda data: set_in(data["tools"][0], "steps", ["set_value"]),
    "an op given as a list": lambda data: set_in(first_step(data), "op", ["press"]),
    "an anchor given as a string": lambda data: set_in(first_step(data), "anchor", "AXTextArea"),
    "a chain given as an object": lambda data: set_in(first_step(data)["anchor"], "chain", {"role": "AXWindow"}),
    "a chain link given as a string": lambda data: set_in(first_step(data)["anchor"], "chain", ["AXWindow"]),
    "a chain ordinal given as text": lambda data: set_in(first_step(data)["anchor"]["chain"][0], "ordinal", "0"),
    "labels given as a string": lambda data: set_in(first_step(data)["anchor"], "labels", "Body"),
    "a value with both a param and a literal": lambda data: set_in(
        first_step(data), "value", {"param": "text", "literal": "x"}
    ),
    "a set_value with no value": lambda data: drop(first_step(data), "value"),
    "settle given as text": lambda data: set_in(first_step(data), "settle", "false"),
    "requires_frontmost given as text": lambda data: set_in(data["tools"][0], "requires_frontmost", "yes"),
    "preconditions given as an object": lambda data: set_in(data["tools"][0], "preconditions", {"kind": "app_running"}),
    "a precondition given as a string": lambda data: set_in(data["tools"][0], "preconditions", ["app_running"]),
    "a verify given as a string": lambda data: set_in(data["tools"][0], "verify", ["value_contains"]),
    "a verify on an undeclared param": lambda data: set_in(data["tools"][0]["verify"][0], "param", "ghost"),
    "a description given as a number": lambda data: set_in(data["tools"][0], "description", 7),
    "an os_version given as a number": lambda data: set_in(data, "os_version", 26.3),
    "healed_pending given as an object": lambda data: set_in(data, "healed_pending", {"original": {}}),
    "a heal entry given as a string": lambda data: set_in(data, "healed_pending", ["fix"]),
    "a heal entry with no healed anchor": lambda data: set_in(
        data, "healed_pending", [{"original": {"role": "AXTextArea"}}]
    ),
    "a format_version of true": lambda data: set_in(data, "format_version", True),
}


@pytest.mark.parametrize("shape", sorted(MALFORMED))
def test_every_malformed_shape_is_a_packerror(tmp_path, shape):
    """One malformed pack used to crash vent serve at startup: a list where
    an object belongs raised AttributeError out of the loader, and params
    given as a list loaded and then broke tools/list."""
    path = write_json(tmp_path / "com.apple.TextEdit" / "pack.json", edited(MALFORMED[shape]))
    with pytest.raises(PackError) as raised:
        load(path)
    assert str(path) in str(raised.value)


def test_bytes_that_are_not_utf8_are_a_packerror(tmp_path):
    path = tmp_path / "p" / "pack.json"
    path.parent.mkdir()
    path.write_bytes(b'{"format_version": 2, "app_name": "Caf\xe9"}')
    with pytest.raises(PackError, match="not UTF-8"):
        load(path)


def test_a_utf8_byte_order_mark_is_accepted(tmp_path):
    path = tmp_path / "p" / "pack.json"
    save(sample_pack(), path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert load(path).bundle_id == "com.apple.TextEdit"


def test_load_all_skips_every_kind_of_broken_pack(tmp_path, capsys):
    from vent.packs import load_all

    save(sample_pack(), tmp_path / "good" / "pack.json")
    write_json(tmp_path / "listed" / "pack.json", [sample_pack().to_dict()])
    (tmp_path / "latin1" / "pack.json").parent.mkdir()
    (tmp_path / "latin1" / "pack.json").write_bytes(b"\xff\xfe")
    write_json(tmp_path / "params" / "pack.json", edited(MALFORMED["params given as a list"]))
    assert [pack.bundle_id for pack in load_all(tmp_path)] == ["com.apple.TextEdit"]
    assert capsys.readouterr().err.count("warning: skipping") == 3


def test_shipped_packs_pin_no_window_titles():
    """A recorded window title names one person's document (the TextEdit
    pack once shipped with "vent-demo.txt"). In a shipped pack it steers
    every install toward a window with that name and away from the
    document actually in front, so shipped anchors leave it empty."""
    from vent.paths import BUNDLED_PACKS_DIR

    for path in sorted(BUNDLED_PACKS_DIR.glob("*/pack.json")):
        for tool in load(path).tools:
            items = [*tool.steps, *tool.verify, *getattr(tool, "preconditions", [])]
            pinned = [
                item.anchor.window_title
                for item in items
                if getattr(item, "anchor", None) is not None and item.anchor.window_title
            ]
            assert not pinned, f"{path.parent.name} {tool.name} pins window titles {pinned}"
