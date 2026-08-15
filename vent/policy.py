"""Action safety policy for autonomous exploration.

Every action the explorer wants to take passes through here after the model
proposes it and before anything touches the app. The model cannot bypass
this layer; that ordering is the containment story of SECURITY.md T5. The
rules implement threats T2 and T3:

- Destructive verbs are blocked by label match, with context: verbs like
  "clear" are judged with their window title and siblings, because "Clear
  Formatting" and "Clear All Messages" are different risks.
- Unclassifiable elements are default-deny. Empty labels, non-Latin labels,
  icon-only buttons: unknown never means allowed.
- set_value is gated by op, not label: overwriting a populated field is
  destruction with no verb attached. Probing may only set values on empty
  fields, or after capturing the prior value for restore.
- Actions are classed reversible, cumulative, or destructive. Cumulative
  actions (New Note, New Window) are budgeted per session so exploration
  cannot leave forty empty notes synced to iCloud.

The verb lists are data on purpose: easy to audit, easy to extend, and
their insufficiency is documented rather than papered over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .snapshot import Node

DESTRUCTIVE_VERBS = {
    "delete", "remove", "empty", "erase", "send", "pay", "buy", "purchase",
    "subscribe", "sign", "submit", "post", "publish", "share", "shut down",
    "restart", "log out", "logout", "format", "uninstall", "trash", "discard",
    "revert", "unpin", "archive", "block", "report", "unsubscribe", "eject",
    "disconnect", "end", "terminate", "kill", "quit", "close",
}

# Destructive only in some contexts; judged with surroundings.
CONTEXTUAL_VERBS = {"clear", "reset", "replace", "overwrite", "cancel", "stop", "hide"}

# Signals in a window or sheet title that make a contextual verb dangerous.
DANGEROUS_CONTEXT_WORDS = {"all", "history", "everything", "account", "library", "messages"}

CUMULATIVE_VERBS = {"new", "add", "create", "duplicate", "insert", "compose"}

AUTH_WINDOW_WORDS = {"password", "login", "log in", "sign in", "unlock", "authenticate"}

DEFAULT_CUMULATIVE_BUDGET = 3


@dataclass
class Verdict:
    allowed: bool
    reason: str
    classification: str  # "reversible" | "cumulative" | "destructive" | "unknown"


@dataclass
class Policy:
    """Session-scoped policy state. One instance per exploration session."""

    cumulative_budget: int = DEFAULT_CUMULATIVE_BUDGET
    cumulative_spent: int = 0
    risky_ids: set[str] = field(default_factory=set)

    def mark_risky(self, node: Node) -> None:
        """Remember an element whose press produced an unexpected dialog.
        It is never touched again this session (T3 dialog watchdog)."""
        self.risky_ids.add(self._node_key(node))

    def _node_key(self, node: Node) -> str:
        return f"{node.role}|{node.identifier}|{node.label}|{node.window_title}"

    def screen_press(self, node: Node) -> Verdict:
        """May the explorer press this element?"""
        if self._node_key(node) in self.risky_ids:
            return Verdict(False, "previously produced an unexpected dialog", "unknown")

        if node.subrole == "AXSecureTextField":
            return Verdict(False, "secure field", "destructive")

        label = node.label.strip().lower()

        if not label:
            # T2: unclassifiable never means allowed.
            return Verdict(False, "unlabeled element; default deny", "unknown")

        if not _mostly_latin(label):
            return Verdict(False, "label not confidently classifiable; default deny", "unknown")

        for verb in DESTRUCTIVE_VERBS:
            if _matches_verb(label, verb):
                return Verdict(False, f"destructive verb {verb!r}", "destructive")

        for verb in CONTEXTUAL_VERBS:
            if _matches_verb(label, verb):
                context = f"{node.window_title} {label}".lower()
                if any(word in context for word in DANGEROUS_CONTEXT_WORDS):
                    return Verdict(False, f"contextual verb {verb!r} in dangerous context", "destructive")
                return Verdict(True, f"contextual verb {verb!r}, context looks benign", "reversible")

        if any(word in node.window_title.lower() for word in AUTH_WINDOW_WORDS):
            return Verdict(False, "element inside an authentication window", "destructive")

        for verb in CUMULATIVE_VERBS:
            if _matches_verb(label, verb):
                if self.cumulative_spent >= self.cumulative_budget:
                    return Verdict(False, "cumulative action budget exhausted", "cumulative")
                return Verdict(True, f"cumulative verb {verb!r}, within budget", "cumulative")

        return Verdict(True, "no destructive signal", "reversible")

    def record_cumulative(self) -> None:
        self.cumulative_spent += 1

    def screen_set_value(self, node: Node, current_value: str) -> Verdict:
        """May the explorer write into this field?

        The op is the risk, not the label (SECURITY.md T2): set_value on a
        populated field destroys content with no verb for a blocklist to
        catch. Probing writes only into empty fields.
        """
        if node.subrole == "AXSecureTextField":
            return Verdict(False, "secure field", "destructive")
        if node.role not in {"AXTextField", "AXTextArea", "AXComboBox"}:
            return Verdict(False, f"set_value not appropriate for {node.role}", "unknown")
        if current_value.strip():
            return Verdict(
                False,
                "field already has content; overwriting it during probing is destructive",
                "destructive",
            )
        if any(word in node.window_title.lower() for word in AUTH_WINDOW_WORDS):
            return Verdict(False, "field inside an authentication window", "destructive")
        return Verdict(True, "empty field", "reversible")


def _matches_verb(label: str, verb: str) -> bool:
    """Word-boundary match so 'end' does not fire on 'calendar'."""
    words = label.replace("…", " ").replace(".", " ").split()
    if " " in verb:
        return verb in label
    return verb in words


def _mostly_latin(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    latin = sum(1 for ch in letters if ch.isascii())
    return latin / len(letters) >= 0.8
