# Security

## Reporting a vulnerability

Please report security problems privately. Open this repository's
[Security tab](https://github.com/ShaanGS/ventriloquist/security) on
GitHub and choose **Report a vulnerability**. Only the maintainer can see
the report, so a fix can be ready before the problem is public. Please
don't open a public issue for a security problem.

## Threat model

Ventriloquist drives real apps on a real Mac, with real data in them. This
document lists what can go wrong and what the code does about it. It was
red-teamed before any code existed and rewritten from the findings (the
review proved two of the first draft's claims false), and checked against
the code again for 0.3.0. Anything not built yet is marked **Planned**.

### Assets

1. Your data inside apps: documents, messages, files, accounts.
2. Your machine's state: nothing destructive runs unattended.
3. Your privacy: accessibility trees are full of personal information.
4. Your credentials: vent must never read or type a password.

### T1. Generated code execution

Agent frameworks that `exec()` model output hand the model a shell. vent's
action space is a closed set of typed ops that its own runtime carries
out: `press`, `set_value`, `pick`, `reveal`, `raise_window`, `open_app`,
`wait_for` and `read_value`. None of them runs code or AppleScript, or
builds a command out of text. `open_app` runs macOS's `open -b` with the
pack's bundle id as a fixed argument, never through a shell, and with its
input and output pointed away from the MCP connection.

`read_value` is the one op that moves data out. It returns an element's
live value or, for an element with no value of its own such as a web view,
the text inside it (up to 20,000 characters). It changes nothing, but it
is how app content reaches an MCP client, so T6 and T12 apply to it, and
it never reads a password field (T4).

`vent/packs.py` rejects an unknown op when a pack loads, and model output
is checked against a JSON schema and rejected on any mismatch, never
repaired. Adding an op changes this threat model, so this document is
updated first.

### T2. Destructive writes without a destructive verb

`set_value` overwrites a field with no button pressed and no verb for a
blocklist to catch. vent's own first demo emptied and rewrote a document
this way. So these rules look at the op, not the label:

- While exploring, the explorer only presses and writes, and it writes
  only into a text field it has read as empty. A field that doesn't
  answer the read counts as unknown, not empty, and isn't touched. The
  field's previous value is put back right after the probe, even if the
  app stops answering in between. If the restore fails, the probe is
  marked destructive in the trace, so neither the compiler nor you treat
  it as clean.
- When compiling, each tool's risk comes from what its recorded steps did.
  A step the policy classed as destructive, or a press on a label with a
  destructive verb, makes the tool `high`. Any write, press or cumulative
  action makes it at least `mutating`. You can raise a tool's risk by
  editing its pack.
- **Planned:** the approval gate saying what a tool does as a whole, in
  words ("this replaces all the text in the document"). Today the gate
  lists each step, and you have to see that a write into a document's
  text area replaces what's there.

### T3. Destructive actions while exploring

Layered, because no single layer is enough:

- An app is explored only when you name it: `vent explore <App>`.
- Every probe the model suggests goes through `vent/policy.py` before it
  runs. A label with a destructive or committing verb is refused: delete,
  remove, send, pay, buy, submit, publish, share, archive, quit, close,
  accept, join, install, sign out, don't save and about 80 more, kept as a
  plain list (`DESTRUCTIVE_VERBS`) so it's easy to audit.
- Labels and window titles are compared the way they render: Unicode
  look-alike forms are normalized, invisible characters dropped, and
  accents and case folded, so a Delete in fullwidth letters or split by a
  zero-width space still reads as delete, and "Log-Out" matches log out.
- The list is incomplete, so its known gaps get rules of their own:
  - The verb lists are English, so the explorer reads the app's menu bar
    first and refuses to probe an app whose menus aren't in English. That
    check goes by the menus, so a single label in another language inside
    an English app can still pass.
  - An element with no label is refused, and so is a label that isn't
    wholly in Latin script, including one that mixes in a look-alike
    letter from another script, like a Cyrillic е inside Delete.
  - Nothing inside a sheet or dialog is probed: its buttons answer
    whatever it asks.
  - Verbs that are only sometimes risky (clear, reset, replace,
    overwrite, cancel, stop, hide) are judged with the rest of the label
    and the window title: "Clear Formatting" passes, "Clear All History"
    doesn't.
  - A plain confirm button (OK, Yes, Continue, Apply) is refused when the
    window's title has a destructive or risky verb in it, like OK in a
    "Delete Report?" dialog.
  - Nothing in a window whose title mentions a password, login, log in,
    sign in, unlock or authenticate is probed.
  - Probes are classed as reversible, cumulative or destructive.
    Cumulative ones (New Note, Add, Duplicate) are capped at three per
    session, so exploring can't leave forty empty notes synced to iCloud.
- Dialog watchdog: when a probe opens a sheet or dialog, the explorer
  presses the button the dialog itself names as its cancel button, and it
  never touches the element that opened it again that session. It presses
  nothing else: OK, Yes and No answer whatever the dialog asks. A dialog
  with no cancel button stops exploring until you close it.
- **Planned:** replaying each proposed tool once before you approve it,
  against a scratch document. Today `vent compile` replays nothing; the
  approval gate shows the recorded steps and you decide.
- The last gate is a person, and what it shows is ground truth (T5), not
  just the model's own account.

### T4. Credentials

Password fields, the ones macOS marks `AXSecureTextField`, are never
read, never written, never sent to a model and never a valid target:

- Snapshots leave them out, so neither the explorer nor its model sees
  one.
- Anchor resolution skips them, so a tool can't land on one, and a read
  that gathers the text inside a container skips them too.
- A pack with an anchor on one fails to load.

This trusts the app to mark its password fields. A custom-drawn password
field that reports itself as a plain text field isn't caught. Exploring
and healing refuse anything in a window whose title mentions a password,
login, sign in, unlock or authenticate; at run time only the secure-field
mark counts. **Planned:** treating a field that shows only bullet
characters as a password field. vent never asks for a password, and Sign
and Submit are on the destructive list.

### T5. Prompt injection through the app's interface

Anything an app shows ends up as text in a snapshot, so a malicious web
page or message can address instructions to the explorer's model. The
first draft of this document said an injection "can waste a session but
cannot mint a destructive tool call". Review proved that false: a
poisoned model can propose a tool named `save_document` whose steps press
Send in Mail, and an approval gate showing only the model's name and
description would wave it through. So containment has these layers:

- Model output is held to a JSON schema, the action space is closed, and
  the policy screens every probe after the model, where the model can't
  skip it.
- App text in a prompt is wrapped in delimiters that carry a fresh random
  nonce and say it is untrusted app content. An app can't forge the
  closing tag, and the prompt tells the model which text is data instead
  of leaving it to guess.
- The compiler builds tools only from probes that actually ran. The model
  groups and names them; it can't add a step.
- Tool names and descriptions the model writes have control characters
  and bidirectional overrides removed and are cut to a fixed length before
  the gate shows them, so they can't fake structure or hide text.
- The gate prints every step from the steps themselves, not from the model
  (app, window, element role and label, op), next to the model's
  description. The steps are the ground truth; the description is
  marketing. The gate warns when a tool's steps act in a window its
  description never mentions, and flags high-risk tools in red.

### T6. Personal data leaving the machine

What reaches a model is app text from the accessibility tree: snapshots
while exploring and healing, and the list of probes that ran while
compiling. Password fields are never in it, and other field values are
cut to 80-character previews. `vent explore --no-values` replaces every
field value with a length marker (`<N chars redacted>`), so exploring
sends no field contents; element structure and labels still go. Healing
can redact the same way: `--no-values` on `vent run --heal`, and
`VENT_NO_VALUES=1` for `vent serve`. Without it, exploring sends up to 80
characters of each field it sees (a note body, a draft), so `--no-values`
is the switch for an app holding sensitive text. The cut is for privacy,
not a defense against injection: a short payload survives it, and T5
handles that.

Traces and packs stay on your disk, and traces, which hold an app's
labels and window titles, are readable by your user only. There's no
telemetry. Every network call lives in one file, `vent/llm.py`, including
the fallback that runs the Claude Code CLI when there's no API key: a
different route to the same model, started from the same file, carrying
the same text and nothing else. That call runs `claude -p` in print mode
with its tools turned off and with `--safe-mode --strict-mcp-config
--setting-sources "" --no-session-persistence`, in a fresh empty
temporary directory, so none of your MCP servers, hooks, CLAUDE.md files,
memory or settings come into it and no session is saved. A CLI too old
for those flags is treated as unavailable, never run without them.

Probing is reversible on the local field, not across a network the app
owns. The explorer writes a short probe string into an empty field and
puts the old value back at once, but an app that sends every keystroke
(Notes syncing to iCloud, a shared document, search as you type) may have
sent the string before it was removed.

### T7. Poisoned or stale packs

Packs are JSON on disk. `vent/packs.py` validates each one when it loads:
the format version, the op set, risk levels, verify and precondition
kinds, that every parameter a step uses is declared, and that no anchor
is on a password field. A tampered pack is held to the same closed action
space as an honest one. Review a pack someone else shares the way you'd
review code; the format is readable so that you can.

A pack that fails to load, or a tool whose name collides with another
pack's, is skipped with a warning, and the rest keep serving. If your own
pack for an app fails to load, the bundled pack for that app doesn't
quietly take its place.

Staleness is the more common problem: the app updated overnight, and the
pack is now wrong about it. A pack records the app version and the macOS
version it was recorded on. When the app version, or macOS's major.minor
version, no longer matches, the pack's anchors load as low-confidence:
the resolver raises the score a match needs, so a stale pack fails toward
a refusal or the healer, not toward a plausible wrong element. Locale is
recorded but not compared.

`vent verify <App>` dry-resolves every anchor in a pack, steps and checks
alike, without acting, and says how many still resolve. It calls no model.
With `--report`, it writes a small JSON table meant to be run on another
Mac and sent back; the only app content in it is each resolved element's
live label.

### T8. The healer rewriting packs

The healer exists to fix broken anchors, which means it can re-point
them, which means a hostile app state could steer a harmless tool's
anchor onto a dangerous element. And "the replay worked" is no safety
check, because destructive replays work too. The fences, in layers:

- Healing asks a model only when the caller turns it on: `VENT_HEAL=1`
  for `vent serve`, `--heal` for `vent run`. With it off, serving never
  calls a model.
- It looks only at the anchor's own window: the one whose title the
  anchor pinned, the focused window for an anchor with no title, or the
  menu bar for a menu command. If that window isn't open, it refuses. It
  can keep field values from the model (T6).
- Given a cut-off snapshot, it refuses rather than guess at the nearest
  lookalike; it can't choose in a window it can't fully see.
- A fix must land on the same kind of control (the same role), and in
  the same kind of place: a step recorded in a sheet only heals onto an
  element in one, and an anchor with no window title only heals into
  another with none. A step that pressed a button can't heal onto a text
  field, or the other way round.
- Every fix is screened by the policy against its new target: destructive
  and context-dependent verbs, labels that mix scripts, confirm buttons in
  windows with a destructive title, login windows and password fields.
- Matching a fix to the anchor it replaces includes the anchor's chain of
  ancestors, so two identical unlabeled twins in one window are never
  confused.
- Saving a fix to the quarantine reads the pack from disk first, so it
  never undoes what `vent compile` or `vent verify` saved in the
  meantime.

Fixes are never promoted on their own. A fix goes into the pack's
quarantine (`healed_pending`), and only `vent verify` moves it into a
tool's anchor of record, after showing you the target window and the risk
of every tool it would rewrite. A quarantined fix can still rescue a live
call while it waits: on a repeat break, the runtime reuses a matching fix
with no model call and screens the live target again every time. So an
unpromoted fix changes what the calls it rescues do, but it never becomes
the anchor of record, and it never skips the per-call screen.

### T9. The MCP client side

After a tool is approved, no person sits between an MCP client and a
call, and clients are often models themselves. So approval at compile
time is not taken as consent for every call. Each tool carries a risk
level, `read_only`, `mutating` or `high`, set at the approval gate or by
the pack's author, and the server acts on it:

- `read_only` tools run freely.
- `mutating` tools run without asking each time. The approval gate is
  the control: you approved the tool, its steps and its risk.
- `high` tools are hidden from the client and refused, unless the server
  was started with `VENT_ALLOW_HIGH=1`, in which case they run without
  asking, or the client supports MCP elicitation, in which case every call
  asks you first and runs only if you accept. Tools that pipe free text
  into chat, mail or a terminal are injection tools in their own right,
  and belong here.
- `VENT_READ_ONLY=1` hides and refuses every tool that isn't `read_only`.

A call that fails says what it already did. Once a press, write or pick
has gone out, a later error in the same call names it, so a client
doesn't take the failure to mean nothing happened and try again. The call
also presses the cancel button of any sheet or dialog that opened while
it ran (ones open before it are left alone), so the next call doesn't
meet a half-finished dialog. A tool that needs its app in front fails
without running a step if the app doesn't come forward.

Tools also describe themselves to clients: `read_only` tools carry
`readOnlyHint`, `high` ones `destructiveHint`, and every tool has a title
written for people and an ASCII name of at most 128 characters. The hints
help a client decide what to ask you about; the server enforces the rules
above whatever the client does.

The server speaks stdio only, started by the client. There's no network
port and nothing on the LAN. If an HTTP transport is ever added, it must
bind 127.0.0.1 with a bearer token, and this document changes first.

### T10. Blast radius of the Accessibility grant

macOS grants Accessibility to the host process, not per target app and
not per pack. Once your terminal is trusted, anything running in it can
drive every app on the system. vent's own scoping (named apps, a policy,
risk levels) is self-discipline inside that grant, not an OS boundary.
This document says so plainly rather than imply otherwise. In practice:
grant the permission to a terminal profile you keep for vent, review
packs before serving them, and treat `vent serve` as handing the
connected client exactly what the approved packs describe, unattended.

Claude Desktop moves the grant somewhere else. It starts each MCP server
through a helper that makes the server responsible for itself, so macOS
checks the program the helper ran: vent's Python interpreter for the
entry `vent mcp-config` prints, or uv when the config runs vent through
`uvx`. The grant then belongs to that binary, and other programs can
share it: anything that runs on the same interpreter as its own process,
such as another MCP server Claude Desktop starts on that Python, gets the
same reach. vent's "not trusted" message names the exact binary, so you
grant that file rather than something broader, and the README steers
Claude Desktop users to the interpreter over uv, since less runs on it.

### T11. Semantic drift under a stable anchor

Anchor durability and semantic safety pull in opposite directions, and
the resolver is built for the first. An anchor is designed to keep
resolving through relabels, moves and rebuilds, and that same strength
means an app update can change what a control does while the anchor
still binds confidently. A tool named `archive_note` keeps working after
the button under it becomes Delete, and every durability number in
[BENCHMARKS.md](BENCHMARKS.md) measures exactly the property that makes
this possible.

Anchors record every label they have been seen with. At run time, a
press, write or pick refuses an element wearing a label its anchor has
never been recorded under, and names the element it declined.
`vent verify` lists the same resolutions as drift suspects for a person
to re-confirm. The trigger is the label mismatch itself, not an app
version change: Electron apps relabel from their servers and behind
feature flags with no version bump, so gating on versions would miss the
apps most likely to drift. The risk left over is a control whose meaning
changed while its label didn't. No anchor-side check can see that, which
is one more reason risk levels and the high-risk gate exist.

### T12. Read tools feeding the client's model

This section was written before read tools shipped, and they have now
(`textedit_read_document`, `notes_read_open_note`, and
`outlook_read_selected_message` in the example pack). A tool that reads
changes the trust picture. App content is text an attacker can
influence, and a read tool hands it straight to the client's model, which
also holds this server's tools that write. vent's own model is out of the
loop here: the injection doesn't need it, because the client brings its
own.

The client's defenses aren't vent's to assume. The server wraps every
value a read returns as untrusted app content, with the same
nonce-delimited wrapper `llm.py` uses for its own prompts (it lives in
`untrusted.py`, so the server uses it without importing `llm.py`). App
text quoted in an error message, such as a window title or a label, is
wrapped the same way. Every tool with a read step says in its description
that its output is app data, not instructions. A well-behaved client can
quarantine it. This doesn't make injection impossible; it makes the
boundary visible to the party that can act on it. Tests pin both,
including that app text can't close the wrapper early.

## Standing rules for contributors

- A new step op needs this document updated and a policy review first.
- `runtime.py` and `server.py` don't import `llm.py`. The server reaches
  a model only through the healer, and only with `VENT_HEAL=1`.
- Healed anchors are never promoted automatically. No exceptions for
  "obvious" fixes.
- No screenshots, no pixel coordinates, no `exec`, no AppleScript built
  from model output. These absences are load-bearing.
