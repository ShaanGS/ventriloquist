# Ventriloquist plugin for Claude Code and OpenClaw

One folder that both apps install. It starts vent's MCP server through
`uvx` and adds the `ventriloquist` skill, which tells the agent how to use
vent's tools and how to make new ones.

It turns on live mode (`VENT_LIVE=1`): the agent can look at any running
Mac app through its accessibility tree, press and type in it, and save
steps that worked as a tool once you approve them. Presses that look like
sending, deleting or paying, and buttons with no label, ask you first.
Password fields, sign-in windows, terminals, password managers and System
Settings are refused. To serve only the packs' tools, set up vent with
`vent mcp-config` instead of this plugin.

You need macOS and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

## Claude Code

```bash
claude plugin marketplace add ShaanGS/ventriloquist
claude plugin install ventriloquist@ventriloquist
```

Then give your terminal the Accessibility permission (System Settings >
Privacy & Security > Accessibility) and restart it. Claude Code can ask
you to confirm a step, so live mode's questions show up in the session.

## OpenClaw

From a clone of the repo, on the Mac that runs the Gateway:

```bash
openclaw plugins install ./integrations/claude-plugin
```

The permission belongs to the app that runs the Gateway. Run
`uvx --from ventriloquist-mcp vent doctor` from the same place to see
which one. If your OpenClaw setup can't ask you to confirm a step, live
mode refuses the steps that would ask, and a tool to save waits for you
to run `uvx --from ventriloquist-mcp vent approve` in a terminal.

## What you get

- The bundled tools for TextEdit, Notes, VS Code and Slack, named like
  `textedit_write_document`.
- `vent_live_look`, `vent_live_act` and `vent_live_save` for every other
  app.
- Your own packs from `~/.ventriloquist/packs`, including tools you saved
  in live mode.

The full docs are in the [main README](../../README.md).
