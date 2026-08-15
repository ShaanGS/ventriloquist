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

The full loop exists: introspection, scored anchor resolution, the pack
format, the deterministic replay runtime, the MCP server, the durability
harness, the model-driven explorer and compiler with a human approval gate,
and quarantined healing with `vent verify` promotion. 79 offline tests run
in CI on every push; live behavior is verified against TextEdit, Notes,
and VS Code. Two hand-written packs ship: TextEdit, and VS Code — the
Electron proof, with view navigation and a parameterized workspace search
replayed through the deterministic runtime. What remains before a release:
live model-in-the-loop runs of `vent explore` and `--heal` (they need an
`ANTHROPIC_API_KEY`) and a demo recording.

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
TextEdit: baseline 100%,  resized 100%,   0 wrong
Finder:   baseline 100%,  resized 100%,   0 wrong
VS Code:  baseline  95%,  resized  87.5%, restart 92.5%, 0 wrong  (40 anchors)
```

The VS Code restart round quits and relaunches the app entirely; 37 of 40
anchors still bind to provably the same element afterward, with zero wrong
bindings. The non-survivors are ambiguity refusals and one loss: Chromium
trees are full of unlabeled twin groups, and when candidates score too
close the resolver refuses to guess. The compiled VS Code pack's own
curated anchors resolve 6/6 after view switches, a window resize, and an
app update. One operational requirement: VS Code must be launched with
`--force-renderer-accessibility`. Without it the tree can still be read
after a web-accessibility request, but actions are silently ignored.

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

## The full workflow

```bash
vent doctor Spotify                 # does this app expose a usable tree?
vent explore Notes --no-values      # model probes safely, policy screens, trace recorded
vent compile Notes                  # model proposes tools; you approve against literal steps
vent serve                          # every approved pack becomes MCP tools
vent run Notes create_note --arg body="..."   # or call one tool directly
vent verify Notes                   # dry-resolve anchors; promote quarantined heals
```

`vent explore` and `vent run --heal` call a model and need Anthropic
credentials (`ANTHROPIC_API_KEY`). Everything else, including serving
compiled packs and reusing already-quarantined heals, runs with no model
and no network.

## Roadmap to release

1. Live model-in-the-loop validation of explore and heal (needs a key).
2. A compiled pack for a third-party Electron app via the `vent doctor`
   tree probe.
3. Demo recording and a tagged release.
