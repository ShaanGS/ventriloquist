# Handoff: where Ventriloquist stands

This is the current state of the project, written so a new contributor
can pick up without re-deriving anything. Read [ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](SECURITY.md)
first; this file is the "what's done, what's next" layer on top.

## What Ventriloquist is

Compile a Mac app into an MCP server. An agent explores an app's
accessibility tree once, a compiler emits a JSON "pack" of typed tools
backed by recorded accessibility actions, and a deterministic runtime
replays them in milliseconds with no model calls. A healer re-grounds
anchors that break when apps update. Interpreter frameworks make an LLM
drive every click; this is the compiler.

## What is done and working

The whole loop exists and is committed. Every phase went through an
adversarial code review and a separate security review before the next
began, and each review's findings were fixed in a follow-up commit.

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

91 offline tests, green on Linux and macOS in CI.

## The release checklist (complete, v0.1.0)

1. **Live model-in-the-loop validation: DONE** (August 2026, through the
   claude CLI backend with a signed-in login). Every
   model-driven path ran live:
   - `vent explore Notes --no-values`: the model nominated targets over
     multiple rounds, the policy screened all of them (0 refusals
     needed), probes executed and the trace saved.
   - `vent compile Notes`: the approval gate earned its keep twice. A
     three-round trace compiled into a tool that would press New Note
     three times per call; the deterministic step summary exposed it and
     it was declined. A single-round trace compiled into a clean
     press-then-set_value tool that matches the hand-written pack's
     structure. That tool also demonstrated why descriptions are not
     ground truth: the model said "types into the resulting note", the
     steps typed into the toolbar search field, and only the live run
     revealed which field the probe had actually touched.
   - Anchor healing, full lifecycle: an anchor was broken on purpose,
     `vent run --heal` re-grounded it live (the model correctly refused
     everything but the true text area), the fix stayed quarantined, and
     `vent verify` promotion rewrote all three anchor sites, capturing
     the element's true current identifier.

   Two compiler findings for a future pass: `vent compile` OVERWRITES an
   existing pack rather than merging into it (the hand-written Notes pack
   was restored from a backup), and reviewers at the gate must map roles
   to what they mean in the app; the summary tells the truth but tersely.
   One llm.py finding, fixed: chat-tuned models fence their JSON even
   when told not to; exactly one wrapping fence is stripped as transport
   framing, everything else still rejects.
2. **A third-party Electron app pack: DONE (VS Code).**
   `packs/com.microsoft.VSCode/pack.json` ships four tools (Explorer,
   Search, and Source Control view switches plus a parameterized
   `search_workspace`), all passing live through `vent run`, with
   `vent verify` at 6/6 anchors. Harness numbers are in the README
   (95% baseline, 77.5% resized, zero wrong bindings over 40 anchors).
   The full harness now passes live including `--restart`: baseline 95%,
   resized 87.5%, restart 92.5%, zero wrong bindings over 40 anchors.
3. **Demo recording: DONE.** A 65-second screen recording
   (`~/Desktop/vent-demo.mov`, not committed) shows TextEdit and VS Code
   side by side being driven end to end by pack tools: narration written
   into TextEdit by `write_document`, then the Search view opened, a
   workspace search typed, and Source Control and Explorer switched, all
   deterministic replays. Re-record any time by staging the two
   windows side by side and running the pack tools under
   `screencapture -v`. Tagged as v0.1.0.

One more real-world proof worth knowing about: VS Code auto-updated from
1.129 to 1.133 mid-development and renamed its sidebar headings from
SHOUTING CASE to Title Case, breaking the two view-content anchors:
precisely the healing scenario, live and unprompted. Those anchors were
re-captured and now carry both label spellings (anchors remember every
label observed); the deliberate break-heal-promote exercise in item 1
covered the model-driven path as well.

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

VS Code specifics: launch with `--force-renderer-accessibility`. The
AXManualAccessibility request alone yields a tree that reads fine but
silently ignores every action, a trap because everything looks healthy
until a press does nothing. Its process name is `Code` but its
AppleScript name is `Visual Studio Code` (activating "Code" silently
fails), and an in-place auto-update can wedge its AX bridge (AXWindows
returns a bogus Application-role element) until the app is fully quit
and relaunched. Two more environment truths from the harness work:
NSWorkspace's running-app list freezes in a process that never pumps
the run loop (ax.running_apps now pumps), and VS Code can ignore the
first polite terminate and honor a repeat (the harness re-asks).

## Where the design gets pushed next

An outside review (August 2026) landed several points now reflected in
the code and docs, plus a queue of open work in priority order:

0. The Slack numbers exist now, and the thesis holds: 95% baseline,
   82.5% resized, 82.5% after a full restart, zero wrong bindings over
   40 anchors, with no identifiers anywhere in the tree. Slack needs
   `--force-renderer-accessibility` like VS Code (readable but
   action-deaf without it), serves only an open modal's subtree while a
   promo overlay is up, and its composer and search are the two text
   inputs. Figma desktop probed as the expected worst case: usable shell
   chrome, opaque pixel canvas. Next Slack step is a pack: channel
   navigation plus a parameter-bound select is what send_message_to
   needs, and the DM list is virtualized, so select must compose with
   scrolling.
1. Portability is the untested load-bearing claim. Packs carry window
   titles and chain ordinals that may be coupled to one machine's state.
   AppKit identifiers ship in compiled nibs and should travel; Electron
   anchors ride on labels and ordinals that depend on window width,
   sidebar state, and extensions, and Electron is the target market.
   First cheap probe: a fresh macOS user account on the same machine
   (clean geometry, no extensions, no workspace state), then a real
   second machine.
2. Tool-level success is the honest headline, and it is measured now:
   `vent harness <app> --tools`. The per-anchor and per-tool gap on VS
   Code (87.5% vs 75%) is entirely state-dependence, which is the case
   for stateful preconditions. Do not design preconditions until a
   hostile Electron app (Slack) has been probed; "the Search view is
   open" assumes the app exposes a stable assertable element for view
   state, and Chromium was already unreliable about exactly that.
3. T11 (semantic drift under a stable anchor) is written up and
   `vent verify` now flags any resolution onto a label outside the
   anchor's recorded set, version change or not.
4. T12 (read tools feeding the client model) is written down before read
   tools exist. Read it before shipping any read tool.
5. A parameter-bound `select` op (press the row whose label equals the
   parameter) is the highest-value capability gap. Design it against
   Chromium list virtualization from day one: a Slack DM list only
   publishes rendered rows, so select must compose with scrolling, and
   that constraint shapes the op more than the threat model does.
6. Control flow stays in the MCP client, not in packs. The pitch is
   "no model per UI step, none on the serving path", not "no model".

## Gotchas the next contributor will hit

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
