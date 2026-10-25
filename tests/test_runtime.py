"""Runtime execution against fake trees: happy path, wrong-element refusal,
verification with live reads, settle semantics, healing, and the server's
risk gate. Every test asserts an observable outcome; a test that passes
when the feature is deleted is a review finding, not a test."""

import pytest

from vent import anchors, runtime
from vent.packs import Pack, PackError, Step, ToolSpec, Verify
from vent.server import risk_refusal
from vent.snapshot import VALUE_PREVIEW_LIMIT, snapshot

from .fakes import (
    FakeElement,
    GuardedElement,
    TickingElement,
    on_another_space,
    textedit_like,
    textedit_with_menus,
)


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
    assert result.detail == "1 step, 1 check passed"


def test_read_value_returns_live_content():
    tree = textedit_like()
    pack = build_write_tool(tree)
    result = runtime.execute(pack, "read_document", {}, tree)
    assert result.values == ["hello"]
    assert result.detail == "1 step"  # a read has no checks, so none are claimed


def reading_pane_like(*pane_children) -> FakeElement:
    """An app whose reading pane is a web view, shaped like Outlook's: the
    web area's own value is empty and the words sit in static text below."""
    default = [
        FakeElement("AXGroup", children=[FakeElement("AXStaticText", value="Your order has shipped.")]),
        FakeElement(
            "AXGroup",
            children=[
                FakeElement("AXStaticText", value="Get"),
                FakeElement(
                    "AXLink",
                    label="Outlook for Mac",
                    actions=["AXPress"],
                    children=[FakeElement("AXStaticText", value="Outlook for Mac")],
                ),
            ],
        ),
    ]
    pane = FakeElement("AXWebArea", label="Reading Pane", value="", children=list(pane_children) or default)
    return FakeElement("AXApplication", label="Outlook", children=[FakeElement("AXWindow", children=[pane])])


def read_pane(tree) -> list[str]:
    pane = next(n for n in snapshot(tree).nodes if n.role == "AXWebArea")
    tool = ToolSpec(
        name="read_pane",
        description="Read the reading pane.",
        risk="read_only",
        steps=[Step(op="read_value", anchor=anchors.build(pane), expect={"role": "AXWebArea"})],
    )
    pack = Pack(bundle_id="com.microsoft.Outlook", app_name="Outlook", tools=[tool])
    return runtime.execute(pack, "read_pane", {}, tree).values


def test_read_value_collects_the_text_inside_a_web_view():
    assert read_pane(reading_pane_like()) == ["Your order has shipped.\nGet\nOutlook for Mac"]


def test_read_value_does_not_repeat_nested_text():
    # Outlook's header line "Recipients: To: ..." is a static text whose own
    # children repeat its words; the read returns them once.
    header = FakeElement(
        "AXStaticText",
        value="To: someone@example.com",
        children=[FakeElement("AXStaticText", value="To:")],
    )
    assert read_pane(reading_pane_like(header)) == ["To: someone@example.com"]


def test_read_value_skips_secure_fields_and_cuts_cycles():
    secret = FakeElement(
        "AXTextField",
        subrole="AXSecureTextField",
        children=[FakeElement("AXStaticText", value="hunter2")],
    )
    loop = FakeElement("AXGroup")
    loop._children = [loop, FakeElement("AXStaticText", value="after the loop")]
    assert read_pane(reading_pane_like(secret, loop)) == ["after the loop"]


def test_read_value_caps_text_gathered_from_a_container():
    long_run = FakeElement("AXStaticText", value="x" * (runtime.READ_TEXT_LIMIT + 500))
    tail = FakeElement("AXStaticText", value="never reached")
    [text] = read_pane(reading_pane_like(long_run, tail))
    assert len(text) == runtime.READ_TEXT_LIMIT
    assert "never reached" not in text


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


def test_a_write_recorded_in_one_document_never_lands_in_another():
    """The tool was recorded in vent-demo.txt. With only taxes-2025.txt
    open, its text area matches everything but the window, and the call
    must fail rather than overwrite the taxes."""

    def document(title, body):
        return FakeElement(
            "AXWindow",
            title,
            children=[
                FakeElement(
                    "AXScrollArea",
                    children=[FakeElement("AXTextArea", value=body, identifier="doc-body")],
                )
            ],
        )

    pack = build_write_tool(FakeElement("AXApplication", "TextEdit", children=[document("vent-demo.txt", "demo")]))
    taxes_window = document("taxes-2025.txt", "1040 figures")
    taxes = FakeElement("AXApplication", "TextEdit", children=[taxes_window])
    with pytest.raises(runtime.ToolExecutionError, match="window title mismatch"):
        runtime.execute(pack, "write_document", {"text": "overwritten"}, taxes)
    assert taxes_window.children()[0].children()[0].value == "1040 figures"


def account_form():
    """A sign-in form whose account field a pack recorded while it was a
    plain text field, with tools that write and read it."""
    field = FakeElement("AXTextField", "Account", identifier="account", value="")
    tree = FakeElement("AXApplication", "App", children=[FakeElement("AXWindow", "Sign In", children=[field])])
    anchor = anchors.build(next(n for n in snapshot(tree).nodes if n.identifier == "account"))
    write = ToolSpec(
        name="type_account",
        description="Type into the account field.",
        risk="mutating",
        params={"text": {"type": "string", "description": "Text."}},
        steps=[Step(op="set_value", anchor=anchor, value={"param": "text"}, timeout_s=0.2)],
    )
    read = ToolSpec(
        name="read_account",
        description="Read the account field.",
        risk="read_only",
        steps=[Step(op="read_value", anchor=anchor)],
    )
    return tree, field, Pack(bundle_id="app.test", app_name="App", tools=[write, read])


def test_tools_never_write_or_read_a_field_that_turned_secure():
    tree, field, pack = account_form()
    field.subrole = "AXSecureTextField"
    field.value = "hunter2"
    with pytest.raises(runtime.ToolExecutionError):
        runtime.execute(pack, "type_account", {"text": "typed"}, tree)
    assert field.value == "hunter2"
    with pytest.raises(runtime.ToolExecutionError) as refused:
        runtime.execute(pack, "read_account", {}, tree)
    assert "hunter2" not in str(refused.value)


def test_a_field_that_turns_secure_after_resolution_is_refused(monkeypatch):
    """Resolution and the write are separate moments. The runtime reads the
    subrole again, live, so a field that turned secure in between is
    still refused."""
    tree, field, pack = account_form()
    real_resolve = anchors.resolve

    def resolve_then_turn_secure(root, anchor, low_confidence=False):
        element = real_resolve(root, anchor, low_confidence=low_confidence)
        field.subrole = "AXSecureTextField"
        return element

    monkeypatch.setattr(runtime.anchors, "resolve", resolve_then_turn_secure)
    with pytest.raises(runtime.ToolExecutionError, match="secure text field"):
        runtime.execute(pack, "type_account", {"text": "typed"}, tree)
    assert field.value == ""
    field.subrole = ""
    with pytest.raises(runtime.ToolExecutionError, match="secure text field"):
        runtime.execute(pack, "read_account", {}, tree)


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


def test_write_never_walks_the_menu_bar():
    """Resolve, settle and verify each crawled the menu bar on every call,
    which in TextEdit is about 365 of roughly 410 elements and was most of
    a write's time."""
    tree = textedit_like()
    tree._children.append(GuardedElement("AXMenuBar"))
    pack = build_write_tool(tree)
    result = runtime.execute(pack, "write_document", {"text": "fast now"}, tree)
    assert result.ok
    assert doc_body(tree).value == "fast now"


def test_settle_ignores_menu_bar_churn_unless_asked():
    tree = textedit_like()
    tree._children.append(
        FakeElement("AXMenuBar", children=[TickingElement("AXMenuItem", value="x", ticks=10_000)])
    )
    # A menu item that never stops changing must not hold a window step...
    assert runtime.settle(tree, deadline_s=1.0, include_menus=False) == "no_reaction"
    # ...but a step that acted inside the menu bar still watches it.
    assert runtime.settle(tree, deadline_s=0.5, include_menus=True) == "timeout"


def test_only_menu_bar_steps_settle_on_the_menu_bar(monkeypatch):
    seen = []

    def recording_settle(root, deadline_s, include_menus=True, baseline=None):
        seen.append(include_menus)
        return "settled"

    monkeypatch.setattr(runtime, "settle", recording_settle)
    tree = textedit_with_menus()
    runtime.execute(build_write_tool(tree), "write_document", {"text": "hi"}, tree)

    menu_node = next(n for n in snapshot(tree, include_menus=True).nodes if n.label == "New")
    new_document = ToolSpec(
        name="new_document",
        description="Open a new document.",
        risk="mutating",
        steps=[Step(op="press", anchor=anchors.build(menu_node))],
    )
    menu_pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[new_document])
    runtime.execute(menu_pack, "new_document", {}, tree)

    assert seen == [False, True]


def test_write_settles_once_the_new_value_holds():
    """A write lands before set_value returns. With the fingerprint taken
    afterwards, settle saw no change and waited out its whole no-reaction
    window, 0.6 s on every TextEdit write. Taken before, the new value
    counts as the change, and settle returns once the tree holds still."""
    tree = textedit_like()
    pack = build_write_tool(tree)
    pack.tools[0].steps[0].timeout_s = 2.0
    result = runtime.execute(pack, "write_document", {"text": "settled text"}, tree)
    assert result.steps[0].detail == "settled"


def test_press_waits_for_a_reaction_beyond_its_own_state():
    """A press waits on the app, not on the control it pressed, so the
    control's own state change must not count as the reaction."""

    class ToggleElement(FakeElement):
        def perform(self, action: str = "AXPress") -> None:
            super().perform(action)
            self.value = 0 if self.value else 1

    toggle = ToggleElement("AXCheckBox", "Bold", identifier="bold", value=0, actions=["AXPress"])
    tree = FakeElement(
        "AXApplication", "App", children=[FakeElement("AXWindow", "Main", children=[toggle])]
    )
    node = next(n for n in snapshot(tree).nodes if n.identifier == "bold")
    tool = ToolSpec(
        name="toggle_bold",
        description="Toggle bold.",
        risk="mutating",
        steps=[Step(op="press", anchor=anchors.build(node), timeout_s=2.0)],
    )
    pack = Pack(bundle_id="app.test", app_name="App", tools=[tool])
    result = runtime.execute(pack, "toggle_bold", {}, tree)
    assert toggle.value == 1
    assert result.steps[0].detail == "no_reaction"


def test_tools_run_on_a_window_on_another_space():
    """With its window on another Space, TextEdit lists no window, and
    every tool failed with "no open window" though the document was open.
    Reads and writes reach it through the app's main window instead."""
    tree = textedit_like()
    pack = build_write_tool(tree)
    body = doc_body(tree)
    on_another_space(tree)
    result = runtime.execute(pack, "write_document", {"text": "written off-Space"}, tree)
    assert result.ok
    assert body.value == "written off-Space"
    assert runtime.execute(pack, "read_document", {}, tree).values == ["written off-Space"]


def test_settle_watches_a_window_on_another_space():
    """A fingerprint of only what the app lists would miss the window, see
    nothing change, and wait out the whole no-reaction window."""
    tree = textedit_like()
    pack = build_write_tool(tree)
    pack.tools[0].steps[0].timeout_s = 2.0
    on_another_space(tree)
    result = runtime.execute(pack, "write_document", {"text": "settled text"}, tree)
    assert result.steps[0].detail == "settled"


def test_modal_sheet_on_another_space_still_blocks_execution():
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
    on_another_space(tree)
    with pytest.raises(runtime.ToolExecutionError, match="modal sheet is open"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)
    assert window.children()[2].children()[0].value == "hello"


def test_raise_window_reaches_a_window_on_another_space():
    window = FakeElement("AXWindow", "untitled.txt", actions=["AXRaise"])
    raised = []
    window.perform = raised.append
    tree = on_another_space(FakeElement("AXApplication", "TextEdit", children=[window]))
    tool = ToolSpec(
        name="raise",
        description="Raise the document window.",
        risk="read_only",
        steps=[Step(op="raise_window", timeout_s=0.2)],
    )
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    assert runtime.execute(pack, "raise", {}, tree).ok
    assert raised == ["AXRaise"]


def test_wait_for_nudges_a_web_area_on_another_space():
    """Between polls, wait_for scrolls an element into view so a Chromium
    host republishes its tree. With the window on another Space that
    element has to be found through the main window, or the wait can only
    time out."""
    web = FakeElement("AXWebArea", "Slack", actions=["AXScrollToVisible"])
    nudged = []
    web.perform = nudged.append
    window = FakeElement("AXWindow", "Slack", children=[FakeElement("AXGroup", children=[web])])
    tree = on_another_space(FakeElement("AXApplication", "Slack", children=[window]))
    missing = anchors.Anchor("AXButton", "", ["Later"], "", [])
    tool = ToolSpec(
        name="wait",
        description="Wait for a button that never appears.",
        risk="read_only",
        steps=[Step(op="wait_for", anchor=missing, timeout_s=0.4)],
    )
    pack = Pack(bundle_id="com.tinyspeck.slackmacgap", app_name="Slack", tools=[tool])
    with pytest.raises(runtime.ToolExecutionError, match="did not appear"):
        runtime.execute(pack, "wait", {}, tree)
    assert "AXScrollToVisible" in nudged


def test_bring_to_front_waits_for_the_app_to_come_forward(monkeypatch):
    """Activation lands a moment after it's asked for: from another Space
    TextEdit reported itself frontmost after 0.13 s. A press sent before
    then reaches a background app, where TextEdit's Bold flips the button
    and leaves the text alone, so the runtime waits for AXFrontmost."""
    reads_before_front = [3]
    pressed = []

    class App(FakeElement):
        def attribute(self, name):
            if name == "AXFrontmost":
                if reads_before_front[0]:
                    reads_before_front[0] -= 1
                    return False
                return True
            return super().attribute(name)

    bold = FakeElement("AXCheckBox", "bold", actions=["AXPress"])
    bold.perform = lambda action="AXPress": pressed.append(reads_before_front[0] == 0)
    tree = App("AXApplication", "TextEdit", children=[FakeElement("AXWindow", "notes.rtf", children=[bold])])
    node = next(n for n in snapshot(tree).nodes if n.label == "bold")
    tool = ToolSpec(
        name="bold",
        description="Press Bold.",
        risk="mutating",
        requires_frontmost=True,
        steps=[Step(op="press", anchor=anchors.build(node), settle=False)],
    )
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    monkeypatch.setattr(runtime.ax, "frontmost_app", lambda: None)
    monkeypatch.setattr(runtime.ax, "activate", lambda app: True)
    app = runtime.ax.RunningApp("TextEdit", "com.apple.TextEdit", 1)
    assert runtime.execute(pack, "bold", {}, tree, app=app).ok
    assert pressed == [True], "the press went out before the app came to the front"
