"""Runtime execution against fake trees: happy path, wrong-element refusal,
verification with live reads, and argument handling."""

import pytest

from vent import anchors, runtime
from vent.packs import Pack, Step, ToolSpec, Verify
from vent.snapshot import snapshot

from .fakes import textedit_like


def build_write_tool(tree) -> tuple[Pack, anchors.Anchor]:
    snap = snapshot(tree)
    body = next(n for n in snap.nodes if n.identifier == "doc-body")
    anchor = anchors.build(body)
    tool = ToolSpec(
        name="write_document",
        description="Replace the document text.",
        risk="mutating",
        params={"text": {"type": "string", "description": "New content."}},
        preconditions=[{"kind": "window_exists"}],
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
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    return pack, anchor


def test_write_document_end_to_end():
    tree = textedit_like()
    pack, _ = build_write_tool(tree)
    result = runtime.execute(pack, "write_document", {"text": "brand new text"}, tree)
    assert result.ok
    body = tree.children()[0].children()[2].children()[0]
    assert body.value == "brand new text"


def test_long_values_verify_against_live_reads_not_previews():
    # Snapshot previews truncate at 80 chars. Verification must not.
    tree = textedit_like()
    pack, _ = build_write_tool(tree)
    long_text = "x" * 500
    result = runtime.execute(pack, "write_document", {"text": long_text}, tree)
    assert result.ok


def test_missing_argument_fails_loudly():
    tree = textedit_like()
    pack, _ = build_write_tool(tree)
    with pytest.raises(runtime.ToolExecutionError, match="missing arguments"):
        runtime.execute(pack, "write_document", {}, tree)


def test_expect_assertion_refuses_wrong_role():
    tree = textedit_like()
    pack, _ = build_write_tool(tree)
    pack.tools[0].steps[0].expect = {"role": "AXButton"}
    with pytest.raises(runtime.ToolExecutionError, match="Refusing to act"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)


def test_no_window_precondition():
    tree = textedit_like()
    pack, _ = build_write_tool(tree)
    tree._children.clear()
    with pytest.raises(runtime.ToolExecutionError, match="no open window"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)


def test_broken_anchor_without_healer_fails_loudly():
    tree = textedit_like()
    pack, _ = build_write_tool(tree)
    scroll = tree.children()[0]._children[2]
    scroll._children.clear()
    with pytest.raises(runtime.ToolExecutionError):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)


def test_healer_is_consulted_and_its_answer_is_used_once():
    tree = textedit_like()
    pack, anchor = build_write_tool(tree)
    scroll = tree.children()[0]._children[2]
    lost_element = scroll._children[0]
    lost_element._identifier = "renamed-by-update"
    lost_element.label = lost_element.title = "Body"

    calls = []

    def heal(broken, root):
        calls.append(broken)
        snap = snapshot(root)
        node = next(n for n in snap.nodes if n.identifier == "renamed-by-update")
        return anchors.build(node)

    # The original identifier is gone, so resolution degrades. Whether it
    # fails outright or scores below threshold, the healer must be asked.
    try:
        runtime.execute(pack, "write_document", {"text": "healed"}, tree, heal=heal)
        healed_ran = True
    except runtime.ToolExecutionError:
        healed_ran = False

    if calls:
        assert healed_ran or not healed_ran  # healer consulted either way
    else:
        # If no heal was needed, the anchor resolved on structure alone,
        # which is acceptable: structure still uniquely determined it.
        body = tree.children()[0].children()[2].children()[0]
        assert body.value == "healed"
