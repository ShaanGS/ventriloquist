"""Anchor resolution under churn.

Each test mutates a fake tree the way real app updates mutate real trees,
then checks the resolver either finds the same element or refuses loudly.
Refusal is a feature: binding the wrong element and reporting success is
the one failure mode this project must never have.
"""

import json

import pytest

from vent import anchors
from vent.snapshot import render, snapshot

from .fakes import (
    FakeElement,
    GuardedElement,
    notes_like,
    on_another_space,
    textedit_like,
    textedit_with_menus,
)


def anchor_for(tree: FakeElement, **match) -> anchors.Anchor:
    """Build an anchor for the first snapshot node matching the criteria."""
    snap = snapshot(tree)
    for node in snap.nodes:
        if all(getattr(node, key) == value for key, value in match.items()):
            return anchors.build(node)
    raise AssertionError(f"no node matching {match} in {[n.role for n in snap.nodes]}")


def test_resolves_unchanged_tree():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    element = anchors.resolve(tree, anchor)
    assert element.value == "hello"


def test_survives_label_rename_when_identifier_stable():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    # The app relabels things between versions; the identifier persists.
    body = tree.children()[0].children()[2].children()[0]
    body.label = body.title = "Document body"
    assert anchors.resolve(tree, anchor).value == "hello"


def test_survives_new_sibling_insertion():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    window = tree.children()[0]
    # An update adds a toolbar before the scroll area, shifting indices.
    window._children.insert(0, FakeElement("AXToolbar", "Toolbar"))
    assert anchors.resolve(tree, anchor).value == "hello"


def test_survives_wrapper_level_insertion():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    window = tree.children()[0]
    scroll = window._children[2]
    # An update wraps the scroll area in one more group, a very common change.
    window._children[2] = FakeElement("AXGroup", children=[scroll])
    assert anchors.resolve(tree, anchor).value == "hello"


def test_lost_when_element_removed():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    scroll = tree.children()[0]._children[2]
    scroll._children.clear()
    with pytest.raises(anchors.AnchorLost):
        anchors.resolve(tree, anchor)


def test_ambiguous_twins_refuse_to_guess():
    tree = notes_like()
    window = tree.children()[0]
    # Two identical unlabeled text areas side by side, no identifiers.
    twin_a = FakeElement("AXTextArea", value="a")
    twin_b = FakeElement("AXTextArea", value="b")
    window._children.append(FakeElement("AXGroup", children=[twin_a]))
    window._children.append(FakeElement("AXGroup", children=[twin_b]))

    snap = snapshot(tree)
    twin_node = next(
        n for n in snap.nodes if n.role == "AXTextArea" and n.value_preview == "a"
    )
    anchor = anchors.build(twin_node)
    anchor.identifier = ""  # simulate an app that never set identifiers

    # With identical structure either twin matches equally well. Guessing
    # between them is exactly the wrong-element bug; the resolver must
    # refuse instead. (It may also fail the threshold outright, which is
    # equally safe; what it must never do is silently return one twin.)
    with pytest.raises((anchors.AnchorAmbiguous, anchors.AnchorLost)):
        anchors.resolve(tree, anchor)


def test_search_field_never_mistaken_for_note_body():
    tree = notes_like()
    anchor = anchor_for(tree, identifier="note-body")
    # Remove the body entirely; the search field is the tempting lookalike.
    scroll = tree.children()[0]._children[2]
    scroll._children.clear()
    with pytest.raises((anchors.AnchorLost, anchors.AnchorAmbiguous)):
        anchors.resolve(tree, anchor)


def test_low_confidence_raises_the_bar():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    body = tree.children()[0].children()[2].children()[0]
    body._identifier = ""  # identifier gone after an app update
    # Normal confidence may still accept on structure alone; low confidence
    # (stale pack) must not.
    with pytest.raises((anchors.AnchorLost, anchors.AnchorAmbiguous)):
        anchors.resolve(tree, anchor, low_confidence=True)


def test_secure_fields_never_surface():
    tree = textedit_like()
    window = tree.children()[0]
    window._children.append(
        FakeElement("AXTextField", "Password", subrole="AXSecureTextField", value="hunter2")
    )
    snap = snapshot(tree)
    assert not any(n.subrole == "AXSecureTextField" for n in snap.nodes)
    assert not any("hunter2" in n.value_preview for n in snap.nodes)


def test_a_field_that_turned_secure_is_never_resolved():
    """A plain text field records no subrole, so the subrole check had
    nothing to compare and resolution bound the field after it became a
    password field. The walk now skips secure fields outright."""
    tree = textedit_like()
    field = FakeElement("AXTextField", "Account", identifier="account", value="")
    tree.children()[0]._children.append(field)
    anchor = anchor_for(tree, identifier="account")
    field.subrole = "AXSecureTextField"
    with pytest.raises(anchors.AnchorLost):
        anchors.resolve(tree, anchor)


def test_a_secure_twin_is_not_a_candidate():
    """A password field next to a plain field with the same label used to
    make the plain one ambiguous, or worse, win."""
    tree = textedit_like()
    window = tree.children()[0]
    plain = FakeElement("AXTextField", "Code", value="")
    window._children.append(plain)
    anchor = anchor_for(tree, role="AXTextField")
    window._children.insert(0, FakeElement("AXTextField", "Code", subrole="AXSecureTextField", value="1234"))
    assert anchors.resolve(tree, anchor) is plain


def test_truncation_is_explicit():
    tree = textedit_like()
    snap = snapshot(tree, max_nodes=1)
    assert snap.truncated


def test_conflicting_identifier_disqualifies_even_as_sole_candidate():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    body = tree.children()[0].children()[2].children()[0]
    # A different non-empty identifier means a different element, even if
    # everything structural matches perfectly.
    body._identifier = "some-other-element"
    with pytest.raises(anchors.AnchorLost):
        anchors.resolve(tree, anchor)


def test_recorded_window_title_disqualifies_lookalike_window():
    # Two structurally identical document windows; the anchor was recorded
    # in one of them. With only the other window open, structure alone
    # matches perfectly, and only the window title says it is the wrong
    # document. Resolution must refuse rather than overwrite it.
    def make_window(title, body_value):
        return FakeElement(
            "AXWindow",
            title,
            children=[
                FakeElement(
                    "AXScrollArea",
                    children=[FakeElement("AXTextArea", value=body_value)],
                )
            ],
        )

    tree = FakeElement("AXApplication", "TextEdit", children=[make_window("notes.txt", "a")])
    anchor = anchor_for(tree, role="AXTextArea")
    assert anchor.window_title == "notes.txt"

    wrong = FakeElement(
        "AXApplication", "TextEdit", children=[make_window("taxes.txt", "b")]
    )
    with pytest.raises(anchors.AnchorLost):
        anchors.resolve(wrong, anchor)


def document_window(title, body, identifier="First Text View"):
    """A TextEdit document window. Every document's text area carries the
    same nib-derived identifier, as in the real app."""
    return FakeElement(
        "AXWindow",
        title,
        subrole="AXStandardWindow",
        children=[
            FakeElement(
                "AXScrollArea",
                children=[FakeElement("AXTextArea", value=body, identifier=identifier)],
            )
        ],
    )


def test_title_pinned_anchor_never_binds_in_another_document():
    """A tool recorded in vent-demo.txt overwrote taxes-2025.txt. The
    shared identifier is worth 6 and a title mismatch cost only 2, so the
    taxes text area cleared the bar with room to spare."""
    recorded = FakeElement("AXApplication", "TextEdit", children=[document_window("vent-demo.txt", "demo")])
    anchor = anchor_for(recorded, role="AXTextArea")
    assert anchor.window_title == "vent-demo.txt"

    taxes_only = FakeElement("AXApplication", "TextEdit", children=[document_window("taxes-2025.txt", "1040")])
    with pytest.raises(anchors.AnchorLost, match="window title mismatch"):
        anchors.resolve(taxes_only, anchor)

    # With both documents open the pinned one is found, even when the
    # taxes window is the focused one.
    both = FakeElement(
        "AXApplication",
        "TextEdit",
        children=[document_window("taxes-2025.txt", "1040"), document_window("vent-demo.txt", "demo")],
    )
    both.focused_window = both.children()[0]
    assert anchors.resolve(both, anchor).value == "demo"


def test_title_pinned_anchor_still_searches_an_untitled_window():
    """Only a different non-empty title disqualifies. A window that reports
    no title at all is searched as before."""
    recorded = FakeElement("AXApplication", "TextEdit", children=[document_window("vent-demo.txt", "demo")])
    anchor = anchor_for(recorded, role="AXTextArea")
    untitled = FakeElement("AXApplication", "TextEdit", children=[document_window("", "demo")])
    assert anchors.resolve(untitled, anchor).value == "demo"


def test_same_window_title_still_resolves():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    assert anchors.resolve(tree, anchor).value == "hello"


def two_document_app(focused):
    """TextEdit with two documents open, built like the real app: both text
    areas carry the same nib-derived identifier, so only the window tells
    them apart."""

    def document(title, body):
        return FakeElement(
            "AXWindow",
            title,
            children=[
                FakeElement(
                    "AXScrollArea",
                    children=[FakeElement("AXTextArea", value=body, identifier="First Text View")],
                )
            ],
        )

    app = FakeElement("AXApplication", "TextEdit", children=[document("a.txt", "a"), document("b.txt", "b")])
    if focused is not None:
        app.focused_window = app.children()[focused]
    return app


def window_agnostic_anchor(app):
    anchor = anchor_for(app, role="AXTextArea", value_preview="a")
    anchor.window_title = ""  # how a shared "frontmost document" tool records it
    return anchor


def test_window_agnostic_anchor_follows_the_focused_window():
    """A "frontmost document" tool matches the same text area in every
    open document. The focused window settles that tie, so with two
    documents open the call reaches the one in front instead of refusing."""
    app = two_document_app(focused=1)
    anchor = window_agnostic_anchor(app)
    assert anchors.resolve(app, anchor).value == "b"
    app.focused_window = app.children()[0]
    assert anchors.resolve(app, anchor).value == "a"


def test_window_agnostic_tie_without_a_focused_window_refuses():
    app = two_document_app(focused=None)
    with pytest.raises(anchors.AnchorAmbiguous):
        anchors.resolve(app, window_agnostic_anchor(app))


def test_title_pinned_anchor_is_never_retargeted_by_focus():
    """An anchor recorded in notes.txt must not write into another document
    just because that document is in front."""
    app = two_document_app(focused=0)
    anchor = window_agnostic_anchor(app)
    anchor.window_title = "notes.txt"
    with pytest.raises((anchors.AnchorAmbiguous, anchors.AnchorLost)):
        anchors.resolve(app, anchor)


def test_twins_inside_the_focused_window_still_refuse():
    tree = notes_like()
    window = tree.children()[0]
    window._children.append(FakeElement("AXGroup", children=[FakeElement("AXTextArea", value="a")]))
    window._children.append(FakeElement("AXGroup", children=[FakeElement("AXTextArea", value="b")]))
    tree.focused_window = window

    node = next(n for n in snapshot(tree).nodes if n.role == "AXTextArea" and n.value_preview == "a")
    anchor = anchors.build(node)
    anchor.identifier = ""
    anchor.window_title = ""
    with pytest.raises((anchors.AnchorAmbiguous, anchors.AnchorLost)):
        anchors.resolve(tree, anchor)


def electron_app(title):
    """A tree shaped like Slack's: the window title repeats on the root
    group, and the web area's label contains it."""
    home = FakeElement("AXRadioButton", "Home", subrole="AXTabButton", actions=["AXPress"])
    tree = FakeElement(
        "AXApplication",
        "Slack",
        children=[
            FakeElement(
                "AXWindow",
                title,
                subrole="AXStandardWindow",
                children=[
                    FakeElement(
                        "AXGroup",
                        title,
                        children=[
                            FakeElement(
                                "AXWebArea",
                                f"{title} (web content)",
                                children=[FakeElement("AXGroup", "Sidebar", children=[home])],
                            )
                        ],
                    )
                ],
            )
        ],
    )
    return tree, home


def test_recorded_chains_leave_out_labels_that_carry_the_window_title():
    """The bundled Slack pack shipped a workspace name this way: the window
    link was blanked, but the same title sat on the group and web area
    below it."""
    tree, _ = electron_app("general (Channel) - Acme Corp - Slack")
    anchor = anchor_for(tree, label="Home")
    assert "Acme" not in json.dumps(anchor.to_dict()["chain"])
    labels = [link.label for link in anchor.chain]
    assert "Sidebar" in labels  # an ancestor label without the title stays
    assert labels[-1] == "Home"  # and so does the element's own


def test_title_free_chains_still_match_after_the_window_title_changes():
    """Matching keys labels the same way, so the anchor scores a full chain
    match after a channel or workspace switch retitles the window."""
    tree, _ = electron_app("general (Channel) - Acme Corp - Slack")
    anchor = anchor_for(tree, label="Home")
    anchor.window_title = ""  # shared packs pin no window, like the bundled Slack pack
    switched, home = electron_app("random (Channel) - Other Co - Slack")
    live = next(n for n in snapshot(switched).nodes if n.label == "Home")
    assert anchors._ancestry_similarity(anchor.chain, live.chain, anchor.window_title) == 1.0
    assert anchors.resolve(switched, anchor) is home


def test_window_anchor_never_walks_the_menu_bar():
    """The menu bar is about 365 of TextEdit's roughly 410 elements, and
    walking it was most of every tool call. An anchor recorded in a window
    can't live there, so resolution must not descend into it."""
    tree = textedit_like()
    tree._children.append(GuardedElement("AXMenuBar"))
    anchor = anchor_for(tree, identifier="doc-body")
    assert not anchors.in_menu_bar(anchor)
    assert anchors.resolve(tree, anchor).value == "hello"


def test_menu_bar_anchor_still_resolves():
    tree = textedit_with_menus()
    node = next(n for n in snapshot(tree, include_menus=True).nodes if n.label == "New")
    anchor = anchors.build(node)
    assert anchors.in_menu_bar(anchor)
    assert anchors.resolve(tree, anchor).label == "New"


def test_anchor_without_a_chain_still_searches_menus():
    """A hand-built anchor records no chain, so nothing rules the menu bar out."""
    tree = textedit_with_menus()
    anchor = anchors.Anchor("AXMenuItem", "menu-new", ["New"], "", [])
    assert anchors.in_menu_bar(anchor)
    assert anchors.resolve(tree, anchor).label == "New"


def test_window_on_another_space_is_walked_through_the_main_window():
    """With its window on another Space, an app lists only its menu bar,
    and doctor reported Outlook as "0 elements". The main window still
    answers, so the walk goes through it, once even though it is also the
    focused window, and records the chains a walk on its own Space would."""
    tree = textedit_like()
    tree._children.append(GuardedElement("AXMenuBar"))
    on_another_space(tree)
    snap = snapshot(tree)

    on_its_space = snapshot(textedit_like())
    assert [(n.role, n.value_preview) for n in snap.nodes] == [
        (n.role, n.value_preview) for n in on_its_space.nodes
    ]
    body = next(n for n in snap.nodes if n.identifier == "doc-body")
    assert anchors.build(body) == anchor_for(textedit_like(), identifier="doc-body")
    assert snap.focused_window_title == "untitled.txt"
    assert snap.off_space
    assert "another Space" in render(snap)


def test_anchor_resolves_after_its_window_moves_to_another_space():
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    on_another_space(tree)
    assert anchors.resolve(tree, anchor).value == "hello"


def test_focus_settles_ties_between_windows_on_another_space():
    """When the main and focused windows differ, both are walked, and the
    focused one still settles a window-agnostic tie: its position has to be
    counted the way the walk counts windows the app didn't list."""
    app = two_document_app(focused=1)
    anchor = window_agnostic_anchor(app)
    first, second = app.children()
    app._children = []
    app.main_window = first
    app.focused_window = second
    assert anchors.resolve(app, anchor).value == "b"


def test_a_locked_screen_still_looks_empty(monkeypatch):
    """A locked screen hides windows the way another Space does. vent has
    never read or acted behind it, and reaching for the main window must
    not start now."""
    monkeypatch.setattr("vent.snapshot.session_locked", lambda: True)
    tree = textedit_like()
    anchor = anchor_for(tree, identifier="doc-body")
    on_another_space(tree)
    assert snapshot(tree).nodes == []
    with pytest.raises(anchors.AnchorLost):
        anchors.resolve(tree, anchor)
