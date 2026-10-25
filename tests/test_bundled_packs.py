"""The packs that ship: the bundled ones in every wheel, and the examples
in the repo that people copy. They must load, and they must carry nobody's
data. Window titles are where that data hides (a document, a channel, a
workspace), and Electron apps repeat the window title on the group right
under the window and on the web area inside it, which is how the bundled
Slack pack shipped a Slack workspace name through 0.2.x."""

import json
import re
from pathlib import Path

import pytest

from vent.packs import load
from vent.paths import BUNDLED_PACKS_DIR

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
SHIPPED = sorted(BUNDLED_PACKS_DIR.glob("*/pack.json")) + sorted(EXAMPLES_DIR.glob("*/pack.json"))

# What a window title or account looks like once it leaks into a label:
# the separators apps put between a document and the app or folder name,
# a file name, or an email address.
TITLE_LIKE = [
    re.compile(r" [-–—] (Slack|Visual Studio Code|Code|Outlook|Notes|TextEdit)$"),
    re.compile(r" [–—] "),
    re.compile(r"\(Channel\)|\(DM\)"),
    re.compile(
        r"\w\.(txt|rtf|rtfd|md|pdf|docx?|xlsx?|pptx?|pages|numbers|key|csv|json|ya?ml|py|js|ts|html?|png|jpe?g|gif|heic|mov|mp4)\b",
        re.IGNORECASE,
    ),
    re.compile(r"[\w.+-]+@[\w-]+\.\w+"),
]


def _name(path: Path) -> str:
    return f"{path.parent.parent.name}/{path.parent.name}"


def _anchors(data: dict):
    """Every anchor in a pack's JSON, with where it sits."""
    for tool in data.get("tools", []):
        for index, step in enumerate(tool.get("steps", [])):
            if "anchor" in step:
                yield f"{tool['name']} step {index}", step["anchor"]
        for index, check in enumerate(tool.get("verify", [])):
            if "anchor" in check:
                yield f"{tool['name']} verify {index}", check["anchor"]


def _labels(anchor: dict):
    yield anchor.get("window_title", "")
    yield from anchor.get("labels", [])
    for link in anchor.get("chain", []):
        yield link.get("label", "")


@pytest.mark.parametrize("path", SHIPPED, ids=_name)
def test_shipped_pack_loads(path):
    pack = load(path)
    assert pack.tools, f"{_name(path)} loaded but has no tools"


@pytest.mark.parametrize("path", SHIPPED, ids=_name)
def test_no_label_looks_like_a_window_title(path):
    data = json.loads(path.read_text())
    leaks = [
        (where, label)
        for where, anchor in _anchors(data)
        for label in _labels(anchor)
        if label and any(pattern.search(label) for pattern in TITLE_LIKE)
    ]
    assert not leaks, f"{_name(path)} records window titles or account names: {leaks}"


@pytest.mark.parametrize("path", SHIPPED, ids=_name)
def test_no_chain_repeats_the_window_title(path):
    """In an Electron app the group under the window and the web area
    inside it both wear the window's title. Any label found on both is
    that title, whatever it says, so neither may keep it."""
    data = json.loads(path.read_text())
    repeats = []
    for where, anchor in _anchors(data):
        chain = anchor.get("chain", [])
        ancestors = chain[:-1]  # the last link is the element itself
        for index, link in enumerate(ancestors[:-1]):
            if link.get("role") != "AXWindow":
                continue
            top = ancestors[index + 1].get("label", "")
            below = ancestors[index + 2 :]
            if top and any(l.get("role") == "AXWebArea" and l.get("label") == top for l in below):
                repeats.append((where, top))
    assert not repeats, f"{_name(path)} keeps the window title in its chains: {repeats}"


@pytest.mark.parametrize("path", SHIPPED, ids=_name)
def test_no_quarantined_heals_ship(path):
    """A quarantine entry records the live label and window title of what a
    heal landed on, so a shipped pack starts with an empty quarantine."""
    data = json.loads(path.read_text())
    assert data.get("healed_pending", []) == [], f"{_name(path)} ships quarantined heals"


@pytest.mark.parametrize("path", SHIPPED, ids=_name)
def test_descriptions_name_tools_as_clients_see_them(path):
    """A description that points at another tool must use the name an MCP
    client sees, which carries the app's prefix (code_show_search, not
    show_search), or the client model looks for a tool that isn't there."""
    data = json.loads(path.read_text())
    prefix = re.sub(r"[^a-z0-9]+", "_", data["app_name"].lower()).strip("_")
    names = [tool["name"] for tool in data["tools"]]
    bare = []
    for tool in data["tools"]:
        for other in names:
            if other == tool["name"]:
                continue
            for match in re.finditer(rf"(?<!\w){re.escape(other)}(?!\w)", tool.get("description", "")):
                bare.append((tool["name"], match.group(0)))
    assert not bare, f"{_name(path)} names tools without the {prefix}_ prefix: {bare}"
