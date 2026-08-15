"""Runtime execution against fake trees: happy path, wrong-element refusal,
verification with live reads, settle semantics, healing, and the server's
risk gate. Every test asserts an observable outcome; a test that passes
when the feature is deleted is a review finding, not a test."""

import pytest

from vent import anchors, runtime
from vent.packs import Pack, PackError, Step, ToolSpec, Verify
from vent.server import risk_refusal
from vent.snapshot import VALUE_PREVIEW_LIMIT, snapshot

from .fakes import FakeElement, TickingElement, textedit_like


def build_write_tool(tree) -> Pack:
    snap = snapshot(tree)
    body = next(n for n in snap.nodes if n.identifier == "doc-body")
    anchor = anchors.build(body)
    tool = ToolSpec(
        name="write_document",
        description="Replace the document text.",
        risk="mutating",
        params={"text": {"type": "string", "description": "New content."}},
        preconditions=[{"kind": "app_running"}, {"kind": "window_exists"}],
        steps=[
            Step(
                op="set_value",
                anchor=anchor,
                expect={"role": "AXTextArea"},
                value={"param": "text"},
                timeout_s=0.2,
            )
        ],
        verify=[Verify(kind="value_contains", anchor=anchor, param="text")],
    )
    read_tool = ToolSpec(
        name="read_document",
        description="Read the document text.",
        risk="read_only",
        steps=[Step(op="read_value", anchor=anchor, expect={"role": "AXTextArea"})],
    )
    return Pack(
        bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool, read_tool]
    )


def doc_body(tree):
    return tree.children()[0].children()[2].children()[0]


def test_write_document_end_to_end():
    tree = textedit_like()
    pack = build_write_tool(tree)
    result = runtime.execute(pack, "write_document", {"text": "brand new text"}, tree)
    assert result.ok
    assert doc_body(tree).value == "brand new text"


def test_read_value_returns_live_content():
    tree = textedit_like()
    pack = build_write_tool(tree)
    result = runtime.execute(pack, "read_document", {}, tree)
    assert result.values == ["hello"]


def test_long_values_verify_against_live_reads_not_previews():
    tree = textedit_like()
    pack = build_write_tool(tree)
    long_text = "x" * 500
    result = runtime.execute(pack, "write_document", {"text": long_text}, tree)
    assert result.ok
    assert doc_body(tree).value == long_text
    # The preview really is truncated, so if verify compared against it,
    # this test would have failed above.
    preview = next(
        n.value_preview for n in snapshot(tree).nodes if n.identifier == "doc-body"
    )
    assert len(preview) <= VALUE_PREVIEW_LIMIT
    assert preview.endswith("...")


def test_missing_argument_fails_loudly():
    tree = textedit_like()
    pack = build_write_tool(tree)
    with pytest.raises(runtime.ToolExecutionError, match="missing arguments"):
        runtime.execute(pack, "write_document", {}, tree)


def test_expect_assertion_refuses_wrong_role():
    tree = textedit_like()
    pack = build_write_tool(tree)
    pack.tools[0].steps[0].expect = {"role": "AXButton"}
    with pytest.raises(runtime.ToolExecutionError, match="Refusing to act"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)


def test_no_window_precondition():
    tree = textedit_like()
    pack = build_write_tool(tree)
    tree._children.clear()
    with pytest.raises(runtime.ToolExecutionError, match="no open window"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)


def test_broken_anchor_without_healer_fails_loudly():
    tree = textedit_like()
    pack = build_write_tool(tree)
    scroll = tree.children()[0]._children[2]
    scroll._children.clear()
    with pytest.raises(runtime.ToolExecutionError, match="write_document step 0"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)


def test_healer_is_consulted_and_used_without_being_persisted():
    tree = textedit_like()
    pack = build_write_tool(tree)
    body = doc_body(tree)
    # An app update renames the identifier: the recorded anchor cannot
    # resolve, so the runtime must consult the healer.
    body._identifier = "renamed-by-update"

    calls = []

    def heal(broken, root):
        calls.append(broken)
        snap = snapshot(root)
        node = next(n for n in snap.nodes if n.identifier == "renamed-by-update")
        return anchors.build(node)

    result = runtime.execute(pack, "write_document", {"text": "healed"}, tree, heal=heal)

    assert result.ok
    assert doc_body(tree).value == "healed"
    # The step's anchor and the verify's anchor both broke, so the healer
    # is consulted for each, always with the original broken anchor.
    assert len(calls) == 2
    assert all(c.identifier == "doc-body" for c in calls)
    # The healed anchor was used for this call only. The pack still holds
    # the original; persistence goes through quarantine (SECURITY.md T8).
    assert pack.tools[0].steps[0].anchor.identifier == "doc-body"


def test_wait_for_requires_anchor_at_load():
    with pytest.raises(PackError, match="requires an anchor"):
        Step.from_dict({"op": "wait_for"}, where="test")


def test_settle_reports_no_reaction_on_static_tree():
    tree = textedit_like()
    assert runtime.settle(tree, deadline_s=1.0) == "no_reaction"


def test_settle_detects_change_then_quiet():
    tree = textedit_like()
    ticking = TickingElement("AXTextArea", value="stable", ticks=3)
    tree.children()[0]._children.append(ticking)
    assert runtime.settle(tree, deadline_s=2.0) == "settled"


def test_modal_sheet_blocks_execution():
    tree = textedit_like()
    pack = build_write_tool(tree)
    window = tree.children()[0]
    sheet = FakeElement("AXSheet", "Save changes?")
    window_attr = window.attribute

    def attribute(name):
        if name == "AXSheets":
            return [sheet]
        return window_attr(name)

    window.attribute = attribute
    tree_attr = tree.attribute

    def root_attribute(name):
        if name == "AXWindows":
            return [window]
        return tree_attr(name)

    tree.attribute = root_attribute
    with pytest.raises(runtime.ToolExecutionError, match="modal sheet is open"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)


def test_high_risk_tool_refused_without_opt_in():
    spec = ToolSpec(
        name="empty_trash",
        description="",
        risk="high",
        steps=[Step(op="press", anchor=anchors.Anchor("AXButton", "", [], "", []))],
    )
    assert "Refusing" in risk_refusal(spec, allow_high=False)
    assert risk_refusal(spec, allow_high=True) is None
    safe = ToolSpec(name="read", description="", risk="read_only", steps=spec.steps)
    assert risk_refusal(safe, allow_high=False) is None


def test_press_pumps_web_backed_elements_only():
    """After a mutating op, the runtime nudges Chromium hosts to republish
    their lazily-serialized tree via a no-op scroll-to-visible. Elements
    that do not advertise the action (eager native trees) are left alone."""

    class RecordingElement(FakeElement):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.performed: list[str] = []

        def perform(self, action: str = "AXPress") -> None:
            super().perform(action)
            self.performed.append(action)

    def tree_with_button(actions):
        button = RecordingElement(
            "AXButton", "Friends", identifier="nav-friends", actions=actions
        )
        tree = FakeElement(
            "AXApplication",
            "App",
            children=[FakeElement("AXWindow", "Main", children=[button])],
        )
        return tree, button

    def press_pack(tree):
        snap = snapshot(tree)
        node = next(n for n in snap.nodes if n.identifier == "nav-friends")
        tool = ToolSpec(
            name="open_friends",
            description="Press the nav button.",
            risk="read_only",
            steps=[
                Step(
                    op="press",
                    anchor=anchors.build(node),
                    expect={"role": "AXButton"},
                    timeout_s=0.2,
                )
            ],
        )
        return Pack(bundle_id="app.test", app_name="App", tools=[tool])

    web_tree, web_button = tree_with_button(["AXPress", "AXScrollToVisible"])
    result = runtime.execute(press_pack(web_tree), "open_friends", {}, web_tree)
    assert result.ok
    assert web_button.performed == ["AXPress", "AXScrollToVisible"]

    native_tree, native_button = tree_with_button(["AXPress"])
    result = runtime.execute(press_pack(native_tree), "open_friends", {}, native_tree)
    assert result.ok
    assert native_button.performed == ["AXPress"]


def test_acting_on_a_drifted_element_is_refused():
    """T11 at runtime: when the recorded element is absent, resolution can
    score a same-role neighbor above threshold. Acting on an element whose
    live label the anchor has never seen is refused; the degraded-state
    alternative was typing into whatever editor happened to be open, with
    verify passing because it re-reads the value just written."""
    tree = textedit_like()
    body = doc_body(tree)
    body.label = "Document"          # recorded label at pack-build time
    pack = build_write_tool(tree)
    body.label = "keybindings.json, Editor Group 2"
    body.title = body.label
    with pytest.raises(runtime.ToolExecutionError, match="never been recorded under"):
        runtime.execute(pack, "write_document", {"text": "oops"}, tree)
    assert body.value == "hello", "the drifted element must not have been written"
    # Reading a drifted element stays permitted; the caller can judge data.
    result = runtime.execute(pack, "read_document", {}, tree)
    assert result.ok
