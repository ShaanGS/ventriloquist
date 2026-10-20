"""Anchor resolution under churn.

Each test mutates a fake tree the way real app updates mutate real trees,
then checks the resolver either finds the same element or refuses loudly.
Refusal is a feature: binding the wrong element and reporting success is
the one failure mode this project must never have.
"""

import pytest

from vent import anchors
from vent.snapshot import snapshot

from .fakes import FakeElement, notes_like, textedit_like


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


def test_recorded_window_title_penalizes_lookalike_window():
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
    with pytest.raises((anchors.AnchorLost, anchors.AnchorAmbiguous)):
        anchors.resolve(wrong, anchor)


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
