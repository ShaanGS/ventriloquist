"""App lookup and Accessibility hints, with AppKit and the process table
faked so these run anywhere. The live behavior they pin: vent serve looks
apps up on a worker thread, where NSWorkspace's app list never refreshes,
and Claude Desktop starts MCP servers so that macOS checks the program it
ran instead of Claude."""

import pytest

from vent import ax

TEXTEDIT = "com.apple.TextEdit"
VENT_PYTHON = "/Users/you/.local/share/uv/python/cpython-3.14.8-macos-aarch64-none/bin/python3.14"
DISCLAIMER = "/Applications/Claude.app/Contents/Helpers/disclaimer"
CLAUDE = "/Applications/Claude.app/Contents/MacOS/Claude"


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


def test_hint_names_vents_python_when_claude_desktop_started_vent(monkeypatch):
    monkeypatch.setattr(ax, "_process_chain", lambda: [VENT_PYTHON, DISCLAIMER, CLAUDE])
    hint = ax.trust_hint()
    assert VENT_PYTHON in hint
    assert "Claude Desktop" in hint
    assert "uv tool install" not in hint


def test_hint_names_uv_and_a_narrower_install_when_claude_runs_vent_through_uvx(monkeypatch):
    uv = "/opt/homebrew/Cellar/uv/0.9.2/bin/uv"
    monkeypatch.setattr(ax, "_process_chain", lambda: [VENT_PYTHON, uv, DISCLAIMER, CLAUDE])
    hint = ax.trust_hint()
    assert uv in hint
    assert VENT_PYTHON not in hint
    assert "uv tool install ventriloquist-mcp" in hint


def test_hint_names_claude_code_when_vent_runs_inside_one_of_its_sessions(monkeypatch):
    """Claude Desktop starts Claude Code sessions through the same helper,
    so there the program macOS checks is Claude Code."""
    claude_code = "/Users/you/Library/Application Support/Claude/claude-code/2.1.304/x/claude.app/Contents/MacOS/claude"
    chain = [VENT_PYTHON, "/bin/zsh", claude_code, DISCLAIMER, CLAUDE]
    monkeypatch.setattr(ax, "_process_chain", lambda: chain)
    hint = ax.trust_hint()
    assert claude_code in hint
    assert VENT_PYTHON not in hint


def test_hint_names_the_launching_app_outside_claude_desktop(monkeypatch):
    terminal = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
    monkeypatch.setattr(ax, "_process_chain", lambda: [VENT_PYTHON, "/bin/zsh", terminal])
    assert ax.trust_hint() == ax.TRUST_HINT


def test_unreadable_process_paths_fall_back_to_the_usual_hint(monkeypatch):
    monkeypatch.setattr(ax, "_process_chain", lambda: ["", DISCLAIMER, CLAUDE])
    assert ax.trust_hint() == ax.TRUST_HINT


def test_refusal_under_claude_desktop_carries_the_file_to_trust(monkeypatch):
    monkeypatch.setattr(ax, "is_trusted", lambda: False)
    monkeypatch.setattr(ax, "_process_chain", lambda: [VENT_PYTHON, DISCLAIMER, CLAUDE])
    with pytest.raises(ax.NotTrustedError, match="python3.14"):
        ax.require_trusted()
