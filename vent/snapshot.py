"""Compact semantic snapshots of an app's accessibility tree.

A snapshot is the agent-facing view of an app: interactive elements with
stable ids, minus the container noise. This is what exploration reads and
what compiled tool paths are anchored against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ax import Element

# Roles that are pure structure — recursed through but not shown.
CONTAINER_ROLES = {
    "AXGroup",
    "AXScrollArea",
    "AXSplitGroup",
    "AXLayoutArea",
    "AXLayoutItem",
    "AXUnknown",
    "AXGenericElement",
}

# Roles worth surfacing even without a label (their value/state matters).
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


@dataclass
class Node:
    """One surfaced element in a snapshot."""

    id: int
    role: str
    label: str
    value: str
    actions: list[str]
    path: tuple[int, ...]  # child indices from the app root — replay anchor
    depth: int
    element: Element = field(repr=False)


def _display_value(element: Element) -> str:
    value = element.value
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def snapshot(root: Element, max_depth: int = 25, max_nodes: int = 800) -> list[Node]:
    """Walk the tree and return surfaced nodes with stable ids and paths."""
    nodes: list[Node] = []

    def visit(element: Element, path: tuple[int, ...], depth: int, shown_depth: int) -> None:
        if len(nodes) >= max_nodes or depth > max_depth:
            return

        role = element.role
        label = element.label
        actions = element.actions()
        interesting = (
            role in ALWAYS_SHOW_ROLES
            or (role not in CONTAINER_ROLES and (label or actions))
        )

        if interesting:
            nodes.append(
                Node(
                    id=len(nodes),
                    role=role,
                    label=label,
                    value=_display_value(element),
                    actions=[a for a in actions if a != "AXShowMenu"],
                    path=path,
                    depth=shown_depth,
                    element=element,
                )
            )

        for index, child in enumerate(element.children()):
            visit(child, path + (index,), depth + 1, shown_depth + (1 if interesting else 0))

    visit(root, (), 0, 0)
    return nodes


def render(nodes: list[Node]) -> str:
    """Human/LLM-readable one-line-per-element rendering."""
    lines = []
    for node in nodes:
        indent = "  " * node.depth
        label = f" {node.label!r}" if node.label else ""
        value = f" = {node.value!r}" if node.value else ""
        actions = f" [{','.join(a.removeprefix('AX') for a in node.actions)}]" if node.actions else ""
        lines.append(f"{indent}#{node.id} {node.role.removeprefix('AX')}{label}{value}{actions}")
    return "\n".join(lines)


def resolve_path(root: Element, path: tuple[int, ...]) -> Element:
    """Follow child indices from the root — the deterministic replay primitive."""
    element = root
    for index in path:
        children = element.children()
        if index >= len(children):
            raise IndexError(f"Path {path} broke at index {index}: element has {len(children)} children")
        element = children[index]
    return element
