"""Pack schema validation: the closed op set is enforced at load, and
packs survive a save/load round trip with anchors intact."""

import json
from pathlib import Path

import pytest

from vent import anchors, packs
from vent.packs import Pack, PackError, Step, ToolSpec, Verify, load, load_search_path, save
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


@pytest.mark.parametrize("seconds", [0, -1, 120.5, float("nan"), float("inf"), "5", True])
def test_step_timeouts_are_bounded(tmp_path, seconds):
    """Every call to an app waits behind the one before it, so one step
    allowed to wait an hour would hold up every later call to its app."""
    data = edited(lambda data: set_in(first_step(data), "timeout_s", seconds))
    path = tmp_path / "p" / "pack.json"
    path.parent.mkdir()
    path.write_text(json.dumps(data))  # NaN and Infinity, as Python's json writes them
    with pytest.raises(PackError, match="timeout_s"):
        load(path)


def test_a_step_may_wait_up_to_two_minutes():
    data = edited(lambda data: set_in(first_step(data), "timeout_s", 120))
    assert Pack.from_dict(data).tools[0].steps[0].timeout_s == 120.0


def press_pack(action):
    pack = sample_pack()
    body = pack.tools[0].steps[0].anchor
    pack.tools.append(
        ToolSpec(name="open_menu", description="", risk="mutating", steps=[Step(op="press", anchor=body, action=action)])
    )
    return pack.to_dict()


@pytest.mark.parametrize("action", sorted(packs.PRESS_ACTIONS))
def test_standard_press_actions_load(action):
    assert Pack.from_dict(press_pack(action)).tool("open_menu").steps[0].action == action


@pytest.mark.parametrize("action", ["AXZoomWindow", "AXOpen", "press", ""])
def test_other_press_actions_are_refused(action):
    with pytest.raises(PackError, match="not allowed"):
        Pack.from_dict(press_pack(action))


@pytest.mark.parametrize(
    "bundle_id", ["com.apple.TextEdit", "com.1password.1password", "com.bitdefender.antivirus_mac", "us.zoom.xos"]
)
def test_reverse_dns_bundle_ids_are_valid(bundle_id):
    assert packs.valid_bundle_id(bundle_id)
    assert packs.pack_file(Path("/packs"), bundle_id) == Path("/packs") / bundle_id / "pack.json"


@pytest.mark.parametrize(
    "bundle_id", ["../../etc", "com.apple/../../x", "com..apple", "/abs.path", ".hidden.app", "TextEdit", "", None, 7]
)
def test_bundle_ids_that_could_leave_their_directory_are_refused(bundle_id):
    assert not packs.valid_bundle_id(bundle_id)
    with pytest.raises(PackError, match="not a bundle id"):
        packs.pack_file(Path("/packs"), bundle_id)
    with pytest.raises(PackError, match="bundle"):
        Pack.from_dict(edited(lambda data: set_in(data, "bundle_id", bundle_id)))


SECURE_LINK = {"role": "AXTextField", "label": "", "identifier": "", "ordinal": 0, "index": 0, "subrole": "AXSecureTextField"}


def onto_a_password_field(anchor):
    anchor["chain"] = anchor["chain"] + [SECURE_LINK]


@pytest.mark.parametrize(
    "where",
    [
        lambda data: onto_a_password_field(first_step(data)["anchor"]),
        lambda data: onto_a_password_field(data["tools"][0]["verify"][0]["anchor"]),
        lambda data: set_in(
            data,
            "healed_pending",
            [{"original": {"role": "AXTextArea"}, "healed": {"role": "AXTextField", "chain": [SECURE_LINK]}}],
        ),
        lambda data: set_in(first_step(data)["anchor"], "subrole", "AXSecureTextField"),
    ],
    ids=["step", "verify", "quarantined heal", "anchor subrole"],
)
def test_anchors_onto_secure_text_fields_do_not_load(where):
    """SECURITY.md T4: a password field is never a valid target. The
    snapshot never surfaces one, so only an edited pack can name it."""
    with pytest.raises(PackError, match="secure text field"):
        Pack.from_dict(edited(where))


def test_a_broken_user_pack_still_claims_its_app(tmp_path, capsys):
    """A typo in the user's pack must not quietly bring back the bundled
    tools it replaced while the user's own tools vanish."""
    bundled = sample_pack()
    save(bundled, tmp_path / "bundled" / bundled.bundle_id / "pack.json")
    other = sample_pack()
    other.bundle_id, other.app_name = "com.example.Other", "Other"
    save(other, tmp_path / "bundled" / other.bundle_id / "pack.json")
    broken = tmp_path / "user" / bundled.bundle_id / "pack.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{ not json")

    loaded = load_search_path([tmp_path / "user", tmp_path / "bundled"])
    assert [pack.bundle_id for pack in loaded] == ["com.example.Other"]
    err = capsys.readouterr().err
    assert "warning: skipping" in err
    assert f"not serving the TextEdit pack in {tmp_path / 'bundled'}" in err
    assert str(broken) in err


def test_a_broken_pack_claims_the_bundle_id_written_inside_it(tmp_path, capsys):
    bundled = sample_pack()
    save(bundled, tmp_path / "bundled" / bundled.bundle_id / "pack.json")
    data = bundled.to_dict()
    data["tools"] = "oops"
    write_json(tmp_path / "user" / "my-textedit" / "pack.json", data)
    assert load_search_path([tmp_path / "user", tmp_path / "bundled"]) == []
    assert "not serving the TextEdit pack" in capsys.readouterr().err


def test_a_good_user_pack_shadows_without_a_warning(tmp_path, capsys):
    save(sample_pack(), tmp_path / "bundled" / "com.apple.TextEdit" / "pack.json")
    save(sample_pack(), tmp_path / "user" / "com.apple.TextEdit" / "pack.json")
    assert len(load_search_path([tmp_path / "user", tmp_path / "bundled"])) == 1
    assert capsys.readouterr().err == ""


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
