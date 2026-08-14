# Ventriloquist

**Compile any Mac app into an MCP server.**

Every computer-use agent today is an interpreter: a language model stares at
your screen and drives every click, every time. Slow, expensive, flaky.
Ventriloquist is a compiler. Point it at a running app and an agent explores
the app's accessibility tree once. Out comes a persistent MCP server with
typed, semantic tools like `create_playlist(name)` or `write_document(text)`,
each backed by a deterministic accessibility replay that runs in milliseconds
with zero model calls. The model is only re-engaged when a replay breaks, and
its fix is quarantined until you approve it.

No screenshots. No vision models. No pixel coordinates. The accessibility
layer has shipped with every Mac app for decades. Screen readers proved it
works; Ventriloquist makes it programmable.

## Status

Early. The introspection and action layers work today:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .

.venv/bin/vent doctor            # check Accessibility permission
.venv/bin/vent apps              # list running apps
.venv/bin/vent inspect Notes     # semantic snapshot of a live app
.venv/bin/vent act Notes 4       # press element #4 from that snapshot
```

`vent inspect` requires Accessibility permission for your terminal:
System Settings, then Privacy & Security, then Accessibility.

## Design

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before reading the code.
The threat model lives in [docs/SECURITY.md](docs/SECURITY.md). Both were
adversarially reviewed before implementation and rewritten from the findings.

## Roadmap

1. **Prove the hypothesis.** Scored anchor resolution plus a durability
   harness measured against structurally diverse apps, including an Electron
   app. Hand-written packs for TextEdit and Notes served over MCP stdio.
2. **The explorer.** Survey, probe under the safety policy, and compile
   traces into ToolSpecs that match the hand-written packs.
3. **Healing and a third-party app.** Quarantined anchor healing end to end.
4. **Ship.** Docs, tests, demo video, release.
