# Ventriloquist

**Compile any Mac app into an MCP server.**

Every computer-use agent today is an interpreter: a language model stares at
your screen and drives every click, every time. Slow, expensive, flaky.
Ventriloquist is a compiler. Point it at a running app and an agent explores
the app's accessibility tree once. Out comes a persistent MCP server with
typed, semantic tools like `write_document(text)`, each backed by a
deterministic accessibility replay that runs with zero model calls. The model
is only re-engaged when a replay breaks, and its fix is quarantined until you
approve it.

No screenshots. No vision models. No pixel coordinates. The accessibility
layer has shipped with every Mac app for decades. Screen readers proved it
works; Ventriloquist makes it programmable.

## Status

Early and honest about it: the deterministic half is real today, the agentic
half is next. Working now: accessibility introspection, scored anchor
resolution, the pack format, the replay runtime, and an MCP server. One
hand-written pack ships (TextEdit, two tools). The explorer that writes packs
for you, the healer, and the durability harness are the current roadmap, in
that order.

## Quick start

Requires macOS and Python 3.11 or newer.

```bash
git clone <this repo> && cd Panther
python3 -m venv .venv && .venv/bin/pip install -e .

.venv/bin/vent doctor            # check Accessibility permission
.venv/bin/vent apps              # list running apps
.venv/bin/vent inspect Notes     # semantic snapshot of a live app
```

`vent` needs Accessibility permission for whatever runs it: System
Settings, then Privacy & Security, then Accessibility, then enable your
terminal.

## Try the shipped TextEdit pack

The example pack was recorded against a document window, so give it one:

```bash
echo "hello" > /tmp/vent-demo.txt && open -a TextEdit /tmp/vent-demo.txt

.venv/bin/vent run TextEdit write_document --arg text="typed by a pack"
.venv/bin/vent run TextEdit read_document
```

Anchors are recorded per machine and per app version. The shipped pack is an
example of the format, not a guarantee for your exact setup; if it refuses to
resolve, that refusal is the safety design working, and you can re-record it
in about a minute with `vent inspect` plus `vent anchor` (see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 5).

## Wire it into an MCP client

`vent serve` speaks MCP over stdio. For Claude Code:

```bash
claude mcp add ventriloquist -- /FULL/PATH/TO/Panther/.venv/bin/vent serve
```

For Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ventriloquist": {
      "command": "/FULL/PATH/TO/Panther/.venv/bin/vent",
      "args": ["serve"]
    }
  }
}
```

Tools appear as `textedit_write_document` and friends. Tools marked
`risk: high` in a pack are refused unless the server is started with
`VENT_ALLOW_HIGH=1` in its environment; see
[docs/SECURITY.md](docs/SECURITY.md) for why.

## Anchor durability, measured

`vent harness <app>` records anchors for an app's elements, perturbs the
app (verified window resize, and optionally a full quit and relaunch), and
re-resolves every anchor. Survival only counts when the element found is
provably the element recorded; lookalike bindings count as WRONG, and
anchors with nothing to verify identity against count as unverifiable, not
as survivors.

Current numbers on this machine (macOS 26, small anchor sets, early days):

```
TextEdit: baseline 100%, resized 100%, 0 wrong
Finder:   baseline 100%, resized 100%, 0 wrong
```

Field notes from measuring, kept because they shaped the code: macOS AX
trees can be cyclic (a wedged TextEdit returned the app element as its own
child, which is why every walk carries an ancestor-path cycle guard); apps
relaunched by a background process can serve degenerate menubar-only trees
until genuinely foregrounded; a pending accessibility permission dialog
silently degrades AX for every app launched after it appears; and
AXZoomWindow advertises itself on buttons that then refuse to perform it.

## Design

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before reading the code.
The threat model lives in [docs/SECURITY.md](docs/SECURITY.md). Both were
adversarially reviewed before implementation and rewritten from the
findings, and every phase since gets the same treatment.

## Roadmap

1. **Prove the hypothesis.** Scored anchor resolution plus a durability
   harness measured against structurally diverse apps, including an
   Electron app. A second hand-written pack (Notes) to pressure the format
   with a stateful app. In progress; the runtime and server halves are done.
2. **The explorer.** Survey, probe under the safety policy, and compile
   traces into ToolSpecs that match the hand-written packs.
3. **Healing and a third-party app.** Quarantined anchor healing end to end.
4. **Ship.** Docs, tests, demo video, release.
