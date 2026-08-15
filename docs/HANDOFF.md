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
2. **A third-party Electron app pack.** `vent doctor <app>` already probes
   whether an app exposes a usable tree (it sets AXManualAccessibility /
   AXEnhancedUserInterface for Chromium hosts). Pick one that passes the
   probe (Spotify, Slack, VS Code), compile a pack, and add its numbers to
   the README durability table. This is the headline demo: an Electron app
   made programmable.
3. **Demo recording and a tagged release.** The split-screen story is a
   vision agent fumbling with an app versus vent-compiled tools doing it in
   milliseconds.

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
