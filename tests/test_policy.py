"""Policy screening: the safety layer between model and app.

These pin the SECURITY.md T2/T3 rules directly, without the explorer around
them, so a regression names the exact rule it broke.
"""

from vent import policy as policy_mod
from vent.snapshot import Node


def node(role="AXButton", label="", identifier="", subrole="", value="", window="Main"):
    return Node(
        id=0, role=role, subrole=subrole, label=label, identifier=identifier,
        value_preview=value, actions=["AXPress"], chain=(), depth=0, element=None,
    )


def make_node_with_window(label, window):
    from vent.snapshot import ChainLink
    n = node(label=label)
    n.chain = (ChainLink(role="AXWindow", label=window, identifier="", ordinal=0, index=0),)
    return n


def test_destructive_verb_blocked():
    pol = policy_mod.Policy()
    assert not pol.screen_press(node(label="Delete")).allowed
    assert not pol.screen_press(node(label="Send")).allowed


def test_punctuation_glued_verbs_still_blocked():
    pol = policy_mod.Policy()
    for label in ["Delete!", "Send,", "Send/Receive", "Erase—All", "Delete…"]:
        assert not pol.screen_press(node(label=label)).allowed, label


def test_generic_confirm_on_destructive_sheet_blocked():
    pol = policy_mod.Policy()
    ok = make_node_with_window("OK", "Delete Report.docx?")
    assert not pol.screen_press(ok).allowed
    # But OK on a benign sheet is fine.
    fine = make_node_with_window("OK", "Preferences")
    assert pol.screen_press(fine).allowed


def test_risky_key_survives_relabel():
    pol = policy_mod.Policy()
    from vent.snapshot import ChainLink
    chain = (ChainLink(role="AXWindow", label="Main", identifier="w1", ordinal=0, index=0),)
    n1 = node(label="Options", identifier="btn-1"); n1.chain = chain
    pol.mark_risky(n1)
    # Same element, relabelled and window retitled: still risky.
    n2 = node(label="Preferences", identifier="btn-1"); n2.chain = chain
    assert not pol.screen_press(n2).allowed


def test_unlabeled_default_deny():
    pol = policy_mod.Policy()
    verdict = pol.screen_press(node(label=""))
    assert not verdict.allowed
    assert verdict.classification == "unknown"


def test_non_latin_label_default_deny():
    pol = policy_mod.Policy()
    assert not pol.screen_press(node(label="削除")).allowed  # "delete" in Japanese


def test_contextual_verb_benign_vs_dangerous():
    pol = policy_mod.Policy()
    benign = make_node_with_window("Clear Formatting", "Untitled")
    assert pol.screen_press(benign).allowed
    dangerous = make_node_with_window("Clear", "Message History")
    assert not pol.screen_press(dangerous).allowed


def test_cumulative_budget_exhausts():
    pol = policy_mod.Policy(cumulative_budget=2)
    for _ in range(2):
        verdict = pol.screen_press(node(label="New Note"))
        assert verdict.allowed
        pol.record_cumulative()
    assert not pol.screen_press(node(label="New Note")).allowed


def test_set_value_blocked_on_populated_field():
    pol = policy_mod.Policy()
    empty = pol.screen_set_value(node(role="AXTextField", label="Search"), "")
    assert empty.allowed
    populated = pol.screen_set_value(node(role="AXTextField", label="Search"), "existing text")
    assert not populated.allowed


def test_secure_field_untouchable():
    pol = policy_mod.Policy()
    secure = node(role="AXTextField", subrole="AXSecureTextField", label="Password")
    assert not pol.screen_press(secure).allowed
    assert not pol.screen_set_value(secure, "").allowed


def test_auth_window_blocks_press():
    pol = policy_mod.Policy()
    assert not pol.screen_press(make_node_with_window("OK", "Sign In")).allowed


def test_risky_element_never_pressed_again():
    pol = policy_mod.Policy()
    n = node(label="Options")
    assert pol.screen_press(n).allowed
    pol.mark_risky(n)
    assert not pol.screen_press(n).allowed


def test_disguised_spellings_of_a_verb_are_still_caught():
    """NFKC first, then invisible characters and accents out: each of these
    renders as (or close to) Delete and used to match no verb."""
    pol = policy_mod.Policy()
    disguised = [
        "Ｄｅｌｅｔｅ",  # fullwidth
        "\U0001d403\U0001d41e\U0001d425\U0001d41e\U0001d42d\U0001d41e",  # mathematical bold
        "Del​ete",  # zero-width space
        "Dele­te",  # soft hyphen
        "Déléte",  # accents
    ]
    for label in disguised:
        verdict = pol.screen_press(node(label=label))
        assert not verdict.allowed, ascii(label)
        assert verdict.reason == "destructive verb 'delete'", ascii(label)


def test_labels_that_mix_scripts_are_refused():
    """A Cyrillic e (U+0435) or Greek o (U+03BF) inside a Latin word looks
    the same and matches no verb, so the mix itself is refused."""
    pol = policy_mod.Policy()
    for label in ["Dеlete", "Sеnd", "Blοck"]:
        verdict = pol.screen_press(node(label=label))
        assert not verdict.allowed, ascii(label)
        assert "mixes scripts" in verdict.reason
    assert not policy_mod.screen_heal_target("AXButton", "Sеnd", "Main").allowed
    # A label wholly in one other script is still "not classifiable" when
    # probing, and left to the role fence when healing.
    assert "not confidently classifiable" in pol.screen_press(node(label="削除")).reason
    assert policy_mod.screen_heal_target("AXButton", "保存する", "Main").allowed


def test_committing_verbs_are_refused():
    pol = policy_mod.Policy()
    for label in [
        "Leave Channel", "Call", "Start Call", "Accept", "Decline", "Archive",
        "Unsubscribe", "Sign Out", "Sign Out…", "Log Out", "Log-Out",
        "Don’t Save", "Join Meeting", "Allow", "Install Update", "Approve",
    ]:
        assert not pol.screen_press(node(label=label)).allowed, label


def test_everyday_labels_still_pass():
    """Word matching keeps the longer list from catching verbs inside other
    words: "Calendar" holds no "call", "Banner" no "ban"."""
    pol = policy_mod.Policy()
    for label in ["Bold", "Italic", "Calendar", "Banner", "Find", "Show Sidebar", "Zoom In", "Café Menu"]:
        assert pol.screen_press(node(label=label)).allowed, label


def test_nothing_inside_a_sheet_or_dialog_is_probed():
    """Its buttons answer whatever it asks. The watchdog only cancels."""
    from vent.snapshot import ChainLink

    app = ChainLink(role="AXApplication", label="App", identifier="", ordinal=0, index=0)
    window = ChainLink(role="AXWindow", label="", identifier="", ordinal=0, index=0, subrole="AXStandardWindow")
    sheet = ChainLink(role="AXSheet", label="", identifier="", ordinal=0, index=3)
    alert = ChainLink(role="AXWindow", label="", identifier="", ordinal=1, index=1, subrole="AXDialog")
    pol = policy_mod.Policy()

    in_sheet = node(label="Options")
    in_sheet.chain = (app, window, sheet, ChainLink(role="AXButton", label="Options", identifier="", ordinal=0, index=0))
    assert "inside a sheet or dialog" in pol.screen_press(in_sheet).reason
    field = node(role="AXTextField", label="Name")
    field.chain = in_sheet.chain[:-1] + (ChainLink(role="AXTextField", label="Name", identifier="", ordinal=0, index=1),)
    assert not pol.screen_set_value(field, "").allowed

    in_alert = node(label="Show Details")
    in_alert.chain = (app, alert, ChainLink(role="AXButton", label="Show Details", identifier="", ordinal=0, index=0))
    assert not pol.screen_press(in_alert).allowed

    in_window = node(label="Options")
    in_window.chain = (app, window, ChainLink(role="AXButton", label="Options", identifier="", ordinal=0, index=0))
    assert pol.screen_press(in_window).allowed
