"""What the accessibility wrapper says when an app doesn't answer an
action in time. The AX calls are faked, so these run anywhere."""

import pytest

from vent import ax


def test_an_unanswered_press_says_it_may_still_happen(monkeypatch):
    """A timed-out press was delivered and waits in the app's queue. Calling
    it a failure invites a retry that presses twice."""
    monkeypatch.setattr(
        ax, "AXUIElementPerformAction", lambda ref, action: ax.AX_ERROR_CANNOT_COMPLETE, raising=False
    )
    with pytest.raises(ax.AXTransientError, match="may still carry it out"):
        ax.Element(object()).perform("AXPress")


def test_an_unanswered_write_says_it_may_still_happen(monkeypatch):
    monkeypatch.setattr(
        ax, "AXUIElementSetAttributeValue", lambda ref, name, value: ax.AX_ERROR_CANNOT_COMPLETE, raising=False
    )
    with pytest.raises(ax.AXTransientError, match="may still carry it out"):
        ax.Element(object()).set_value("typed")


def test_other_action_errors_stay_plain_failures(monkeypatch):
    monkeypatch.setattr(ax, "AXUIElementPerformAction", lambda ref, action: -25206, raising=False)
    monkeypatch.setattr(ax.Element, "__repr__", lambda self: "<Element>")
    with pytest.raises(ax.AXError) as failed:
        ax.Element(object()).perform("AXPress")
    assert not isinstance(failed.value, ax.AXTransientError)
