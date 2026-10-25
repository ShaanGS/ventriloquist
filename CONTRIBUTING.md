# Contributing to Ventriloquist

Thanks for looking at this. A few things will make a change land smoothly,
and some of them are load-bearing rather than stylistic.

## Getting set up

vent needs Python 3.11 or newer. The `python3` that comes with macOS is
3.9, so it won't do. The easy path is [uv](https://docs.astral.sh/uv/)
(`brew install uv`), which fetches a suitable Python and the dependencies
for you:

```bash
uv run pytest
```

Without uv, make the environment with Python 3.11 or newer (`brew install
python` gives you one; check with `python3 --version`):

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

An editable install reads files straight from the checkout, so it cannot
show a packaging mistake. CI also builds the wheel, installs it into a
clean environment, and runs `tests/install_smoke.py` against it, and the
release workflow runs the whole suite against the built wheel before it
publishes anything. To do the same locally:

```bash
python3 -m venv --clear /tmp/vent-env
/tmp/vent-env/bin/pip install .    # a regular install, built as a wheel
cd /tmp && /tmp/vent-env/bin/python "$OLDPWD/tests/install_smoke.py" /tmp/vent-env/bin
```

## Read these first

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains every module and why
  it exists. Read it before the code.
- [docs/SECURITY.md](docs/SECURITY.md) is the threat model. It is not
  aspirational; the code implements it, several of its rules are checked
  in tests, and what isn't built yet is marked Planned.

## The rules that are not negotiable

These are the load-bearing invariants. A change that breaks one is wrong
even if the tests pass, so the tests exist to make breaking them loud.

1. **The action space stays closed.** The runtime executes a fixed set of
   typed ops. There is no `exec`, no shell, no AppleScript built from model
   output. Adding an op means updating SECURITY.md T1 first, then the
   validator in `packs.py`, then the runtime.
2. **`runtime.py` and `server.py` never import `llm.py`.** The deterministic
   half does not depend on a model. Healing reaches it only through the
   callback `runtime.execute` is handed, and only when healing is on.
3. **The model composes, it never authors.** The compiler builds tools only
   from actions the explorer actually executed. Model text is shown to the
   human but never becomes a step.
4. **Policy screens after the model, never before.** A model cannot approve
   its own action past the policy layer.
5. **Healed anchors are quarantined, not silently promoted.** Only
   `vent verify`, under a human, rewrites a tool's anchor of record.
6. **No screenshots, no pixel coordinates, no vision.** If a feature seems
   to need them, redesign the feature.

## Shipping a pack

Packs you record land in `~/.ventriloquist/packs/<bundle id>/pack.json`
(or under `VENT_HOME`). To ship one with vent:

1. Run `vent verify <app>` and the tool-level harness
   (`vent harness <app> --tools`) and put the numbers in the PR.
2. Scrub anything personal from the anchors: window titles, workspace
   and account names, document names. Recording leaves the window title
   out of the chain, including the copies Electron apps put on the group
   under the window and on its web area, but check labels and
   descriptions yourself too.
3. Copy the file to `vent/bundled_packs/<bundle id>/pack.json`.
   `tests/test_bundled_packs.py` checks that it loads, that no label looks
   like a window title, a file name or an email address, and that its
   descriptions name other tools the way clients see them
   (`code_show_search`, not `show_search`).
4. Add a row to the "What ships" table in the README.

## Gotchas you will hit

- **Run outside any sandbox.** The accessibility API needs the real
  permission grant, and a sandboxed shell reports the process as untrusted.
- **An app relaunched from a background process serves a menu-bar-only
  tree** until it is really brought to the front. If `vent inspect` shows
  next to nothing, click the app's window once.
- **A pending Accessibility permission prompt degrades accessibility for
  every app launched after it appears.** If everything suddenly reads
  empty, look for a permission prompt.
- **A locked screen makes every app look empty** while the permission check
  still passes. `vent doctor` says so.
- **Electron apps need `--force-renderer-accessibility`** at launch. Without
  it the tree reads fine and every action is silently ignored, which looks
  healthy until a press does nothing. VS Code's process is called `Code`
  but its app name is `Visual Studio Code`, and an in-place auto-update can
  wedge its accessibility bridge until a full quit and relaunch.
- **The scripted-model seam** is `llm.set_completer_for_tests(fn)`. Tests
  install a fake completer so the explorer, compiler and healer run with no
  network. Clear it in the fixture's teardown.

## Style

- Plain prose in docs and comments. No em dashes; a comma or a period does
  the same work.
- Match the surrounding code: its naming, its comment density, its idiom.
- A comment should say something the code cannot. Skip comments that
  restate the next line.
- Tests assert behavior, not implementation details, and each test name
  says what property it pins.

## Before you open a PR

- `uv run pytest` passes.
- New ops, new model prompts, or new network calls come with a SECURITY.md
  update in the same PR.
- If you touched the healer, the explorer, the policy, or the anchor
  resolver, add a test that would fail without your change.

Ventriloquist has been built one phase at a time, each phase adversarially
reviewed before the next began. A PR that keeps that bar (a clear
invariant, a test that pins it, an honest note on what it does not cover)
is easy to accept. Everyone taking part is expected to follow the
[code of conduct](CODE_OF_CONDUCT.md).
