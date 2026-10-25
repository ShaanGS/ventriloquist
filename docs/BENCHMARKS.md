# Benchmarks

Measurements of vent on the author's Mac (macOS 26): two comparisons with
Claude's built-in computer use, how long a call takes, and how well
anchors survive changes to the app. These are small samples, not a study;
they show where vent helps and where it doesn't.

## Against built-in computer use

Both comparisons ran on one Mac through Claude Code, with the same model
making the decisions on both sides. Times and tokens come from the session
log.

### A one-step task: TextEdit (2026-10-22)

Replace the text of a TextEdit document, then confirm it. Three rounds per
method, alternating.

| Per run, median of 3 | Computer use | vent before the fixes | vent with the tree-walk fix |
| --- | --- | --- | --- |
| Tool calls | 2 | 2 | 2 |
| Total time | 8.6 s | 8.9 s | 7.5 s |
| New context tokens | about 2,600 | about 410 | about 410 |

The first measurement went against vent: every call walked TextEdit's
whole accessibility tree, most of it the menu bar. Fixing that gave the
right-hand column. 0.2.2 also removed a 0.6 s wait from every write, which
brought a TextEdit write through Claude Code to about 1.1 s and a read to
about half a second. Even so, on a task this short the two are about even
on time, and most of it is the model thinking. vent's edge is the context:
it returns text, not screenshots.

### A multi-step task: Outlook triage (2026-10-23)

Six test emails in a folder of new Outlook for Mac 16.110.1, in a personal
Gmail account. For each one: read it, flag it if it needs a reply or has a
deadline, and file it into Receipts, Meetings or Reading. Run 1 used vent's
[Outlook example pack](../examples/outlook); run 2 used computer use in
full-screen mode (screenshots, zooms, clicks, typing). The folder was reset
between runs, and both were scored against a fixed answer key.

| | vent | Computer use |
| --- | --- | --- |
| Final result | 6/6 folders, both flags right | 6/6 folders, both flags right |
| Wrong actions on the way | 0 | 2 |
| Wall time | 1:59 | 2:31 |
| Model time | 67 s | about 103 s |
| Tool calls | 14 | 12 |
| New context tokens | 5,063 | 18,089 |
| Output tokens | 1,198 | 8,018 |

Computer use's two wrong actions came from Outlook's Move menu, which lists
recently used folders above Choose Folder... in an order that changes after
every move: two clicks moved an email into the folder it was already in.
After that it checked the menu before each click. vent's filing tools always
go through the folder picker and require exactly one match.

Caveats:

- One run each, so the times will move around between runs.
- The same model ran both sides and knew the answer key, so this compares
  doing the work, not judging the emails. (The demo video is a separate run
  by a fresh session that had never seen the answers; it also filed 6 of 6.)
- Computer use had a shortcut: the recent-folder list only existed because
  the vent run had just used those folders. On a fresh mailbox it would need
  the folder picker, with more steps.
- Most of vent's tool time is Outlook coming to the front and handing focus
  back, 4 to 6 s per flag or filing call.

Context tokens are the cache-creation tokens of the request that follows
each model response, counted once per response.

## Time per call: TextEdit (2026-10-22)

The TextEdit pack's write and read tools on vent 0.2.2: five
write-then-read rounds through Claude Code, and ten through a plain MCP
client that started `vent serve` itself.

| Per call | Write | Read |
| --- | --- | --- |
| Through Claude Code, end to end | 0.91 to 1.16 s (median 1.09 s) | 0.47 to 0.69 s, one at 1.67 s |
| Through a plain MCP client | 0.54 to 0.55 s | 0.12 s |

The plain client is vent's own share; the rest of the top row is the
client. A write sets the text, waits for TextEdit to settle, and reads it
back to check it. A read finds one element (about 45 ms once warm) and
returns its text.

## Anchor durability

`vent harness <app>` records anchors for an app's elements, messes with
the app (a real window resize, and optionally a full quit and relaunch),
and then tries to resolve every anchor again. Survival only counts when
the element found is provably the one that was recorded. Binding a
lookalike counts as WRONG, which is the number that actually matters.

Numbers from the author's Mac (macOS 26, small anchor sets, early days):

```
TextEdit: baseline 100%,  resized 100%,   0 wrong
Finder:   baseline 100%,  resized 100%,   0 wrong
VS Code:  baseline  95%,  resized  87.5%, restart 92.5%, 0 wrong  (40 anchors)
Slack:    baseline  95%,  resized  82.5%, restart 82.5%, 0 wrong  (40 anchors)
```

Slack matters here more than the others. TextEdit and Finder have
AppleScript, VS Code has a CLI and an extension API, so the apps that
justify this project are the ones with no scripting surface at all, and
Slack is the first of those measured. Its tree has no identifiers
anywhere, so anchors ride entirely on labels and ancestor chains, and
they still come back with zero wrong bindings across resize and a full
restart. Every non-survivor is an ambiguity refusal.

Figma desktop is the deliberate worst case and it behaves like one: the
app shell (tabs, navigation, buttons) publishes a usable tree, and the
design canvas publishes nothing, because it's rendered pixels. That's
the honest boundary of the accessibility thesis. Apps whose content is
a custom-drawn canvas keep their chrome scriptable and their content
opaque.

The VS Code restart round quits and relaunches the app completely, and 37
of 40 anchors still bind to provably the same element afterward. The ones
that don't survive are refusals, not mistakes: Chromium trees are full of
unlabeled twin groups, and when two candidates score too close together
the resolver refuses to pick one. The VS Code pack's own curated anchors
went 6 for 6 across view switches, a resize, and an actual app update.

### Whole tools

Per-anchor survival flatters what a caller actually experiences, because
a tool touches several anchors plus settling and verification, so there
is a second harness mode that runs whole tools end to end.
`vent harness <app> --tools` executes every tool in the app's pack
repeatedly, resizing the window every other cycle:

```
TextEdit: 8/8 calls passed   (4 cycles)
VS Code:  12/16 calls passed (4 cycles, every failure the same tool)
Slack:    6/8 calls passed   (4 cycles, both failures ambiguity refusals)
```

The Slack pack is thin on purpose (navigation and a parameterized
sidebar filter), but these are whole tools doing work, not just anchors
surviving.
Its two failures happen when the resized window reflows the nav rail and
the Home tab gains a lookalike; the resolver refuses to pick between
them, which is a safe failure and still a failure. Nothing wrong was
pressed in any cycle on any app.

The VS Code failures are all `search_workspace` called cold. Its
description says to open the Search view first, but that dependency is
prose, not a machine-checked precondition, so a caller who ignores it
fails and so does this harness, on purpose. The anchors were fine in
every failing call. That gap between 87.5% per-anchor and 75% per-tool
is the honest cost of state in a GUI, and stateful preconditions are the
planned fix.

### A changed environment

There is also a first portability data point, from a deliberately
degraded environment rather than a second machine: the VS Code pack was
re-verified after collapsing the sidebar, closing every file and folder,
switching theme, disabling extensions, and resizing the window. The
three activity bar anchors resolved exactly, unmoved by the extension
removals that changed their sibling set. The two view-content anchors
whose views no longer existed failed the right way, one by refusing
outright, and one by binding a same-role neighbor, an open editor pane,
which the semantic drift check flagged. That near-miss became a runtime
rule: an element wearing a label its anchor has never been recorded
under is never acted on, so the call that would have typed into the open
editor now refuses and names the element it declined to touch. What this
run does not show is cross-machine portability; it was the same machine,
account, and app install. `vent verify <app> --report out.json` exists
for the real test: it produces a small JSON resolution table with no
model calls and no app content beyond each element's live label, meant
to be run on someone else's Mac and sent back.

### Field notes

Some field notes that ended up shaping the code: macOS AX trees can be
cyclic (a wedged TextEdit once returned the app element as its own child,
so every walk carries a cycle guard), apps relaunched from a background
process serve junk menubar-only trees until they're genuinely foregrounded,
a pending permission dialog silently degrades AX for every app launched
after it, and a locked screen makes every app read as empty while the
permission check still passes.
