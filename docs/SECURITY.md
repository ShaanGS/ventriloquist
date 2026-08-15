# Security Model

Ventriloquist drives real applications on a real machine with real user data.
That demands a written threat model, not vibes. This document lists what can
go wrong and what the code does about it. It was red-teamed before
implementation and rewritten from the findings; the review proved two of the
original claims false, which is exactly why the review existed. If you find a
gap, open an issue with the `security` label.

## Assets we are protecting

1. The user's data inside apps (documents, messages, files, accounts).
2. The user's machine state (nothing destructive runs unattended).
3. The user's privacy (accessibility trees contain personal information).
4. Credentials (Ventriloquist must never read or type secrets).

## Threats and mitigations

### T1. Generated code execution

Agent frameworks that `exec()` model output hand the model a shell. Our
action space is a closed set of typed ops (`press`, `set_value`, `pick`,
`reveal`, `raise_window`, `open_app`, `wait_for`, `read_value`) executed by
our own runtime. `read_value` is the one op that moves data outward: it
returns an element's live value to the tool caller. It mutates nothing, but
it is how app content reaches an MCP client, so T6's scrubbing rules apply
to it and it is never valid against a secure field (T4 removes those before
any op can see them). No op runs code, shell commands, or AppleScript.
Adding an op is a breaking change to this threat model and requires
updating it first. Model
output is parsed against a JSON schema; anything outside it is rejected.

### T2. Destructive writes without a destructive verb

Review made this its own threat instead of a bullet: `set_value` overwrites
a field's contents with no button pressed and no verb for a blocklist to
catch. Our own first demo emptied and rewrote a document this way.
Mitigations gate on the op, not the label:

- During autonomous probing, `set_value` is allowed only when the target's
  current value is empty, or after the prior value is captured so it can be
  restored. The same rule covers `pick` on multi-select controls.
- Compound risk is assessed at the tool level, not just per step. A sequence
  of individually bland steps (select all, then set value) can be
  destructive as a unit. The compiler classifies each candidate tool's net
  effect ("this replaces the entire contents of a document") and the
  approval gate says so in those words.

### T3. Destructive actions during exploration

Layered, because no single layer is sufficient:

- Apps are explored only when the user names them.
- Probing actions pass through `vent/policy.py`. Labels matching a
  destructive-verb list (delete, remove, empty, erase, send, pay, buy,
  subscribe, sign, submit, post, publish, share, shut down, restart, log
  out, format, uninstall, close-without-saving patterns) are blocked. The
  list is data, easy to audit.
- The blocklist is known to be insufficient on its own, so its gaps get
  explicit rules rather than hope:
  - Elements with empty labels, non-Latin labels, or labels the policy
    cannot confidently classify are default-deny during autonomous probing.
    Unclassifiable never means allowed.
  - Context-dependent verbs (Clear, Reset, Replace, Overwrite) are judged
    with their surroundings: the containing window or sheet title and
    sibling labels. "Clear Formatting" and "Clear All Messages" are
    different risks and a bare string match cannot tell them apart.
  - Actions are classified reversible, cumulative, or destructive.
    Cumulative actions (New Note, New Window) are budgeted per session, so
    exploration cannot leave forty empty notes synced to iCloud.
- Dialog watchdog: an unexpected sheet or dialog gets cancelled and its
  trigger marked risky for the rest of the session.
- Validation replay during synthesis mutates real app state. The approval
  gate says so explicitly, and replay targets a user-designated scratch
  document where the app supports one.
- The final compile gate is human, and per T5 it shows ground truth, not
  just model prose.

### T4. Credential exposure

Secure text fields are untouchable: never read, never written, never sent to
a model, never a valid anchor target. This is enforced in `snapshot.py`
itself, the lowest level that surfaces elements, keying on the
`AXSecureTextField` subrole. Honest caveat from review: this protection
trusts the target app's own role reporting. A custom-drawn password field
that presents as a plain text field is invisible to it, so policy adds
heuristics (fields rendering bullet characters, fields inside windows whose
titles match authentication keywords are treated as secure) and this
document does not claim the guarantee is unconditional. Ventriloquist never
asks for passwords, and login-form submission verbs sit on the destructive
list.

### T5. Prompt injection through app UI

Anything an app renders becomes text in a snapshot, so a malicious page or
message can address instructions to the explorer model. The original claim
here was "an injection can waste a session but cannot mint a destructive
tool call". Review proved that false: a poisoned model can honestly emit a
tool named `save_document` whose steps press Send in Mail, and an approval
gate showing only the model's own name and description would launder it.

Containment therefore has one more layer than before:

- Model output is schema-constrained and the action space is closed; policy
  screening runs after the model and cannot be bypassed by it.
- Snapshot text sent to models is wrapped in a delimiter carrying a
  per-call random nonce and tagged as untrusted app content, so an app
  cannot forge the closing tag to break out, and prompts distinguish data
  from instructions instead of relying on the model to guess.
- Model-authored tool names and descriptions are stripped of control and
  bidirectional-override characters and length-capped before they reach the
  approval gate, so they cannot forge structure or hide their true target in
  the text shown to the human.
- The approval gate renders a deterministic, non-model summary of every
  step (app, window, element role and label, op) next to the model's
  description. The steps are the ground truth; the description is marketing.
  The gate emits a loud warning when a tool's steps act on a window its
  description never mentions, and marks high risk tools in red.

### T6. Personal data leaving the machine

Snapshots are the only thing ever sent to a model, scrubbed first: secure
fields are dropped entirely, and other field values are truncated to short
previews. `vent explore --no-values` replaces every value with a length
marker (`<N chars redacted>`) so exploration can run without any field
content leaving the machine; element structure and labels still go through.
Truncation is a privacy measure, not an injection defense (short payloads
survive truncation; T5 handles that). Honest limits: without `--no-values`,
a probe session does send up to the preview length of non-secure field
content (a note body, a draft) to the model, so `--no-values` is the switch
to reach for on an app holding sensitive text. Traces and packs stay on
local disk. No telemetry. All network calls live in `vent/llm.py`, one file
to audit. That includes the fallback backend that shells out to the Claude
Code CLI (`claude -p`, print mode, tools disabled): a different transport
to the same model endpoint, initiated from the same one file, carrying the
same scrubbed snapshot content and nothing else.

Probe reversibility has an honest limit too: the explorer writes a short
probe string into an empty field and restores the prior value immediately,
but an app that transmits on every keystroke (iCloud-syncing Notes, a
live-collaboration document, search-as-you-type) may have already sent that
string before the restore. Probing is reversible on the local field, not
across a network the app owns. When a restore fails outright, the trace
action is reclassified destructive so neither the compiler nor a reviewer
treats it as clean.

### T7. Poisoned or stale packs

Packs are JSON on disk. `vent/packs.py` validates schema and version on
load and rejects unknown ops, so a tampered pack is confined to the same
closed action space as a legitimate one. Packs shared between users should
be reviewed like code; the format is human-readable to make that real.

Staleness is the more common corruption: the app updated overnight and the
pack is now wrong about the world. Packs record app version, macOS version,
and locale at compile time. On mismatch, anchors load as low-confidence
(raising the resolver's ambiguity bar, making it fail toward the healer
rather than toward a plausible wrong element). The planned `vent verify`
command will dry-resolve every anchor to report durability without
executing anything.

### T8. The healer rewriting packs

The healer exists to fix broken anchors, which means it can re-point them,
which means a hostile UI state could steer a benign tool's anchor onto a
dangerous element. And "the replay succeeded" is not a safety check, because
destructive replays also succeed. The fences, in layers:

- A healer handed a truncated snapshot refuses rather than guess at the
  nearest lookalike; it cannot choose in a window it cannot fully see.
- A re-grounding must land on the same kind of control the broken anchor
  described (same role). A step that pressed a button cannot heal onto a
  text field, and vice versa. This sharply narrows what a hostile UI can
  steer the choice toward.
- Every re-grounding is re-screened by policy against the new target,
  including destructive and contextual verbs and authentication windows.
- Anchor matching for reuse and promotion includes the chain signature, so
  two structurally identical elements (unlabeled twins in one window) are
  never confused for one another.

Honest scope of "never silently persisted": promotion into a tool's anchor
of record happens only through `vent verify`, under a human who is shown
the target window and the risk of every tool the promotion would rewrite.
A quarantined fix that has not been promoted does not change any tool's
stored anchor. It can, however, be reused to satisfy a live call while it
sits in quarantine: on a repeat break the runtime reuses a matching
quarantined fix deterministically (no model call), re-screening the live
target every time. So an un-promoted fix affects behavior for the calls it
rescues, but it never becomes the durable anchor and never escapes the
per-call re-screen. Model-backed healing that mints new quarantine entries
is opt-in (`VENT_HEAL=1` on the server, `--heal` on `vent run`); the served
default neither calls a model nor writes quarantine entries.

### T9. The MCP client side

After compile-time approval, no human sits between an MCP client and a tool
call. Clients are often themselves model-driven, so compile-time approval
must not be treated as call-time consent. Mitigations:

- Every ToolSpec carries a `risk` level (`read_only`, `mutating`, `high`)
  assigned at the approval gate. The server executes `read_only` freely,
  `mutating` per user-configured policy, and `high` only after a call-time
  confirmation round trip (MCP elicitation), especially tools that pipe
  free-text parameters into `set_value` against terminals, mail, or chat
  apps, which are injection primitives in their own right.
- The server speaks stdio only, launched by the client. No network port, no
  LAN surface. If an HTTP transport is ever added it must bind 127.0.0.1
  with a bearer token, and this document must change first.

### T10. Blast radius of the Accessibility grant

macOS grants Accessibility to the host process, not per target app, and not
per pack. Once the user's terminal is trusted, anything running in it can
drive every app on the system; Ventriloquist's own scoping (named apps,
policy, risk levels) is self-discipline within that grant, not an OS
boundary. This document says so plainly instead of implying otherwise.
Practical guidance: grant the permission to a dedicated terminal profile
used for Ventriloquist, review packs before serving them, and treat
`vent serve` as giving the connected MCP client exactly the capabilities the
approved packs describe, unattended.

## Standing rules for contributors

- New step ops require an update to this document and a policy review.
- `runtime.py` and `server.py` must never import `llm.py`.
- Healed anchors are never auto-promoted. No exceptions for "obvious" fixes.
- No screenshots, no pixel coordinates, no `exec`, no AppleScript built from
  model output. These are load-bearing absences.
