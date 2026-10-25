# Ventriloquist Architecture

This document is the source of truth for how Ventriloquist is designed and why.
Read it before reading the code. Every module in `vent/` maps to a section
here. The design was adversarially reviewed before implementation began; the
decisions below reflect that review, and the biggest revisions it forced are
marked inline so future contributors know which walls are load-bearing.
Parts of the design that aren't built yet are marked **Planned**.

## 1. The idea in one paragraph

Computer-use agents today are interpreters. A language model looks at the app
and decides every action, every time, so every run pays again for screenshots
and model decisions. Ventriloquist is a compiler. An agent explores a Mac
app's accessibility tree once, discovers what the app can do, and writes that
knowledge down as a "pack": a set of typed tools, each backed by a recorded
sequence of accessibility actions. After compilation, calling a tool replays
the recording with zero model calls: finding an element takes tens of
milliseconds in a small app like TextEdit, and each step that changes
something then waits for the app to settle (usually 0.15 to 0.6 seconds)
before the next step trusts the tree. Through a plain MCP client, a TextEdit
write takes about half a second and a read about a tenth of one
(docs/BENCHMARKS.md). The model only comes back if you turn healing on and a
recording breaks, and then the fix is quarantined until you approve it.
Interpreter when needed, compiler by default.

## 2. What we are not building

Non-goals keep the scope honest:

- Not a general computer-use agent. We make specific apps programmable; we do
  not compete with open-ended screen agents.
- Not a vision system. No screenshots, no OCR, no pixel coordinates anywhere
  in the codebase. If a feature seems to need vision, redesign the feature.
- Not cross-platform (yet). macOS only.
- Not a background daemon. Ventriloquist runs when invoked.
- Not (in v1) a driver of system-owned dialogs. File pickers and save sheets
  are often served by a separate process and are out of scope until the core
  loop is proven. Tools that need "export to path" ship after that.

## 3. System overview

```
                 one-time, agentic                every call, deterministic
  ┌─────────┐   ┌──────────┐   ┌──────────┐     ┌─────────┐   ┌─────────┐
  │ Running │──▶│ Explorer │──▶│ Compiler │────▶│  Pack   │──▶│ Runtime │
  │ Mac app │   │ (LLM)    │   │ (LLM)    │     │ (JSON)  │   │ (no LLM)│
  └─────────┘   └──────────┘   └──────────┘     └─────────┘   └────┬────┘
       ▲                                             ▲             │
       │              ┌──────────┐                   │        ┌────▼────┐
       └──────────────│  Healer  │◀── replay failure ┘        │   MCP   │
                      │ (LLM)    │   (quarantined)            │ server  │
                      └──────────┘                            └─────────┘
```

1. **Explorer** walks a live app through snapshots and safe probing, and
   records traces.
2. **Compiler** turns traces into typed tool definitions with anchored steps,
   shown to the user for approval.
3. **Pack** is the durable artifact on disk. Everything upstream exists to
   produce it; everything downstream only reads it.
4. **Runtime** executes a tool deterministically. No model in the loop.
5. **Healer** re-grounds broken anchors against a fresh snapshot, when
   healing is turned on. Healed anchors are quarantined, not silently
   persisted (see SECURITY.md T8).

The MCP server is a thin adapter exposing pack tools over stdio to any MCP
client.

## 4. App classes and tree availability

Review finding: not every app hands you a tree. There are three classes and
the code must know which one it is talking to.

- **AppKit native** (Finder, Notes, TextEdit): full tree available
  immediately.
- **Chromium and Electron hosts** (Spotify, Slack, VS Code, Discord): the
  renderer's accessibility tree is off by default and is only built when an
  assistive client signals demand. `vent doctor <app>` and the durability
  harness set `AXManualAccessibility` and `AXEnhancedUserInterface` on the
  app element to ask for it. That has a real side effect (the app spends
  more CPU on accessibility until it relaunches), and it is not enough on
  its own: the tree it produces reads fine but silently ignores every
  action. VS Code and Slack have to be launched with
  `--force-renderer-accessibility`, for example
  `open -a "Visual Studio Code" --args --force-renderer-accessibility`,
  which turns the renderer's accessibility fully on. The bundled packs for
  both say so.
- **Catalyst and SwiftUI** apps: trees exist but lean heavily on
  `AXIdentifier` rather than labels, which the anchor design already prefers.

`vent doctor <app>` reports whether a usable tree came up, how many web
areas it holds, and why it might be empty (no open window, a locked
screen). No app is promised a pack until it passes.

## 5. Core concepts

### Snapshot

A pruned, ordered list of the interesting elements in an app's accessibility
tree at one moment. The snapshot contract, revised after review:

- Windows are enumerated through `AXWindows` (with `AXChildren` fallback),
  and the focused window is marked. macOS lists only the windows on the
  current Space, so an app whose window is on another Space lists none;
  its `AXMainWindow` and `AXFocusedWindow` then stand in, placed where a
  listed window would sit, and the snapshot says the window is on another
  Space. Anchor resolution, settle and the runtime's window checks walk
  the same children. A locked screen is left looking empty.
- The snapshot reports whether a sheet or a modal dialog is open, because
  a modal changes what every other element means. A sheet is an `AXSheet`
  child of its window; a modal dialog is a window whose subrole is
  `AXDialog` or `AXSystemDialog`, or whose `AXModal` is true.
- Every surfaced node records its full ancestor chain (role, label,
  `AXIdentifier`, same-role sibling ordinal, raw index) so anchors can be
  built without re-walking the tree. Container levels stay in the chain; they
  are the structural skeleton.
- Truncation is explicit. A snapshot that hit `max_nodes` or the depth
  limit says so. Consumers must be able to tell "not present" from "the
  walk stopped early".
- Two value representations exist: a display value (truncated, scrubbed,
  model-facing) and a live verification read taken fresh from the element at
  verify time. Verification never reads from a snapshot.
- Secure text fields are excluded here, at the lowest level, not by callers,
  and anchor resolution skips them too (SECURITY.md T4).

### Anchor

A durable address for one element. Review finding: the original three-tier
"try exact, then index, then fuzzy" resolution could silently bind the wrong
element and then "verify" successfully against it. Resolution is therefore a
scoring function, not a cascade:

- Candidate elements in the target window are scored per chain level on:
  `AXIdentifier` match (highest weight), role match, label match (labels are
  a hint, not an address: they are localized and state-dependent, like a
  Play button that relabels itself Pause), same-role ordinal distance, and
  raw index distance as the weakest term.
- An anchor resolves only if the best candidate clears an accept threshold
  AND beats the runner-up by a margin. Two close candidates raise
  `AnchorAmbiguous`; zero viable candidates raise `AnchorLost`. Both go to
  the healer when healing is on, and fail the call otherwise. Picking a
  coin-flip winner is banned.
- An anchor that records a window title never resolves in a window with a
  different title. Window identifiers are nib-derived and identical across
  documents, so the title is the only thing separating "vent-demo.txt"
  from "taxes-2025.txt".
- An anchor that records no window title is window-agnostic by design (a
  "frontmost document" tool, like the bundled TextEdit ones). When its close
  candidates sit in different windows, the one in the app's focused window
  wins. Ties inside one window, and ties for anchors that pin a window
  title, still raise `AnchorAmbiguous`.
- Window titles carry user data (document names, channels, workspaces), so
  recording leaves them out of the chain: the window link's label is blank,
  and so is any ancestor label that repeats the title, as Electron apps do
  on the group under the window and on the web area inside it.
- Anchors record every label an element was observed under (toggle buttons
  legitimately have several).
- **Planned:** the compiler refusing a tool whose anchor wasn't unique in
  the snapshot it was recorded from. Today a non-unique anchor shows up the
  first time the tool runs, as a refusal.

### Trace

The explorer's raw output: the list of probes it ran or the policy refused,
each with the element's role, label and window, what the policy decided,
how the app reacted, and, for every probe that ran, the anchor built for its
element at the time.

**Planned (issue #2):** traces that begin with an `AppState` fingerprint
(the window set and titles, the focused element, the current selection),
which compiled tools carry as a precondition, so a tool recorded "with a
note selected" refuses to run when nothing is selected instead of acting on
the wrong thing. Today the preconditions are `app_running` and
`window_exists`, and a tool's other needs are written in its description.

### ToolSpec

One compiled capability. The bundled TextEdit pack's `write_document`,
with each anchor shortened to `{...}`:

```json
{
  "name": "write_document",
  "description": "Replace the entire text of the frontmost TextEdit document.",
  "risk": "mutating",
  "requires_frontmost": false,
  "params": {
    "text": {"type": "string", "description": "The new document content."}
  },
  "preconditions": [{"kind": "window_exists"}],
  "steps": [
    {
      "op": "set_value",
      "anchor": {...},
      "value": {"param": "text"},
      "expect": {"role": "AXTextArea"}
    }
  ],
  "verify": [
    {"kind": "value_contains", "anchor": {...}, "param": "text"}
  ]
}
```

Notes on the shape:

- `risk` is one of `read_only`, `mutating`, `high`, assigned at the human
  approval gate and enforced by the server (SECURITY.md T9).
- Every step that acts carries an `expect` block, a check on the resolved
  element's role before anything happens. Compiled steps always get one,
  and every shipped pack has them. Post-hoc verify alone is not enough,
  because a wrongly-bound element can pass a naive verify.
- Steps may include a navigation preamble: the recorded prefix that drives
  the app from a generic state to the tool's required state. Preambles are
  first-class steps, not assumptions.
- Step ops are a closed set: `press`, `set_value`, `pick`, `reveal`,
  `raise_window`, `open_app`, `wait_for`, `read_value`. `reveal` scrolls a
  resolved element into view (`AXScrollToVisible`), for lazily populated
  lists (every table in every Electron app, most AppKit outlines) that only
  expose visible rows. There is deliberately no `exec`, no `shell`, no
  AppleScript op, and model output can never extend this set.
- Preconditions are `app_running` and `window_exists`. Verify checks are
  `value_equals`, `value_contains` (against a parameter or a literal) and
  `element_exists`.

### Pack

A versioned JSON file per app, `<bundle-id>/pack.json`, in one of two
places. Packs bundled with vent live in `vent/bundled_packs/` and are never
written to. The user's own live in `~/.ventriloquist/packs/` (`VENT_HOME`
moves it), and one there shadows a bundled pack with the same bundle id.
Every write (compile, heal, promote) goes to the user's directory, so
healing a bundled pack saves the user's own copy, and compiling adds the
approved tools to the app's existing pack rather than replacing it. A pack
that fails to load is skipped with a warning and the rest keep serving; if
it is the user's own, the bundled pack does not quietly take its place.

Besides the ToolSpecs, a pack records the environment it was recorded in:
app version (`CFBundleShortVersionString`), macOS version, and locale. When
the running app's version, or macOS's major.minor version, differs from
what the pack recorded, every anchor is treated as low-confidence, which
raises the resolver's accept threshold. Locale is recorded but not
compared. `vent verify <app>`
dry-resolves every anchor without executing a step and reports how many
resolve. Packs are human-readable and human-editable on purpose;
hand-writing one is the supported way to bootstrap an app, and every tool
that ships with vent was written that way.

## 6. Module map

```
vent/
  ax.py            AX API wrapper: Element, apps, trust, activation
  snapshot.py      semantic snapshots per the contract in section 5
  anchors.py       anchor build + scored resolution, AnchorLost/Ambiguous
  packs.py         pack schema, load/save/validate/merge, staleness checks
  runtime.py       execute a ToolSpec: preconditions, steps, settle, verify
  heal.py          re-ground lost anchors, quarantine the fixes
  server.py        MCP stdio server, per-app serialization, risk gating
  llm.py           single home for every model call
  untrusted.py     marks app text as data for any model that reads it
  policy.py        action and tool safety policy (see SECURITY.md)
  explorer.py      probe rounds under the policy, producing traces
  compiler.py      traces to ToolSpecs, risk from what the steps did
  harness.py       anchor and tool durability measurements
  cli.py           doctor/apps/inspect/act/anchor/run/explore/compile/
                   verify/harness/serve/mcp-config
  paths.py         where packs and traces are read and written
  bundled_packs/   the packs that ship with vent, read-only
tests/
  fakes.py         in-memory fake element trees, no macOS needed
  test_anchors.py  scoring under rename/reorder/removal/ambiguity
  test_runtime.py  step execution, settle, verification against fakes
  test_packs.py    schema validation, versioning, staleness
docs/
  ARCHITECTURE.md  this file
  SECURITY.md      threat model and mitigations
  BENCHMARKS.md    measurements against computer use, and durability
```

Dependency rule: `runtime` and `server` never import `llm`. `heal` is the
only bridge between the deterministic and model worlds, invoked through a
callback, and it asks a model only when healing is turned on. If a change
makes the deterministic path depend on a model, the change is wrong.

## 7. Timing, focus, and concurrency

Three things the first draft ignored and review demanded. They shape
`runtime.py` more than anything else.

**Settle.** `AXUIElementPerformAction` returns when the action is dispatched,
not when the UI has finished reacting. After every mutating op the runtime
runs a settle loop: poll the app's windows until their shape is stable for a
quiet period or a per-step deadline passes. The menu bar is left out unless
the step acted inside it; it is most of a native app's tree, and anchor
resolution skips it on the same terms. A write takes its first fingerprint
before setting the value, so the new value counts as the change; a press
takes it afterwards, because what it waits for is the app's reaction.
`wait_for` polls until its anchor resolves, with an explicit timeout. vent
sets a one-second messaging timeout (`AXUIElementSetMessagingTimeout`) on
the system-wide element, which makes it the default for every element the
process touches, so a hung app fails in about a second instead of the
six-second system default. Polling is the deliberate v1
choice over `AXObserver` notifications; observers need a run loop and are a
v2 upgrade with a clean seam (the settle loop is one function).

**Focus and activation.** Many AX operations misbehave when the app is not
frontmost, and activating an app steals focus from whatever the user is
doing, which can send their keystrokes somewhere they did not intend. So:
each ToolSpec declares `requires_frontmost`; when true, the runtime activates
the app, waits for it to report itself frontmost, runs the tool, and
restores the previously frontmost app after. An app that doesn't come
forward within two seconds fails the call before any step runs, since a
press that reaches an app in the background can flip a button without
doing anything.
When the app's window is on another Space, activating it switches the
display to that Space and restoring the previous app switches back. Only
tools that need it declare it: the TextEdit tools and the Outlook example's
read tool work from the background.

**Running-app state.** AppKit updates NSWorkspace's app list and front app,
and every NSRunningApplication, only while the main run loop runs. The CLI
pumps that loop before it reads. `vent serve` runs the MCP event loop on a
thread of its own and keeps its main thread turning AppKit's run loop
(`serve_beside_run_loop`), so tool calls, which run on worker threads, see
apps opened, quit or brought forward after the server started, and the
client's messages never wait on a pump. Packs find their app by asking
LaunchServices for the bundle id, skipping processes that have already
exited. Without both, the server kept the state from its first
call: apps opened later never appeared, and a relaunched app kept its dead
pid.

**Concurrency.** MCP clients fire tool calls in parallel; a GUI is one global
mutable resource. The server holds a per-bundle-id lock for the entire
duration of a tool call and queues the rest. Tools that need the front also
take turns across apps, because there is only one front. Queue waits have a
timeout and fail loudly, and a call the client cancels while it waits never
runs. There is no parallel execution against a single app, ever.

**Modals.** Before each step, the runtime checks for a sheet or dialog in the
way: a sheet on the window the step's element is in, or a modal dialog window
in the app. Unless the step's own element was recorded inside that sheet or
dialog, the step refuses and names it. The runtime never acts "through" an
unexpected modal. When a call fails, it presses the cancel button of any
sheet or dialog that opened during the call, and leaves alone the ones that
were open before it; its error also says which presses, writes or picks had
already gone out.

## 8. The explorer

Built last (section 10 explains the order). Two phases, then the gate:

1. **Probe rounds.** First the explorer reads the menu bar and refuses an
   app whose menus aren't in English, since the policy's verb lists are
   English. Then each round takes a snapshot (window contents; the menu
   bar is left out), and the model nominates up to four elements to press
   or type into. `policy.py` screens every nomination (SECURITY.md T2 and
   T3: op-type gating, default-deny for unlabeled or unreadable elements,
   nothing inside a sheet or dialog, reversibility classes, a budget for
   cumulative actions), and approved probes run while the settle protocol
   from section 7 attributes effects to the right action. A probe that
   changes the tree ends the round, so the next nomination works from a
   fresh snapshot. A dialog a probe opens is closed with the cancel button
   it names, and nothing else, and its trigger is marked risky. Everything
   is recorded in a trace.
2. **Synthesize.** `vent compile` sends the list of probes that ran to the
   model, which groups them into candidate tools and names them. The tools
   are built from the recorded steps and anchors only. **Planned:**
   validating each candidate by replaying it once through the normal
   runtime, labeled at the gate as mutating real app state and pointed at a
   scratch document where the app supports one.

The human gate: `vent compile` shows each proposed tool two ways. The
model's name and description, and below it a deterministic, non-model
rendering of the actual steps (app, window, element role and label, op).
The deterministic rendering is the ground truth a poisoned description
cannot fake (SECURITY.md T5). Tools whose steps touch a window their
description never mentions are flagged loudly. Nothing is written without
approval.

Model roles live in `llm.py` and nowhere else: exploration and synthesis on
a Sonnet-class model, healing on a fast model, ids upgradeable in one place.

## 9. The runtime and healing

`runtime.execute(pack, tool, args)`: check preconditions, then per step:
resolve the anchor by scoring, refuse if a sheet or dialog is in the way,
check the `expect` assertion, perform the op, settle, and finally run
verifications against live reads. Structured result out. No retries inside
a step.

On `AnchorLost` or `AnchorAmbiguous` the runtime calls the heal callback, if
it was given one, with the broken anchor. The healer looks only at the
anchor's own window (the one whose title it pinned, the focused window for
an anchor with no title, or the menu bar for a menu command) and refuses if
that isn't open. It takes a fresh snapshot of that window, with field values
redacted if the caller asked, refuses if it was cut short, and proposes a
re-grounding; the runtime replays the step against it once. The healed
anchor goes to quarantine inside the pack (`healed_pending`), saved onto the
pack as it is on disk so nothing saved since is undone, is policy-checked,
and is only promoted to the anchor of record by the user (`vent verify`
offers the promotion). Asking a model is opt-in (`VENT_HEAL=1` for the
server, `--heal` for `vent run`); without it, only fixes already in
quarantine are reused, with no model call. If healing fails, the tool call
fails loudly, naming the app, tool, and step. Silent degradation is banned,
and so is silent self-modification.

## 10. Build order and milestones

This is the order the first release (0.1.0, August 2026) was built in, kept
because the reasoning still holds. The explorer is built last even though
it is the headline. The pack format and runtime are the load-bearing walls,
and the central hypothesis (anchors survive UI churn) must be measured
before anything is built on top of it. Review moved that measurement from
week 3 to week 1.

- **Week 1: prove the hypothesis.** Anchors with scored resolution. An
  anchor durability harness: hand-written anchors against four or five
  structurally diverse apps, including one Electron app (per section 4),
  measured across app restart, window resize, sidebar collapse, and document
  switch. The resolution rate gets reported, whatever it is.
  Pack schema, runtime, and hand-written packs for TextEdit AND Notes (a
  stateful app with selection and navigation, so the format is pressured by
  a real shape, not just one text area). `vent serve` over MCP stdio.
  Demo: an MCP client calls `textedit_write_document` with no vision.
- **Week 2: the explorer.** Probing with the policy module, traces,
  compiler emitting ToolSpecs for TextEdit and Notes that match or beat the
  hand-written packs.
- **Week 3: healing and a third-party app.** Quarantined healing end to end.
  The third-party app is chosen by running the `vent doctor` tree probe
  against candidates, not named in advance.
- **Week 4: ship.** Docs, tests, README, contribution guide, demo video,
  release.

Each week ends with something that runs end to end. When a week is at risk,
cut breadth (fewer apps, fewer tools), never the loop.

## 11. Testing strategy

The AX layer is the only part that needs a real Mac and real permissions, so
it hides behind the `Element` interface. `tests/fakes.py` provides in-memory
fake trees modeled on real apps (`textedit_like`, `notes_like`), letting
anchors, runtime, packs, and compiler logic run in plain pytest anywhere,
including Linux CI. The suite has no live tests: `tests/mcp_smoke.py` is a
separate script that drives TextEdit over MCP on a trusted Mac, and
`tests/install_smoke.py` serves the bundled packs from an installed wheel.
The anchor scoring tests cover renames, new siblings, inserted wrapper
levels, removed elements, ambiguous twins, window titles, and windows on
another Space.
