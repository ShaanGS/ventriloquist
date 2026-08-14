"""Low-level macOS Accessibility (AX) API wrapper.

Everything Ventriloquist knows about a running app flows through this module.
It wraps the C-style AXUIElement API from ApplicationServices into a small
`Element` class that supports attribute reads, action invocation, and stable
path addressing — the foundation for deterministic replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional

from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    kAXErrorSuccess,
)
from Cocoa import NSRunningApplication, NSWorkspace


class AXError(RuntimeError):
    """Raised when an accessibility call fails in a way we can't ignore."""


class NotTrustedError(AXError):
    """The current process lacks Accessibility permission."""


def is_trusted() -> bool:
    """Whether this process may use the Accessibility API."""
    return bool(AXIsProcessTrusted())


def require_trusted() -> None:
    if not is_trusted():
        raise NotTrustedError(
            "This process is not trusted for Accessibility. Grant access in "
            "System Settings → Privacy & Security → Accessibility (add your "
            "terminal app), then rerun."
        )


@dataclass(frozen=True)
class RunningApp:
    name: str
    bundle_id: Optional[str]
    pid: int


def running_apps() -> list[RunningApp]:
    """List regular (Dock-visible) running applications."""
    apps = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        # activationPolicy 0 == NSApplicationActivationPolicyRegular
        if app.activationPolicy() != 0:
            continue
        apps.append(
            RunningApp(
                name=str(app.localizedName() or ""),
                bundle_id=str(app.bundleIdentifier()) if app.bundleIdentifier() else None,
                pid=int(app.processIdentifier()),
            )
        )
    return apps


def find_app(name_or_bundle: str) -> RunningApp:
    """Find a running app by (case-insensitive) name or bundle id."""
    needle = name_or_bundle.strip().lower()
    candidates = running_apps()
    for app in candidates:
        if app.bundle_id and app.bundle_id.lower() == needle:
            return app
    for app in candidates:
        if app.name.lower() == needle:
            return app
    for app in candidates:
        if needle in app.name.lower():
            return app
    raise AXError(
        f"No running application matches {name_or_bundle!r}. "
        f"Running apps: {', '.join(sorted(a.name for a in candidates))}"
    )


def _ax_get(element: Any, attribute: str) -> Any:
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    if err != kAXErrorSuccess:
        return None
    return value


class Element:
    """A node in an app's accessibility tree."""

    def __init__(self, ref: Any):
        self._ref = ref

    # -- attribute access ---------------------------------------------------

    def attribute(self, name: str) -> Any:
        return _ax_get(self._ref, name)

    def attribute_names(self) -> list[str]:
        err, names = AXUIElementCopyAttributeNames(self._ref, None)
        if err != kAXErrorSuccess or names is None:
            return []
        return [str(n) for n in names]

    @property
    def role(self) -> str:
        return str(self.attribute("AXRole") or "")

    @property
    def subrole(self) -> str:
        return str(self.attribute("AXSubrole") or "")

    @property
    def title(self) -> str:
        return str(self.attribute("AXTitle") or "")

    @property
    def description(self) -> str:
        return str(self.attribute("AXDescription") or "")

    @property
    def value(self) -> Any:
        return self.attribute("AXValue")

    @property
    def enabled(self) -> bool:
        val = self.attribute("AXEnabled")
        return bool(val) if val is not None else True

    @property
    def label(self) -> str:
        """Best human-readable name for this element."""
        return self.title or self.description or str(self.attribute("AXHelp") or "")

    # -- tree navigation ----------------------------------------------------

    def children(self) -> list["Element"]:
        raw = self.attribute("AXChildren")
        if not raw:
            return []
        return [Element(child) for child in raw]

    def walk(self, max_depth: int = 25, _depth: int = 0) -> Iterator[tuple[int, "Element"]]:
        """Depth-first traversal yielding (depth, element)."""
        yield _depth, self
        if _depth >= max_depth:
            return
        for child in self.children():
            yield from child.walk(max_depth, _depth + 1)

    # -- actions ------------------------------------------------------------

    def actions(self) -> list[str]:
        err, names = AXUIElementCopyActionNames(self._ref, None)
        if err != kAXErrorSuccess or names is None:
            return []
        return [str(n) for n in names]

    def perform(self, action: str = "AXPress") -> None:
        err = AXUIElementPerformAction(self._ref, action)
        if err != kAXErrorSuccess:
            raise AXError(f"Action {action!r} failed on {self!r} (AXError {err})")

    def set_value(self, value: Any) -> None:
        err = AXUIElementSetAttributeValue(self._ref, "AXValue", value)
        if err != kAXErrorSuccess:
            raise AXError(f"Setting value failed on {self!r} (AXError {err})")

    def __repr__(self) -> str:
        label = self.label
        return f"<Element {self.role}{f' {label!r}' if label else ''}>"


def app_element(app: RunningApp) -> Element:
    """Root accessibility element for a running application."""
    require_trusted()
    return Element(AXUIElementCreateApplication(app.pid))
