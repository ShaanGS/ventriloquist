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
