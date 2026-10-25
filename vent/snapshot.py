"""Semantic snapshots of an app's accessibility tree.

A snapshot is how every other part of Ventriloquist perceives an app. It is
a pruned list of interesting elements, each carrying enough context (role,
label, identifier, full ancestor chain) to build a durable anchor from it
later. The contract this module implements is section 5 of ARCHITECTURE.md.

Design notes worth knowing before editing:

- Container roles are recursed through but not surfaced. They still appear
  in each node's ancestor chain, because the chain is the structural
  skeleton that anchor resolution scores against.
- Secure text fields are excluded here, at the lowest level, on purpose.
  See SECURITY.md T4 before changing that.
- Truncation is always explicit. Consumers must be able to tell "the app
  does not have this element" from "the walk stopped early".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .ax import Element, session_locked

# Roles that are pure structure. Recursed through, never surfaced.
CONTAINER_ROLES = {
    "AXGroup",
    "AXScrollArea",
    "AXSplitGroup",
    "AXSplitter",
    "AXLayoutArea",
    "AXLayoutItem",
    "AXUnknown",
    "AXGenericElement",
}

# Roles surfaced even without a label, because their value or state matters.
ALWAYS_SHOW_ROLES = {
    "AXTextField",
    "AXTextArea",
    "AXCheckBox",
    "AXRadioButton",
    "AXPopUpButton",
    "AXComboBox",
    "AXSlider",
    "AXTable",
    "AXOutline",
    "AXList",
    "AXWebArea",
}

SECURE_SUBROLE = "AXSecureTextField"

# macOS has no attribute listing a window's sheets. A sheet is a child of
# the window it hangs from, with this role.
SHEET_ROLE = "AXSheet"

# Window subroles of modal dialogs. A window can also be modal with an
# ordinary subrole, which its AXModal attribute reports.
DIALOG_SUBROLES = {"AXDialog", "AXSystemDialog"}

# One depth limit shared by snapshot walks and anchor resolution. If the
# two disagreed, an anchor minted from a deep inspect could describe an
# element the resolver structurally cannot reach. 25 sufficed for native
# apps, but Chromium hosts nest far deeper: VS Code's search input sits
# below depth 25, discovered the day its anchor could never resolve.
MAX_TREE_DEPTH = 50

VALUE_PREVIEW_LIMIT = 80


@dataclass(frozen=True)
class ChainLink:
    """One ancestor level in an element's address.

    ordinal is the position among same-role siblings, which survives
    unrelated siblings being added or removed. index is the raw position
    among all siblings, kept only as a weak tiebreak.
    """

    role: str
    label: str
    identifier: str
    ordinal: int
    index: int
    subrole: str = ""  # AXCloseButton and friends; separates window-control twins


@dataclass
class Node:
    """One surfaced element."""

    id: int
    role: str
    subrole: str
    label: str
    identifier: str
    value_preview: str
    actions: list[str]
    chain: tuple[ChainLink, ...]
    depth: int
    element: Element = field(repr=False)

    @property
    def window_title(self) -> str:
        for link in self.chain:
            if link.role == "AXWindow":
                return link.label
        return ""


@dataclass
class Snapshot:
    """The full result of one walk."""

    nodes: list[Node]
    truncated: bool
    modal_present: bool
    focused_window_title: str
    off_space: bool = False  # the app listed no window, so its main one was walked; see _unlisted_windows


def _preview(value: Any) -> str:
    """Display value for humans and models. Never used for verification;
    the runtime always re-reads live values (ARCHITECTURE.md section 5)."""
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= VALUE_PREVIEW_LIMIT else text[: VALUE_PREVIEW_LIMIT - 3] + "..."


def _identifier(element: Element) -> str:
    return str(element.attribute("AXIdentifier") or "")


def _as_element(ref: Any) -> Element:
    """Wrap a raw AX reference, passing through anything already
    element-shaped (real Elements, or the fakes the test suite uses)."""
    return ref if hasattr(ref, "attribute") else Element(ref)


def _unlisted_windows(root: Element) -> list[Element]:
    """The app's main and focused windows, for an app that lists none.

    macOS shows accessibility clients only the windows on the current
    Space. With an app's window on another desktop, or the user in a
    full-screen app, AXWindows is empty and AXChildren holds only the menu
    bar, but AXMainWindow and AXFocusedWindow still return the window.
    Reads and writes through it work without switching Spaces. A press
    there acts as in any app that isn't in front, sometimes flipping only
    the button, which is what requires_frontmost is for. Found with
    Outlook, confirmed on TextEdit. A hidden app and a minimized window
    stay listed, so neither lands here. The app's other windows on other
    Spaces stay out of reach.

    A locked screen hides windows too. It is left looking empty, as
    before, so nothing is read or done behind it.
    """
    if session_locked():
        return []
    found: list[Element] = []
    for name in ("AXMainWindow", "AXFocusedWindow"):
        ref = root.attribute(name)
        if ref is None or any(window.wraps(ref) for window in found):
            continue
        window = _as_element(ref)
        if window.role == "AXWindow":
            found.append(window)
    return found


def _lists_windows(root: Element, children: list[Element]) -> bool:
    return any(child.role == "AXWindow" for child in children) or bool(root.attribute("AXWindows"))


def app_children(root: Element) -> list[Element]:
    """The app root's children as every walk sees them.

    When the app lists no window at all, its main and focused windows go
    first, where a listed window sits, so a walk made while the window is
    on another Space records the same chains as one made on its Space.
    """
    children = root.children()
    if _lists_windows(root, children):
        return children
    return _unlisted_windows(root) + children


def app_windows(root: Element) -> list[Element]:
    """Enumerate windows through AXWindows, the authoritative list.

    Falls back to AXChildren for app roots that do not report AXWindows.
    This exists because AXChildren alone misses windows in some states,
    which we learned the day TextEdit's file picker made the app root
    look empty. When neither lists a window, the main and focused windows
    stand in (see _unlisted_windows).
    """
    raw = root.attribute("AXWindows")
    if raw:
        return [_as_element(ref) for ref in raw]
    listed = [child for child in root.children() if child.role == "AXWindow"]
    return listed or _unlisted_windows(root)


def same_element(a: Element, b: Element) -> bool:
    """Whether two wrappers hold the same AX element. Real refs compare by
    CFEqual through wraps; a fake is its own ref."""
    return a.wraps(getattr(b, "_ref", b))


def is_dialog_window(window: Element) -> bool:
    """Whether a window is a modal dialog, which holds up the whole app
    until it is answered: an alert or dialog by subrole, or any window the
    app runs modally."""
    if window.role != "AXWindow":
        return False
    return window.subrole in DIALOG_SUBROLES or bool(window.attribute("AXModal"))


def window_sheets(window: Element) -> list[Element]:
    """The sheets hanging from a window."""
    return [child for child in window.children() if child.role == SHEET_ROLE]


@dataclass
class OpenDialog:
    """A sheet or modal dialog that is open now."""

    element: Element  # the AXSheet, or the dialog's own AXWindow
    window: Element  # the window a sheet hangs from; a dialog is its own
    kind: str  # "sheet" or "dialog"

    def describe(self) -> str:
        """"the sheet on 'Inbox'", "the dialog 'Export'", for messages."""
        title = self.window.label
        if self.kind == "sheet":
            return f"the sheet on {title!r}" if title else "the sheet on an untitled window"
        return f"the dialog {title!r}" if title else "the dialog"


def open_dialogs(root: Element, windows: Optional[list[Element]] = None) -> list[OpenDialog]:
    """Every sheet and modal dialog the app has open, among `windows` when
    given (the app's windows otherwise)."""
    found = []
    for window in app_windows(root) if windows is None else windows:
        if is_dialog_window(window):
            found.append(OpenDialog(element=window, window=window, kind="dialog"))
        for sheet in window_sheets(window):
            found.append(OpenDialog(element=sheet, window=window, kind="sheet"))
    return found


def snapshot(
    root: Element,
    max_depth: int = MAX_TREE_DEPTH,
    max_nodes: int = 800,
    include_menus: bool = False,
    only_top: Optional[int] = None,
) -> Snapshot:
    """Walk an app and return its snapshot.

    The menu bar is skipped by default. It dwarfs window content (a plain
    Finder walk yields about 75 menu items before the first window element)
    and gets its own targeted pass during exploration instead.

    only_top walks just one child of the app root (a window, or the menu
    bar with include_menus), given by its index among the root's children
    as app_children lists them. The healer looks at one window this way.
    Chains still start at the app root, so anchors built from the nodes
    resolve as usual.
    """
    nodes: list[Node] = []
    truncated = False
    # Ancestor-path cycle guard: real AX trees can contain cycles (a wedged
    # TextEdit was observed returning the app element as its own child) and
    # are DAGs besides, so only true ancestor cycles are cut; an element
    # reachable through two different paths is walked through both.
    path_keys: set[int] = set()

    def visit(element: Element, chain: tuple[ChainLink, ...], depth: int, shown_depth: int) -> None:
        nonlocal truncated
        if depth > max_depth:
            truncated = True
            return
        if len(nodes) >= max_nodes:
            truncated = True
            return
        key = element.ref_key() if hasattr(element, "ref_key") else None
        if key is not None and key in path_keys:
            return
        if key is not None:
            path_keys.add(key)

        role = element.role
        subrole = element.subrole
        label = element.label

        if subrole == SECURE_SUBROLE:
            return  # SECURITY.md T4: secure fields never surface, full stop.

        actions = element.actions()
        interesting = (
            role in ALWAYS_SHOW_ROLES
            or (role not in CONTAINER_ROLES and role != "AXApplication" and (label or actions))
        )

        if interesting:
            nodes.append(
                Node(
                    id=len(nodes),
                    role=role,
                    subrole=subrole,
                    label=label,
                    identifier=_identifier(element),
                    value_preview=_preview(element.value),
                    actions=[a for a in actions if a != "AXShowMenu"],
                    chain=chain,
                    depth=shown_depth,
                    element=element,
                )
            )

        role_counts: dict[str, int] = {}
        for index, child in enumerate(top_level if depth == 0 else element.children()):
            child_role = child.role
            ordinal = role_counts.get(child_role, 0)
            role_counts[child_role] = ordinal + 1

            if not include_menus and child_role == "AXMenuBar":
                # Menu bars can appear at any depth (and duplicated) in
                # some app states; skip them everywhere unless asked.
                continue
            if depth == 0 and only_top is not None and index != only_top:
                continue

            link = ChainLink(
                role=child_role,
                label=child.label,
                identifier=_identifier(child),
                ordinal=ordinal,
                index=index,
                subrole=child.subrole,
            )
            visit(child, chain + (link,), depth + 1, shown_depth + (1 if interesting else 0))

        if key is not None:
            path_keys.discard(key)

    # The app root itself. Its children follow app_children, spelled out
    # here so the snapshot can say when the window came from outside the
    # app's own list.
    children = root.children()
    unlisted = [] if _lists_windows(root, children) else _unlisted_windows(root)
    top_level = unlisted + children
    root_link = ChainLink(role=root.role, label=root.label, identifier=_identifier(root), ordinal=0, index=0)
    visit(root, (root_link,), 0, 0)

    windows = unlisted or app_windows(root)
    focused = root.attribute("AXFocusedWindow")
    focused_title = ""
    if focused is not None:
        # Look the focused window up among the windows already listed, the
        # same identity check anchors.py uses. One that isn't listed (a
        # locked screen hides them all) goes through _as_element, since
        # wrapping a fake afresh would make it call the live AX API.
        match = next((w for w in windows if w.wraps(focused)), None)
        focused_title = str((match or _as_element(focused)).title)
    modal = bool(open_dialogs(root, windows))

    return Snapshot(
        nodes=nodes,
        truncated=truncated,
        modal_present=modal,
        focused_window_title=focused_title,
        off_space=bool(unlisted),
    )


def render(snap: Snapshot, redact_values: bool = False) -> str:
    """One line per element, readable by humans and models alike.

    redact_values replaces field contents with a length marker so a
    snapshot can be sent to a model without leaking what the user typed
    (SECURITY.md T6). Element structure and labels still go through; only
    values are withheld.
    """
    lines = []
    for node in snap.nodes:
        indent = "  " * node.depth
        label = f" {node.label!r}" if node.label else ""
        ident = f" id={node.identifier}" if node.identifier else ""
        if node.value_preview and redact_values:
            value = f" = <{len(node.value_preview)} chars redacted>"
        elif node.value_preview:
            value = f" = {node.value_preview!r}"
        else:
            value = ""
        cleaned = [" ".join(a.removeprefix("AX").split()) for a in node.actions]
        actions = f" [{','.join(c[:30] for c in cleaned)}]" if cleaned else ""
        lines.append(f"{indent}#{node.id} {node.role.removeprefix('AX')}{label}{ident}{value}{actions}")
    if snap.off_space:
        lines.append("(the window is on another Space, reached through the app's main window)")
    if snap.modal_present:
        lines.append("(a sheet or dialog is open)")
    if snap.truncated:
        lines.append(f"(truncated: walk stopped at {len(snap.nodes)} elements)")
    return "\n".join(lines)
