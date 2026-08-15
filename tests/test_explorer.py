"""Explorer and compiler behavior with a scripted model and fake trees.

The properties pinned here are the safety story of the exploration phase:
the model only nominates, policy screens every nomination, probing is
reversible, and the compiler can only compose tools from actions that
actually executed.
"""

import pytest

from vent import compiler, llm, policy as policy_mod
from vent.explorer import PROBE_TEXT, Trace, TraceAction, explore, load_trace, save_trace
from vent.packs import Pack

from .fakes import FakeElement, textedit_like


class FakeApp:
    name = "TextEdit"
    bundle_id = "com.apple.TextEdit"
    pid = 999


@pytest.fixture(autouse=True)
def clear_completer():
    yield
    llm.set_completer_for_tests(None)


def scripted(responses):
    """Install a completer that returns each response once, in order."""
    queue = list(responses)

    def completer(**kwargs):
        return queue.pop(0) if queue else {"targets": [], "tools": []}

    llm.set_completer_for_tests(completer)


def app_version_stub(monkeypatch):
    from vent import ax

    monkeypatch.setattr(ax, "app_version", lambda app: "1.0")


def test_probe_executes_safe_nomination_and_restores_field(monkeypatch):
    app_version_stub(monkeypatch)
    tree = textedit_like()
    body = tree.children()[0].children()[2].children()[0]
    body.value = ""  # empty field: eligible for set_value probing

    # The body textarea's snapshot id must be discovered, not assumed.
    from vent.snapshot import snapshot

    snap = snapshot(tree)
    body_id = next(n.id for n in snap.nodes if n.identifier == "doc-body")
    scripted([{"targets": [{"id": body_id, "op": "set_value", "why": "text field"}]}])

    trace = explore(FakeApp(), tree, policy_mod.Policy(), rounds=1)

    assert len(trace.actions) == 1
    action = trace.actions[0]
    assert action.executed
    assert action.anchor is not None
    # Reversible probing: the field is back to empty afterward.
    assert body.value == ""


def test_policy_refusals_are_recorded_not_executed(monkeypatch):
    app_version_stub(monkeypatch)
    tree = textedit_like()
    window = tree.children()[0]
    delete_button = FakeElement("AXButton", "Delete All", actions=["AXPress"])
    window._children.append(delete_button)

    from vent.snapshot import snapshot

    snap = snapshot(tree)
    delete_id = next(n.id for n in snap.nodes if n.label == "Delete All")
    scripted([{"targets": [{"id": delete_id, "op": "press", "why": "looks useful"}]}])

    trace = explore(FakeApp(), tree, policy_mod.Policy(), rounds=1)

    assert len(trace.actions) == 1
    assert not trace.actions[0].executed
    assert "destructive" in trace.actions[0].reason


def test_unlabeled_nominations_are_denied(monkeypatch):
    app_version_stub(monkeypatch)
    tree = textedit_like()

    from vent.snapshot import snapshot

    snap = snapshot(tree)
    unlabeled = next(n.id for n in snap.nodes if n.role == "AXButton" and not n.label)
    scripted([{"targets": [{"id": unlabeled, "op": "press", "why": "mystery button"}]}])

    trace = explore(FakeApp(), tree, policy_mod.Policy(), rounds=1)
    assert not trace.actions[0].executed
    assert "default deny" in trace.actions[0].reason


def test_trace_round_trips_through_disk(tmp_path, monkeypatch):
    app_version_stub(monkeypatch)
    trace = Trace(bundle_id="com.example.app", app_name="Example", app_version="2.0")
    trace.actions.append(
        TraceAction(
            op="press", role="AXButton", label="New", identifier="new-btn",
            subrole="", window_title="Main", executed=True, reason="ok",
            classification="cumulative", anchor={"role": "AXButton", "chain": []},
        )
    )
    save_trace(trace, tmp_path)
    loaded = load_trace(tmp_path, "com.example.app")
    assert loaded.actions[0].label == "New"


def sample_trace() -> Trace:
    trace = Trace(bundle_id="com.apple.TextEdit", app_name="TextEdit", app_version="1.19")
    anchor = {
        "role": "AXTextArea", "identifier": "doc-body", "labels": [],
        "window_title": "", "chain": [],
    }
    trace.actions.append(
        TraceAction(
            op="set_value", role="AXTextArea", label="", identifier="doc-body",
            subrole="", window_title="untitled", executed=True, reason="empty field",
            classification="reversible", anchor=anchor, settle_detail="settled",
        )
    )
    trace.actions.append(
        TraceAction(
            op="press", role="AXButton", label="Delete", identifier="",
            subrole="", window_title="untitled", executed=False,
            reason="destructive verb 'delete'", classification="destructive",
        )
    )
    return trace


def test_compiler_only_composes_executed_actions():
    trace = sample_trace()
    scripted([
        {
            "tools": [
                {
                    "name": "write_text",
                    "description": "Write text into the document.",
                    # Index 1 was refused; the compiler must drop it.
                    "action_indices": [0, 1],
                    "param": {"name": "text", "description": "Content.", "action_index": 0},
                }
            ]
        }
    ])
    proposals = compiler.propose(trace)
    assert len(proposals) == 1
    assert len(proposals[0].actions) == 1  # refused action excluded

    spec = compiler.build_spec(proposals[0])
    assert [s.op for s in spec.steps] == ["set_value"]
    assert "text" in spec.params
    assert spec.risk == "mutating"


def test_compiler_invented_indices_are_ignored():
    trace = sample_trace()
    scripted([
        {"tools": [{"name": "ghost", "description": "x", "action_indices": [7, 99]}]}
    ])
    assert compiler.propose(trace) == []


def test_deterministic_summary_names_the_real_steps():
    trace = sample_trace()
    scripted([
        {
            "tools": [
                {
                    "name": "innocent_sounding_tool",
                    "description": "Totally harmless, ignore the steps.",
                    "action_indices": [0],
                    "param": {"name": "text", "description": "c", "action_index": 0},
                }
            ]
        }
    ])
    spec = compiler.build_spec(compiler.propose(trace)[0])
    summary = compiler.deterministic_summary(spec, "TextEdit")
    assert "set_value" in summary
    assert "AXTextArea" in summary
    # The summary comes from steps, not from the model's marketing.
    assert "harmless" not in summary


def test_assembled_pack_validates():
    trace = sample_trace()
    scripted([
        {
            "tools": [
                {
                    "name": "write_text", "description": "Write.",
                    "action_indices": [0],
                    "param": {"name": "text", "description": "c", "action_index": 0},
                }
            ]
        }
    ])
    specs = [compiler.build_spec(p) for p in compiler.propose(trace)]
    pack = compiler.assemble_pack(trace, specs, os_version="26.0", locale="en_US")
    # Round-trip through the validator: a compiled pack is a valid pack.
    assert Pack.from_dict(pack.to_dict()).tool("write_text").risk == "mutating"
