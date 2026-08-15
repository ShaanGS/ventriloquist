"""Anchor durability harness.

The project's central hypothesis is that anchors recorded once keep
resolving after UI churn. This module measures that instead of assuming
it: record anchors for an app's surfaced elements, perturb the app in
ways real life perturbs it, and re-resolve every anchor after each
perturbation. The output is a survival table, and the numbers go in the
README whatever they turn out to be.

A resolution only counts as survival if the element found is the element
recorded. Resolving to a lookalike is counted as "wrong", and wrong is
worse than lost: lost fails loudly, wrong acts on the wrong thing. The
resolver is designed to make wrong nearly impossible; this harness is the
proof or the refutation.

Perturbations:

- baseline: re-resolve immediately, nothing changed. Anything below 100
  here is a resolver bug, not churn.
- zoom: toggle the window's zoom (a real resize and relayout), resolve,
  toggle back.
- restart (opt-in): quit and relaunch the app. Every AXUIElement ref dies
  and the whole tree is rebuilt, the strongest churn short of an update.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from . import anchors, ax
from .snapshot import Node, snapshot


@dataclass
class AnchorRecord:
    anchor: anchors.Anchor
    role: str
    identifier: str
    label: str
    subrole: str


@dataclass
class RoundResult:
    name: str
    resolved: int = 0
    lost: int = 0
    ambiguous: int = 0
    wrong: int = 0
    unverifiable: int = 0  # resolved, but identity cannot be judged; not survival
    note: str = ""

    @property
    def total(self) -> int:
        return self.resolved + self.lost + self.ambiguous + self.wrong + self.unverifiable

    @property
    def survival(self) -> float:
        return (self.resolved / self.total * 100) if self.total else 0.0


@dataclass
class HarnessReport:
    app_name: str
    anchor_count: int
    rounds: list[RoundResult] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"{self.app_name}: {self.anchor_count} anchors"]
        for r in self.rounds:
            line = (
                f"  {r.name:10} {r.survival:5.1f}% survived "
                f"({r.resolved} ok, {r.lost} lost, {r.ambiguous} ambiguous, "
                f"{r.wrong} WRONG, {r.unverifiable} unverifiable)"
            )
            if r.note:
                line += f"  [{r.note}]"
            lines.append(line)
        return "\n".join(lines)


def _pick_nodes(nodes: list[Node], cap: int) -> list[Node]:
    """Prefer elements a pack would actually anchor: actionable or
    identified ones first, then the rest, up to the cap.

    Scroll-to-visible does not count as actionable: Chromium advertises it
    on every node, which otherwise floods the cap with unlabeled twin
    groups no pack would ever anchor."""

    def actionable(n: Node) -> bool:
        return any(a != "AXScrollToVisible" for a in n.actions)

    prioritized = sorted(
        nodes,
        key=lambda n: (
            (actionable(n) or bool(n.identifier)) and bool(n.label or n.identifier),
            bool(n.identifier),
            actionable(n),
        ),
        reverse=True,
    )
    return prioritized[:cap]


def _record(root: ax.Element, cap: int) -> list[AnchorRecord]:
    snap = snapshot(root)
    records = []
    for node in _pick_nodes(snap.nodes, cap):
        records.append(
            AnchorRecord(
                anchor=anchors.build(node),
                role=node.role,
                identifier=node.identifier,
                label=node.label,
                subrole=node.subrole,
            )
        )
    return records


def _identity_verdict(record: AnchorRecord, element: ax.Element) -> str:
    """Judge whether a resolved element is the recorded one.

    Returns "same", "wrong", or "unverifiable". Refs are not comparable
    across restarts, so identity is judged on stable facets. A record with
    no identifier, no label, and no subrole gives nothing to judge by;
    counting those as survivors would overcount, so they are their own
    bucket and excluded from the survival numerator.
    """
    if not (record.identifier or record.label or record.subrole):
        return "unverifiable"
    if not element.role:
        # Every attribute of a dead ref reads empty. The element existed
        # when the resolver walked (it scored a role match) and vanished
        # before this read; Chromium recreates nodes while an app is still
        # settling after launch. That is churn, not a lookalike binding.
        return "stale"
    if element.role != record.role:
        return "wrong"
    identifier = str(element.attribute("AXIdentifier") or "")
    if record.identifier and identifier and identifier != record.identifier:
        return "wrong"
    if record.subrole and element.subrole and element.subrole != record.subrole:
        return "wrong"
    if record.label and element.label and element.label != record.label:
        # Labels legitimately change (toggles, counters); only treat a
        # mismatch as wrong when the identifier gives no tiebreak.
        if not (record.identifier and identifier == record.identifier):
            return "wrong"
    return "same"


def _resolve_round(name: str, root: ax.Element, records: list[AnchorRecord]) -> RoundResult:
    # A locked screen makes every app serve an empty tree, which would
    # score here as total anchor loss. Say what actually happened instead.
    if ax.session_locked():
        return RoundResult(name=name, note="screen locked; round skipped")
    result = RoundResult(name=name)
    for record in records:
        try:
            element = anchors.resolve(root, record.anchor)
        except anchors.AnchorAmbiguous:
            result.ambiguous += 1
            continue
        except anchors.AnchorLost:
            result.lost += 1
            continue
        except ax.AXTransientError:
            result.lost += 1
            continue
        verdict = _identity_verdict(record, element)
        if verdict == "same":
            result.resolved += 1
        elif verdict == "unverifiable":
            result.unverifiable += 1
        elif verdict == "stale":
            result.lost += 1
        else:
            result.wrong += 1
    return result


def _resize_window(root: ax.Element):
    """Shrink the first window and return a restore callback, or None.

    A real resize relayouts toolbars and collapses adaptive UI, which is
    exactly the churn anchors must survive. The window's own AXZoomWindow
    action advertises itself and then refuses to perform on modern macOS,
    a fact this harness discovered, so the size attribute is set directly.
    """
    from ApplicationServices import AXValueCreate, AXValueGetValue, kAXValueCGSizeType

    def decode(ref):
        ok, size = AXValueGetValue(ref, kAXValueCGSizeType, None)
        return (size.width, size.height) if ok else None

    windows = root.attribute("AXWindows") or []
    if not windows:
        return None
    window = ax.Element(windows[0])
    original = window.attribute("AXSize")
    dims = decode(original) if original is not None else None
    if dims is None:
        return None

    # Shrink relative to the current size; a fixed target can be a no-op
    # or even a grow, and the window server clamps to the app's minimum.
    target = (max(400.0, dims[0] * 0.6), max(300.0, dims[1] * 0.6))
    try:
        window.set_attribute("AXSize", AXValueCreate(kAXValueCGSizeType, target))
    except ax.AXError:
        return None

    time.sleep(0.3)
    after = decode(window.attribute("AXSize") or original)
    if after is None or (abs(after[0] - dims[0]) < 20 and abs(after[1] - dims[1]) < 20):
        # The window server refused the resize. A round measured against
        # an unchanged UI would publish a lie; skip it instead.
        try:
            window.set_attribute("AXSize", original)
        except ax.AXError:
            pass
        return None

    def restore():
        try:
            window.set_attribute("AXSize", original)
        except ax.AXError:
            pass

    return restore


def _wait_for_tree(root: ax.Element, min_nodes: int = 10, timeout_s: float = 20.0) -> bool:
    """Wait until the app serves a tree with real content in it.

    A window existing is not enough for Chromium hosts: the web tree is
    built lazily after window creation, so resolving against a
    just-restarted Electron app measures startup timing, not anchors."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if len(snapshot(root).nodes) >= min_nodes:
                return True
        except ax.AXTransientError:
            pass
        time.sleep(1.0)
    return False


def _wait_for_window(root: ax.Element, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if root.attribute("AXWindows"):
                return True
        except ax.AXTransientError:
            pass
        time.sleep(0.3)
    return False


def run(app_query: str, cap: int = 40, restart: bool = False, reopen: str | None = None) -> HarnessReport:
    app = ax.find_app(app_query)
    root = ax.app_element(app)
    enable_web_accessibility(root)
    # Recording against a still-loading app yields a handful of window
    # chrome anchors and nothing else; wait for real content first.
    _wait_for_tree(root)

    records = _record(root, cap)
    report = HarnessReport(app_name=app.name, anchor_count=len(records))
    if not records:
        return report

    report.rounds.append(_resolve_round("baseline", root, records))

    restore = _resize_window(root)
    if restore is not None:
        time.sleep(0.8)
        report.rounds.append(_resolve_round("resized", root, records))
        restore()
        time.sleep(0.5)

    if restart:
        if not app.bundle_id:
            raise ax.AXError(
                f"{app.name} has no bundle id; refusing to quit an app the "
                "harness cannot relaunch."
            )
        # Terminate through the NSRunningApplication handle. Building
        # AppleScript source from an app-controlled name is an injection
        # vector (SECURITY.md T1 applies to us too, not just to packs).
        ax.terminate(app)
        # Termination is asynchronous and heavyweight apps quit slowly.
        # Relaunching before the old process exits re-binds the dying pid
        # and every later read comes back empty, so wait it out first.
        # Electron apps have been seen ignoring the first polite quit and
        # honoring a repeat, so keep asking; never escalate to force-kill.
        deadline = time.monotonic() + 60.0
        last_ask = time.monotonic()
        while time.monotonic() < deadline:
            try:
                if ax.find_app_by_bundle(app.bundle_id).pid != app.pid:
                    break
            except ax.AXError:
                break
            if time.monotonic() - last_ask >= 5.0:
                ax.terminate(app)
                last_ask = time.monotonic()
            time.sleep(0.5)
        launch = ["open", "-b", app.bundle_id]
        if reopen:
            launch.append(reopen)
        try:
            subprocess.run(launch, check=True, timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ax.AXError(f"could not relaunch {app.bundle_id}: {exc}") from exc
        fresh_app = None
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                candidate = ax.find_app_by_bundle(app.bundle_id)
                if candidate.pid != app.pid:
                    fresh_app = candidate
                    break
            except ax.AXError:
                pass
            time.sleep(0.5)
        if fresh_app is None:
            report.rounds.append(
                RoundResult(name="restart", note="app never relaunched; round skipped")
            )
            return report
        fresh_root = ax.app_element(fresh_app)
        enable_web_accessibility(fresh_root)
        # Background-launched apps serve degraded trees until genuinely
        # foregrounded; activation also lets Chromium hosts build web AX.
        ax.activate(fresh_app)
        if _wait_for_window(fresh_root) and _wait_for_tree(fresh_root):
            time.sleep(1.0)
            report.rounds.append(_resolve_round("restart", fresh_root, records))
        else:
            # Without a window the losses would measure session restore,
            # not anchor durability. Say so instead of publishing a lie.
            report.rounds.append(
                RoundResult(name="restart", note="app restored no windows; round skipped")
            )

    return report


def enable_web_accessibility(root: ax.Element) -> None:
    """Ask Chromium and Electron hosts to build their renderer trees.

    These apps ship with web-content accessibility off and only build the
    tree when an assistive client signals demand (ARCHITECTURE.md section
    4). Setting these attributes is harmless on native apps, which simply
    do not have them. Side effect worth knowing: the app spends more CPU
    on accessibility until it relaunches.
    """
    for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        try:
            root.set_attribute(attr, True)
        except ax.AXError:
            pass


@dataclass
class ToolRound:
    phase: str
    passed: int = 0
    failed: int = 0
    failures: list = field(default_factory=list)  # (tool, message)


@dataclass
class ToolReport:
    """End-to-end tool success under perturbation.

    The anchor numbers above measure parts; this measures the thing a
    caller experiences. A tool with three steps and a verify touches
    several anchors, waits, settles, and re-reads, and any of it can
    break. Per-anchor survival flatters that compound risk."""

    app_name: str = ""
    rounds: list = field(default_factory=list)

    def render(self) -> str:
        lines = [f"{self.app_name}: tool-level success"]
        for r in self.rounds:
            total = r.passed + r.failed
            pct = (r.passed / total * 100) if total else 0.0
            lines.append(f"  {r.phase:10} {r.passed}/{total} calls passed ({pct:.0f}%)")
            for tool, message in r.failures:
                lines.append(f"    ✗ {tool}: {message}")
        return "\n".join(lines)


def _sample_args(tool) -> dict:
    """Placeholder arguments for measurement runs. Strings are visibly
    probe-flavored so anything left behind in a text field explains itself."""
    return {name: f"vent harness probe ({name})" for name in tool.params}


def run_tools(pack, app_query: str, cycles: int = 3) -> ToolReport:
    """Run every non-high tool in the pack end to end, repeatedly, with a
    window resize between cycles. Reports per-phase pass counts and every
    failure message. Mutating tools do run for real; point this at scratch
    documents, not work."""
    from . import runtime

    app = ax.find_app(app_query)
    root = ax.app_element(app)
    enable_web_accessibility(root)
    _wait_for_tree(root)

    report = ToolReport(app_name=app.name)
    tools = [t for t in pack.tools if t.risk != "high"]

    for cycle in range(cycles):
        restore = None
        if cycle % 2 == 1:
            restore = _resize_window(root)
            time.sleep(0.8)
        phase = f"cycle {cycle + 1}" + (" (resized)" if restore else "")
        rnd = ToolRound(phase=phase)
        for tool in tools:
            if ax.session_locked():
                rnd.failures.append((tool.name, "screen locked; call not attempted"))
                rnd.failed += 1
                continue
            try:
                runtime.execute(pack, tool.name, _sample_args(tool), root, app=app)
                rnd.passed += 1
            except runtime.ToolExecutionError as exc:
                rnd.failed += 1
                rnd.failures.append((tool.name, str(exc)[:120]))
        report.rounds.append(rnd)
        if restore is not None:
            restore()
            time.sleep(0.5)
    return report
