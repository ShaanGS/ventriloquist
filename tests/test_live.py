"""Live mode: looking at any app, acting on it by number, and saving the
steps that ran as a tool. Runs on fake trees, so no app or permission is
involved."""

import json
from pathlib import Path

import anyio
import mcp.types as types
import pytest
from click.testing import CliRunner

from vent import anchors, ax, cli, live, packs, paths, policy, runtime
from vent import server as server_mod
from vent.packs import load, save
from vent.server import build_server
from vent.snapshot import Node, snapshot

from .fakes import FakeElement, PressRecorder
from .test_packs import sample_pack
from .test_server import MODES, answering, call, connect, never_answered, tool_names

MAIL = "com.example.Mail"


def mail_like():
    """A small mail app: a search field, a draft body, and a toolbar with
    a harmless button, a Send button and an icon with no label."""
    body = FakeElement("AXTextArea", value="", identifier="draft-body")
    search = FakeElement("AXTextField", "Search", value="")
    folders_view = PressRecorder("AXButton", "Show Folders")
    send = PressRecorder("AXButton", "Send")
    icon = PressRecorder("AXButton", "")
    window = FakeElement(
        "AXWindow",
        "Inbox",
        children=[
            FakeElement("AXToolbar", children=[folders_view, send, icon]),
            search,
            FakeElement("AXScrollArea", children=[body]),
        ],
    )
    root = FakeElement("AXApplication", "Mail", children=[window])
    return root, {"body": body, "search": search, "show": folders_view, "send": send, "icon": icon}


@pytest.fixture
def mail(monkeypatch):
    root, parts = mail_like()
    app = ax.RunningApp("Mail", MAIL, 42)

    def find_app(name):
        if name.lower() in {"mail", MAIL.lower()}:
            return app
        if name.lower() == "terminal":
            return ax.RunningApp("Terminal", "com.apple.Terminal", 7)
        raise ax.AXError(f"No running application matches {name!r}.")

    monkeypatch.setattr(ax, "find_app", find_app)
    monkeypatch.setattr(ax, "find_app_by_bundle", lambda bundle_id: app)
    monkeypatch.setattr(ax, "app_element", lambda running: root)
    monkeypatch.setattr(ax, "app_version", lambda running: "16.0")
    monkeypatch.setattr(ax, "activate", lambda running: True)
    monkeypatch.setattr(runtime, "_wait_frontmost", lambda *args: None)
    monkeypatch.setattr(runtime, "SETTLE_NO_REACTION_S", 0.01)
    return root, parts


def number_of(session, element):
    return next(node.id for node in session.nodes if node.element is element)


def node_for(element, window="Inbox", role=None, subrole=""):
    return Node(
        id=0,
        role=role or element.role,
        subrole=subrole,
        label=element.label,
        identifier="",
        value_preview="",
        actions=["AXPress"],
        chain=(),
        depth=0,
        element=element,
    )


# The screen


@pytest.mark.parametrize(
    "label, kind",
    [
        ("Bold", "reversible"),
        ("+", "reversible"),
        ("New Message", "cumulative"),
        ("Send", "destructive"),
        ("Delete", "destructive"),
        ("", "destructive"),
        ("Dеlete", "destructive"),  # a Cyrillic e
    ],
)
def test_presses_run_or_ask_by_label(label, kind):
    verdict = policy.screen_live(node_for(FakeElement("AXButton", label)), "press")
    assert verdict.allowed
    assert verdict.classification == kind


def test_password_fields_and_sign_in_windows_are_refused():
    secure = node_for(FakeElement("AXTextField", "Password"), subrole="AXSecureTextField")
    assert not policy.screen_live(secure, "set_value").allowed
    button = FakeElement("AXButton", "Next")
    signin = Node(0, "AXButton", "", "Next", "", "", ["AXPress"], (), 0, button)
    signin.chain = (packs_link("Sign in to your account"),)
    assert not policy.screen_live(signin, "press").allowed


def packs_link(title):
    from vent.snapshot import ChainLink

    return ChainLink(role="AXWindow", label=title, identifier="", ordinal=0, index=0)


def test_typing_only_goes_into_text_fields_and_asks_before_replacing_a_document():
    assert not policy.screen_live(node_for(FakeElement("AXButton", "Go")), "set_value").allowed
    field = node_for(FakeElement("AXTextField", "Search"))
    assert policy.screen_live(field, "set_value", "old query").classification == "reversible"
    long = policy.screen_live(field, "set_value", "x" * 1000)
    assert long.allowed and long.classification == "destructive"


# Look, act, save


def test_look_lists_numbered_elements_and_refuses_terminals(mail):
    state = live.Live()
    session, body = state.look("Mail")
    assert "Send" in body and "#0" in body
    assert session.app_version == "16.0"
    with pytest.raises(live.LiveError, match="terminal"):
        state.look("Terminal")


def test_act_needs_a_look_first(mail):
    with pytest.raises(live.LiveError, match="vent_live_look"):
        live.Live().prepare("Mail", 0, None)


def test_a_press_and_a_typed_text_run_through_the_runtime_and_are_recorded(mail):
    root, parts = mail
    state = live.Live()
    session, _ = state.look("Mail")
    pressed = state.prepare("Mail", number_of(session, parts["show"]), None)
    assert not pressed.asks
    assert "step 0" in state.run(pressed)
    assert parts["show"].presses == 1
    typed = state.prepare("Mail", number_of(session, parts["body"]), "Hi Sam")
    assert "step 1" in state.run(typed)
    assert parts["body"].value == "Hi Sam"
    assert [act.op for act in session.recorded] == ["press", "set_value"]


def test_send_and_unlabelled_buttons_ask_first(mail):
    root, parts = mail
    state = live.Live()
    session, _ = state.look("Mail")
    send = state.prepare("Mail", number_of(session, parts["send"]), None)
    assert send.asks
    assert 'the Button "Send" in the window "Inbox"' in send.question()
    assert state.prepare("Mail", number_of(session, parts["icon"]), None).asks


def test_save_builds_a_tool_from_what_ran(mail):
    root, parts = mail
    state = live.Live()
    session, _ = state.look("Mail")
    state.run(state.prepare("Mail", number_of(session, parts["show"]), None))
    state.run(state.prepare("Mail", number_of(session, parts["search"]), "receipts"))
    state.run(state.prepare("Mail", number_of(session, parts["body"]), "Hi Sam"))

    draft = state.build(
        "Mail",
        "draft_reply",
        "Open folders, search receipts, write a reply.",
        [0, 1, 2],
        [{"name": "body", "description": "What to write.", "step": 2}],
    )
    spec = draft.spec
    assert spec.risk == "mutating"
    assert spec.requires_frontmost
    assert [step.op for step in spec.steps] == ["press", "set_value", "set_value"]
    assert spec.steps[1].value == {"literal": "receipts"}
    assert spec.steps[2].value == {"param": "body"}
    assert spec.params == {"body": {"type": "string", "description": "What to write."}}
    summary = draft.question(replaces=False)
    assert "with the literal text 'receipts'" in summary
    assert "with argument 'body'" in summary

    # The saved tool replays on the same app.
    parts["body"].value = ""
    pack = draft.pack()
    result = runtime.execute(pack, "draft_reply", {"body": "Thanks!"}, root)
    assert result.ok and parts["body"].value == "Thanks!"
    assert parts["show"].presses == 2


def test_a_step_the_user_had_to_approve_makes_the_tool_high_risk(mail):
    root, parts = mail
    state = live.Live()
    session, _ = state.look("Mail")
    state.run(state.prepare("Mail", number_of(session, parts["send"]), None))
    assert state.build("Mail", "send_it", "Send the draft.", [0]).spec.risk == "high"


def test_the_model_can_raise_risk_but_not_lower_it(mail):
    root, parts = mail
    state = live.Live()
    session, _ = state.look("Mail")
    state.run(state.prepare("Mail", number_of(session, parts["show"]), None))
    assert state.build("Mail", "show", "Show the folders.", [0], risk="high").spec.risk == "high"
    with pytest.raises(live.LiveError, match="risk"):
        state.build("Mail", "show", "Show the folders.", [0], risk="read_only")


@pytest.mark.parametrize(
    "kwargs, problem",
    [
        ({"name": "Bad Name"}, "snake_case"),
        ({"steps": [5]}, "numbers from 0 to 0"),
        ({"steps": []}, "steps must list"),
        ({"description": ""}, "description"),
        ({"params": [{"name": "x", "step": 0}]}, "a press"),
    ],
)
def test_save_refuses_what_it_cannot_build(mail, kwargs, problem):
    root, parts = mail
    state = live.Live()
    session, _ = state.look("Mail")
    state.run(state.prepare("Mail", number_of(session, parts["show"]), None))
    args = {"name": "show", "description": "Show the folders.", "steps": [0]} | kwargs
    with pytest.raises(live.LiveError, match=problem):
        state.build("Mail", args["name"], args["description"], args["steps"], args.get("params"))


def test_a_saved_tool_joins_the_bundled_pack_instead_of_hiding_it(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    save(sample_pack(), bundled / "com.apple.TextEdit" / "pack.json")
    monkeypatch.setattr(paths, "BUNDLED_PACKS_DIR", bundled)
    added = sample_pack()
    added.tools[0].name = "write_twice"
    pack, path = live.write(added, tmp_path / "user")
    assert path == tmp_path / "user" / "com.apple.TextEdit" / "pack.json"
    assert [tool.name for tool in load(path).tools] == ["write_document", "write_twice"]


# Over MCP


@pytest.fixture
def live_server(tmp_path, monkeypatch, mail):
    monkeypatch.setenv("VENT_LIVE", "1")
    monkeypatch.delenv("VENT_ALLOW_HIGH", raising=False)
    monkeypatch.delenv("VENT_READ_ONLY", raising=False)
    monkeypatch.setattr(paths, "BUNDLED_PACKS_DIR", tmp_path / "no-bundled")
    monkeypatch.setenv("VENT_HOME", str(tmp_path / "home"))
    save(sample_pack(), tmp_path / "user" / "com.apple.TextEdit" / "pack.json")
    return build_server([tmp_path / "user"]), tmp_path / "user"


def test_live_tools_are_served_only_with_vent_live(tmp_path, monkeypatch):
    monkeypatch.delenv("VENT_LIVE", raising=False)
    save(sample_pack(), tmp_path / "com.apple.TextEdit" / "pack.json")
    assert tool_names(build_server([tmp_path])) == ["textedit_write_document"]
    monkeypatch.setenv("VENT_LIVE", "1")
    assert set(live.TOOL_NAMES) <= set(tool_names(build_server([tmp_path])))
    monkeypatch.setenv("VENT_READ_ONLY", "1")
    names = tool_names(build_server([tmp_path]))
    assert live.LOOK_TOOL in names and live.ACT_TOOL not in names and live.SAVE_TOOL not in names


def run_session(server, script, **options):
    async def action(client):
        results = []
        for name, arguments in script:
            results.append(await client.call_tool(name, arguments))
        results.append([tool.name for tool in (await client.list_tools()).tools])
        return results

    return connect(server, action, **options)


def test_look_wraps_app_text_as_untrusted(live_server):
    server, _ = live_server
    result = call(server, live.LOOK_TOOL, {"app": "Mail"})
    assert not result.is_error, result.content[0].text
    text = result.content[0].text
    assert "untrusted_app_content_" in text and "Send" in text


def test_an_act_that_asks_is_refused_when_the_client_cannot_ask(live_server, mail):
    server, _ = live_server
    root, parts = mail
    send = snapshot(root).nodes
    number = next(node.id for node in send if node.element is parts["send"])
    results = run_session(server, [(live.LOOK_TOOL, {"app": "Mail"}), (live.ACT_TOOL, {"app": "Mail", "element": number})])
    assert results[1].is_error
    assert "cannot ask" in results[1].content[0].text
    assert parts["send"].presses == 0


@pytest.mark.parametrize("mode", MODES)
def test_an_act_that_asks_runs_on_yes(live_server, mail, mode):
    server, _ = live_server
    root, parts = mail
    number = next(node.id for node in snapshot(root).nodes if node.element is parts["send"])
    asked = []
    results = run_session(
        server,
        [(live.LOOK_TOOL, {"app": "Mail"}), (live.ACT_TOOL, {"app": "Mail", "element": number})],
        mode=mode,
        elicitation_callback=answering("accept", asked),
    )
    assert not results[1].is_error, results[1].content[0].text
    assert parts["send"].presses == 1
    assert len(asked) == 1 and "Send" in asked[0].message


def test_save_without_a_way_to_ask_leaves_a_draft_for_vent_approve(live_server, mail, tmp_path, monkeypatch):
    server, user_dir = live_server
    root, parts = mail
    number = next(node.id for node in snapshot(root).nodes if node.element is parts["show"])
    results = run_session(
        server,
        [
            (live.LOOK_TOOL, {"app": "Mail"}),
            (live.ACT_TOOL, {"app": "Mail", "element": number}),
            (live.SAVE_TOOL, {"app": "Mail", "name": "show_folders", "description": "Show the folders.", "steps": [0]}),
        ],
    )
    assert not results[2].is_error, results[2].content[0].text
    assert "vent approve" in results[2].content[0].text
    assert not (user_dir / MAIL).exists()

    monkeypatch.setattr(paths, "user_packs_dir", lambda: user_dir)
    monkeypatch.setattr(cli, "_human_at_terminal", lambda: True)
    approved = CliRunner().invoke(cli.main, ["approve", "Mail"], input="y\n")
    assert approved.exit_code == 0, approved.output
    assert "does exactly this to 'Mail'" in approved.output
    assert [tool.name for tool in load(user_dir / MAIL / "pack.json").tools] == ["show_folders"]
    assert live.load_drafts() == []


@pytest.mark.parametrize("mode", MODES)
def test_save_on_yes_writes_the_pack_and_serves_the_tool_at_once(live_server, mail, mode):
    server, user_dir = live_server
    root, parts = mail
    number = next(node.id for node in snapshot(root).nodes if node.element is parts["body"])
    asked = []
    results = run_session(
        server,
        [
            (live.LOOK_TOOL, {"app": "Mail"}),
            (live.ACT_TOOL, {"app": "Mail", "element": number, "text": "Hi"}),
            (
                live.SAVE_TOOL,
                {
                    "app": "Mail",
                    "name": "write_draft",
                    "description": "Write the draft.",
                    "steps": [0],
                    "params": [{"name": "body", "description": "Draft text.", "step": 0}],
                },
            ),
        ],
        mode=mode,
        elicitation_callback=answering("accept", asked),
    )
    assert not results[2].is_error, results[2].content[0].text
    assert len(asked) == 1 and "Save a new vent tool for Mail?" in asked[0].message
    assert "mail_write_draft" in results[3]
    saved = load(user_dir / MAIL / "pack.json")
    assert saved.tools[0].params["body"]["description"] == "Draft text."


def test_save_on_no_writes_nothing(live_server, mail):
    server, user_dir = live_server
    root, parts = mail
    number = next(node.id for node in snapshot(root).nodes if node.element is parts["show"])
    results = run_session(
        server,
        [
            (live.LOOK_TOOL, {"app": "Mail"}),
            (live.ACT_TOOL, {"app": "Mail", "element": number}),
            (live.SAVE_TOOL, {"app": "Mail", "name": "show", "description": "Show it.", "steps": [0]}),
        ],
        elicitation_callback=answering("decline", []),
    )
    assert results[2].is_error and "was not saved" in results[2].content[0].text
    assert not (user_dir / MAIL).exists()


def test_a_pack_tool_cannot_take_a_live_tool_name(tmp_path, capsys):
    pack = sample_pack()
    pack.app_name = "Vent"
    pack.bundle_id = "com.example.Vent"
    pack.tools[0].name = "live_look"
    save(pack, tmp_path / pack.bundle_id / "pack.json")
    with pytest.raises(SystemExit):
        build_server([tmp_path])
    assert "vent's own live-mode tools" in capsys.readouterr().err


def test_mcp_config_live_adds_the_env(monkeypatch):
    result = CliRunner().invoke(cli.main, ["mcp-config", "--live"])
    assert result.exit_code == 0
    entry = json.loads(result.stdout[: result.stdout.rindex("}") + 1])
    assert entry["mcpServers"]["ventriloquist"]["env"] == {"VENT_LIVE": "1"}
    assert "-e VENT_LIVE=1" in result.output


# Shared packs


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_human_at_terminal", lambda: True)
    monkeypatch.setattr(paths, "user_packs_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(paths, "BUNDLED_PACKS_DIR", tmp_path / "no-bundled")
    return tmp_path / "user"


EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_add_pack_adds_only_the_tools_you_approve(user_dir):
    outlook = load(EXAMPLES / "outlook" / "pack.json")
    answers = "".join("y\n" if tool.name != "flag_message" else "n\n" for tool in outlook.tools)
    result = CliRunner().invoke(cli.main, ["add-pack", str(EXAMPLES / "outlook")], input=answers)
    assert result.exit_code == 0, result.output
    assert "does exactly this to 'Outlook'" in result.output
    added = load(user_dir / outlook.bundle_id / "pack.json")
    assert sorted(tool.name for tool in added.tools) == sorted(
        tool.name for tool in outlook.tools if tool.name != "flag_message"
    )


def test_add_pack_writes_nothing_when_you_approve_nothing(user_dir):
    outlook = load(EXAMPLES / "outlook" / "pack.json")
    result = CliRunner().invoke(cli.main, ["add-pack", str(EXAMPLES / "outlook")], input="n\n" * len(outlook.tools))
    assert result.exit_code == 0 and "Nothing added." in result.output
    assert not user_dir.exists()


# What the review found


YES = {server_mod.CONFIRM_KEY: types.ElicitResult(action="accept", content={})}


def test_one_yes_runs_one_act_however_often_it_is_resent(live_server, mail):
    """2026-07-28: the yes rides back in the request state, which used to
    be good for any number of retries, so one approval could send three
    times."""
    server, _ = live_server
    root, parts = mail
    number = next(n.id for n in snapshot(root).nodes if n.element is parts["send"])
    args = {"app": "Mail", "element": number}

    async def action(client):
        await client.call_tool(live.LOOK_TOOL, {"app": "Mail"})
        first = await client.session.call_tool(live.ACT_TOOL, args, allow_input_required=True)
        assert isinstance(first, types.InputRequiredResult)
        return [
            await client.session.call_tool(
                live.ACT_TOOL, args, input_responses=YES, request_state=first.request_state, allow_input_required=True
            )
            for _ in range(3)
        ]

    results = connect(server, action, elicitation_callback=answering("accept", []))
    assert parts["send"].presses == 1
    assert all(isinstance(r, types.InputRequiredResult) for r in results[1:])


def test_a_yes_for_one_unlabelled_button_does_not_press_another(live_server, mail):
    """The model looks again so the number now names a different button
    that reads the same; the question names the element, so it asks again."""
    server, _ = live_server
    root, parts = mail
    toolbar = root._children[0]._children[0]
    other = PressRecorder("AXButton", "", identifier="move-to-trash")
    toolbar._children.append(other)
    icon = parts["icon"]
    number = next(n.id for n in snapshot(root).nodes if n.element is icon)
    args = {"app": "Mail", "element": number}

    async def action(client):
        await client.call_tool(live.LOOK_TOOL, {"app": "Mail"})
        first = await client.session.call_tool(live.ACT_TOOL, args, allow_input_required=True)
        toolbar._children.remove(icon)
        await client.call_tool(live.LOOK_TOOL, {"app": "Mail"})
        return await client.session.call_tool(
            live.ACT_TOOL, args, input_responses=YES, request_state=first.request_state, allow_input_required=True
        )

    result = connect(server, action, elicitation_callback=answering("accept", []))
    assert isinstance(result, types.InputRequiredResult)
    assert other.presses == 0 and icon.presses == 0


def test_approve_and_add_pack_refuse_piped_input(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_human_at_terminal", lambda: False)
    for command in (["approve"], ["add-pack", str(EXAMPLES / "outlook")]):
        result = CliRunner().invoke(cli.main, command, input="y\n" * 5)
        assert result.exit_code == 1
        assert "terminal you type in" in result.output


def test_typed_text_in_a_question_shows_hidden_characters(mail):
    root, parts = mail
    parts["body"].value = "x" * 1000
    state = live.Live()
    session, _ = state.look("Mail")
    pending = state.prepare("Mail", number_of(session, parts["body"]), "pay Bob\u202e001$")
    assert pending.asks
    question = pending.question()
    assert "\u202e" not in question and "\\u202e" in question


@pytest.mark.parametrize("name", [live.ACT_TOOL, live.SAVE_TOOL])
def test_read_only_mode_refuses_acts_and_saves(live_server, monkeypatch, name):
    monkeypatch.setenv("VENT_READ_ONLY", "1")
    server = build_server([live_server[1]])
    result = call(server, name, {"app": "Mail", "element": 0})
    assert result.is_error and "VENT_READ_ONLY" in result.content[0].text


def test_refused_apps_are_refused_for_acts_and_saves_too(mail):
    state = live.Live()
    with pytest.raises(live.LiveError, match="terminal"):
        state.prepare("Terminal", 0, None)
    with pytest.raises(live.LiveError, match="terminal"):
        state.build("Terminal", "x", "y", [0])


def test_allow_high_runs_an_asking_act_without_asking(live_server, mail, monkeypatch):
    monkeypatch.setenv("VENT_ALLOW_HIGH", "1")
    server = build_server([live_server[1]])
    root, parts = mail
    number = next(n.id for n in snapshot(root).nodes if n.element is parts["send"])
    results = run_session(
        server,
        [(live.LOOK_TOOL, {"app": "Mail"}), (live.ACT_TOOL, {"app": "Mail", "element": number})],
        elicitation_callback=never_answered,
    )
    assert not results[1].is_error and parts["send"].presses == 1


def test_a_name_another_app_serves_is_refused_before_asking(live_server, mail):
    """Another app also called Mail already serves mail_show, so saving a
    Mail tool named show would write a tool nobody can call."""
    _, user_dir = live_server
    other = sample_pack()
    other.bundle_id, other.app_name = "com.example.OtherMail", "Mail"
    other.tools[0].name = "show"
    save(other, user_dir / other.bundle_id / "pack.json")
    server = build_server([user_dir])
    root, parts = mail
    number = next(n.id for n in snapshot(root).nodes if n.element is parts["show"])
    asked = []
    results = run_session(
        server,
        [
            (live.LOOK_TOOL, {"app": "Mail"}),
            (live.ACT_TOOL, {"app": "Mail", "element": number}),
            (live.SAVE_TOOL, {"app": "Mail", "name": "show", "description": "Show folders.", "steps": [0]}),
        ],
        elicitation_callback=answering("accept", asked),
    )
    assert results[2].is_error and "another app's tool" in results[2].content[0].text
    assert asked == []
    assert not (user_dir / MAIL).exists()


def test_a_look_waits_out_an_app_that_is_still_launching(mail, monkeypatch):
    """Seen live: TextEdit opened and looked at two seconds later answered
    "cannot complete" (AXError -25204) for a moment."""
    root, _ = mail
    calls = []

    def app_element(running):
        calls.append(1)
        if len(calls) < 3:
            raise ax.AXTransientError("App is not answering accessibility queries (AXError -25204).")
        return root

    monkeypatch.setattr(ax, "app_element", app_element)
    monkeypatch.setattr(live, "LOOK_RETRY_S", 0)
    session, body = live.Live().look("Mail")
    assert "Send" in body and len(calls) == 3


# Lookalikes: found by the app sweep (Freeform's two scroll bars carry
# identical arrow buttons, and their anchors tie).


def twin_scrollbars():
    first = PressRecorder("AXButton", "", subrole="AXIncrementArrow")
    second = PressRecorder("AXButton", "", subrole="AXIncrementArrow")
    window = FakeElement(
        "AXWindow",
        "Board",
        children=[
            FakeElement("AXScrollArea", children=[
                FakeElement("AXScrollBar", children=[first]),
                FakeElement("AXScrollBar", children=[second]),
            ]),
            PressRecorder("AXButton", "Share Board"),
        ],
    )
    return FakeElement("AXApplication", "Board", children=[window]), first, second


def test_anchors_refuse_twins_unless_told_which_one():
    root, first, second = twin_scrollbars()
    node = next(n for n in snapshot(root).nodes if n.element is second)
    anchor = anchors.build(node)
    with pytest.raises(anchors.AnchorAmbiguous):
        anchors.locate(root, anchor)
    found = anchors.locate(root, anchor, prefer=second)
    assert found.element is second and found.tied
    # A preference that isn't among the tied candidates changes nothing.
    with pytest.raises(anchors.AnchorAmbiguous):
        anchors.locate(root, anchor, prefer=root)


def test_a_live_act_on_a_twin_presses_that_one_but_cannot_be_saved(monkeypatch):
    root, first, second = twin_scrollbars()
    app = ax.RunningApp("Board", "com.example.Board", 9)
    monkeypatch.setattr(ax, "find_app", lambda name: app)
    monkeypatch.setattr(ax, "find_app_by_bundle", lambda bundle_id: app)
    monkeypatch.setattr(ax, "app_element", lambda running: root)
    monkeypatch.setattr(ax, "app_version", lambda running: "1.0")
    monkeypatch.setattr(ax, "activate", lambda running: True)
    monkeypatch.setattr(runtime, "_wait_frontmost", lambda *args: None)
    monkeypatch.setattr(runtime, "SETTLE_NO_REACTION_S", 0.01)
    state = live.Live()
    session, _ = state.look("Board")
    pending = state.prepare("Board", number_of(session, second), None)
    message = state.run(pending)
    assert second.presses == 1 and first.presses == 0
    assert "can't be part of a saved tool" in message
    share = state.prepare("Board", number_of(session, root._children[0]._children[1]), None)
    state.run(share)
    with pytest.raises(live.LiveError, match="looks just like another one"):
        state.build("Board", "scroll_down", "Scroll down.", [0, 1])
    assert state.build("Board", "share", "Share the board.", [1]).spec.steps[0].op == "press"


def test_a_slow_app_gets_a_partial_look_that_says_so(mail, monkeypatch):
    """Found by the app sweep: Finder's file list took 22 s for 400
    elements, so a look now stops at a time budget and says so."""
    state = live.Live()
    session, _ = state.look("Mail")
    assert not session.out_of_time
    session.out_of_time = True  # as if the budget ran out partway
    assert "only part of it" in state.look_header(session)
    monkeypatch.setattr(live, "LOOK_BUDGET_S", 0)
    session, _ = state.look("Mail")
    assert session.out_of_time and not session.nodes
    assert "got nothing" in state.look_header(session)


def test_an_app_that_never_answers_gets_a_plain_answer(mail, monkeypatch):
    def app_element(running):
        raise ax.AXTransientError("App is not answering accessibility queries (AXError -25204).")

    monkeypatch.setattr(ax, "app_element", app_element)
    monkeypatch.setattr(live, "LOOK_RETRY_S", 0)
    with pytest.raises(live.LiveError, match="look again"):
        live.Live().look("Mail")


def test_the_look_budget_stops_inside_a_wide_list(monkeypatch):
    """Review finding: the budget was checked only per element, so a
    400-row list read every remaining row's attributes after time ran out."""
    reads = []

    class Row(FakeElement):
        @property
        def label(self):
            reads.append(1)
            return "row"

        @label.setter
        def label(self, value):
            pass

    table = FakeElement("AXTable", children=[Row("AXRow") for _ in range(400)])
    root = FakeElement("AXApplication", "Files", children=[FakeElement("AXWindow", "Files", children=[table])])
    from vent import snapshot as snapshot_mod

    clock = iter([0.0] * 6 + [100.0] * 10_000)
    monkeypatch.setattr(snapshot_mod.time, "monotonic", lambda: next(clock))
    snap = snapshot_mod.snapshot(root, budget_s=1.0)
    assert snap.out_of_time
    assert len(reads) < 20


def test_retries_share_one_deadline(mail, monkeypatch):
    calls = []

    def app_element(running):
        calls.append(1)
        raise ax.AXTransientError("App is not answering accessibility queries (AXError -25204).")

    now = [0.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(live.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(ax, "app_element", app_element)
    with pytest.raises(live.LiveError, match=r"hasn't for \d+ s"):
        live.Live().look("Mail")
    assert now[0] <= live.LOOK_WAIT_S and len(calls) <= live.LOOK_RETRIES + 1


def test_prefer_never_rescues_a_candidate_below_the_threshold(monkeypatch):
    """The tie-break only chooses among candidates that pass on their own.
    Here a window's close button ties with a labelled button deeper in,
    and the threshold sits between their scores."""
    close = PressRecorder("AXButton", "", subrole="AXCloseButton")
    inner = PressRecorder("AXButton", "Go")
    window = FakeElement("AXWindow", "", children=[close, FakeElement("AXSplitGroup", children=[inner])])
    root = FakeElement("AXApplication", "Board", children=[window])
    anchor = anchors.build(next(n for n in snapshot(root).nodes if n.element is close))
    scores = {}
    for element, chain, _ in anchors._walk_with_chains(root, 50):
        score, _ = anchors._score(anchor, chain)
        if element in (close, inner):
            scores[element] = score
    assert 0 < scores[close] - scores[inner] < anchors.AMBIGUITY_MARGIN, scores
    monkeypatch.setattr(anchors, "ACCEPT_THRESHOLD", (scores[close] + scores[inner]) / 2)
    with pytest.raises(anchors.AnchorAmbiguous):
        anchors.locate(root, anchor, prefer=inner)


def test_a_tie_the_focused_window_settles_stays_replayable():
    """Review finding: prefer used to win before the focused-window
    settle, marking steps a saved tool could replay as unsaveable."""
    def board(name):
        button = PressRecorder("AXButton", "Share")
        return FakeElement("AXWindow", "", children=[button]), button

    back, back_button = board("back")
    front, front_button = board("front")
    root = FakeElement("AXApplication", "Board", children=[back, front])
    root.focused_window = front
    node = next(n for n in snapshot(root).nodes if n.element is front_button)
    anchor = anchors.build(node)
    found = anchors.locate(root, anchor, prefer=front_button)
    assert found.element is front_button and not found.tied
    found = anchors.locate(root, anchor, prefer=back_button)
    assert found.element is back_button and found.tied


def test_an_empty_look_on_a_locked_screen_says_so(mail, monkeypatch):
    root, _ = mail
    root._children = []  # what a locked screen looks like to accessibility
    monkeypatch.setattr(ax, "session_locked", lambda: True)
    state = live.Live()
    session, _ = state.look("Mail")
    assert "screen is locked" in state.look_header(session)


# Readiness: what vent doctor <App> reports for live mode.


def test_readiness_counts_twins_as_live_only():
    root, first, second = twin_scrollbars()
    snap = snapshot(root)
    ready = live.readiness(root, snap, look_s=0.1)
    assert ready.pressable == 3 and ready.typeable == 0
    assert ready.live_only == 2 and ready.replays == 1 and ready.lost == 0
    assert ready.asks_unlabelled == 2 and ready.asks_risky_word == 1  # "Share Board" says share


def test_doctor_reports_live_readiness_without_app_content(monkeypatch):
    from vent import harness

    root, parts = mail_like()
    window = root._children[0]
    window.title = window.label = "Re: salary review"  # private, must not reach the report
    app = ax.RunningApp("Mail", MAIL, 42)
    monkeypatch.setattr(cli.ax, "is_trusted", lambda: True)
    monkeypatch.setattr(cli.ax, "find_app", lambda name: app)
    monkeypatch.setattr(cli.ax, "app_element", lambda running: root)
    monkeypatch.setattr(cli.ax, "app_version", lambda running: "16.0")
    monkeypatch.setattr(harness, "enable_web_accessibility", lambda root: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    result = CliRunner().invoke(cli.main, ["doctor", "Mail", "--report"])
    assert result.exit_code == 0, result.output
    assert "Live mode" in result.output
    assert "presses would ask you first" in result.output
    row = next(line for line in result.output.splitlines() if line.startswith("| Mail |"))
    assert "Mail 16.0, macOS" in row
    for private in ("salary", "Send", "Search", "Show Folders"):
        assert private not in row


def test_doctor_says_live_mode_refuses_terminals(monkeypatch):
    from vent import harness
    from .fakes import textedit_like

    monkeypatch.setattr(cli.ax, "is_trusted", lambda: True)
    monkeypatch.setattr(cli.ax, "find_app", lambda name: ax.RunningApp("Terminal", "com.apple.Terminal", 7))
    monkeypatch.setattr(cli.ax, "app_element", lambda running: textedit_like())
    monkeypatch.setattr(harness, "enable_web_accessibility", lambda root: None)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    result = CliRunner().invoke(cli.main, ["doctor", "Terminal"])
    assert "Live mode won't drive Terminal" in result.output


def test_a_look_shows_unlabelled_text_that_has_a_value():
    """Found pressing buttons in Calculator: its result is an unlabelled
    static text with no actions, and the look left it out."""
    window = FakeElement(
        "AXWindow",
        "Calculator",
        children=[
            FakeElement("AXGroup", children=[
                FakeElement("AXScrollArea", "Edit field", children=[FakeElement("AXStaticText", value="5")]),
                FakeElement("AXStaticText", value=""),
            ]),
            PressRecorder("AXButton", "Equals"),
        ],
    )
    root = FakeElement("AXApplication", "Calculator", children=[window])
    texts = [n for n in snapshot(root).nodes if n.role == "AXStaticText"]
    assert [n.value_preview for n in texts] == ["5"]


def test_a_saved_tool_still_works_when_the_window_title_changes(mail):
    """Found pressing buttons in Dictionary: its window title becomes the
    word looked up, and a saved step that recorded the old title refused
    to replay. Saved tools leave window titles out, like bundled packs."""
    root, parts = mail
    state = live.Live()
    session, _ = state.look("Mail")
    state.run(state.prepare("Mail", number_of(session, parts["search"]), "receipts"))
    draft = state.build(
        "Mail", "search", "Search mail.", [0], [{"name": "query", "description": "What to find.", "step": 0}]
    )
    assert all(step.anchor.window_title == "" for step in draft.spec.steps)
    assert "Inbox" not in draft.question(replaces=False)
    window = root._children[0]
    window.title = window.label = "Search results"
    result = runtime.execute(draft.pack(), "search", {"query": "invoices"}, root)
    assert result.ok and parts["search"].value == "invoices"


def test_previews_drop_invisible_direction_marks():
    """Calculator shows 5 as U+200E 5, which a look printed as \\u200e5."""
    window = FakeElement("AXWindow", "Calculator", children=[FakeElement("AXStaticText", value="‎5")])
    root = FakeElement("AXApplication", "Calculator", children=[window])
    assert [n.value_preview for n in snapshot(root).nodes if n.role == "AXStaticText"] == ["5"]
