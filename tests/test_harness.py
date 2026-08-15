"""Harness identity judgment and the walk guards it depends on.

The harness publishes survival numbers; these tests pin the honesty rules
the numbers rest on: unverifiable is not survival, cycles terminate, and
DAG-shared elements stay reachable through every path.
"""

from vent import anchors
from vent.harness import AnchorRecord, _identity_verdict, _pick_nodes
from vent.snapshot import snapshot

from .fakes import FakeElement, textedit_like


def record_for(role="AXButton", identifier="", label="", subrole="") -> AnchorRecord:
    return AnchorRecord(
        anchor=anchors.Anchor(role=role, identifier=identifier, labels=[label] if label else [], window_title="", chain=[]),
        role=role,
        identifier=identifier,
        label=label,
        subrole=subrole,
    )


def test_featureless_records_are_unverifiable_not_survivors():
    record = record_for()
    element = FakeElement("AXButton")
    assert _identity_verdict(record, element) == "unverifiable"


def test_subrole_mismatch_is_wrong():
    record = record_for(subrole="AXCloseButton")
    zoom = FakeElement("AXButton", subrole="AXFullScreenButton")
    assert _identity_verdict(record, zoom) == "wrong"
    close = FakeElement("AXButton", subrole="AXCloseButton")
    assert _identity_verdict(record, close) == "same"


def test_identifier_mismatch_is_wrong():
    record = record_for(identifier="save-button", label="Save")
    other = FakeElement("AXButton", "Save", identifier="send-button")
    assert _identity_verdict(record, other) == "wrong"


def test_label_change_tolerated_when_identifier_confirms():
    record = record_for(identifier="play-pause", label="Play")
    toggled = FakeElement("AXButton", "Pause", identifier="play-pause")
    assert _identity_verdict(record, toggled) == "same"


def test_pick_nodes_prefers_actionable_and_identified():
    snap = snapshot(textedit_like())
    picked = _pick_nodes(snap.nodes, cap=3)
    assert all(n.actions or n.identifier for n in picked)


def test_self_referential_tree_terminates():
    tree = textedit_like()
    tree._children.insert(0, tree)  # the wedged-TextEdit case, literally
    snap = snapshot(tree, max_nodes=200)
    # Walk must terminate and still find the real content.
    assert any(n.identifier == "doc-body" for n in snap.nodes)


def test_dag_shared_element_reachable_via_both_paths():
    shared = FakeElement("AXButton", "Shared", identifier="shared-btn", actions=["AXPress"])
    tree = FakeElement(
        "AXApplication",
        "App",
        children=[
            FakeElement(
                "AXWindow",
                "Main",
                children=[
                    FakeElement("AXGroup", children=[shared]),
                    FakeElement("AXToolbar", "Bar", children=[shared]),
                ],
            )
        ],
    )
    snap = snapshot(tree)
    hits = [n for n in snap.nodes if n.identifier == "shared-btn"]
    # A global visited-set guard would surface it once; the path-based
    # guard surfaces it through both parents.
    assert len(hits) == 2


def test_anchor_resolution_survives_self_reference():
    tree = textedit_like()
    anchor = anchors.build(next(n for n in snapshot(tree).nodes if n.identifier == "doc-body"))
    tree._children.insert(0, tree)
    assert anchors.resolve(tree, anchor).value == "hello"
