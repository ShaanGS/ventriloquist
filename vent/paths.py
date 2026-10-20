"""Where vent reads and writes files.

Two kinds of pack live in two places. Bundled packs ship inside the
installed package and are never written to: an install may be read-only,
and an upgrade would silently discard edits made there. Everything vent
writes (compiled packs, healed or promoted copies, exploration traces)
goes under VENT_HOME, which defaults to ~/.ventriloquist.

A pack in VENT_HOME shadows a bundled pack with the same bundle id. That is
how healing or re-recording a bundled pack takes effect: the change is
saved as the user's own copy and the bundled original stays untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

BUNDLED_PACKS_DIR = Path(__file__).resolve().parent / "bundled_packs"


def home() -> Path:
    """The directory vent writes to. Set VENT_HOME to move it."""
    configured = os.environ.get("VENT_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".ventriloquist"


def user_packs_dir() -> Path:
    return home() / "packs"


def traces_dir() -> Path:
    return home() / "traces"


def pack_search_path() -> list[Path]:
    """Directories to load packs from, highest priority first. The first
    entry is the only one vent ever writes packs into."""
    return [user_packs_dir(), BUNDLED_PACKS_DIR]
