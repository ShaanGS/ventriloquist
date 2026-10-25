"""Action safety policy for autonomous exploration.

Every action the explorer wants to take passes through here after the model
proposes it and before anything touches the app. The model cannot bypass
this layer; that ordering is the containment story of SECURITY.md T5. The
rules implement threats T2 and T3:

- Destructive verbs are blocked by label match, with context: verbs like
  "clear" are judged with their window title and siblings, because "Clear
  Formatting" and "Clear All Messages" are different risks.
- Labels are matched after NFKC normalisation, with invisible format
  characters dropped and accents folded, so Delete in fullwidth or
  mathematical bold letters, or split by a zero-width space, still reads
  as delete.
- Unclassifiable elements are default-deny. Empty labels, non-Latin labels,
  labels that mix scripts (Delete spelled with a Cyrillic e, U+0435, looks
  like Delete and matches no verb), icon-only buttons: unknown never means
  allowed.
- Nothing inside a sheet or dialog is probed. Its buttons answer whatever
  it asks, and the explorer's watchdog only ever cancels one.
- set_value is gated by op, not label: overwriting a populated field is
  destruction with no verb attached. Probing may only set values on empty
  fields, or after capturing the prior value for restore.
- Actions are classed reversible, cumulative, or destructive. Cumulative
  actions (New Note, New Window) are budgeted per session so exploration
  cannot leave forty empty notes synced to iCloud.

The verb lists are data on purpose: easy to audit, easy to extend, and
their insufficiency is documented rather than papered over. They are
English only, which is why the explorer refuses to probe an app whose
menus are in another language.
"""

from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass, field

from .snapshot import DIALOG_SUBROLES, SHEET_ROLE, Node

DESTRUCTIVE_VERBS = {
    "delete", "remove", "empty", "erase", "send", "pay", "buy", "purchase",
    "subscribe", "sign", "submit", "post", "publish", "share", "shut down",
    "restart", "log out", "logout", "format", "uninstall", "trash", "discard",
    "revert", "unpin", "archive", "block", "report", "unsubscribe", "eject",
    "disconnect", "end", "terminate", "kill", "quit", "close",
    # Committing verbs: each tells someone else something, spends or moves
    # something, or can't be undone by pressing again.
    "leave", "call", "accept", "decline", "reject", "join", "answer", "dial",
    "hang up", "invite", "approve", "agree", "allow", "grant", "authorize",
    "install", "update", "upgrade", "download", "upload", "sync", "order",
    "checkout", "check out", "donate", "bid", "register", "enroll",
    "sign up", "signup", "sign out", "signout", "sign off", "log off",
    "logoff", "follow", "unfollow", "vote", "revoke", "unshare", "transfer",
    "merge", "push", "commit", "deploy", "record", "broadcast", "go live",
    "rsvp", "unmute", "restore", "don't save", "dont save", "unsend",
    "recall", "junk", "spam", "kick", "ban",
}

# Destructive only in some contexts; judged with surroundings.
CONTEXTUAL_VERBS = {"clear", "reset", "replace", "overwrite", "cancel", "stop", "hide"}

# Signals in a window or sheet title that make a contextual verb dangerous.
DANGEROUS_CONTEXT_WORDS = {"all", "history", "everything", "account", "library", "messages"}

CUMULATIVE_VERBS = {"new", "add", "create", "duplicate", "insert", "compose"}

AUTH_WINDOW_WORDS = {"password", "login", "log in", "sign in", "unlock", "authenticate"}

# Short labels that carry no verb of their own but confirm whatever sheet
# they sit on. Judged against the window/sheet title (SECURITY.md T2).
GENERIC_CONFIRM_LABELS = {"ok", "yes", "confirm", "continue", "proceed", "done", "apply"}

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
        # Keyed on stable facets only. Label and window title are
        # app-controlled and state-dependent, so an element that relabels
        # itself after triggering a dialog must still count as risky.
        ordinals = ",".join(str(link.ordinal) for link in node.chain)
        return f"{node.role}|{node.identifier}|{ordinals}"

    def screen_press(self, node: Node) -> Verdict:
        """May the explorer press this element?"""
        if self._node_key(node) in self.risky_ids:
            return Verdict(False, "previously produced an unexpected dialog", "unknown")

        if node.subrole == "AXSecureTextField":
            return Verdict(False, "secure field", "destructive")

        if _in_sheet_or_dialog(node):
            return Verdict(False, IN_DIALOG_REASON, "unknown")

        label = _fold(node.label)

        if not label:
            # T2: unclassifiable never means allowed.
            return Verdict(False, "unlabeled element; default deny", "unknown")

        unreadable = _script_problem(label)
        if unreadable:
            return Verdict(False, unreadable, "unknown")

        words = _words(label)
        for verb in DESTRUCTIVE_VERBS:
            if _has_verb(words, verb):
                return Verdict(False, f"destructive verb {verb!r}", "destructive")

        window = _fold(node.window_title)
        for verb in CONTEXTUAL_VERBS:
            if _has_verb(words, verb):
                context = f"{window} {label}"
                if any(word in context for word in DANGEROUS_CONTEXT_WORDS):
                    return Verdict(False, f"contextual verb {verb!r} in dangerous context", "destructive")
                return Verdict(True, f"contextual verb {verb!r}, context looks benign", "reversible")

        if any(word in window for word in AUTH_WINDOW_WORDS):
            return Verdict(False, "element inside an authentication window", "destructive")

        # A destructive verb in the WINDOW or SHEET title makes even a
        # verb-free confirm button ("OK" on a "Delete Report?" sheet)
        # dangerous. This is the compound-context case from review.
        if set(_words(window)) & (DESTRUCTIVE_VERBS | CONTEXTUAL_VERBS):
            if label in GENERIC_CONFIRM_LABELS or _has_verb(words, "delete"):
                return Verdict(
                    False,
                    "confirms a window whose title carries a destructive verb",
                    "destructive",
                )

        for verb in CUMULATIVE_VERBS:
            if _has_verb(words, verb):
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
        if _in_sheet_or_dialog(node):
            return Verdict(False, IN_DIALOG_REASON, "unknown")
        if node.role not in {"AXTextField", "AXTextArea", "AXComboBox"}:
            return Verdict(False, f"set_value not appropriate for {node.role}", "unknown")
        if current_value.strip():
            return Verdict(
                False,
                "field already has content; overwriting it during probing is destructive",
                "destructive",
            )
        if any(word in _fold(node.window_title) for word in AUTH_WINDOW_WORDS):
            return Verdict(False, "field inside an authentication window", "destructive")
        return Verdict(True, "empty field", "reversible")


def screen_heal_target(role: str, label: str, window_title: str, subrole: str = "") -> Verdict:
    """May a healed anchor re-point a tool's step onto this element?

    The core T8 concern: a hostile UI state steers healing so a benign
    tool's anchor lands on a dangerous element (a re-grounded "save" step
    now pressing "Send"). Healing may only re-ground onto an element whose
    label and window carry no destructive signal. This runs on every heal,
    on first derivation and on every deterministic reuse, so a heal derived
    once from a hostile state cannot be trusted forever without re-passing
    it. Stateless by design; it takes plain facets so both the healer and
    `vent verify` can call it without a Policy instance.
    """
    if subrole == "AXSecureTextField":
        return Verdict(False, "healed onto a secure field", "destructive")
    text = _fold(label)
    window = _fold(window_title)
    scripts = _scripts(text)
    if "LATIN" in scripts and len(scripts) > 1:
        # Lookalike letters mixed into Latin ones are how a hostile label
        # hides a verb. A label wholly in another script (Japanese mixes
        # three) is left to the role fence: healing a non-English app has
        # nothing better to go on.
        return Verdict(False, f"healed onto an element whose {_script_problem(text)}", "unknown")
    if any(w in window for w in AUTH_WINDOW_WORDS):
        return Verdict(False, "healed onto an element in an authentication window", "destructive")
    words = _words(text)
    for verb in DESTRUCTIVE_VERBS:
        if _has_verb(words, verb):
            return Verdict(False, f"healed onto an element with destructive verb {verb!r}", "destructive")
    # Contextual verbs (Clear, Reset, Replace, Cancel, Stop) are also
    # refused as heal targets. During exploration they can be probed when
    # their context looks benign, but re-grounding a durable tool onto one
    # is exactly the "benign save step now wipes the document" outcome T8
    # exists to prevent, so healing takes the strict reading.
    for verb in CONTEXTUAL_VERBS:
        if _has_verb(words, verb):
            return Verdict(False, f"healed onto an element with contextual verb {verb!r}", "destructive")
    if set(_words(window)) & (DESTRUCTIVE_VERBS | CONTEXTUAL_VERBS):
        if text in GENERIC_CONFIRM_LABELS or _has_verb(words, "delete"):
            return Verdict(
                False,
                "healed onto a confirm control on a window with a destructive title",
                "destructive",
            )
    return Verdict(True, "no destructive signal on the healed target", "reversible")


IN_DIALOG_REASON = "inside a sheet or dialog; exploration only ever cancels those"


def _in_sheet_or_dialog(node: Node) -> bool:
    """Whether a node sits in a sheet or a dialog window, going by its
    chain. A window that is modal only by AXModal can't be told from its
    chain; the explorer refuses to start a round while one is open."""
    chain = node.chain
    if any(link.role == SHEET_ROLE for link in chain[:-1]):
        return True
    return len(chain) > 1 and chain[1].role == "AXWindow" and chain[1].subrole in DIALOG_SUBROLES


# Apostrophes that apps put in labels like "Don't Save", folded to one.
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'"})


def _fold(text: str) -> str:
    """The form a label or title is matched in. NFKC turns fullwidth and
    styled letters into plain ones; format characters that render as
    nothing (a zero-width space can split a word without showing) are
    dropped; accents are folded; case is folded."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch))
    return text.translate(_APOSTROPHES).casefold().strip()


def _words(text: str) -> list[str]:
    """Words split on Unicode word boundaries rather than whitespace, so
    punctuation glued to a verb doesn't hide it (SECURITY.md T2)."""
    return re.findall(r"[^\W_]+", text)


def _has_verb(words: list[str], verb: str) -> bool:
    """Whether the verb's words appear in a row, however the label joins
    them: "Log Out", "log-out" and "Log Out…" all hold "log out"."""
    wanted = _words(verb)
    span = len(wanted)
    return any(words[i : i + span] == wanted for i in range(len(words) - span + 1))


def _matches_verb(label: str, verb: str) -> bool:
    """Word match on the folded label, so 'end' does not fire on 'calendar',
    but 'Delete!', 'Send/Receive' and a fullwidth Delete still fire."""
    return _has_verb(_words(_fold(label)), verb)


def _scripts(text: str) -> set[str]:
    """The scripts a text's letters are written in, named by the first word
    of each letter's Unicode name (LATIN, CYRILLIC, GREEK, CJK...).
    Modifier letters belong to no script of their own."""
    found = set()
    for ch in text:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            if not name.startswith("MODIFIER LETTER"):
                found.add(name.split(" ", 1)[0] if name else "UNKNOWN")
    return found


def _script_problem(text: str) -> str:
    """Why the verb lists can't judge a folded label, or "" when they can."""
    scripts = _scripts(text)
    if scripts == {"LATIN"}:
        return ""
    if "LATIN" in scripts:
        names = " and ".join(sorted(script.title() for script in scripts))
        return f"label mixes scripts ({names}), as lookalike letters do; default deny"
    return "label not confidently classifiable; default deny"
