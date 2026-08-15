# Handoff: where Ventriloquist stands

This is the state of the project at the end of the build sessions, written
so a fresh session (or a new contributor) can pick up without re-deriving
anything. Read [ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](SECURITY.md)
first; this file is the "what's done, what's next" layer on top.

## What Ventriloquist is

Compile a Mac app into an MCP server. An agent explores an app's
accessibility tree once, a compiler emits a JSON "pack" of typed tools
backed by recorded accessibility actions, and a deterministic runtime
replays them in milliseconds with no model calls. A healer re-grounds
anchors that break when apps update. Interpreter frameworks make an LLM
drive every click; this is the compiler.

## What is done and working

The whole loop exists and is committed. Every phase was adversarially
reviewed (an Opus code reviewer and a Sonnet security reviewer) before the
next began, and each review's findings were fixed in a follow-up commit.

- **Foundation** (`ax.py`, `snapshot.py`): accessibility introspection,
  semantic snapshots with ancestor chains, cross-platform imports.
- **Anchors** (`anchors.py`): scored resolution, identifier-first, refuses
  to guess between close candidates. Chain-aware matching.
- **Runtime + server** (`runtime.py`, `server.py`, `packs.py`): the closed
  op set, settle protocol, expect assertions, live-read verification, MCP
  stdio server with per-app locking and risk gating.
- **Durability harness** (`harness.py`): measures anchor survival across
  resize and restart. TextEdit and Finder resolved at 100% with zero wrong
  bindings on the author's machine.
- **Explorer + compiler + policy** (`explorer.py`, `compiler.py`,
  `policy.py`, `llm.py`): model nominates, policy screens after the model,
  probing is reversible, compiler composes only executed actions, human
  approval gate shows deterministic step summaries.
- **Healing** (`heal.py`): re-grounds broken anchors, refuses truncated
  snapshots and cross-role or destructive targets, quarantines fixes,
  promotes only through `vent verify` under a human.

79 offline tests, green on Linux and macOS in CI.

## What is NOT yet done (the release checklist)

1. **Live model-in-the-loop validation.** `vent explore` and
   `vent run --heal` call a model and need `ANTHROPIC_API_KEY` (or an
   `ant auth login` profile). This machine had no credentials, so the
   model-driven paths are covered only by the offline scripted-model seam,
   never against the real API. First real-credential task: run
   `vent explore Notes --no-values` end to end, then `vent compile Notes`,
   and confirm the compiled pack matches the hand-written one. Then break an
   anchor and confirm `vent run --heal` + `vent verify` promote it.
2. **A third-party Electron app pack: DONE (VS Code).**
   `packs/com.microsoft.VSCode/pack.json` ships four tools (Explorer,
   Search, and Source Control view switches plus a parameterized
   `search_workspace`), all passing live through `vent run`, with
   `vent verify` at 6/6 anchors. Harness numbers are in the README
   (95% baseline, 77.5% resized, zero wrong bindings over 40 anchors).
   The full harness now passes live including `--restart`: baseline 95%,
   resized 87.5%, restart 92.5%, zero wrong bindings over 40 anchors.
   The user has ordered that Discord must not be touched or read; do not
   target it for packs, probes, or demos.
3. **Demo recording and a tagged release.** The split-screen story is a
   vision agent fumbling with an app versus vent-compiled tools doing it in
   milliseconds.

## What the first live Electron run taught us

The VS Code pack work (August 2026) surfaced three platform truths that
are now handled in code:

- **Chromium trees are deep.** VS Code's search input sits below the old
  depth budget of 25, which meant its anchor could never resolve: the
  resolver structurally could not reach it. `MAX_TREE_DEPTH` is now 50,
  still one shared limit for snapshot walks and resolution.
- **Chromium publishes its tree lazily.** After an action mutates the
  page, every read returns the pre-action tree until the *next* action
  arrives; a view switch stayed invisible to 13 seconds of polling and
  then appeared the instant any action was performed. The runtime now
  pumps a no-op scroll-to-visible after each mutating op (`_pump_tree`
  in runtime.py) so settle and verify see the world the step created.
  Related: the cached `AXValue` of Chromium radio buttons is unreliable
  as a postcondition; verify view switches by waiting for view *content*
  to exist, not by reading the pressed control's value.
- **A locked screen makes every app serve an empty tree** while the
  permission check still passes. `vent doctor` now names this state
  instead of blaming the app.

VS Code specifics: launch with `--force-renderer-accessibility` — the
AXManualAccessibility request alone yields a tree that READS fine but
silently ignores every action, a trap because everything looks healthy
until a press does nothing. Its process name is `Code` but its
AppleScript name is `Visual Studio Code` (activating "Code" silently
fails), and an in-place auto-update can wedge its AX bridge (AXWindows
returns a bogus Application-role element) until the app is fully quit
and relaunched. Two more environment truths from the harness work:
NSWorkspace's running-app list freezes in a process that never pumps
the run loop (ax.running_apps now pumps), and VS Code can ignore the
first polite terminate and honor a repeat (the harness re-asks).

## Gotchas the next session will hit

- **AX needs the app foregrounded.** Apps relaunched by a background
  process serve degenerate menubar-only trees until genuinely foregrounded;
  `ax.activate` returns False from a background process. If `vent inspect`
  shows zero elements, click the app's window once.
- **A pending Accessibility permission dialog degrades AX for every app
  launched after it appears.** If everything suddenly reads empty, check for
  a permission prompt.
- **Run outside any sandbox.** The accessibility API needs the real
  permission grant; sandboxed shells report the process as untrusted.
- **The scripted-model seam** is `llm.set_completer_for_tests(fn)`; tests
  install a fake completer so the explorer, compiler, and healer run with no
  network. Clear it in a fixture teardown.

## The invariants a change must not break

Listed in [CONTRIBUTING.md](../CONTRIBUTING.md). The short version: closed
action space, `runtime`/`server` never import `llm`, the model composes but
never authors, policy screens after the model, healed anchors are
quarantined not promoted, and no vision anywhere.
