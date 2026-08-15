"""Low-level macOS Accessibility (AX) API wrapper.

Everything Ventriloquist knows about a running app flows through this module.
It wraps the C-style AXUIElement API from ApplicationServices into a small
`Element` class that supports attribute reads, action invocation, and app
lookup, the foundation the snapshot and anchor layers build on.

Error semantics matter here and were a review finding: the AX API returns
distinct error codes for "this attribute does not exist" versus "the app is
not responding right now", and collapsing them both to None makes a busy app
look like an empty app. Attribute-level absences return None; process-level
failures raise AXTransientError so callers can abort instead of acting on a
tree that is not really empty.
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
    AXUIElementSetMessagingTimeout,
    kAXErrorSuccess,
)
from Cocoa import NSBundle, NSRunningApplication, NSWorkspace

# AX error codes that matter to us. Values from AXError.h.
AX_ERROR_API_DISABLED = -25211
AX_ERROR_CANNOT_COMPLETE = -25204
AX_ERROR_ATTRIBUTE_UNSUPPORTED = -25205
AX_ERROR_NO_VALUE = -25212
AX_ERROR_INVALID_ELEMENT = -25202
AX_ERROR_NOT_IMPLEMENTED = -25208

# A hung app blocks each AX call for 6 seconds by default. One second is
# plenty for a healthy app and turns a beachballing one into a fast,
# explicit failure instead of a slow mysterious one.
MESSAGING_TIMEOUT_S = 1.0


class AXError(RuntimeError):
    """Raised when an accessibility call fails in a way we can't ignore."""


class AXTransientError(AXError):
    """The target app is busy, hung, or gone. The tree is not readable right
    now; nothing should be concluded from partial reads."""


class NotTrustedError(AXError):
    """The current process lacks Accessibility permission."""


def is_trusted() -> bool:
    """Whether this process may use the Accessibility API."""
    return bool(AXIsProcessTrusted())


def require_trusted() -> None:
    if not is_trusted():
        raise NotTrustedError(
            "This process is not trusted for Accessibility. Grant access in "
            "System Settings, Privacy & Security, Accessibility (add your "
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
    """Find a running app by (case-insensitive) name or bundle id.

    Matching is exact-first, substring-last. Callers that know the bundle id
    should use find_app_by_bundle instead; substring matching is a
    convenience for humans typing CLI commands, not for packs.
    """
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


def find_app_by_bundle(bundle_id: str) -> RunningApp:
    """Find a running app by exact bundle id. Packs resolve apps this way:
    a pack for com.apple.Notes must never attach to 'Notes Plus' just
    because the names look alike."""
    needle = bundle_id.strip().lower()
    for app in running_apps():
        if app.bundle_id and app.bundle_id.lower() == needle:
            return app
    raise AXError(f"No running application with bundle id {bundle_id!r}. Is the app open?")


def app_version(app: RunningApp) -> str:
    """The app's CFBundleShortVersionString, or empty if unreadable.
    Packs record this at compile time and compare it at load time to
    detect staleness."""
    ns_app = NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
    if ns_app is None or ns_app.bundleURL() is None:
        return ""
    bundle = NSBundle.bundleWithURL_(ns_app.bundleURL())
    if bundle is None:
        return ""
    version = bundle.objectForInfoDictionaryKey_("CFBundleShortVersionString")
    return str(version) if version else ""


def frontmost_app() -> Optional[RunningApp]:
    ns_app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if ns_app is None:
        return None
    return RunningApp(
        name=str(ns_app.localizedName() or ""),
        bundle_id=str(ns_app.bundleIdentifier()) if ns_app.bundleIdentifier() else None,
        pid=int(ns_app.processIdentifier()),
    )


def terminate(app: RunningApp) -> bool:
    """Ask the app to quit (regular Quit, not force kill). Returns False
    if the process is already gone."""
    ns_app = NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
    if ns_app is None:
        return False
    return bool(ns_app.terminate())


def activate(app: RunningApp) -> bool:
    """Bring the app frontmost. Returns False if the process is gone."""
    ns_app = NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
    if ns_app is None:
        return False
    return bool(ns_app.activateWithOptions_(0))


def _ax_get(element: Any, attribute: str) -> Any:
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    if err == kAXErrorSuccess:
        return value
    if err in (AX_ERROR_CANNOT_COMPLETE, AX_ERROR_API_DISABLED):
        raise AXTransientError(
            f"App is not answering accessibility queries (AXError {err}). "
            "It may be busy or hung; retry when it responds."
        )
    # Attribute unsupported, no value, element destroyed mid-walk, or not
    # implemented: all mean "nothing here", which None expresses honestly.
    return None


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
        self.set_attribute("AXValue", value)

    def set_attribute(self, name: str, value: Any) -> None:
        err = AXUIElementSetAttributeValue(self._ref, name, value)
        if err != kAXErrorSuccess:
            raise AXError(f"Setting {name} failed on {self!r} (AXError {err})")

    def ref_key(self):
        """A hashable identity for cycle detection during walks. CF refs
        hash and compare by CFHash/CFEqual through pyobjc; fall back to
        None (no cycle protection) if that ever fails."""
        try:
            return hash(self._ref)
        except TypeError:
            return None

    def __repr__(self) -> str:
        label = self.label
        return f"<Element {self.role}{f' {label!r}' if label else ''}>"


def app_element(app: RunningApp) -> Element:
    """Root accessibility element for a running application."""
    require_trusted()
    ref = AXUIElementCreateApplication(app.pid)
    AXUIElementSetMessagingTimeout(ref, MESSAGING_TIMEOUT_S)
    return Element(ref)
