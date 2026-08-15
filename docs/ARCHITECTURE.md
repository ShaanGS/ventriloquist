# Ventriloquist Architecture

This document is the source of truth for how Ventriloquist is designed and why.
Read it before reading the code. Every module in `vent/` maps to a section
here. The design was adversarially reviewed before implementation began; the
decisions below reflect that review, and the biggest revisions it forced are
marked inline so future contributors know which walls are load-bearing.

## 1. The idea in one paragraph

Computer-use agents today are interpreters. A language model looks at the
screen and decides every click, every time. That is slow, expensive, and
flaky. Ventriloquist is a compiler. An agent explores a Mac app's
accessibility tree once, discovers what the app can do, and writes that
knowledge down as a "pack": a set of typed tools, each backed by a recorded
sequence of accessibility actions. After compilation, calling a tool replays
the recording in milliseconds with zero model calls. The model only comes back
when a recording breaks, fixes it, and the fix is quarantined until approved.
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
5. **Healer** re-grounds broken anchors against a fresh snapshot. Healed
   anchors are quarantined, not silently persisted (see SECURITY.md T8).

The MCP server is a thin adapter exposing pack tools over stdio to any MCP
client.

## 4. App classes and tree availability

Review finding: not every app hands you a tree. There are three classes and
the code must know which one it is talking to.

- **AppKit native** (Finder, Notes, TextEdit): full tree available
  immediately.
- **Chromium and Electron hosts** (Spotify, Slack, VS Code, Discord): the
  renderer's accessibility tree is OFF by default and is only built when an
  assistive client signals demand. Before snapshotting these apps, `ax.py`
  must set `AXManualAccessibility` and `AXEnhancedUserInterface` to true on
  the app element and wait for the web area to populate (planned for the
  same milestone as the first Electron pack). This has a real side effect
  (the app spends more CPU on accessibility until relaunch) and is
  documented as such.
- **Catalyst and SwiftUI** apps: trees exist but lean heavily on
  `AXIdentifier` rather than labels, which the anchor design already prefers.

A planned `vent doctor <app>` probe will report which class an app falls
into and whether a usable tree came up. No app is promised in a milestone
until this probe has passed against it.

## 5. Core concepts

### Snapshot

A pruned, ordered list of the interesting elements in an app's accessibility
tree at one moment. The snapshot contract, revised after review:

- Windows are enumerated through `AXWindows` (with `AXChildren` fallback),
  and the focused window is marked. Sheets and drawers (`AXSheets`,
  `AXDrawers`) are always descended, and the snapshot reports whether a modal
  is present, because a modal changes what every other element means.
- Every surfaced node records its full ancestor chain (role, label,
  `AXIdentifier`, same-role sibling ordinal, raw index) so anchors can be
  built without re-walking the tree. Container levels stay in the chain; they
  are the structural skeleton.
- Truncation is explicit. A snapshot that hit `max_nodes` says so, with
  surfaced vs total counts, and scrollable containers report visible vs total
  rows where the API exposes it. Consumers must be able to tell "not present"
  from "not rendered yet".
- Two value representations exist: a display value (truncated, scrubbed,
  model-facing) and a live verification read taken fresh from the element at
  verify time. Verification never reads from a snapshot.
- Secure text fields are excluded here, at the lowest level, not by callers.

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
  the healer. Picking a coin-flip winner is banned.
- Anchors record every label an element was observed under (toggle buttons
  legitimately have several).
- The compiler must refuse to emit a ToolSpec whose anchor was not unique in
  the recording snapshot. Empty-label anchors are valid only when the chain
  alone determines them uniquely.

### Trace

The explorer's raw output: a sequence of (state fingerprint, action,
resulting state) records. Every trace begins with an `AppState` fingerprint:
the window set and titles, the focused element, and the current selection.
Tools compiled from a trace carry that starting state as a precondition, so a
tool recorded "with a note selected" refuses to run when nothing is selected
instead of acting on the wrong thing.

### ToolSpec

One compiled capability:

```json
{
  "name": "write_document",
  "description": "Replace the text of the frontmost TextEdit document.",
  "risk": "mutating",
  "requires_frontmost": true,
  "params": {
    "text": {"type": "string", "description": "New document content."}
  },
  "preconditions": [
    {"kind": "app_running"},
    {"kind": "state_matches", "fingerprint": "..."}
  ],
  "steps": [
    {
      "op": "set_value",
      "anchor": {"...": "..."},
      "expect": {"role": "AXTextArea"},
      "value": {"param": "text"}
    }
  ],
  "verify": [
    {"kind": "value_contains", "anchor": {"...": "..."}, "param": "text"}
  ]
}
```

Notes on the shape:

- `risk` is one of `read_only`, `mutating`, `high`, assigned at the human
  approval gate and enforced by the server (SECURITY.md T9).
- Every mutating step carries an `expect` block, a pre-step assertion on the
  resolved element (role, and where meaningful the current value shape).
  Post-hoc verify alone is not enough, because a wrongly-bound element can
  pass a naive verify.
- Steps may include a navigation preamble: the recorded prefix that drives
  the app from a generic state to the tool's required state. Preambles are
  first-class steps, not assumptions.
- Step ops are a closed set: `press`, `set_value`, `pick`, `reveal`,
  `raise_window`, `open_app`, `wait_for`, `read_value`. `reveal` exists because lazily
  populated lists (every table in every Electron app, most AppKit outlines)
  only expose visible rows; it scrolls a target into existence via
  `AXScrollToVisible` or selection APIs. There is deliberately no `exec`, no
  `shell`, no AppleScript op, and model output can never extend this set.

### Pack

A versioned JSON file per app: `packs/<bundle-id>/pack.json`. Besides the
ToolSpecs it records the compile-time environment: app version
(`CFBundleShortVersionString`), macOS version, and locale. On load, a
mismatch marks every anchor low-confidence (which raises the resolver's
ambiguity bar). A planned `vent verify <app>` command will dry-resolve every
anchor without executing a step and report a durability percentage. Packs are
human-readable and human-editable on purpose; hand-writing one is the
supported way to bootstrap an app, and it is how the first end-to-end slice
gets built.

## 6. Module map

```
vent/
  ax.py            AX API wrapper: Element, apps, trust, tree activation
  snapshot.py      semantic snapshots per the contract in section 5
  anchors.py       anchor build + scored resolution, AnchorLost/Ambiguous
  packs.py         pack schema, load/save/validate, staleness checks
  runtime.py       execute a ToolSpec: preconditions, steps, settle, verify
  heal.py          re-ground lost anchors, quarantine the fixes
  server.py        MCP stdio server, per-app serialization, risk gating
  llm.py           single home for every model call
  policy.py        action and tool safety policy (see SECURITY.md)
  explorer.py      survey / probe / synthesize, producing traces
  compiler.py      traces to ToolSpecs, uniqueness and risk checks
  cli.py           doctor/apps/inspect/act/explore/compile/verify/serve
tests/
  fakes.py         in-memory fake element trees, no macOS needed
  test_anchors.py  scoring under rename/reorder/removal/ambiguity
  test_runtime.py  step execution, settle, verification against fakes
  test_packs.py    schema validation, versioning, staleness
docs/
  ARCHITECTURE.md  this file
  SECURITY.md      threat model and mitigations
```

Dependency rule: `runtime` and `server` never import `llm`. `heal` is the
only bridge between the deterministic and model worlds, invoked through a
callback. If a change makes the deterministic path depend on a model, the
change is wrong.

## 7. Timing, focus, and concurrency

Three things the first draft ignored and review demanded. They shape
`runtime.py` more than anything else.

**Settle.** `AXUIElementPerformAction` returns when the action is dispatched,
not when the UI has finished reacting. After every mutating op the runtime
runs a settle loop: poll the affected subtree until its shape is stable for a
quiet period or a per-step deadline passes. `wait_for` is a predicate over a
fresh snapshot (element exists, value matches, window appeared, modal gone)
with an explicit timeout. Every element gets `AXUIElementSetMessagingTimeout`
set low so a hung app fails in about a second instead of the six-second
system default. Polling is the deliberate v1 choice over `AXObserver`
notifications; observers need a run loop and are a v2 upgrade with a clean
seam (the settle loop is one function).

**Focus and activation.** Many AX operations misbehave when the app is not
frontmost, and activating an app steals focus from whatever the user is
doing, which can send their keystrokes somewhere they did not intend. So:
each ToolSpec declares `requires_frontmost`; when true, the runtime activates
the app, runs the tool, and restores the previously frontmost app after. The
server exposes a "do not steal focus" mode that queues frontmost-requiring
tools until the machine is idle or the user allows it.

**Concurrency.** MCP clients fire tool calls in parallel; a GUI is one global
mutable resource. The server holds a per-bundle-id mutex for the entire
duration of a tool call and queues the rest. Queue waits have a timeout and
fail loudly. There is no parallel execution against a single app, ever.

**Modals.** If a step begins and the snapshot reports an unexpected modal
sheet, that is a hard precondition failure with a defined recovery (press the
recorded cancel if one exists, otherwise fail and name the blocking sheet).
The runtime never acts "through" an unexpected modal.

## 8. The explorer

Built last (section 10 explains the order). Three phases, strictly ordered:

1. **Survey.** Snapshots only. Windows first, then menus, which are opened
   with Press and closed with Cancel (read-only in effect). Output: a
   capability inventory.
2. **Probe.** The model nominates elements; `policy.py` screens them
   (SECURITY.md T2 and T3: op-type gating, default-deny for unlabeled or
   unclassifiable elements, reversibility classes, probe budgets); approved
   actions run while watching snapshot diffs, with the settle protocol from
   section 7 so effects are attributed to the right action. Unexpected
   dialogs get cancelled and their trigger marked risky.
3. **Synthesize.** The model groups observed behavior into candidate tools
   and proposes ToolSpecs. Candidates are validated by replay through the
   normal runtime. Validation replay mutates real app state, so it is
   labeled as such at the approval gate, capped by the probe budget, and
   pointed at a user-designated scratch document where the app supports one.

The human gate: the planned `vent compile` command shows each proposed
tool two ways. The
model's name and description, and below it a deterministic, non-model
rendering of the actual steps (app, window, element role and label, op).
The deterministic rendering is the ground truth a poisoned description
cannot fake (SECURITY.md T5). Tools whose steps touch a different app or
window than their description claims are flagged loudly. Nothing is written
without approval.

Model roles live in `llm.py` and nowhere else: exploration and synthesis on
a Sonnet-class model, healing on a fast model, ids upgradeable in one place.

## 9. The runtime and healing

`runtime.execute(pack, tool, args)`: check preconditions (including the
state fingerprint), then per step: resolve the anchor by scoring, check the
`expect` assertion, perform the op, settle, and finally run verifications
against live reads. Structured result out. No retries inside a step.

On `AnchorLost` or `AnchorAmbiguous` the runtime calls the heal callback
with the broken anchor and a fresh, untruncated snapshot of the target
window. The healer proposes a re-grounding; the runtime replays the step
against it once. Whatever the outcome, the healed anchor goes to quarantine
inside the pack (`healed_pending`), is policy-checked, and is only promoted
to the anchor of record by the user (`vent verify` offers the promotion). A
healer handed a truncated snapshot must refuse rather than guess. If healing
fails, the tool call fails loudly, naming the app, tool, and step. Silent
degradation is banned, and so is silent self-modification.

## 10. Build order and milestones

The explorer is built last even though it is the headline. The pack format
and runtime are the load-bearing walls, and the central hypothesis (anchors
survive UI churn) must be measured before anything is built on top of it.
Review moved that measurement from week 3 to week 1.

- **Week 1: prove the hypothesis.** Anchors with scored resolution. An
  anchor durability harness: hand-written anchors against four or five
  structurally diverse apps, including one Electron app (per section 4),
  measured across app restart, window resize, sidebar collapse, and document
  switch. The resolution rate gets reported in the README, whatever it is.
  Pack schema, runtime, and hand-written packs for TextEdit AND Notes (a
  stateful app with selection and navigation, so the format is pressured by
  a real shape, not just one text area). `vent serve` over MCP stdio.
  Demo: an MCP client calls `textedit_write_document` with no vision.
- **Week 2: the explorer.** Survey and probe with the policy module, traces,
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
fake trees, letting anchors, runtime, packs, and compiler logic run in plain
pytest anywhere, including Linux CI. Fixtures are serialized snapshots of
real apps, so tests exercise realistic shapes. Live integration tests exist
for TextEdit and Notes but are skipped unless the environment is trusted.
The anchor scoring function gets the densest tests in the repo: renames,
reorders, removed wrapper levels, inserted wrapper levels, ambiguous twins,
and locale-flipped labels.
