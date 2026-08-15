"""Healing: re-ground broken anchors, quarantine, refuse, promote.

The properties pinned here are the T8 fences: healing refuses a truncated
snapshot and a destructive re-grounding, never rewrites the pack without a
promote, reuses a quarantined fix deterministically, and re-screens that
reuse every time.
"""

import pytest

from vent import anchors, heal, llm, packs
from vent.snapshot import snapshot

from .fakes import FakeElement, textedit_like


@pytest.fixture(autouse=True)
def clear_completer():
    yield
    llm.set_completer_for_tests(None)


def scripted(response):
    llm.set_completer_for_tests(lambda **kw: response)


def body_anchor(tree):
    snap = snapshot(tree)
    body = next(n for n in snap.nodes if n.identifier == "doc-body")
    return anchors.build(body)


def pack_with_step(tree, tmp_path):
    """A pack whose one tool presses the doc body, plus its on-disk path."""
    anchor = body_anchor(tree)
    tool = packs.ToolSpec(
        name="touch_body",
        description="Press the document body.",
        risk="mutating",
        steps=[packs.Step(op="press", anchor=anchor)],
    )
    pack = packs.Pack(bundle_id="com.apple.TextEdit", app_name="TextEdit", tools=[tool])
    path = tmp_path / pack.bundle_id / "pack.json"
    return pack, path, anchor


def break_body(tree):
    """Rename the body's identifier so the original anchor no longer resolves."""
    body = tree.children()[0].children()[2].children()[0]
    body._identifier = "renamed-after-update"
    body.label = body.title = "Body"


def test_truncated_snapshot_refuses(tmp_path, monkeypatch):
    tree = textedit_like()
    pack, path, anchor = pack_with_step(tree, tmp_path)
    # Force every snapshot the healer takes to look truncated.
    import vent.heal as heal_mod
    real = heal_mod.snapshot

    def truncated(root, **kw):
        snap = real(root, **kw)
        snap.truncated = True
        return snap

    monkeypatch.setattr(heal_mod, "snapshot", truncated)
    cb = heal.make_heal_callback(pack, path)
    assert cb(anchor, tree) is None


def test_model_regrounds_and_quarantines(tmp_path):
    tree = textedit_like()
    pack, path, anchor = pack_with_step(tree, tmp_path)
    break_body(tree)

    snap = snapshot(tree)
    new_id = next(n.id for n in snap.nodes if n.identifier == "renamed-after-update")
    scripted({"node_id": new_id, "confident": True})

    cb = heal.make_heal_callback(pack, path, ask_model=True)
    healed = cb(anchor, tree)
    assert healed is not None
    assert healed.identifier == "renamed-after-update"
    # Quarantined, not promoted: the tool's step anchor is unchanged.
    assert len(pack.healed_pending) == 1
    assert pack.tools[0].steps[0].anchor.identifier == "doc-body"
    assert path.exists()  # persisted


def test_unconfident_model_refuses(tmp_path):
    tree = textedit_like()
    pack, path, anchor = pack_with_step(tree, tmp_path)
    break_body(tree)
    scripted({"node_id": 1, "confident": False})
    cb = heal.make_heal_callback(pack, path, ask_model=True)
    assert cb(anchor, tree) is None
    assert pack.healed_pending == []


def test_destructive_regrounding_refused(tmp_path):
    tree = textedit_like()
    window = tree.children()[0]
    window._children.append(FakeElement("AXButton", "Delete Everything", actions=["AXPress"]))
    pack, path, anchor = pack_with_step(tree, tmp_path)
    break_body(tree)

    snap = snapshot(tree)
    danger_id = next(n.id for n in snap.nodes if n.label == "Delete Everything")
    scripted({"node_id": danger_id, "confident": True})

    cb = heal.make_heal_callback(pack, path, ask_model=True)
    assert cb(anchor, tree) is None  # policy re-check blocks it
    assert pack.healed_pending == []


def test_quarantined_fix_reused_without_model(tmp_path):
    tree = textedit_like()
    pack, path, anchor = pack_with_step(tree, tmp_path)
    break_body(tree)
    snap = snapshot(tree)
    new_node = next(n for n in snap.nodes if n.identifier == "renamed-after-update")
    healed = anchors.build(new_node)
    pack.healed_pending.append({
        "original": anchor.to_dict(),
        "healed": healed.to_dict(),
        "target_role": new_node.role,
        "target_label": new_node.label,
    })

    # No completer installed: any model call would raise. Reuse must be
    # purely deterministic.
    llm.set_completer_for_tests(None)
    cb = heal.make_heal_callback(pack, path, ask_model=False)
    result = cb(anchor, tree)
    assert result is not None
    assert result.identifier == "renamed-after-update"


def test_promote_rewrites_anchor_of_record(tmp_path):
    tree = textedit_like()
    pack, path, anchor = pack_with_step(tree, tmp_path)
    break_body(tree)
    snap = snapshot(tree)
    new_node = next(n for n in snap.nodes if n.identifier == "renamed-after-update")
    healed = anchors.build(new_node)
    pack.healed_pending.append({
        "original": anchor.to_dict(),
        "healed": healed.to_dict(),
        "target_role": new_node.role,
        "target_label": new_node.label,
    })

    rewritten = heal.promote(pack, 0)
    assert rewritten == 1
    assert pack.tools[0].steps[0].anchor.identifier == "renamed-after-update"
    assert pack.healed_pending == []


def test_promoted_pack_still_validates(tmp_path):
    tree = textedit_like()
    pack, path, anchor = pack_with_step(tree, tmp_path)
    break_body(tree)
    snap = snapshot(tree)
    new_node = next(n for n in snap.nodes if n.identifier == "renamed-after-update")
    healed = anchors.build(new_node)
    pack.healed_pending.append({
        "original": anchor.to_dict(), "healed": healed.to_dict(),
        "target_role": new_node.role, "target_label": new_node.label,
    })
    heal.promote(pack, 0)
    # Round-trip through the validator: a healed pack is a valid pack.
    packs.Pack.from_dict(pack.to_dict())
