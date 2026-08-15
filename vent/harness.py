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


@dataclass
class RoundResult:
    name: str
    resolved: int = 0
    lost: int = 0
    ambiguous: int = 0
    wrong: int = 0
    note: str = ""

    @property
    def total(self) -> int:
        return self.resolved + self.lost + self.ambiguous + self.wrong

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
                f"({r.resolved} ok, {r.lost} lost, {r.ambiguous} ambiguous, {r.wrong} WRONG)"
            )
            if r.note:
                line += f"  [{r.note}]"
            lines.append(line)
        return "\n".join(lines)


def _pick_nodes(nodes: list[Node], cap: int) -> list[Node]:
    """Prefer elements a pack would actually anchor: actionable or
    identified ones first, then the rest, up to the cap."""
    prioritized = sorted(
        nodes,
        key=lambda n: (bool(n.actions) or bool(n.identifier), bool(n.identifier)),
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
            )
        )
    return records


def _same_element(record: AnchorRecord, element: ax.Element) -> bool:
    """Whether a resolved element is plausibly the recorded one. Refs are
    not comparable across restarts, so identity is judged on the stable
    facets the recording captured."""
    if element.role != record.role:
        return False
    identifier = str(element.attribute("AXIdentifier") or "")
    if record.identifier and identifier and identifier != record.identifier:
        return False
    if record.label and element.label and element.label != record.label:
        # Labels legitimately change (toggles, counters); only treat a
        # mismatch as wrong when the identifier gives no tiebreak.
        if not (record.identifier and identifier == record.identifier):
            return False
    return True


def _resolve_round(name: str, root: ax.Element, records: list[AnchorRecord]) -> RoundResult:
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
        if _same_element(record, element):
            result.resolved += 1
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
    from ApplicationServices import AXValueCreate, kAXValueCGSizeType

    windows = root.attribute("AXWindows") or []
    if not windows:
        return None
    window = ax.Element(windows[0])
    original = window.attribute("AXSize")
    if original is None:
        return None
    try:
        window.set_attribute("AXSize", AXValueCreate(kAXValueCGSizeType, (620.0, 480.0)))
    except ax.AXError:
        return None

    def restore():
        try:
            window.set_attribute("AXSize", original)
        except ax.AXError:
            pass

    return restore


def _wait_for_window(root: ax.Element, timeout_s: float = 10.0) -> bool:
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
        subprocess.run(["osascript", "-e", f'tell application "{app.name}" to quit'], timeout=15)
        time.sleep(2.0)
        launch = ["open", "-b", app.bundle_id or ""]
        if reopen:
            launch.append(reopen)
        subprocess.run(launch, check=True, timeout=15)
        time.sleep(3.0)
        fresh_app = ax.find_app_by_bundle(app.bundle_id or "")
        fresh_root = ax.app_element(fresh_app)
        enable_web_accessibility(fresh_root)
        if _wait_for_window(fresh_root):
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
