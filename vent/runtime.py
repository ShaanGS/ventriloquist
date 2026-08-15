"""Deterministic tool execution. No model calls, ever.

This module and server.py form the deterministic half of the system. The
dependency rule from ARCHITECTURE.md section 6 applies: importing llm.py
here is a design violation, not a style nit. The healer participates only
through the heal callback passed into execute().

Execution order per tool call: preconditions (including the modal guard),
then per step (resolve the anchor by scoring, check the expect assertion,
perform the op, settle), then verifications against live reads. AX actions
return when dispatched, not when the UI has finished reacting, so the
settle loop after every mutating op is what makes step N+1 see the world
step N created.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import anchors, ax
from .ax import Element
from .packs import Pack, Step, ToolSpec, Verify

# Settle tuning. After a mutating op the loop waits for the tree to change
# and then go quiet. An op whose effect never appears is reported, not
# silently passed: "no reaction" and "finished reacting" are different
# outcomes and the review caught the first draft conflating them.
SETTLE_POLL_S = 0.05
SETTLE_QUIET_S = 0.15
SETTLE_NO_REACTION_S = 0.6
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


def settle(root: Element, deadline_s: float) -> str:
    """Wait for the tree to react to an op and then go quiet.

    Returns "settled" when a change was observed and the tree then held
    still, "no_reaction" when nothing changed within the grace window, and
    "timeout" when changes kept coming until the deadline. None of these is
    an error by itself; verify and the next step's expect assertion are the
    real checks. The distinction is reported so a probe that concludes
    "this button does nothing" can be told apart from one that acted too
    early, a confusion the design review flagged.
    """
    deadline = time.monotonic() + deadline_s
    baseline = _shape_hash(root)
    changed = False
    last = baseline
    quiet_since = time.monotonic()
    start = quiet_since

    while time.monotonic() < deadline:
        time.sleep(SETTLE_POLL_S)
        current = _shape_hash(root)
        if current != last:
            changed = True
            last = current
            quiet_since = time.monotonic()
            continue
        if changed and time.monotonic() - quiet_since >= SETTLE_QUIET_S:
            return "settled"
        if not changed and time.monotonic() - start >= SETTLE_NO_REACTION_S:
            return "no_reaction"
    return "timeout" if changed else "no_reaction"


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


def _as_element(ref: Any) -> Element:
    """Wrap a raw AX reference, passing through anything already
    element-shaped (real Elements, or the fakes the test suite uses)."""
    return ref if hasattr(ref, "attribute") else Element(ref)


def _modal_open(root: Element) -> str:
    """Return the title of an open modal sheet, or empty if none."""
    windows = root.attribute("AXWindows") or []
    for ref in windows:
        window = _as_element(ref)
        if window.attribute("AXSheets"):
            return window.title or "an untitled window"
    return ""


def _run_step(
    step: Step,
    root: Element,
    args: dict[str, Any],
    pack: Pack,
    tool: ToolSpec,
    low_confidence: bool,
    heal: Optional[HealFn],
    index: int,
) -> StepReport:
    where = f"{pack.app_name}.{tool.name} step {index} ({step.op})"

    if step.op == "wait_for":
        deadline = time.monotonic() + step.timeout_s
        while time.monotonic() < deadline:
            try:
                anchors.resolve(root, step.anchor, low_confidence=low_confidence)
                return StepReport(op=step.op, ok=True)
            except (anchors.AnchorLost, anchors.AnchorAmbiguous):
                time.sleep(SETTLE_POLL_S)
        raise ToolExecutionError(f"{where}: element did not appear within {step.timeout_s}s")

    if step.op == "open_app":
        # Fixed argv, bundle id from the validated pack. Not a shell.
        try:
            subprocess.run(["open", "-b", pack.bundle_id], check=True, timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError(f"{where}: could not open {pack.bundle_id}: {exc}") from exc
        detail = settle(root, step.timeout_s)
        return StepReport(op=step.op, ok=True, detail=detail)

    if step.op == "raise_window":
        windows = root.attribute("AXWindows")
        target = _as_element(windows[0]) if windows else None
        if target is None:
            children = [c for c in root.children() if c.role == "AXWindow"]
            target = children[0] if children else None
        if target is None:
            raise ToolExecutionError(f"{where}: app has no windows to raise")
        target.perform("AXRaise")
        settle(root, step.timeout_s)
        return StepReport(op=step.op, ok=True)

    if step.anchor is None:
        raise ToolExecutionError(f"{where}: step has no anchor; the pack should not have loaded")
    element = _resolve(step.anchor, root, low_confidence, heal, where)
    _check_expect(element, step.expect, where)

    if step.op == "read_value":
        value = "" if element.value is None else str(element.value)
        return StepReport(op=step.op, ok=True, detail=value)

    try:
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
    except ax.AXError as exc:
        # Wrap so the failure names the app, tool, and step, per SECURITY.md.
        raise ToolExecutionError(f"{where}: {exc}") from exc

    detail = settle(root, step.timeout_s)
    return StepReport(op=step.op, ok=True, detail=detail)


def _run_verify(
    check: Verify,
    root: Element,
    args: dict[str, Any],
    tool: ToolSpec,
    low_confidence: bool,
    heal: Optional[HealFn],
    index: int,
) -> None:
    where = f"{tool.name} verify {index} ({check.kind})"
    element = _resolve(check.anchor, root, low_confidence, heal, where)

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
    app: Optional[ax.RunningApp] = None,
    low_confidence: bool = False,
    heal: Optional[HealFn] = None,
) -> ToolResult:
    """Run one tool against a live app root.

    Raises ToolExecutionError with a message naming the app, tool, and step
    on any failure. When the tool declares requires_frontmost and an app
    handle is provided, the app is activated for the duration and the
    previously frontmost app is restored afterward, so a tool call does not
    permanently steal the user's focus.
    """
    tool = pack.tool(tool_name)

    missing = [p for p in tool.params if p not in args]
    if missing:
        raise ToolExecutionError(f"{tool_name}: missing arguments {missing}")

    try:
        for pre in tool.preconditions:
            if pre["kind"] == "app_running":
                if root.role != "AXApplication":
                    raise ToolExecutionError(
                        f"{tool_name}: precondition failed, {pack.app_name} is not responding"
                    )
            elif pre["kind"] == "window_exists":
                has_window = bool(root.attribute("AXWindows")) or any(
                    c.role == "AXWindow" for c in root.children()
                )
                if not has_window:
                    raise ToolExecutionError(
                        f"{tool_name}: precondition failed, {pack.app_name} has no open window"
                    )

        modal_title = _modal_open(root)
        if modal_title:
            raise ToolExecutionError(
                f"{tool_name}: a modal sheet is open on {modal_title!r}. Refusing to act "
                "through it; dismiss it and retry."
            )
    except ax.AXTransientError as exc:
        raise ToolExecutionError(f"{tool_name}: {exc}") from exc

    previous_front = None
    if tool.requires_frontmost and app is not None:
        previous_front = ax.frontmost_app()
        ax.activate(app)

    try:
        result = ToolResult(ok=True, tool=tool_name)
        for index, step in enumerate(tool.steps):
            try:
                report = _run_step(step, root, args, pack, tool, low_confidence, heal, index)
            except ax.AXTransientError as exc:
                raise ToolExecutionError(
                    f"{pack.app_name}.{tool_name} step {index}: {exc}"
                ) from exc
            result.steps.append(report)
            if step.op == "read_value":
                result.values.append(report.detail)

        for index, check in enumerate(tool.verify):
            _run_verify(check, root, args, tool, low_confidence, heal, index)
    finally:
        if previous_front is not None and previous_front.pid != (app.pid if app else -1):
            ax.activate(previous_front)

    result.detail = f"{len(result.steps)} steps, {len(tool.verify)} checks passed"
    return result
