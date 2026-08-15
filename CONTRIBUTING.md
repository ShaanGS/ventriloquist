# Contributing to Ventriloquist

Thanks for looking at this. A few things will make a change land smoothly,
and some of them are load-bearing rather than stylistic.

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/python -m pytest tests/
```

The test suite runs with no Mac permissions, no live apps, and no model
credentials. It uses in-memory fake accessibility trees (`tests/fakes.py`)
and a scripted-model seam (`llm.set_completer_for_tests`), so it runs the
same on your laptop and on Linux CI. If a change you make cannot be tested
that way, that is usually a sign the logic should move behind the `Element`
interface or the `llm` seam rather than a sign the test is impossible.

Live behavior (the `ax` layer, `vent inspect`, `vent run` against a real
app) needs macOS and Accessibility permission for your terminal, granted in
System Settings, Privacy & Security, Accessibility.

## Read these first

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains every module and why
  it exists. Read it before the code.
- [docs/SECURITY.md](docs/SECURITY.md) is the threat model. It is not
  aspirational; the code implements it, and several of its rules are checked
  in tests.

## The rules that are not negotiable

These are the load-bearing invariants. A change that breaks one is wrong
even if the tests pass, so the tests exist to make breaking them loud.

1. **The action space stays closed.** The runtime executes a fixed set of
   typed ops. There is no `exec`, no shell, no AppleScript built from model
   output. Adding an op means updating SECURITY.md T1 first, then the
   validator in `packs.py`, then the runtime.
2. **`runtime.py` and `server.py` never import `llm.py`.** The deterministic
   half does not depend on a model. Healing reaches it only through the
   callback `runtime.execute` is handed.
3. **The model composes, it never authors.** The compiler builds tools only
   from actions the explorer actually executed. Model text is shown to the
   human but never becomes a step.
4. **Policy screens after the model, never before.** A model cannot approve
   its own action past the policy layer.
5. **Healed anchors are quarantined, not silently promoted.** Only
   `vent verify`, under a human, rewrites a tool's anchor of record.
6. **No screenshots, no pixel coordinates, no vision.** If a feature seems
   to need them, redesign the feature.

## Style

- Plain prose in docs and comments. No em dashes; a comma or a period does
  the same work.
- Match the surrounding code: its naming, its comment density, its idiom.
- A comment should say something the code cannot. Skip comments that
  restate the next line.
- Tests assert behavior, not implementation details, and each test name
  says what property it pins.

## Before you open a PR

- `.venv/bin/python -m pytest tests/` passes.
- New ops, new model prompts, or new network calls come with a SECURITY.md
  update in the same PR.
- If you touched the healer, the explorer, the policy, or the anchor
  resolver, add a test that would fail without your change.

Ventriloquist has been built one phase at a time, each phase adversarially
reviewed before the next began. A PR that keeps that bar (a clear
invariant, a test that pins it, an honest note on what it does not cover)
is easy to accept.
