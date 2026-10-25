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
    PressRecorder,
    TickingElement,
    on_another_space,
    save_changes_sheet,
    sheet,
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
    real_locate = anchors.locate

    def locate_then_turn_secure(root, anchor, low_confidence=False):
        found = real_locate(root, anchor, low_confidence=low_confidence)
        field.subrole = "AXSecureTextField"
        return found

    monkeypatch.setattr(runtime.anchors, "locate", locate_then_turn_secure)
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


def test_a_write_behind_a_sheet_is_refused():
    """The old guard read an AXSheets attribute macOS doesn't have, so it
    never fired. A sheet is an AXSheet child of its window."""
    tree = textedit_like()
    pack = build_write_tool(tree)
    window = tree.children()[0]
    tree.attribute = lambda name, attr=tree.attribute: [window] if name == "AXWindows" else attr(name)
    save_changes = save_changes_sheet()
    window._children.append(save_changes)

    with pytest.raises(runtime.ToolExecutionError, match="a sheet is open on 'untitled.txt'"):
        runtime.execute(pack, "write_document", {"text": "hi"}, tree)
    assert doc_body(tree).value == "hello"
    with pytest.raises(runtime.ToolExecutionError, match="a sheet is open"):
        runtime.execute(pack, "read_document", {}, tree)
    assert not any(button.presses for button in save_changes.children())


def outlook_like():
    """Outlook's main window with its Choose Folder sheet open over it. The
    window has a lookalike search field of its own."""
    main_search = FakeElement("AXTextField", "search box", subrole="AXSearchField", value="")
    folder_search = FakeElement("AXTextField", "search box", subrole="AXSearchField", value="")
    move = PressRecorder("AXButton", "Move")
    cancel = PressRecorder("AXButton", "Cancel")
    choose_folder = sheet(folder_search, move, cancel, cancel=cancel)
    window = FakeElement(
        "AXWindow",
        "Inbox",
        subrole="AXStandardWindow",
        children=[FakeElement("AXToolbar", children=[main_search]), choose_folder],
    )
    return FakeElement("AXApplication", "Outlook", children=[window]), window, choose_folder


def file_to_folder_pack(tree) -> Pack:
    """The example pack's filing steps after its menu press: wait for the
    sheet's search box, type the folder, press Move."""
    nodes = snapshot(tree).nodes
    field = anchors.build(next(n for n in nodes if n.role == "AXTextField" and anchors.in_sheet(n.chain)))
    move = anchors.build(next(n for n in nodes if n.label == "Move"))
    tool = ToolSpec(
        name="file_to_folder",
        description="File the selected message.",
        risk="mutating",
        params={"folder": {"type": "string", "description": "Folder name."}},
        steps=[
            Step(op="wait_for", anchor=field, timeout_s=0.4),
            Step(op="set_value", anchor=field, value={"param": "folder"}, timeout_s=0.2),
            Step(op="press", anchor=move, timeout_s=0.2),
        ],
    )
    return Pack(bundle_id="com.microsoft.Outlook", app_name="Outlook", tools=[tool])


def test_steps_recorded_inside_the_sheet_act_in_it():
    tree, window, choose_folder = outlook_like()
    pack = file_to_folder_pack(tree)
    main_search = window.children()[0].children()[0]
    folder_search, move, cancel = choose_folder.children()

    assert runtime.execute(pack, "file_to_folder", {"folder": "Receipts"}, tree).ok
    assert folder_search.value == "Receipts"
    assert move.presses == 1
    assert main_search.value == ""
    assert cancel.presses == 0


def test_a_sheet_step_never_binds_the_windows_lookalike():
    """Before the sheet opens, the window's own search box matches the
    sheet's in everything but the sheet. Typing the folder name into it
    would search the mailbox instead of filing the message."""
    tree, window, choose_folder = outlook_like()
    pack = file_to_folder_pack(tree)
    window._children.remove(choose_folder)
    with pytest.raises(runtime.ToolExecutionError, match="did not appear"):
        runtime.execute(pack, "file_to_folder", {"folder": "Receipts"}, tree)
    assert window.children()[0].children()[0].value == ""


def test_a_modal_dialog_blocks_every_step_outside_it():
    dialogs = [
        FakeElement("AXWindow", "", subrole="AXDialog"),
        FakeElement("AXWindow", "", subrole="AXSystemDialog"),
        FakeElement("AXWindow", "Export", subrole="AXStandardWindow", modal=True),
    ]
    for dialog in dialogs:
        tree = textedit_like()
        pack = build_write_tool(tree)
        tree._children.append(dialog)
        with pytest.raises(runtime.ToolExecutionError, match="TextEdit has a dialog open"):
            runtime.execute(pack, "write_document", {"text": "hi"}, tree)
        assert doc_body(tree).value == "hello"


def test_a_step_recorded_inside_a_dialog_may_act_in_it():
    export = PressRecorder("AXButton", "Export")
    alert = FakeElement("AXWindow", "", subrole="AXDialog", children=[export])
    tree = textedit_like()
    tree._children.append(alert)
    node = next(n for n in snapshot(tree).nodes if n.label == "Export")
    tool = ToolSpec(
        name="export",
        description="Confirm the export.",
        risk="mutating",
        steps=[Step(op="press", anchor=anchors.build(node), timeout_s=0.2)],
    )
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    assert runtime.execute(pack, "export", {}, tree).ok
    assert export.presses == 1


def test_a_window_modal_only_by_axmodal_needs_a_pinned_title():
    """A window the app runs modally keeps a standard subrole, so only an
    anchor that pinned its title can show it was recorded there."""
    export = PressRecorder("AXButton", "Export")
    panel = FakeElement("AXWindow", "Export", subrole="AXStandardWindow", modal=True, children=[export])
    tree = FakeElement("AXApplication", "TextEdit", children=[panel])
    anchor = anchors.build(next(n for n in snapshot(tree).nodes if n.role == "AXButton"))
    assert anchor.window_title == "Export"
    tool = ToolSpec(
        name="export",
        description="Confirm the export.",
        risk="mutating",
        steps=[Step(op="press", anchor=anchor, timeout_s=0.2)],
    )
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    assert runtime.execute(pack, "export", {}, tree).ok

    anchor.window_title = ""
    with pytest.raises(runtime.ToolExecutionError, match="was recorded there"):
        runtime.execute(pack, "export", {}, tree)
    assert export.presses == 1


def test_a_menu_command_is_refused_while_the_focused_window_has_a_sheet():
    tree = textedit_with_menus()
    menu_node = next(n for n in snapshot(tree, include_menus=True).nodes if n.label == "New")
    new_document = ToolSpec(
        name="new_document",
        description="Open a new document.",
        risk="mutating",
        steps=[Step(op="press", anchor=anchors.build(menu_node), timeout_s=0.2)],
    )
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[new_document])
    window = tree.children()[0]
    other = FakeElement("AXWindow", "notes.txt", children=[save_changes_sheet()])
    tree._children.insert(1, other)

    # A sheet on a window the command doesn't land on is not in the way...
    tree.focused_window = window
    assert runtime.execute(pack, "new_document", {}, tree).ok
    # ...but one on the focused window is.
    tree.focused_window = other
    with pytest.raises(runtime.ToolExecutionError, match="a sheet is open on 'notes.txt'"):
        runtime.execute(pack, "new_document", {}, tree)


class ClosingSheetWindow(FakeElement):
    """A window whose sheet stays in the tree for its first few child
    reads, the way a sheet animating closed does."""

    def __init__(self, *args, reads_open: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads_open = reads_open

    def children(self):
        if self.reads_open > 0:
            self.reads_open -= 1
            return self._children
        return [child for child in self._children if child.role != "AXSheet"]


def wait_for_body_pack(tree, timeout_s):
    body = anchors.build(next(n for n in snapshot(tree).nodes if n.identifier == "doc-body"))
    tool = ToolSpec(
        name="wait_for_body",
        description="Wait for the document body.",
        risk="read_only",
        steps=[Step(op="wait_for", anchor=body, timeout_s=timeout_s)],
    )
    return Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])


def test_wait_for_outlasts_a_sheet_that_is_closing():
    body = FakeElement("AXTextArea", value="hello", identifier="doc-body")
    window = ClosingSheetWindow(
        "AXWindow", "untitled.txt", reads_open=0, children=[FakeElement("AXScrollArea", children=[body])]
    )
    tree = FakeElement("AXApplication", "TextEdit", children=[window])
    pack = wait_for_body_pack(tree, timeout_s=2.0)
    window._children.append(save_changes_sheet())
    window.reads_open = 3
    assert runtime.execute(pack, "wait_for_body", {}, tree).ok


def test_wait_for_names_a_sheet_that_never_closes():
    tree = textedit_like()
    pack = wait_for_body_pack(tree, timeout_s=0.4)
    tree.children()[0]._children.append(save_changes_sheet())
    with pytest.raises(runtime.ToolExecutionError, match="the element appeared, but a sheet is open"):
        runtime.execute(pack, "wait_for_body", {}, tree)


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


def test_a_sheet_on_another_space_still_blocks_a_write():
    tree = textedit_like()
    pack = build_write_tool(tree)
    window = tree.children()[0]
    window._children.append(save_changes_sheet())
    on_another_space(tree)
    with pytest.raises(runtime.ToolExecutionError, match="a sheet is open on 'untitled.txt'"):
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


def test_a_call_fails_when_its_app_never_comes_to_the_front(monkeypatch):
    """The wait used to give up quietly and run the steps anyway, so the
    press reached TextEdit in the background and the call reported
    success. Now no step runs, and the user's app is given back."""

    class App(FakeElement):
        def attribute(self, name):
            if name == "AXFrontmost":
                return False
            return super().attribute(name)

    bold = PressRecorder("AXCheckBox", "bold")
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
    activated = []
    monkeypatch.setattr(runtime.ax, "frontmost_app", lambda: runtime.ax.RunningApp("Terminal", "com.apple.Terminal", 2))
    monkeypatch.setattr(runtime.ax, "activate", lambda app: activated.append(app.name) or True)
    monkeypatch.setattr(runtime, "FRONTMOST_WAIT_S", 0.2)
    app = runtime.ax.RunningApp("TextEdit", "com.apple.TextEdit", 1)

    with pytest.raises(runtime.ToolExecutionError, match="TextEdit did not come to the front within 0.2s, so no step ran"):
        runtime.execute(pack, "bold", {}, tree, app=app)
    assert bold.presses == 0
    assert activated == ["TextEdit", "Terminal"]


def test_a_failure_after_a_press_says_the_press_went_out():
    """A caller told only "step 1 failed" may take it that nothing
    happened and call again, pressing a second time."""
    tree = textedit_like()
    window = tree.children()[0]
    next_page = PressRecorder("AXButton", "Next Page")
    window._children.append(next_page)
    nodes = snapshot(tree).nodes
    tool = ToolSpec(
        name="next_then_write",
        description="Turn the page, then write.",
        risk="mutating",
        params={"text": {"type": "string", "description": "Text."}},
        steps=[
            Step(op="press", anchor=anchors.build(next(n for n in nodes if n.label == "Next Page")), timeout_s=0.2),
            Step(
                op="set_value",
                anchor=anchors.build(next(n for n in nodes if n.identifier == "doc-body")),
                value={"param": "text"},
                timeout_s=0.2,
            ),
        ],
    )
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    window._children[2]._children.clear()  # the body is gone by the second step

    with pytest.raises(runtime.ToolExecutionError) as failed:
        runtime.execute(pack, "next_then_write", {"text": "x"}, tree)
    assert next_page.presses == 1
    assert "step 1 (set_value)" in str(failed.value)
    assert str(failed.value).endswith("The press in step 0 was already sent, so TextEdit may have changed.")


def test_a_failure_before_anything_was_sent_claims_nothing():
    tree = textedit_like()
    pack = build_write_tool(tree)
    tree.children()[0]._children[2]._children.clear()
    with pytest.raises(runtime.ToolExecutionError) as failed:
        runtime.execute(pack, "write_document", {"text": "x"}, tree)
    assert "already sent" not in str(failed.value)


def test_a_failed_verify_says_the_write_went_out():
    tree = textedit_like()
    pack = build_write_tool(tree)
    doc_body(tree).set_value = lambda value: None  # the app swallows the write
    with pytest.raises(runtime.ToolExecutionError, match="value does not contain") as failed:
        runtime.execute(pack, "write_document", {"text": "brand new"}, tree)
    assert "The write in step 0 was already sent" in str(failed.value)


class FlakyApp(FakeElement):
    """An app root that stops answering for a stretch of child reads, the
    way a busy app does for a moment."""

    def __init__(self, *args, busy_reads=range(0), **kwargs):
        super().__init__(*args, **kwargs)
        self.busy_reads = busy_reads
        self.reads = 0

    def children(self):
        read = self.reads
        self.reads += 1
        if read in self.busy_reads:
            raise runtime.ax.AXTransientError("App is not answering accessibility queries (AXError -25204).")
        return super().children()


def flaky_textedit():
    return FlakyApp("AXApplication", "TextEdit", children=textedit_like().children())


def test_wait_for_keeps_polling_while_the_app_is_busy():
    """A busy poll used to end the wait at once with a transient error,
    though waiting is what the step is for."""
    tree = flaky_textedit()
    pack = wait_for_body_pack(tree, timeout_s=3.0)
    tree.reads, tree.busy_reads = 0, range(1, 5)  # read 0 is the call's own dialog check
    assert runtime.execute(pack, "wait_for_body", {}, tree).ok
    assert tree.reads > 5


def test_wait_for_says_when_the_app_never_answered():
    tree = flaky_textedit()
    pack = wait_for_body_pack(tree, timeout_s=0.4)
    tree.reads, tree.busy_reads = 0, range(1, 10**9)
    with pytest.raises(runtime.ToolExecutionError, match="TextEdit was still not answering after 0.4s"):
        runtime.execute(pack, "wait_for_body", {}, tree)


def test_a_failed_call_cancels_the_sheet_it_opened():
    """A failed Outlook filing left its Choose Folder sheet open, and the
    next call met it. The sheet's own cancel button is pressed, and the
    message says so."""
    tree, window, choose_folder = outlook_like()
    folder_search, move, cancel = choose_folder.children()
    result = FakeElement("AXRow", "Receipts", actions=["AXPress"])
    choose_folder._children.append(result)
    choose = PressRecorder("AXButton", "Choose Folder...", on_press=lambda: window._children.append(choose_folder))
    window._children.insert(1, choose)
    nodes = snapshot(tree).nodes

    def recorded(label):
        return anchors.build(next(n for n in nodes if n.label == label))

    field = anchors.build(next(n for n in nodes if n.role == "AXTextField" and anchors.in_sheet(n.chain)))
    tool = ToolSpec(
        name="file_to_folder",
        description="File the selected message.",
        risk="mutating",
        params={"folder": {"type": "string", "description": "Folder name."}},
        steps=[
            Step(op="press", anchor=recorded("Choose Folder..."), timeout_s=0.2),
            Step(op="wait_for", anchor=field, timeout_s=0.4),
            Step(op="set_value", anchor=field, value={"param": "folder"}, timeout_s=0.2),
            Step(op="wait_for", anchor=recorded("Receipts"), timeout_s=0.4),
            Step(op="press", anchor=recorded("Move"), timeout_s=0.2),
        ],
    )
    pack = Pack(bundle_id="com.microsoft.Outlook", app_name="Outlook", tools=[tool])
    choose_folder._children.remove(result)  # the search finds no such folder
    window._children.remove(choose_folder)  # and the call starts with the sheet closed
    cancel._on_press = lambda: window._children.remove(choose_folder)

    with pytest.raises(runtime.ToolExecutionError) as failed:
        runtime.execute(pack, "file_to_folder", {"folder": "Receipts"}, tree)
    message = str(failed.value)
    assert "step 3 (wait_for): element did not appear" in message
    assert "The press in step 0 and the write in step 2 were already sent" in message
    assert "vent cancelled the sheet on 'Inbox', which the call had opened." in message
    assert (cancel.presses, move.presses) == (1, 0)
    assert choose_folder not in window.children()


def test_a_sheet_the_call_opened_without_a_cancel_button_is_left_for_a_human():
    tree = textedit_like()
    window = tree.children()[0]
    ok = PressRecorder("AXButton", "OK")
    opens = PressRecorder("AXButton", "Print", on_press=lambda: window._children.append(sheet(ok)))
    window._children.append(opens)
    nodes = snapshot(tree).nodes
    tool = ToolSpec(
        name="print_then_write",
        description="Print, then write.",
        risk="mutating",
        params={"text": {"type": "string", "description": "Text."}},
        steps=[
            Step(op="press", anchor=anchors.build(next(n for n in nodes if n.label == "Print")), timeout_s=0.2),
            Step(
                op="set_value",
                anchor=anchors.build(next(n for n in nodes if n.identifier == "doc-body")),
                value={"param": "text"},
                timeout_s=0.2,
            ),
        ],
    )
    pack = Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    with pytest.raises(runtime.ToolExecutionError, match="a sheet is open on 'untitled.txt'") as failed:
        runtime.execute(pack, "print_then_write", {"text": "x"}, tree)
    assert "The sheet on 'untitled.txt' opened during the call and is still open" in str(failed.value)
    assert ok.presses == 0
    assert doc_body(tree).value == "hello"


def test_a_healed_anchor_that_does_not_resolve_is_a_tool_error():
    """The second resolve after a heal let AnchorLost escape as a
    traceback instead of a message naming the step."""
    tree = textedit_like()
    pack = build_write_tool(tree)
    doc_body(tree)._identifier = "renamed-by-update"
    ghost = anchors.Anchor("AXTextArea", "never-there", [], "", [])
    with pytest.raises(runtime.ToolExecutionError, match="the healed anchor did not resolve either"):
        runtime.execute(pack, "write_document", {"text": "x"}, tree, heal=lambda broken, root: ghost)
