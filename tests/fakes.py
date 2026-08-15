"""In-memory fake accessibility trees.

These let anchor and runtime logic run as plain pytest on any machine, with
no macOS, no permissions, and no live apps. A FakeElement duck-types the
parts of vent.ax.Element that snapshot and anchor code touch. Keep the two
in sync: if Element grows a property that snapshot or anchors read, add it
here and add a test that exercises it.
"""

from __future__ import annotations

from typing import Any, Optional


class FakeElement:
    def __init__(
        self,
        role: str,
        label: str = "",
        identifier: str = "",
        subrole: str = "",
        value: Any = None,
        actions: Optional[list[str]] = None,
        children: Optional[list["FakeElement"]] = None,
        enabled: bool = True,
    ):
        self.role = role
        self.subrole = subrole
        self.title = label
        self.label = label
        self.value = value
        self.enabled = enabled
        self._identifier = identifier
        self._actions = actions or []
        self._children = children or []

    def attribute(self, name: str) -> Any:
        if name == "AXIdentifier":
            return self._identifier or None
        return None

    def actions(self) -> list[str]:
        return self._actions

    def children(self) -> list["FakeElement"]:
        return self._children

    def perform(self, action: str = "AXPress") -> None:
        if action not in self._actions:
            raise RuntimeError(f"{self!r} does not support {action}")

    def set_value(self, value: Any) -> None:
        self.value = value

    def ref_key(self):
        return id(self)

    def __repr__(self) -> str:
        return f"<FakeElement {self.role} {self.label!r}>"


class TickingElement(FakeElement):
    """A fake whose value reads differently for the first `ticks` reads,
    then holds still. Lets settle() tests exercise the change-then-quiet
    path without threads or a real app."""

    def __init__(self, *args, ticks: int = 0, **kwargs):
        self._ticks_left = ticks
        super().__init__(*args, **kwargs)

    @property
    def value(self):
        if self._ticks_left > 0:
            self._ticks_left -= 1
            return f"changing-{self._ticks_left}"
        return self._value

    @value.setter
    def value(self, v):
        self._value = v


def textedit_like() -> FakeElement:
    """A tree shaped like TextEdit: one window, toolbar buttons, a text area."""
    return FakeElement(
        "AXApplication",
        "TextEdit",
        children=[
            FakeElement(
                "AXWindow",
                "untitled.txt",
                children=[
                    FakeElement("AXButton", "", subrole="AXCloseButton", actions=["AXPress"]),
                    FakeElement("AXButton", "", subrole="AXFullScreenButton", actions=["AXPress"]),
                    FakeElement(
                        "AXScrollArea",
                        children=[
                            FakeElement("AXTextArea", value="hello", identifier="doc-body"),
                        ],
                    ),
                ],
            ),
        ],
    )


def notes_like() -> FakeElement:
    """A tree shaped like Notes: sidebar list, search field, note body.

    The search field and the body are the classic ambiguity trap: two
    text-ish elements, both unlabeled. Tests use this to prove the
    resolver refuses to guess between them.
    """
    return FakeElement(
        "AXApplication",
        "Notes",
        children=[
            FakeElement(
                "AXWindow",
                "Notes",
                children=[
                    FakeElement("AXTextField", "", identifier="search-field", value=""),
                    FakeElement(
                        "AXList",
                        "Notes list",
                        children=[
                            FakeElement("AXStaticText", "Groceries", actions=["AXPress"]),
                            FakeElement("AXStaticText", "Ideas", actions=["AXPress"]),
                        ],
                    ),
                    FakeElement(
                        "AXScrollArea",
                        children=[
                            FakeElement("AXTextArea", value="milk, eggs", identifier="note-body"),
                        ],
                    ),
                ],
            ),
        ],
    )
