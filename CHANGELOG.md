# Changelog

The notes for every release are on
[GitHub Releases](https://github.com/ShaanGS/ventriloquist/releases). This
file lists what has changed since the last one.

## 0.3.1 (2026-10-28)

Reads that work on apps like WhatsApp, and a WhatsApp example pack.

- Reading a container now includes the text an app keeps in labels: a
  static text, button, heading or link with no children gives its label,
  then its value if it has one. WhatsApp keeps its messages and chat list
  this way, and reads of them used to come back nearly empty. Each such
  leaf is one line, so a list reads one row per line.
- Reads drop invisible bidi marks, embeddings, overrides and isolates,
  which WhatsApp puts before almost every word.
- A read recorded on an element with an identifier refuses when that
  element is gone, instead of returning the text of a look-alike without
  one. With no chat open, WhatsApp's chat-name read could otherwise have
  returned a sidebar button's label.
- A step refused because of a minimized window that reports itself as a
  dialog, as WhatsApp's does, now says the window is minimized.
- An example WhatsApp pack in `examples/whatsapp`: read the chat list and
  the open chat, leave a draft, move between chats, mark one unread or
  archive it. It has no tool that sends.

## 0.3.0 (2026-10-26)

Safety fixes from a review before the launch, and a gate that asks you
before a high-risk tool runs.

Running tools:

- A step refuses while its window has a sheet open or the app shows a
  modal dialog, unless the step was recorded inside it. The old check read
  an attribute macOS doesn't have, so it never fired.
- A tool recorded in a window with a title never acts in a window with a
  different title. Tools recorded with no title still act on the window
  in front, like the bundled TextEdit tools.
- Password fields are never read or written, and a pack that targets one
  fails to load.
- A failed call cancels any sheet or dialog it opened, and its error says
  which presses or writes had already gone out. A tool that needs its app
  in front fails, without running a step, if the app doesn't come forward.
- Calls to one app run one at a time, tools that need the front take
  turns across apps, and a call cancelled while it waits never runs.

Serving:

- High-risk tools are hidden unless the client can ask you to confirm
  each call (MCP elicitation, #4) or the server runs with `VENT_ALLOW_HIGH=1`.
  `VENT_READ_ONLY=1` serves read-only tools only.
- Tools carry `readOnlyHint` (read-only tools) or `destructiveHint` (all
  others) and a title, and their
  names are ASCII, at most 128 characters.
- App text in error messages is marked as untrusted data, like read
  values.
- A broken pack or a tool name collision is skipped with a warning while
  the other tools keep serving, and your own pack failing to load no
  longer brings back the bundled one in its place.
- A pack counts as stale when macOS's major.minor version changes, not on
  every point release.
- `vent serve` runs MCP on a thread of its own and keeps AppKit's run loop
  turning on its main thread, so apps opened after it started are seen
  without stalling the client.
- `ventriloquist-mcp --version` works.
- Requires `mcp` 2.3 or later.

Exploring, compiling and healing:

- The explorer refuses to probe an app whose menus aren't in English,
  since the safety policy's verb lists are English. Labels are compared
  the way they render, a label mixing scripts is refused, the verb list
  is longer, and nothing inside a sheet or dialog is probed.
- The explorer's dialog watchdog presses only a dialog's own cancel
  button, a field that doesn't answer a read is no longer taken for an
  empty one, and probe text comes out of a field however the probe ends.
- Healing (`VENT_HEAL=1`, or `vent run --heal`) looks only in the broken
  anchor's own window, refuses when it isn't open, can keep field values
  from the model (`--no-values`, or `VENT_NO_VALUES=1` for the server),
  and no longer overwrites changes saved to the pack since the server
  started.
- The Claude Code CLI fallback runs outside your own Claude Code setup:
  no MCP servers, hooks, CLAUDE.md, memory, settings or saved session.
- `vent compile` adds approved tools to the app's existing pack instead
  of replacing it (#3), and asks before a second tool of the same name in
  one run replaces the first.
- The approval gate shows every literal value a tool writes and every
  argument with its description. Writes the compiler can't tie to an
  argument are dropped instead of becoming a write of an empty string, and
  argument names and descriptions written by the model are checked,
  cleaned and capped.
- Explore traces are readable by your user only.

Packs and docs:

- The bundled Slack pack no longer contains the name of the Slack
  workspace it was recorded in, and the VS Code pack no longer contains
  the title of the tab that was open. Two descriptions now name other
  tools the way clients see them.
- `vent run` and tool results say "1 step" rather than "1 steps".
- Packs are checked more strictly when they load: standard accessibility
  press actions only, step timeouts between 0 and 120 seconds, and bundle
  ids that are plain reverse-DNS names.
- `vent anchor` and `vent act` take `--max-nodes`, like `vent inspect`, for
  apps with big trees.
- SECURITY.md matches the code, with what isn't built marked Planned, and
  security problems can be reported privately.
- Releases run the test suite against the built wheel before publishing,
  and publish `server.json` to the MCP registry.
