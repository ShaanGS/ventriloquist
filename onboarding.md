# Setting up Ventriloquist

You're setting vent up with the user, once. Check what's already in place
before asking anything, and stop only for the steps that need their
hands. Ask before you install anything or edit a config file.

## 0. Look first

- `uname -s` has to print `Darwin`. vent is Mac only. On anything else,
  say so and stop.
- `vent --version` tells you whether vent is installed, and
  `uv --version` whether uv is.

## 1. Install

```bash
uv tool install ventriloquist-mcp
```

If vent is already installed, `uv tool upgrade ventriloquist-mcp` instead.
Without uv, `brew install uv` first. If the shell then says
`vent: command not found`, run `uv tool update-shell` and use
`~/.local/bin/vent` for the rest of this session.

## 2. The Accessibility permission

Run `vent doctor`. If it says the permission is missing, it names the app
that needs it, usually the terminal or app you're running in. Tell the
user to open System Settings > Privacy & Security > Accessibility, switch
that app on (with the + button if it isn't listed), and then quit and
reopen it. Tell them in one line that this permission lets that app
control other apps, which is how vent works.

If that app is the one you're running in, reopening it ends this session.
Say so before they do it, and tell them to paste the setup prompt again
afterwards. `vent doctor` will pass, and you carry on from step 3.

## 3. A first run the user can watch

Use a scratch file, never one of their documents:

```bash
printf 'hello\n' > /tmp/vent-demo.txt && open -a TextEdit /tmp/vent-demo.txt
vent run TextEdit write_document --arg text="typed by vent"
vent run TextEdit read_document
```

Tell them what happened: the document changed with no keystrokes and no
screenshot, and vent read the text back to check it.

## 4. Connect vent to their agent app

Ask which app they use. `vent mcp-config` prints the entry with the right
path for this Mac. Back up a config file before you change it, and show
the user the change.

- **Claude Code**: `claude mcp add --scope user ventriloquist -- "$(command -v vent)" serve`
- **Cursor**: merge the entry into `~/.cursor/mcp.json`.
- **Codex CLI**: add this to `~/.codex/config.toml`, with the path from
  `vent mcp-config`:

  ```toml
  [mcp_servers.ventriloquist]
  command = "/Users/<them>/.local/bin/vent"
  args = ["serve"]
  ```

- **Claude Desktop**: the user quits Claude first, because it rewrites
  its config while running. Then merge the entry into
  `~/Library/Application Support/Claude/claude_desktop_config.json`.
  Claude Desktop checks vent's Python for Accessibility, not Claude
  itself: `vent mcp-config` prints that file's path, and the user drags it
  from Finder into the Accessibility list.

Ask the user whether to turn on live mode, in one line: it lets you use
any app that has no vent tool yet, asks them before anything that looks
like sending, deleting or paying, and can save what worked as a tool once
they approve. Live mode needs vent 0.5.0 or later (`vent --version`). If yes, add
`VENT_LIVE=1` to the server's environment:
`vent mcp-config --live` prints the entry with it, and for Claude Code add
`-e VENT_LIVE=1` before `ventriloquist` in the command above.

The permission belongs to whichever app starts vent: the terminal for
Claude Code and Codex, Cursor itself for Cursor. Then the user restarts
that app, and the tools show up with names like
`textedit_write_document`.

## 5. Optional: keep this skill

For Claude Code, ask first, then:

```bash
mkdir -p ~/.claude/skills/ventriloquist && vent skill > ~/.claude/skills/ventriloquist/SKILL.md
```

Other agents can keep the output of `vent skill` wherever they keep
instructions. `vent skill` arrived after 0.4.0. If this vent says there is
no such command, fetch the same file instead:
`curl -fsSL https://raw.githubusercontent.com/ShaanGS/ventriloquist/main/SKILL.md`

## 6. What next

Ask which Mac app they want vent to use. If it's TextEdit, Notes, VS Code
or Slack, show them the tools that ship (the skill lists them). For any
other app, run `vent doctor <App>`. With live mode on, do a small task in
it with the live tools and offer to save it. Otherwise follow "Making a
tool for another app" in the skill.
