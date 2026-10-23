"""Setup shared by every test."""

import pytest


@pytest.fixture(autouse=True)
def unlocked_screen(monkeypatch):
    """Reaching a window on another Space stands down while the screen is
    locked, and on a Mac that check asks the live login session. Pin it to
    unlocked so the suite gives the same answers on a locked Mac as in CI.
    Found when the off-Space tests failed while the Mac's display slept."""
    monkeypatch.setattr("vent.snapshot.session_locked", lambda: False)
