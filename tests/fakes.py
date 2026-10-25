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
        modal: bool = False,
    ):
        self.role = role
        self.subrole = subrole
        self.title = label
        self.label = label
        self.value = value
        self.enabled = enabled
        self.modal = modal  # AXModal, on a window the app runs modally
        self._identifier = identifier
        self._actions = actions or []
        self._children = children or []
        # Set on an app root to mark one of its windows focused or main.
        self.focused_window: Optional["FakeElement"] = None
        self.main_window: Optional["FakeElement"] = None
        # Set on a sheet or dialog: the button it names as its cancel and
        # default buttons, the way AppKit reports them.
        self.cancel_button: Optional["FakeElement"] = None
        self.default_button: Optional["FakeElement"] = None

    def attribute(self, name: str) -> Any:
        if name == "AXIdentifier":
            return self._identifier or None
        if name == "AXFocusedWindow":
            return self.focused_window
        if name == "AXMainWindow":
            return self.main_window
        if name == "AXModal":
            return self.modal if self.role == "AXWindow" else None
        if name == "AXCancelButton":
            return self.cancel_button
        if name == "AXDefaultButton":
            return self.default_button
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

    def wraps(self, ref: Any) -> bool:
        return self is ref


    def walk(self, max_depth: int = 25, _depth: int = 0):
        """Depth-first traversal yielding (depth, element), mirroring
        ax.Element.walk so runtime helpers that crawl the tree work on
        fakes too."""
        yield _depth, self
        if _depth >= max_depth:
            return
        for child in self._children:
            yield from child.walk(max_depth, _depth + 1)

    def __repr__(self) -> str:
        return f"<FakeElement {self.role} {self.label!r}>"


class GuardedElement(FakeElement):
    """A fake that fails the test if anything lists its children. Stands in
    for a menu bar that a walk is supposed to skip."""

    def children(self) -> list[FakeElement]:
        raise AssertionError(f"{self!r} was walked")


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


def textedit_with_menus() -> FakeElement:
    """textedit_like plus a menu bar holding File > New, listed next to the
    window the way real apps list theirs."""
    tree = textedit_like()
    tree._children.append(
        FakeElement(
            "AXMenuBar",
            children=[
                FakeElement(
                    "AXMenuBarItem",
                    "File",
                    children=[
                        FakeElement(
                            "AXMenu",
                            children=[
                                FakeElement(
                                    "AXMenuItem", "New", identifier="menu-new", actions=["AXPress"]
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
    )
    return tree


class PressRecorder(FakeElement):
    """A button that remembers being pressed, and can run a callback when
    it is, to open or close a sheet the way the real button would."""

    def __init__(self, *args, on_press=None, **kwargs):
        kwargs.setdefault("actions", ["AXPress"])
        super().__init__(*args, **kwargs)
        self.presses = 0
        self._on_press = on_press

    def perform(self, action: str = "AXPress") -> None:
        super().perform(action)
        self.presses += 1
        if self._on_press is not None:
            self._on_press()


def sheet(*children: FakeElement, cancel: Optional[FakeElement] = None) -> FakeElement:
    """A sheet, which macOS shows as an AXSheet child of its window. There
    is no AXSheets attribute; the old guard read one and never fired."""
    found = FakeElement("AXSheet", children=list(children))
    found.cancel_button = cancel
    return found


def save_changes_sheet() -> FakeElement:
    """TextEdit's "Do you want to save the changes" sheet. Its buttons are
    listed with the default one first, as AppKit lists them."""
    save = PressRecorder("AXButton", "Save")
    cancel = PressRecorder("AXButton", "Cancel")
    found = sheet(save, PressRecorder("AXButton", "Don't Save"), cancel, cancel=cancel)
    found.default_button = save
    return found


def on_another_space(tree: FakeElement) -> FakeElement:
    """The same app with its window on another Space, as macOS shows it to
    accessibility clients: the app lists only its menu bar and no windows,
    but AXMainWindow and AXFocusedWindow still return the window. Seen on
    Outlook and TextEdit."""
    window = next(child for child in tree._children if child.role == "AXWindow")
    tree._children = [child for child in tree._children if child.role != "AXWindow"]
    tree.main_window = window
    tree.focused_window = window
    return tree


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