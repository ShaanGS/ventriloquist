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


def test_the_messaging_timeout_is_set_for_every_element_once(monkeypatch):
    """Set on the app root alone, the 1 s timeout covered only the root's
    own calls; each child a walk reads is a new ref, which waited out the
    six-second system default on a hung app."""
    system_wide, set_on = object(), []
    monkeypatch.setattr(ax, "require_trusted", lambda: None)
    monkeypatch.setattr(ax, "_timeout_set_globally", False)
    monkeypatch.setattr(ax, "AXUIElementCreateSystemWide", lambda: system_wide, raising=False)
    monkeypatch.setattr(ax, "AXUIElementCreateApplication", lambda pid: f"app {pid}", raising=False)
    monkeypatch.setattr(
        ax, "AXUIElementSetMessagingTimeout", lambda ref, seconds: set_on.append((ref, seconds)), raising=False
    )
    ax.app_element(ax.RunningApp("TextEdit", "com.apple.TextEdit", 1))
    ax.app_element(ax.RunningApp("Notes", "com.apple.Notes", 2))
    assert set_on.count((system_wide, ax.MESSAGING_TIMEOUT_S)) == 1
    assert set_on[0] == (system_wide, ax.MESSAGING_TIMEOUT_S)


def test_other_action_errors_stay_plain_failures(monkeypatch):
    monkeypatch.setattr(ax, "AXUIElementPerformAction", lambda ref, action: -25206, raising=False)
    monkeypatch.setattr(ax.Element, "__repr__", lambda self: "<Element>")
    with pytest.raises(ax.AXError) as failed:
        ax.Element(object()).perform("AXPress")
    assert not isinstance(failed.value, ax.AXTransientError)
