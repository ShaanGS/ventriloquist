"""Deterministic tool execution. No model calls, ever.

This module and server.py form the deterministic half of the system. The
dependency rule from ARCHITECTURE.md section 6 applies: importing llm.py
here is a design violation, not a style nit. The healer participates only
through the heal callback passed into execute().

Execution order per tool call: preconditions, then per step (resolve the
anchor by scoring, check the expect assertion, perform the op, settle),
then verifications against live reads. AX actions return when dispatched,
not when the UI has finished reacting, so the settle loop after every
mutating op is what makes step N+1 see the world step N created.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import anchors
from .ax import Element
from .packs import Pack, Step, ToolSpec, Verify

# Settle tuning. The loop polls a shallow shape hash of the tree until it
# stops changing for QUIET_S, or gives up at the step deadline.
SETTLE_POLL_S = 0.05
SETTLE_QUIET_S = 0.15
SETTLE_DEPTH = 6

HealFn = Callable[[anchors.Anchor, Element], Optional[anchors.Anchor]]


class ToolExecutionError(RuntimeError):
    """A tool call failed. The message names the tool, step, and reason,
    because SECURITY.md bans silent degradation."""


@dataclass
class StepReport:
    op: str
    ok: bool
    detail: str = ""


@dataclass
class ToolResult:
    ok: bool
    tool: str
    steps: list[StepReport] = field(default_factory=list)
    detail: str = ""
    values: list[str] = field(default_factory=list)  # read_value output, in step order


def _shape_hash(element: Element, depth: int = SETTLE_DEPTH) -> int:
    """A cheap fingerprint of the tree's current shape and values."""
    parts: list[str] = []

    def visit(el: Element, d: int) -> None:
        parts.append(f"{el.role}|{el.label}|{el.value}")
        if d >= depth:
            return
        for child in el.children():
            visit(child, d + 1)

    visit(element, 0)
    return hash("\n".join(parts))


def settle(root: Element, deadline_s: float) -> None:
    """Wait until the tree stops changing, or the deadline passes.

    Timing out here is not an error. Some apps animate forever; the step's
    verify (and the next step's expect assertion) are the real checks.
    """
    deadline = time.monotonic() + deadline_s
    last = _shape_hash(root)
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(SETTLE_POLL_S)
        current = _shape_hash(root)
        if current == last:
            if time.monotonic() - quiet_since >= SETTLE_QUIET_S:
                return
        else:
            last = current
            quiet_since = time.monotonic()


def _bind(value_spec: Optional[dict], args: dict[str, Any], tool: str) -> Any:
    if not value_spec:
        return None
    if "param" in value_spec:
        name = value_spec["param"]
        if name not in args:
            raise ToolExecutionError(f"{tool}: missing argument {name!r}")
        return args[name]
    if "literal" in value_spec:
        return value_spec["literal"]
    raise ToolExecutionError(f"{tool}: value spec must contain 'param' or 'literal'")


def _resolve(
    step_anchor: anchors.Anchor,
    root: Element,
    low_confidence: bool,
    heal: Optional[HealFn],
    where: str,
) -> Element:
    try:
        return anchors.resolve(root, step_anchor, low_confidence=low_confidence)
    except (anchors.AnchorLost, anchors.AnchorAmbiguous) as exc:
        if heal is None:
            raise ToolExecutionError(f"{where}: {exc}") from exc
        healed = heal(step_anchor, root)
        if healed is None:
            raise ToolExecutionError(f"{where}: {exc} (healing declined)") from exc
        # The healed anchor is used for this one call. Persisting it is the
        # healer's job and goes through quarantine, never through here.
        return anchors.resolve(root, healed, low_confidence=False)


def _check_expect(element: Element, expect: Optional[dict], where: str) -> None:
    if not expect:
        return
    expected_role = expect.get("role")
    if expected_role and element.role != expected_role:
        raise ToolExecutionError(
            f"{where}: resolved element is {element.role}, expected {expected_role}. "
            f"Refusing to act on the wrong element."
        )


def _run_step(
    step: Step,
    root: Element,
    args: dict[str, Any],
    tool: ToolSpec,
    low_confidence: bool,
    heal: Optional[HealFn],
    index: int,
) -> StepReport:
    where = f"{tool.name} step {index} ({step.op})"

    if step.op == "wait_for":
        deadline = time.monotonic() + step.timeout_s
        while time.monotonic() < deadline:
            try:
                if step.anchor:
                    anchors.resolve(root, step.anchor, low_confidence=low_confidence)
                return StepReport(op=step.op, ok=True)
            except (anchors.AnchorLost, anchors.AnchorAmbiguous):
                time.sleep(SETTLE_POLL_S)
        raise ToolExecutionError(f"{where}: condition not met within {step.timeout_s}s")

    if step.op == "raise_window":
        windows = root.attribute("AXWindows")
        target = Element(windows[0]) if windows else None
        if target is None:
            children = [c for c in root.children() if c.role == "AXWindow"]
            target = children[0] if children else None
        if target is None:
            raise ToolExecutionError(f"{where}: app has no windows to raise")
        target.perform("AXRaise")
        settle(root, step.timeout_s)
        return StepReport(op=step.op, ok=True)

    assert step.anchor is not None  # validated at pack load
    element = _resolve(step.anchor, root, low_confidence, heal, where)
    _check_expect(element, step.expect, where)

    if step.op == "read_value":
        value = "" if element.value is None else str(element.value)
        return StepReport(op=step.op, ok=True, detail=value)

    if step.op == "press":
        element.perform(step.action)
    elif step.op == "set_value":
        element.set_value(_bind(step.value, args, tool.name))
    elif step.op == "pick":
        element.perform("AXPick")
    elif step.op == "reveal":
        element.perform("AXScrollToVisible")
    else:
        raise ToolExecutionError(f"{where}: op not implemented in runtime")

    settle(root, step.timeout_s)
    return StepReport(op=step.op, ok=True)


def _run_verify(
    check: Verify,
    root: Element,
    args: dict[str, Any],
    tool: ToolSpec,
    low_confidence: bool,
    index: int,
) -> None:
    where = f"{tool.name} verify {index} ({check.kind})"
    try:
        element = anchors.resolve(root, check.anchor, low_confidence=low_confidence)
    except (anchors.AnchorLost, anchors.AnchorAmbiguous) as exc:
        if check.kind == "element_exists":
            raise ToolExecutionError(f"{where}: {exc}") from exc
        raise ToolExecutionError(f"{where}: cannot verify, {exc}") from exc

    if check.kind == "element_exists":
        return

    expected = args.get(check.param) if check.param else check.literal
    if expected is None:
        raise ToolExecutionError(f"{where}: nothing to compare against")

    # Live read, never a snapshot preview. Snapshot values are truncated
    # for display; comparing against them fails on any long write.
    actual = "" if element.value is None else str(element.value)
    expected = str(expected)

    if check.kind == "value_equals" and actual != expected:
        raise ToolExecutionError(f"{where}: value mismatch (got {actual[:60]!r})")
    if check.kind == "value_contains" and expected not in actual:
        raise ToolExecutionError(f"{where}: value does not contain expected text")


def execute(
    pack: Pack,
    tool_name: str,
    args: dict[str, Any],
    root: Element,
    low_confidence: bool = False,
    heal: Optional[HealFn] = None,
) -> ToolResult:
    """Run one tool against a live app root. Raises ToolExecutionError with
    a message naming the app, tool, and step on any failure."""
    tool = pack.tool(tool_name)

    missing = [p for p in tool.params if p not in args]
    if missing:
        raise ToolExecutionError(f"{tool_name}: missing arguments {missing}")

    for pre in tool.preconditions:
        if pre["kind"] == "window_exists":
            has_window = bool(root.attribute("AXWindows")) or any(
                c.role == "AXWindow" for c in root.children()
            )
            if not has_window:
                raise ToolExecutionError(
                    f"{tool_name}: precondition failed, {pack.app_name} has no open window"
                )

    result = ToolResult(ok=True, tool=tool_name)
    for index, step in enumerate(tool.steps):
        report = _run_step(step, root, args, tool, low_confidence, heal, index)
        result.steps.append(report)
        if step.op == "read_value":
            result.values.append(report.detail)

    for index, check in enumerate(tool.verify):
        _run_verify(check, root, args, tool, low_confidence, index)

    result.detail = f"{len(result.steps)} steps, {len(tool.verify)} checks passed"
    return result
