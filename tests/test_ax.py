"""App lookup, with AppKit and process liveness faked so these run
anywhere. The live behavior they pin: vent serve looks apps up on a
worker thread, where NSWorkspace's app list never refreshes."""

import pytest

from vent import ax

TEXTEDIT = "com.apple.TextEdit"


class FakeNSApp:
    def __init__(self, pid, bundle_id=TEXTEDIT, policy=0, terminated=False):
        self.pid, self.bundle_id, self.policy, self.terminated = pid, bundle_id, policy, terminated

    def processIdentifier(self):
        return self.pid

    def bundleIdentifier(self):
        return self.bundle_id

    def localizedName(self):
        return "TextEdit"

    def activationPolicy(self):
        return self.policy

    def isTerminated(self):
        return self.terminated


@pytest.fixture
def apps(monkeypatch):
    """`launchservices` is what macOS reports now; `frozen` is NSWorkspace's
    list as a worker thread sees it, stuck at the server's first lookup."""
    state = {"launchservices": [], "frozen": [], "dead": set()}

    class FakeNSRunningApplication:
        @staticmethod
        def runningApplicationsWithBundleIdentifier_(bundle_id):
            return [app for app in state["launchservices"] if app.bundle_id == bundle_id]

    monkeypatch.setattr(ax, "_PYOBJC_AVAILABLE", True)
    monkeypatch.setattr(ax, "NSRunningApplication", FakeNSRunningApplication, raising=False)
    monkeypatch.setattr(ax, "running_apps", lambda: state["frozen"])
    monkeypatch.setattr(ax, "_pid_alive", lambda pid: pid not in state["dead"])
    return state


def test_bundle_lookup_finds_an_app_opened_after_the_first_lookup(apps):
    apps["launchservices"] = [FakeNSApp(pid=200)]
    assert ax.find_app_by_bundle(TEXTEDIT).pid == 200


def test_bundle_lookup_follows_a_relaunch_to_the_new_pid(apps):
    apps["frozen"] = [ax.RunningApp(name="TextEdit", bundle_id=TEXTEDIT, pid=100)]
    apps["dead"] = {100}
    apps["launchservices"] = [FakeNSApp(pid=200)]
    assert ax.find_app_by_bundle(TEXTEDIT).pid == 200


def test_bundle_lookup_skips_a_dead_process_launchservices_still_lists(apps):
    apps["launchservices"] = [FakeNSApp(pid=100), FakeNSApp(pid=200)]
    apps["dead"] = {100}
    assert ax.find_app_by_bundle(TEXTEDIT).pid == 200


def test_bundle_lookup_skips_terminated_and_background_instances(apps):
    apps["launchservices"] = [
        FakeNSApp(pid=100, terminated=True),
        FakeNSApp(pid=101, policy=1),
        FakeNSApp(pid=102),
    ]
    assert ax.find_app_by_bundle(TEXTEDIT).pid == 102


def test_bundle_lookup_says_the_app_is_not_open_rather_than_reaching_a_dead_pid(apps):
    apps["frozen"] = [ax.RunningApp(name="TextEdit", bundle_id=TEXTEDIT, pid=100)]
    apps["launchservices"] = [FakeNSApp(pid=100)]
    apps["dead"] = {100}
    with pytest.raises(ax.AXError, match="Is the app open"):
        ax.find_app_by_bundle(TEXTEDIT)


def test_bundle_lookup_still_matches_case_insensitively(apps):
    apps["frozen"] = [ax.RunningApp(name="TextEdit", bundle_id=TEXTEDIT, pid=100)]
    assert ax.find_app_by_bundle("com.apple.textedit").pid == 100
