"""Deterministic tool execution. No model calls, ever.

This module and server.py form the deterministic half of the system. The
dependency rule from ARCHITECTURE.md section 6 applies: importing llm.py
here is a design violation, not a style nit. The healer participates only
through the heal callback passed into execute().

Execution order per tool call: preconditions, then per step (resolve the
anchor by scoring, refuse if a sheet or dialog is in the way, check the
expect assertion, perform the op, settle), then verifications against
live reads. AX actions return when dispatched, not when the UI has
finished reacting, so the settle loop after every mutating op is what
makes step N+1 see the world step N created.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import anchors, ax
from .ax import Element
from .packs import Pack, Step, ToolSpec, Verify
from .snapshot import (
    DIALOG_SUBROLES,
    MAX_TREE_DEPTH,
    SECURE_SUBROLE,
    SHEET_ROLE,
    OpenDialog,
    _as_element,
    app_children,
    app_windows,
    is_dialog_window,
    open_dialogs,
    same_element,
    window_sheets,
)

# Settle tuning. After a mutating op the loop waits for the tree to change
# and then go quiet. An op whose effect never appears is reported, not
# silently passed: "no reaction" and "finished reacting" are different
# outcomes and the review caught the first draft conflating them.
SETTLE_POLL_S = 0.05
SETTLE_QUIET_S = 0.15
SETTLE_NO_REACTION_S = 0.6
SETTLE_DEPTH = 6

# Activation lands a moment after it's asked for. With its window on
# another Space, TextEdit reported itself frontmost 0.13 s after the
# request, while a tool's first press can go out about 0.1 s after it,
# and a press that beats activation reaches a background app, which may
# flip the button without acting on it. An app that hasn't come forward
# by the deadline fails the call, so the deadline leaves room for a slow
# Space switch; one that comes forward ends the wait at once.
FRONTMOST_WAIT_S = 2.0

# The ops that change the app, and how a failure message names one that
# was already sent.
SENT_OPS = {"press": "press", "set_value": "write", "pick": "pick"}

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


def _shape_hash(element: Element, depth: int = SETTLE_DEPTH, include_menus: bool = True) -> int:
    """A cheap fingerprint of the tree's current shape and values.

    With include_menus off, menu bar subtrees are left out, the same rule
    anchor resolution follows for anchors that cannot live in one. The
    root's children come from app_children, so a window on another Space
    is watched too.
    """
    parts: list[str] = []
    path_keys: set[int] = set()

    def visit(el: Element, d: int) -> None:
        key = el.ref_key() if hasattr(el, "ref_key") else None
        if key is not None and key in path_keys:
            return
        role = el.role
        if not include_menus and role == "AXMenuBar":
            return
        if key is not None:
            path_keys.add(key)
        parts.append(f"{role}|{el.label}|{el.value}")
        if d < depth:
            for child in app_children(el) if d == 0 else el.children():
                visit(child, d + 1)
        if key is not None:
            path_keys.discard(key)

    visit(element, 0)
    return hash("\n".join(parts))


def settle(
    root: Element,
    deadline_s: float,
    include_menus: bool = True,
    baseline: Optional[int] = None,
) -> str:
    """Wait for the tree to react to an op and then go quiet.

    Returns "settled" when a change was observed and the tree then held
    still, "no_reaction" when nothing changed within the grace window, and
    "timeout" when changes kept coming until the deadline. None of these is
    an error by itself; verify and the next step's expect assertion are the
    real checks. The distinction is reported so a probe that concludes
    "this button does nothing" can be told apart from one that acted too
    early, a confusion the design review flagged.

    The runtime turns include_menus off unless the step acted inside a menu
    bar. Fingerprinting TextEdit's menu bar on every poll took 0.15 to 0.7
    seconds and was most of a write's time.

    baseline is a fingerprint taken before the op, for ops whose own effect
    is the change to wait on. Without one, the first fingerprint is taken
    now, after the op.
    """
    deadline = time.monotonic() + deadline_s
    if baseline is None:
        baseline = _shape_hash(root, include_menus=include_menus)
    changed = False
    last = baseline
    quiet_since = time.monotonic()
    start = quiet_since

    while time.monotonic() < deadline:
        time.sleep(SETTLE_POLL_S)
        current = _shape_hash(root, include_menus=include_menus)
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


def _refuse_secure(element: Element, where: str) -> None:
    """SECURITY.md T4 where it counts. Resolution never binds a secure text
    field, but a field can turn secure after the walk passed it (a sign-in
    form swapping a plain field for a password one), so the subrole is read
    again, live, before anything reads or writes the element."""
    if element.subrole == SECURE_SUBROLE:
        raise ToolExecutionError(
            f"{where}: the element is a secure text field. vent never reads or writes those."
        )


def _resolve(
    step_anchor: anchors.Anchor,
    root: Element,
    low_confidence: bool,
    heal: Optional[HealFn],
    where: str,
) -> tuple[anchors.Candidate, anchors.Anchor]:
    """Find a step's element, healing the anchor once if a healer is set.
    Returns what was found and the anchor that found it."""
    used = step_anchor
    try:
        found = anchors.locate(root, step_anchor, low_confidence=low_confidence)
    except (anchors.AnchorLost, anchors.AnchorAmbiguous) as exc:
        if heal is None:
            raise ToolExecutionError(f"{where}: {exc}") from exc
        healed = heal(step_anchor, root)
        if healed is None:
            raise ToolExecutionError(f"{where}: {exc} (healing declined)") from exc
        # The healed anchor is used for this one call. Persisting it is the
        # healer's job and goes through quarantine, never through here.
        try:
            found = anchors.locate(root, healed, low_confidence=False)
        except (anchors.AnchorLost, anchors.AnchorAmbiguous) as again:
            raise ToolExecutionError(f"{where}: {exc}; the healed anchor did not resolve either: {again}") from again
        used = healed
    _refuse_secure(found.element, where)
    return found, used


def cancel_dialog(dialog: OpenDialog) -> bool:
    """Press the cancel button a sheet or dialog names as its own
    (AXCancelButton, the button Escape presses). Returns False when it
    names none or the press fails. Nothing else is ever pressed: OK, Done,
    Yes and No answer whatever the dialog asks, and a button that only
    reads "Cancel" says so in the app's words, not the dialog's."""
    try:
        ref = dialog.element.attribute("AXCancelButton")
        if ref is None:
            return False
        _as_element(ref).perform("AXPress")
    except ax.AXError:
        return False
    return True


def _recorded_in_dialog(anchor: anchors.Anchor, dialog: Element) -> bool:
    """Whether an anchor was recorded inside this dialog. An anchor that
    pinned a window title must name the dialog's; one that pinned none
    needs the dialog's subrole on its window link. A window that is modal
    only by AXModal has an ordinary subrole, so for it only a pinned title
    counts."""
    chain = anchor.chain
    if len(chain) < 2 or chain[1].role != "AXWindow":
        return False
    if anchor.window_title:
        return anchor.window_title == dialog.label
    return dialog.subrole in DIALOG_SUBROLES and chain[1].subrole == dialog.subrole


def _command_windows(root: Element) -> list[Element]:
    """The windows a step outside any window acts on, such as a menu
    command: the focused and main windows, or every window when the app
    names neither."""
    found: list[Element] = []
    for name in ("AXFocusedWindow", "AXMainWindow"):
        ref = root.attribute(name)
        if ref is None:
            continue
        window = _as_element(ref)
        if not any(same_element(window, other) for other in found):
            found.append(window)
    return found or app_windows(root)


def _dialog_in_the_way(
    root: Element, step_anchor: anchors.Anchor, found: anchors.Candidate, app_name: str
) -> str:
    """Why an open sheet or modal dialog stops this step, or "" if none does.

    AX presses a button or rewrites a field behind a sheet as readily as
    anywhere, while the user may be halfway through answering it. So a
    step acts inside a sheet or dialog only when its anchor was recorded
    there and its element was found there. A modal dialog holds up the
    whole app. A sheet holds up the window it hangs from, and for a step
    outside any window, such as a menu command, the focused and main
    windows, since that is where the command lands.
    """
    top = found.top
    in_window = top is not None and top.role == "AXWindow"
    for window in app_windows(root):
        if not is_dialog_window(window):
            continue
        title = window.label
        dialog = f"{app_name} has a dialog open{f' ({title!r})' if title else ''}"
        if not (in_window and same_element(top, window)):
            return f"{dialog}. Refusing to act behind it; dismiss it and retry."
        if not _recorded_in_dialog(step_anchor, window):
            return (
                f"{dialog}, and this step's element is in it, but nothing in its anchor "
                "says it was recorded there. Refusing to act on it; dismiss the dialog and retry."
            )
    for window in [top] if in_window else _command_windows(root):
        if window.role == SHEET_ROLE:
            return "a sheet is open. Refusing to act behind it; dismiss it and retry."
        if not window_sheets(window):
            continue
        if in_window and anchors.in_sheet(found.chain) and anchors.in_sheet(step_anchor.chain):
            continue
        title = window.label
        return (
            f"a sheet is open on {repr(title) if title else 'an untitled window'}. "
            "Refusing to act behind it; dismiss it and retry."
        )
    return ""


def _check_expect(element: Element, expect: Optional[dict], where: str) -> None:
    if not expect:
        return
    expected_role = expect.get("role")
    if expected_role and element.role != expected_role:
        raise ToolExecutionError(
            f"{where}: resolved element is {element.role}, expected {expected_role}. "
            f"Refusing to act on the wrong element."
        )


WAIT_FOR_POLL_S = 0.3  # wait_for pumps between polls; pumping at the
# settle poll rate would be an IPC storm for no faster an answer.


def _pump_anywhere(root: Element) -> None:
    """Pump the tree when no acted-on element is at hand (see _pump_tree).

    Walks shallowly for the first element advertising scroll-to-visible,
    which in a Chromium host is the web area a few levels down. Bounded so
    a native app with no such element costs a quick no-op, not a crawl."""
    inspected = 0
    for top in app_children(root):
        for _, element in top.walk(max_depth=19):
            inspected += 1
            if inspected > 100:
                return
            try:
                if "AXScrollToVisible" in element.actions():
                    element.perform("AXScrollToVisible")
                    return
            except ax.AXError:
                continue


def _pump_tree(element: Element) -> None:
    """Nudge a Chromium host into republishing its accessibility tree.

    Chromium serializes its tree lazily: after an action mutates the page,
    reads keep returning the pre-action tree until the next action arrives,
    however long that takes. Observed on VS Code, where a view switch stayed
    invisible to walks for 13+ seconds of polling and then appeared the
    instant any action was performed. A scroll-to-visible on the element we
    just acted on is that action: visually a no-op, but it flushes the
    stale tree so settle and verify see the world the step created. The
    nudge only fires on elements that advertise the action, which in
    practice means web-backed ones; eager native trees skip the extra IPC.
    A ref the action itself invalidated just fails quietly; verify still
    re-resolves from scratch.
    """
    try:
        if "AXScrollToVisible" in element.actions():
            element.perform("AXScrollToVisible")
    except ax.AXError:
        pass


# Cap on text a read gathers from inside a container, so one long page
# can't flood the caller's context.
READ_TEXT_LIMIT = 20_000


def _text_inside(element: Element) -> str:
    """The text an element holds in its descendants, one line per text run.

    Web views report an empty AXValue for the whole page and keep the words
    in static text below it: Outlook's reading pane is an AXWebArea whose
    value is "" while the email body sits in AXStaticText nodes. A read of
    such a container returns those runs in tree order. A static text's own
    children repeat its words, so the walk stops at it; secure fields are
    never entered (SECURITY.md T4), and the ancestor-cycle guard matches the
    other walks.
    """
    lines: list[str] = []
    total = 0
    path_keys: set[int] = set()

    def visit(el: Element, depth: int) -> bool:
        nonlocal total
        if el.subrole == SECURE_SUBROLE:
            return True
        if el.role == "AXStaticText":
            text = el.value
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
                total += len(text)
                return total < READ_TEXT_LIMIT
        key = el.ref_key() if hasattr(el, "ref_key") else None
        if (key is not None and key in path_keys) or depth >= MAX_TREE_DEPTH:
            return True
        if key is not None:
            path_keys.add(key)
        try:
            for child in el.children():
                if not visit(child, depth + 1):
                    return False
        finally:
            if key is not None:
                path_keys.discard(key)
        return True

    visit(element, 0)
    return "\n".join(lines)[:READ_TEXT_LIMIT]


def _wait_frontmost(root: Element, app_name: str, tool_name: str) -> None:
    """Wait for an app just activated to report itself frontmost.

    Returns at once for an app already in front, or one that doesn't
    answer AXFrontmost at all. Raises when the app hasn't come forward
    by FRONTMOST_WAIT_S: this used to give up quietly and run the steps
    anyway, so a press could reach the app in the background, flip a
    button without acting on it, and the call reported success. A busy
    app is waited out like a slow one."""
    deadline = time.monotonic() + FRONTMOST_WAIT_S
    busy = False
    while True:
        try:
            frontmost = root.attribute("AXFrontmost")
        except ax.AXTransientError:
            busy = True
        else:
            if frontmost is None or frontmost:
                return
            busy = False
        if time.monotonic() >= deadline:
            break
        time.sleep(SETTLE_POLL_S)
    why = " (it was not answering)" if busy else ""
    raise ToolExecutionError(
        f"{tool_name}: {app_name} did not come to the front within {FRONTMOST_WAIT_S:g}s{why}, "
        f"so no step ran. Bring {app_name} to the front and retry."
    )


def _run_step(
    step: Step,
    root: Element,
    args: dict[str, Any],
    pack: Pack,
    tool: ToolSpec,
    low_confidence: bool,
    heal: Optional[HealFn],
    index: int,
    sent: list[str],
) -> StepReport:
    """Run one step. A press, write or pick that reaches the app is added
    to `sent`, so a later failure in the call can say it went out."""
    where = f"{pack.app_name}.{tool.name} step {index} ({step.op})"

    if step.op == "wait_for":
        deadline = time.monotonic() + step.timeout_s
        failure = f"element did not appear within {step.timeout_s}s"
        while time.monotonic() < deadline:
            try:
                found = anchors.locate(root, step.anchor, low_confidence=low_confidence)
                _refuse_secure(found.element, where)
                blocked = _dialog_in_the_way(root, step.anchor, found, pack.app_name)
                if not blocked:
                    return StepReport(op=step.op, ok=True)
                # Found, but behind a sheet or dialog. One that is closing
                # stays in the tree for a moment, so keep polling.
                failure = f"the element appeared, but {blocked}"
            except (anchors.AnchorLost, anchors.AnchorAmbiguous):
                failure = f"element did not appear within {step.timeout_s}s"
            except ax.AXTransientError as exc:
                # A busy app answers again a moment later, and waiting is
                # what this step is for, so a busy poll is not the end.
                failure = f"{pack.app_name} was still not answering after {step.timeout_s}s ({exc})"
            # Resolution only reads, and reads never flush a Chromium
            # host's lazily-published tree: content that rendered after
            # the last action stays invisible to every poll, however
            # long we wait. Nudge with a benign action between polls.
            try:
                _pump_anywhere(root)
            except ax.AXTransientError:
                pass
            time.sleep(WAIT_FOR_POLL_S)
        raise ToolExecutionError(f"{where}: {failure}")

    if step.op == "open_app":
        # Fixed argv, bundle id from the validated pack. Not a shell.
        try:
            subprocess.run(["open", "-b", pack.bundle_id], check=True, timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError(f"{where}: could not open {pack.bundle_id}: {exc}") from exc
        detail = settle(root, step.timeout_s, include_menus=False)
        return StepReport(op=step.op, ok=True, detail=detail)

    if step.op == "raise_window":
        windows = app_windows(root)
        if not windows:
            raise ToolExecutionError(f"{where}: app has no windows to raise")
        try:
            windows[0].perform("AXRaise")
        except ax.AXError as exc:
            raise ToolExecutionError(f"{where}: {exc}") from exc
        settle(root, step.timeout_s, include_menus=False)
        return StepReport(op=step.op, ok=True)

    if step.anchor is None:
        raise ToolExecutionError(f"{where}: step has no anchor; the pack should not have loaded")
    found, used = _resolve(step.anchor, root, low_confidence, heal, where)
    blocked = _dialog_in_the_way(root, used, found, pack.app_name)
    if blocked:
        raise ToolExecutionError(f"{where}: {blocked}")
    element = found.element
    _check_expect(element, step.expect, where)

    # Semantic drift guard (SECURITY.md T11), acting ops only. When the
    # recorded element is absent, resolution can score a same-role
    # neighbor above threshold; in a collapsed-sidebar VS Code the search
    # box anchor bound an open editor pane, and typing into it would have
    # passed verify because verify re-reads the value just written. An
    # element wearing a label the anchor has never been recorded under is
    # not acted on, ever. Reads stay permitted: reading a drifted element
    # returns data a caller can judge, acting on one edits the wrong thing.
    if step.op in ("press", "set_value", "pick") and anchors.semantic_drift(step.anchor, element):
        raise ToolExecutionError(
            f"{where}: resolved element wears label {element.label[:60]!r}, which this "
            "anchor has never been recorded under. Refusing to act on it; re-record "
            "the anchor or approve the new label through vent verify."
        )

    if step.op == "read_value":
        value = "" if element.value is None else str(element.value)
        if not value:
            value = _text_inside(element)
        return StepReport(op=step.op, ok=True, detail=value)

    include_menus = anchors.in_menu_bar(step.anchor)
    # A write has landed by the time set_value returns, so a fingerprint
    # taken afterwards already holds it, and settle used to wait out its
    # whole no-reaction window (0.6 s on every TextEdit write) for nothing.
    # Fingerprinting first lets the new value count as the change. Presses
    # keep the fingerprint taken afterwards: what they wait for is the
    # app's reaction, not the button's own state.
    before = (
        _shape_hash(root, include_menus=include_menus)
        if step.op == "set_value" and step.settle
        else None
    )

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

    if step.op in SENT_OPS:
        sent.append(f"the {SENT_OPS[step.op]} in step {index}")
        _pump_tree(element)

    detail = (
        settle(root, step.timeout_s, include_menus=include_menus, baseline=before)
        if step.settle
        else "settle skipped"
    )
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
    # A verify only reads, to check what the steps did, so the dialog
    # guard doesn't apply: a tool whose last step opens a sheet can still
    # check the window behind it.
    element = _resolve(check.anchor, root, low_confidence, heal, where)[0].element

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
                if not app_windows(root):
                    raise ToolExecutionError(
                        f"{tool_name}: precondition failed, {pack.app_name} has no open window"
                    )
        # Open sheets and dialogs are judged per step (_dialog_in_the_way),
        # because a tool may open one and then act inside it. The ones open
        # now are the user's, and a failure never cancels those.
        open_before = {dialog.element.ref_key() for dialog in open_dialogs(root)}
    except ax.AXTransientError as exc:
        raise ToolExecutionError(f"{tool_name}: {exc}") from exc

    sent: list[str] = []
    previous_front = None
    try:
        if tool.requires_frontmost and app is not None:
            previous_front = ax.frontmost_app()
            ax.activate(app)
            _wait_frontmost(root, pack.app_name, tool_name)

        result = ToolResult(ok=True, tool=tool_name)
        for index, step in enumerate(tool.steps):
            try:
                report = _run_step(step, root, args, pack, tool, low_confidence, heal, index, sent)
            except ax.AXTransientError as exc:
                raise ToolExecutionError(
                    f"{pack.app_name}.{tool_name} step {index}: {exc}"
                ) from exc
            result.steps.append(report)
            if step.op == "read_value":
                result.values.append(report.detail)

        for index, check in enumerate(tool.verify):
            try:
                _run_verify(check, root, args, tool, low_confidence, heal, index)
            except ax.AXTransientError as exc:
                raise ToolExecutionError(f"{tool_name} verify {index}: {exc}") from exc
    except ToolExecutionError as exc:
        # The app is still in front here, so a cancel press reaches it.
        raise ToolExecutionError(
            f"{exc}{_sent_note(sent, pack.app_name)}{_cancel_left_open(root, open_before)}"
        ) from exc
    finally:
        if previous_front is not None and previous_front.pid != (app.pid if app else -1):
            ax.activate(previous_front)

    checks = len(tool.verify)
    result.detail = _count(len(result.steps), "step") + (f", {_count(checks, 'check')} passed" if checks else "")
    return result


def _sent_note(sent: list[str], app_name: str) -> str:
    """What a failed call had already done to the app, so a caller doesn't
    take the failure to mean nothing happened and press again."""
    if not sent:
        return ""
    done = sent[0] if len(sent) == 1 else ", ".join(sent[:-1]) + " and " + sent[-1]
    was = "was" if len(sent) == 1 else "were"
    return f" {done[0].upper()}{done[1:]} {was} already sent, so {app_name} may have changed."


def _cancel_left_open(root: Element, open_before: set) -> str:
    """Cancel each sheet or dialog that opened during a call that then
    failed, and say what was done.

    A failed Outlook filing used to leave its Choose Folder sheet open, and
    the next call met it. Only the dialog's own cancel button is pressed
    (cancel_dialog), and only on dialogs that weren't open when the call
    started."""
    try:
        opened = [dialog for dialog in open_dialogs(root) if dialog.element.ref_key() not in open_before]
    except ax.AXTransientError:
        return " The app stopped answering, so vent could not check for a sheet or dialog the call left open."
    notes = []
    for dialog in opened:
        try:
            what = dialog.describe()
        except ax.AXTransientError:
            what = "a sheet or dialog"
        if cancel_dialog(dialog):
            notes.append(f" vent cancelled {what}, which the call had opened.")
        else:
            notes.append(
                f" {what[0].upper()}{what[1:]} opened during the call and is still open, and vent "
                "could not cancel it. Close it by hand."
            )
    if opened:
        try:
            settle(root, 1.0, include_menus=False)
        except ax.AXTransientError:
            pass
    return "".join(notes)


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
