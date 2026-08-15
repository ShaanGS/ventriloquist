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
from typing import Any

from .ax import Element

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

# One depth limit shared by snapshot walks and anchor resolution. If the
# two disagreed, an anchor minted from a deep inspect could describe an
# element the resolver structurally cannot reach.
MAX_TREE_DEPTH = 25

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


def _preview(value: Any) -> str:
    """Display value for humans and models. Never used for verification;
    the runtime always re-reads live values (ARCHITECTURE.md section 5)."""
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= VALUE_PREVIEW_LIMIT else text[: VALUE_PREVIEW_LIMIT - 3] + "..."


def _identifier(element: Element) -> str:
    return str(element.attribute("AXIdentifier") or "")


def _windows(root: Element) -> list[Element]:
    """Enumerate windows through AXWindows, the authoritative list.

    Falls back to AXChildren for app roots that do not report AXWindows.
    This exists because AXChildren alone misses windows in some states,
    which we learned the day TextEdit's file picker made the app root
    look empty.
    """
    raw = root.attribute("AXWindows")
    if raw:
        return [Element(ref) for ref in raw]
    return [child for child in root.children() if child.role == "AXWindow"]


def _has_sheet(window: Element) -> bool:
    return bool(window.attribute("AXSheets"))


def snapshot(
    root: Element,
    max_depth: int = MAX_TREE_DEPTH,
    max_nodes: int = 800,
    include_menus: bool = False,
) -> Snapshot:
    """Walk an app and return its snapshot.

    The menu bar is skipped by default. It dwarfs window content (a plain
    Finder walk yields about 75 menu items before the first window element)
    and gets its own targeted pass during exploration instead.
    """
    nodes: list[Node] = []
    truncated = False
    seen: set[int] = set()

    def visit(element: Element, chain: tuple[ChainLink, ...], depth: int, shown_depth: int) -> None:
        nonlocal truncated
        if depth > max_depth:
            truncated = True
            return
        if len(nodes) >= max_nodes:
            truncated = True
            return
        # Real AX trees can contain cycles: a wedged TextEdit was observed
        # returning the app element as its own child. Without this guard a
        # cyclic tree makes the walk explode instead of terminate.
        key = element.ref_key()
        if key is not None:
            if key in seen:
                return
            seen.add(key)

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
        for index, child in enumerate(element.children()):
            child_role = child.role
            ordinal = role_counts.get(child_role, 0)
            role_counts[child_role] = ordinal + 1

            if not include_menus and child_role == "AXMenuBar":
                # Menu bars can appear at any depth (and duplicated) in
                # some app states; skip them everywhere unless asked.
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

    # The app root itself.
    root_link = ChainLink(role=root.role, label=root.label, identifier=_identifier(root), ordinal=0, index=0)
    visit(root, (root_link,), 0, 0)

    windows = _windows(root)
    focused = root.attribute("AXFocusedWindow")
    focused_title = str(Element(focused).title) if focused is not None else ""
    modal = any(_has_sheet(w) for w in windows)

    return Snapshot(
        nodes=nodes,
        truncated=truncated,
        modal_present=modal,
        focused_window_title=focused_title,
    )


def render(snap: Snapshot) -> str:
    """One line per element, readable by humans and models alike."""
    lines = []
    for node in snap.nodes:
        indent = "  " * node.depth
        label = f" {node.label!r}" if node.label else ""
        ident = f" id={node.identifier}" if node.identifier else ""
        value = f" = {node.value_preview!r}" if node.value_preview else ""
        actions = f" [{','.join(a.removeprefix('AX') for a in node.actions)}]" if node.actions else ""
        lines.append(f"{indent}#{node.id} {node.role.removeprefix('AX')}{label}{ident}{value}{actions}")
    if snap.modal_present:
        lines.append("(a modal sheet is open)")
    if snap.truncated:
        lines.append(f"(truncated: walk stopped at {len(snap.nodes)} elements)")
    return "\n".join(lines)
