# Ventriloquist

**Compile any Mac app into an MCP server.**

Every computer-use agent today is an *interpreter*: an LLM stares at your screen and drives every click, every time — slow, expensive, flaky. Ventriloquist is a *compiler*: point it at a running app, an agent explores the app's accessibility tree **once**, and out comes a persistent MCP server with typed, semantic tools (`create_playlist(name)`, `export_pdf(path)`) — each backed by a deterministic accessibility-path replay that runs in milliseconds with **zero LLM calls**. The agent is only re-engaged when a path breaks, and the fix is cached back.

No screenshots. No vision models. No pixel coordinates. The accessibility layer has been in every Mac app since forever — screen readers proved it works; we make it programmable.

## Status

Early. Foundation layer (accessibility introspection) is working:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .

.venv/bin/vent doctor            # check Accessibility permission
.venv/bin/vent apps              # list running apps
.venv/bin/vent inspect Notes     # semantic snapshot of a live app
```

`vent inspect` requires Accessibility permission for your terminal:
System Settings → Privacy & Security → Accessibility.

## Roadmap

1. **Inspect** — semantic snapshots of any app's UI (done)
2. **Act** — press / type / set-value on elements by stable path
3. **Explore** — agent walks the app, discovers workflows, names them
4. **Compile** — workflows → typed tool definitions + replay scripts on disk
5. **Serve** — `vent serve <app>` exposes the compiled tools as an MCP server
6. **Heal** — on replay failure, re-ground the path with the agent and re-cache
