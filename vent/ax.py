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

import os
import subprocess
from dataclasses import dataclass
from typing import Any, Iterator, Optional

# pyobjc is macOS-only. Importing it lazily keeps the pure-logic modules
# (snapshot, anchors, packs, runtime, policy, compiler, heal) importable on
# any platform, so the offline test suite runs in CI on Linux. Every symbol
# here is used only inside functions that make live accessibility calls; a
# non-macOS import leaves them None and any real call raises a clear error
# rather than an ImportError at module load.
try:
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

    _PYOBJC_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off macOS
    _PYOBJC_AVAILABLE = False
    kAXErrorSuccess = 0


def _require_pyobjc() -> None:
    if not _PYOBJC_AVAILABLE:
        raise AXError(
            "The macOS accessibility API is unavailable on this platform. "
            "Live app commands need macOS with pyobjc; the offline logic and "
            "tests do not."
        )

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
    _require_pyobjc()
    return bool(AXIsProcessTrusted())


def session_locked() -> bool:
    """Whether the login session's screen is locked.

    A locked screen makes every app serve an empty tree while permission
    checks still pass, a combination that reads as "this app is broken"
    until you know to look. Quartz is queried lazily and any failure,
    including running off macOS, reports unlocked, so this stays a
    diagnostic aid, never a gate."""
    if not _PYOBJC_AVAILABLE:
        return False
    try:
        from Quartz import CGSessionCopyCurrentDictionary

        session = CGSessionCopyCurrentDictionary()
        return bool(session and session.get("CGSSessionScreenIsLocked"))
    except Exception:  # pragma: no cover - Quartz querying is best effort
        return False


# macOS grants Accessibility to the app that started a process, not to the
# process itself, so the app to name depends on who launched vent.
TRUST_HINT = (
    "Grant access in System Settings, Privacy & Security, Accessibility to "
    "the app that started vent: your terminal, or the MCP client (such as "
    "Cursor) that launched vent serve. Then quit and reopen that app."
)

# Claude Desktop starts its MCP servers, and its Claude Code sessions,
# through this helper, which makes each one responsible for itself. macOS
# then checks the program the helper ran instead of Claude, so Claude's own
# grant never reaches vent.
_CLAUDE_DISCLAIMER = "/Claude.app/Contents/Helpers/disclaimer"


def executable_path(pid: int) -> str:
    """The file a process is running, symlinks resolved, or "" when it
    can't be read. This is the file macOS checks for a permission. ps
    sometimes reports a bare name instead, and a framework Python's
    sys.executable is a stub that hands off to a binary inside Python.app."""
    import ctypes

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return ""
    buffer = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
    if libproc.proc_pidpath(pid, buffer, ctypes.sizeof(buffer)) <= 0:
        return ""
    return buffer.value.decode(errors="replace")


def _process_chain(limit: int = 8) -> list[str]:
    """Executable paths of this process and its ancestors, nearest first."""
    try:
        listing = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    parents = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = int(fields[1])
    chain, pid = [], os.getpid()
    while pid > 1 and len(chain) < limit:
        chain.append(executable_path(pid))
        pid = parents.get(pid, 0)
    return chain


def claude_desktop_permission_owner(chain: Optional[list[str]] = None) -> Optional[str]:
    """The program macOS checks for Accessibility when vent runs under
    Claude Desktop's disclaimer helper, or None when it doesn't. It is
    whatever the helper ran: vent's Python for the entry `vent mcp-config`
    prints, uv when the config runs vent through uvx, or Claude Code when
    vent runs inside one of its sessions."""
    if chain is None:
        chain = _process_chain()
    for index, path in enumerate(chain):
        if path.endswith(_CLAUDE_DISCLAIMER):
            return (chain[index - 1] or None) if index > 0 else None
    return None


def trust_hint() -> str:
    """How to grant Accessibility, given the way this process was started."""
    owner = claude_desktop_permission_owner()
    if owner is None:
        return TRUST_HINT
    hint = (
        f"Under Claude Desktop, macOS checks {owner} for Accessibility, not "
        "Claude, because Claude starts that program as its own process for "
        "permissions. Add that file in System Settings, Privacy & Security, "
        "Accessibility (drag it in from Finder if the + picker greys it out), "
        "then quit and reopen Claude."
    )
    if os.path.basename(owner) in ("uv", "uvx"):
        hint += (
            " That file is uv itself, because the config runs vent through uvx. "
            "For a grant that doesn't cover everything uv runs, install vent "
            "with `uv tool install ventriloquist-mcp` and use the entry "
            "`vent mcp-config` prints."
        )
    return hint


def require_trusted() -> None:
    if not is_trusted():
        raise NotTrustedError(f"This process is not trusted for Accessibility. {trust_hint()}")


@dataclass(frozen=True)
class RunningApp:
    name: str
    bundle_id: Optional[str]
    pid: int


def running_apps() -> list[RunningApp]:
    """List regular (Dock-visible) running applications."""
    _require_pyobjc()
    # NSWorkspace's running-application list is maintained by KVO on the
    # main run loop. A headless process that never pumps the loop sees the
    # list frozen at first access: quit apps stay listed, relaunched ones
    # never appear. Found when the harness polled for a restarted VS Code
    # for a full minute while ps showed it had already come back. One brief
    # pump keeps every read current, but only on the main thread, where the
    # CLI calls this. On a worker thread it runs that thread's own loop and
    # changes nothing, so vent serve uses find_app_by_bundle and
    # refresh_app_state instead.
    from Cocoa import NSDate, NSRunLoop

    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
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
    because the names look alike.

    Asks LaunchServices on every call instead of reading NSWorkspace's
    list. vent serve looks apps up on a worker thread, where that list
    never refreshes, so an app opened after the server's first lookup
    stayed invisible and a relaunched one kept its dead pid."""
    _require_pyobjc()
    needle = bundle_id.strip()
    for ns_app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(needle) or []:
        # activationPolicy 0 == NSApplicationActivationPolicyRegular
        if ns_app.activationPolicy() != 0 or ns_app.isTerminated():
            continue
        pid = int(ns_app.processIdentifier())
        if _pid_alive(pid):
            return RunningApp(
                name=str(ns_app.localizedName() or ""),
                bundle_id=str(ns_app.bundleIdentifier()) if ns_app.bundleIdentifier() else None,
                pid=pid,
            )
    # LaunchServices matches bundle ids case-sensitively. The list scan
    # keeps the old case-insensitive match for hand-written packs.
    lowered = needle.lower()
    for app in running_apps():
        if app.bundle_id and app.bundle_id.lower() == lowered and _pid_alive(app.pid):
            return app
    raise AXError(f"No running application with bundle id {bundle_id!r}. Is the app open?")


def _pid_alive(pid: int) -> bool:
    """Whether a process exists. LaunchServices can go on listing an app
    whose process was killed, and AX calls to it then fail as if it hung."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def refresh_app_state() -> None:
    """Let AppKit apply the app launches, quits and activations it has
    queued. NSWorkspace and NSRunningApplication update only while the
    main run loop runs, and vent serve's main thread runs the asyncio loop
    instead. The server calls this there before each tool call, so
    frontmost_app() on the worker thread reports the app in front now,
    not the one in front at the server's first call. A no-op off the main
    thread."""
    if not _PYOBJC_AVAILABLE:
        return
    from Cocoa import NSDate, NSRunLoop, NSThread

    if NSThread.isMainThread():
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))


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

    def wraps(self, ref: Any) -> bool:
        """Whether this element is the raw AX reference `ref`, such as an
        app's AXFocusedWindow value. CF refs compare by CFEqual through
        pyobjc."""
        try:
            return bool(self._ref == ref)
        except TypeError:
            return False

    def __repr__(self) -> str:
        label = self.label
        return f"<Element {self.role}{f' {label!r}' if label else ''}>"


def app_element(app: RunningApp) -> Element:
    """Root accessibility element for a running application."""
    require_trusted()
    ref = AXUIElementCreateApplication(app.pid)
    AXUIElementSetMessagingTimeout(ref, MESSAGING_TIMEOUT_S)
    return Element(ref)
