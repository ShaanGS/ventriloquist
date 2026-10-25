"""Ventriloquist CLI: compile Mac apps into MCP servers.

Foundation commands (doctor / apps / inspect / act) talk to live apps
directly. Pack commands (anchor / run / serve) exercise the compiled
path: anchor helps author packs by hand, run executes one tool locally,
and serve exposes every pack to MCP clients.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import click

from . import __version__, anchors, ax, packs, paths, runtime
from .snapshot import MAX_TREE_DEPTH, app_windows, render, snapshot


def _user_pack_path(bundle_id: str) -> Path:
    """Where a pack is saved. Always the user's directory, even when the pack
    was loaded from the bundled set (see paths.py)."""
    return paths.user_packs_dir() / bundle_id / "pack.json"


@click.group()
@click.version_option(__version__, prog_name="vent")
def main() -> None:
    """Give Mac apps an API."""


@main.command()
@click.argument("app_name", required=False)
def doctor(app_name: str | None) -> None:
    """Check Accessibility permission, or probe APP_NAME's tree health.

    With an app name, reports whether the app exposes a usable tree and
    whether web-content accessibility had to be requested (Chromium and
    Electron apps ship with it off).
    """
    if not ax.is_trusted():
        click.secho("✗ Accessibility permission missing.", fg="red")
        click.echo(f"  {ax.trust_hint()}")
        sys.exit(1)
    click.secho("✓ Accessibility permission granted. Ready to go.", fg="green")
    click.echo(
        "  Using Claude Desktop? It checks vent's Python rather than itself, "
        f"so give this file Accessibility too:\n    {_vent_python()}"
    )

    if not app_name:
        return
    from . import harness as harness_mod

    try:
        app = ax.find_app(app_name)
        root = ax.app_element(app)
        harness_mod.enable_web_accessibility(root)
        import time as time_mod

        time_mod.sleep(1.0)
        snap = snapshot(root, max_nodes=3000)
        web_children = sum(
            1 for n in snap.nodes if n.role == "AXWebArea"
        )
        windows = app_windows(root)
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)

    click.echo(f"{app.name}: {len(snap.nodes)} elements after web-accessibility request")
    if not snap.nodes:
        if ax.session_locked():
            click.secho(
                "✗ The screen is locked. Every app serves an empty tree until "
                "the session is unlocked; this says nothing about the app.",
                fg="red",
            )
        elif not windows:
            click.secho(
                f"✗ {app.name} is running but has no open window. "
                "Open one and run vent doctor again.",
                fg="red",
            )
        else:
            click.secho("✗ No usable tree. This app cannot be packed yet.", fg="red")
            click.echo(
                "  If the app was launched from a script, foreground it once by "
                "clicking its window; background-launched apps serve degraded trees."
            )
        sys.exit(1)
    if web_children:
        click.secho(f"✓ Tree present, including {web_children} web area(s).", fg="green")
    else:
        click.secho("✓ Tree present.", fg="green")
    if snap.off_space:
        click.echo(
            "  Its window is on another Space, so vent reached it through the "
            "app's main window. Other windows the app has on other Spaces stay "
            "out of reach until you switch to them."
        )


@main.command("harness")
@click.argument("app_name")
@click.option("--cap", default=40, show_default=True, help="How many anchors to record.")
@click.option("--restart", is_flag=True, help="Also quit and relaunch the app (strongest churn).")
@click.option("--reopen", default=None, help="File to reopen after --restart, restoring the app's document state.")
@click.option("--tools", "tools_mode", is_flag=True, help="Measure end-to-end tool success instead of per-anchor survival. Runs every non-high tool in the app's pack for real.")
@click.option("--cycles", default=3, show_default=True, help="With --tools: how many run cycles (every second cycle is resized).")
def harness_cmd(app_name: str, cap: int, restart: bool, reopen: str | None, tools_mode: bool, cycles: int) -> None:
    """Measure anchor durability for a running app.

    Records anchors, perturbs the app (zoom resize, and optionally a full
    restart), and reports how many anchors still resolve to the right
    element. WRONG resolutions are the number to watch; the design goal
    is zero.

    With --tools, measures what a caller actually experiences instead:
    every tool in the app's pack is executed end to end, repeatedly, with
    a window resize between cycles. A tool touches several anchors plus
    settling and verification, so this number is always at or below the
    per-anchor one, and it is the honest headline.
    """
    from . import harness as harness_mod

    try:
        if tools_mode:
            pack = _find_pack(app_name)
            report = harness_mod.run_tools(pack, app_name, cycles=cycles)
        else:
            report = harness_mod.run(app_name, cap=cap, restart=restart, reopen=reopen)
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)
    click.echo(report.render())


@main.command()
def apps() -> None:
    """List running applications vent can attach to."""
    for app in sorted(ax.running_apps(), key=lambda a: a.name.lower()):
        click.echo(f"{app.name:30} pid={app.pid:<8} {app.bundle_id or ''}")


@main.command()
@click.argument("app_name")
@click.option("--depth", default=MAX_TREE_DEPTH, show_default=True, help="Maximum tree depth to walk.")
@click.option("--max-nodes", default=800, show_default=True, help="Cap on surfaced elements.")
@click.option("--menus", is_flag=True, help="Include the menu bar (skipped by default).")
def inspect(app_name: str, depth: int, max_nodes: int, menus: bool) -> None:
    """Print the semantic accessibility snapshot of a running app."""
    try:
        app = ax.find_app(app_name)
        root = ax.app_element(app)
        snap = snapshot(root, max_depth=depth, max_nodes=max_nodes, include_menus=menus)
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)

    click.secho(f"{app.name} (pid {app.pid}): {len(snap.nodes)} elements", bold=True)
    click.echo(render(snap))


@main.command()
@click.argument("app_name")
@click.argument("node_id", type=int)
@click.option("--action", "action_name", default="AXPress", show_default=True)
@click.option("--menus", is_flag=True, help="Include the menu bar when resolving ids.")
@click.option("--text", default=None, help="Set this text as the element's value instead of performing an action.")
def act(app_name: str, node_id: int, action_name: str, menus: bool, text: str | None) -> None:
    """Perform an action on element NODE_ID from a fresh `inspect` snapshot."""
    try:
        app = ax.find_app(app_name)
        root = ax.app_element(app)
        snap = snapshot(root, include_menus=menus)
        if node_id >= len(snap.nodes):
            click.secho(f"No element #{node_id} (snapshot has {len(snap.nodes)}).", fg="red")
            sys.exit(1)
        node = snap.nodes[node_id]
        click.echo(f"acting on: #{node.id} {node.role} {node.label!r} in {node.window_title!r}")
        if text is not None:
            node.element.set_value(text)
            click.secho(f"✓ Set value of #{node_id} {node.role} {node.label!r}", fg="green")
        else:
            node.element.perform(action_name)
            click.secho(f"✓ {action_name} on #{node_id} {node.role} {node.label!r}", fg="green")
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)


@main.command()
@click.argument("app_name")
@click.argument("node_id", type=int)
@click.option("--menus", is_flag=True, help="Include the menu bar when resolving ids.")
def anchor(app_name: str, node_id: int, menus: bool) -> None:
    """Print the durable anchor for element NODE_ID, for authoring packs."""
    try:
        app = ax.find_app(app_name)
        root = ax.app_element(app)
        snap = snapshot(root, include_menus=menus)
        if node_id >= len(snap.nodes):
            click.secho(f"No element #{node_id} (snapshot has {len(snap.nodes)}).", fg="red")
            sys.exit(1)
        built = anchors.build(snap.nodes[node_id])
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)
    click.echo(json.dumps(built.to_dict(), indent=2))


def _find_pack(name: str) -> packs.Pack:
    try:
        loaded = packs.load_search_path(paths.pack_search_path())
    except packs.PackError as exc:
        raise click.ClickException(str(exc))
    for pack in loaded:
        if name.lower() in {pack.bundle_id.lower(), pack.app_name.lower()}:
            return pack
    available = ", ".join(p.app_name for p in loaded) or "none"
    raise click.ClickException(f"No pack for {name!r}. Available: {available}")


@main.command()
@click.argument("app_name")
@click.argument("tool_name")
@click.option("--arg", "arg_pairs", multiple=True, help="Tool argument as key=value. Repeatable.")
@click.option("--heal", "do_heal", is_flag=True, help="Re-ground broken anchors with a model and quarantine the fix.")
def run(app_name: str, tool_name: str, arg_pairs: tuple[str, ...], do_heal: bool) -> None:
    """Execute one compiled tool against the live app, without MCP."""
    from . import heal as heal_mod

    pack = _find_pack(app_name)
    args = {}
    for pair in arg_pairs:
        if "=" not in pair:
            raise click.ClickException(f"--arg must be key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        args[key] = value

    try:
        app = ax.find_app_by_bundle(pack.bundle_id)
        root = ax.app_element(app)
        low_confidence = packs.is_stale(pack, ax.app_version(app))
        heal_cb = None
        if do_heal or pack.healed_pending:
            pack_path = _user_pack_path(pack.bundle_id)
            heal_cb = heal_mod.make_heal_callback(
                pack, pack_path, notify=click.echo, ask_model=do_heal, low_confidence=low_confidence,
            )
        result = runtime.execute(
            pack, tool_name, args, root, app=app, low_confidence=low_confidence, heal=heal_cb,
        )
    except (ax.AXError, runtime.ToolExecutionError, packs.PackError, OSError) as exc:
        click.secho(f"✗ {exc}", fg="red")
        sys.exit(1)

    click.secho(f"✓ {pack.app_name}.{tool_name}: {result.detail}", fg="green")
    for value in result.values:
        click.echo(value)


@main.command()
@click.argument("app_name")
@click.option(
    "--report",
    "report_path",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help=(
        "Write a JSON portability report and skip the interactive promotion "
        "walk. Made for running a pack on a machine other than the one that "
        "recorded it: no model calls, and the only app content included is "
        "the live label of each resolved element, so the file can be read "
        "before it is shared."
    ),
)
def verify(app_name: str, report_path: str | None) -> None:
    """Dry-resolve every anchor in a pack and offer to promote healed ones.

    Reports a durability percentage without executing any step, then walks
    any quarantined re-groundings and asks whether to promote each into the
    tool's anchor of record. Promotion is the only path that rewrites a
    tool's anchors, and it always passes through this human gate.
    """
    from . import anchors, heal as heal_mod

    pack = _find_pack(app_name)
    try:
        app = ax.find_app_by_bundle(pack.bundle_id)
        root = ax.app_element(app)
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)

    low_confidence = packs.is_stale(pack, ax.app_version(app))
    if low_confidence:
        click.secho("Pack is stale for this app version; anchors load low-confidence.", fg="yellow")

    # Dry-resolve every distinct anchor across steps AND verify blocks. An
    # anchor shared by several steps is counted once; verify anchors break
    # too, so they belong in the durability number.
    seen: set = set()
    total = resolved = 0
    drift: list = []
    entries: list = []
    for tool in pack.tools:
        anchored = [(s.op, s.anchor) for s in tool.steps if s.anchor]
        anchored += [("verify", v.anchor) for v in tool.verify if v.anchor]
        for op, anchor in anchored:
            key = (anchor.role, anchor.identifier, tuple(anchor.labels), anchor.window_title,
                   tuple((l.role, l.ordinal) for l in anchor.chain))
            if key in seen:
                continue
            seen.add(key)
            total += 1
            entry = {
                "tool": tool.name,
                "op": op,
                "role": anchor.role,
                "recorded_labels": list(anchor.labels),
                "recorded_identifier": anchor.identifier,
            }
            try:
                element = anchors.resolve(root, anchor, low_confidence=low_confidence)
                resolved += 1
            except (anchors.AnchorLost, anchors.AnchorAmbiguous) as exc:
                click.secho(f"  {tool.name} ({op}): {exc}", fg="yellow")
                entry["outcome"] = "ambiguous" if isinstance(exc, anchors.AnchorAmbiguous) else "lost"
                entry["detail"] = str(exc)[:200]
                entries.append(entry)
                continue
            drifted = anchors.semantic_drift(anchor, element)
            entry["outcome"] = "resolved"
            entry["live_label"] = element.label[:80]
            entry["drift"] = drifted
            entries.append(entry)
            if drifted:
                drift.append((tool.name, op, anchor.labels, element.label))

    pct = (resolved / total * 100) if total else 100.0
    color = "green" if resolved == total else "yellow"
    click.secho(f"{pack.app_name}: {resolved}/{total} anchors resolve ({pct:.0f}%)", fg=color)

    if drift:
        click.echo()
        click.secho(
            f"⚠ {len(drift)} anchor(s) resolved onto a label they have never "
            "been recorded under. The element was found, but its meaning may "
            "have changed. Re-confirm these tools do what their names say, "
            "then re-record the anchors to adopt the new labels:",
            fg="yellow",
        )
        for tool_name, op, known, live in drift:
            click.echo(f"  {tool_name} ({op}): recorded {known!r}, live label {live!r}")

    if report_path:
        import platform as platform_mod
        from datetime import datetime, timezone

        report = {
            "vent_report_version": 1,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": {
                "os_version": platform_mod.mac_ver()[0],
                "arch": platform_mod.machine(),
            },
            "app": {
                "name": pack.app_name,
                "bundle_id": pack.bundle_id,
                "live_version": ax.app_version(app),
                "pack_recorded_version": pack.app_version,
                "pack_recorded_os": pack.os_version,
            },
            "summary": {
                "total": total,
                "resolved": resolved,
                "drift_flagged": len(drift),
            },
            "anchors": entries,
        }
        Path(report_path).write_text(json.dumps(report, indent=2) + "\n")
        click.echo()
        click.echo(f"Report written to {report_path}.")
        click.echo(
            "It contains no packs, no snapshots, and no model output; the only "
            "app content is each resolved element's live label. Read it before "
            "sharing it."
        )
        return

    if not pack.healed_pending:
        return

    click.echo()
    click.secho(f"{len(pack.healed_pending)} quarantined re-grounding(s):", bold=True)
    approved = []
    for entry in pack.healed_pending:
        original = entry.get("original", {})
        window = entry.get("target_window", "")
        click.echo(
            f"  a broken {original.get('role', '?')} "
            f"{original.get('labels') or original.get('identifier') or ''} "
            f"was healed onto {entry.get('target_role', '?')} {entry.get('target_label', '')!r}"
            + (f" in window {window!r}" if window else "")
        )
        # Show which tools this promotion would rewrite and their risk, so a
        # reviewer can weigh a re-grounding against what the tool can do.
        broken = anchors.Anchor.from_dict(original)
        for tool in pack.tools:
            touched = any(
                s.anchor and heal_mod._same_broken(s.anchor.to_dict(), broken) for s in tool.steps
            )
            if touched:
                risk_color = "red" if tool.risk == "high" else "yellow"
                click.secho(f"    would rewrite tool {tool.name!r} (risk: {tool.risk})", fg=risk_color)
        if click.confirm("  Promote this fix into the tool?", default=False):
            approved.append(entry)

    # Apply after all decisions, matching by object identity so pops during
    # promotion cannot shift the wrong entry out.
    promoted_any = False
    for entry in approved:
        index = next(i for i, e in enumerate(pack.healed_pending) if e is entry)
        count = heal_mod.promote(pack, index)
        if count:
            click.secho(f"  ✓ rewrote {count} anchor(s)", fg="green")
            promoted_any = True
        else:
            click.secho("  ⚠ nothing to rewrite; the tool changed since this fix. Kept in quarantine.", fg="yellow")

    if promoted_any:
        pack_path = _user_pack_path(pack.bundle_id)
        packs.save(pack, pack_path)
        click.secho(f"Pack updated: {pack_path}", fg="green")


@main.command()
@click.argument("app_name")
@click.option("--rounds", default=3, show_default=True, help="Probe rounds to run.")
@click.option("--no-values", is_flag=True, help="Redact field values from snapshots sent to the model.")
def explore(app_name: str, rounds: int, no_values: bool) -> None:
    """Probe a running app under the safety policy and record a trace.

    The model nominates elements; the policy screens every nomination;
    only reversible or budgeted actions execute. The trace is saved for
    `vent compile`.
    """
    from . import explorer, llm, policy as policy_mod

    try:
        app = ax.find_app(app_name)
        root = ax.app_element(app)
        ax.activate(app)
        pol = policy_mod.Policy()
        trace = explorer.explore(app, root, pol, rounds=rounds, notify=click.echo, redact_values=no_values)
    except (ax.AXError, llm.ModelError, explorer.ExplorationBlocked) as exc:
        click.secho(f"✗ {exc}", fg="red")
        sys.exit(1)

    path = explorer.save_trace(trace, paths.traces_dir())
    executed = sum(1 for a in trace.actions if a.executed)
    refused = len(trace.actions) - executed
    click.secho(
        f"✓ Explored {app.name}: {executed} action(s) executed, "
        f"{refused} refused by policy. Trace: {path}",
        fg="green",
    )


@main.command("compile")
@click.argument("app_name")
def compile_cmd(app_name: str) -> None:
    """Compile a recorded trace into pack tools, with human approval.

    Each proposed tool is shown two ways: the model's description, and a
    deterministic summary built from the literal recorded steps. The steps
    are the ground truth; nothing is written without approval.
    """
    import locale as locale_mod
    import platform as platform_mod

    from . import compiler, explorer, llm

    try:
        app = ax.find_app(app_name)
        if not app.bundle_id:
            raise ax.AXError(f"{app.name} has no bundle id; cannot compile a pack for it.")
        trace = explorer.load_trace(paths.traces_dir(), app.bundle_id)
    except FileNotFoundError:
        click.secho(f"No trace for {app_name!r}. Run `vent explore {app_name}` first.", fg="red")
        sys.exit(1)
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)

    # New tools join the pack already there, so a pack can grow over several
    # explore runs. With no pack of the user's own, the bundled one is the
    # start: the user's pack shadows it, so leaving it out would hide its
    # tools. A user pack that can't be read is only replaced on a yes.
    pack_path = _user_pack_path(app.bundle_id)
    bundled_path = paths.BUNDLED_PACKS_DIR / app.bundle_id / "pack.json"
    existing = None
    if pack_path.exists():
        try:
            existing = packs.load(pack_path)
        except packs.PackError as exc:
            click.secho(f"✗ {exc}", fg="red")
            if not click.confirm(f"Replace your {app.name} pack with only the tools you approve now?", default=False):
                sys.exit(1)
    elif bundled_path.exists():
        existing = packs.load(bundled_path)
        click.echo(f"Starting from the bundled {app.name} pack, so its tools stay available.")
    have = {tool.name for tool in existing.tools} if existing else set()

    try:
        proposals = compiler.propose(trace)
    except llm.ModelError as exc:
        click.secho(f"✗ {exc}", fg="red")
        sys.exit(1)

    if not proposals:
        click.echo("The model proposed no tools from this trace.")
        return

    approved = []
    for proposal in proposals:
        try:
            spec = compiler.build_spec(proposal)
        except compiler.CompileError as exc:
            click.secho(f"  skipping a proposal: {exc}", fg="yellow")
            continue
        click.echo()
        click.secho(f"Proposed: {spec.name}", bold=True)
        click.echo(f"  Model description: {spec.description}")
        click.echo(compiler.deterministic_summary(spec, trace.app_name))
        for warning in compiler.description_mismatch(spec, trace.app_name):
            click.secho(f"  ⚠ {warning}", fg="yellow")
        if spec.risk == "high":
            click.secho("  ⚠ high risk tool", fg="red")
        if click.confirm("Approve this tool?", default=False):
            if spec.name in have and not click.confirm(
                f"Your pack already has {spec.name}. Replace it with this one?", default=False
            ):
                continue
            approved.append(spec)

    if not approved:
        click.echo("Nothing approved; no pack written.")
        return

    pack = compiler.assemble_pack(
        trace,
        approved,
        os_version=platform_mod.mac_ver()[0],
        locale=(locale_mod.getlocale()[0] or ""),
    )
    if existing:
        merged = packs.merge(existing, pack)
        if merged.app_version != pack.app_version or merged.os_version != pack.os_version:
            click.secho(
                f"  The tools already in the pack were recorded on a different {app.name} or macOS "
                "version, so the pack keeps that version and its anchors load cautiously. "
                "Re-record those tools to clear it.",
                fg="yellow",
            )
        pack = merged
    packs.save(pack, pack_path)
    click.secho(f"✓ Wrote {len(approved)} tool(s) to {pack_path} ({len(pack.tools)} in the pack)", fg="green")


@main.command()
def serve() -> None:
    """Serve every compiled pack as MCP tools over stdio.

    High risk tools are refused unless the server was started with
    VENT_ALLOW_HIGH=1 in the environment. See docs/SECURITY.md.
    """
    from .server import serve_stdio

    serve_stdio(paths.pack_search_path())


def serve_entry() -> None:
    """The `ventriloquist-mcp` command: the MCP server on its own, for
    launchers that run a package by its name (`uvx ventriloquist-mcp`)."""
    serve()


def _vent_executable() -> str:
    """Absolute path of the vent command that is running. Symlinks are not
    resolved: a uv or pipx shim stays valid across upgrades, while the
    environment it points into may be replaced."""
    invoked = Path(sys.argv[0])
    if invoked.name == "vent":
        return os.path.abspath(invoked)
    return shutil.which("vent") or "vent"


def _vent_python() -> str:
    """The interpreter binary running vent. Claude Desktop checks this file
    for Accessibility when it starts the entry mcp-config prints, since the
    vent script runs on it."""
    return ax.executable_path(os.getpid()) or os.path.realpath(sys.executable)


@main.command("mcp-config")
def mcp_config() -> None:
    """Print a ready-to-paste MCP client entry for this install of vent.

    Claude Desktop and Cursor start MCP servers without your shell's PATH,
    so the entry names vent by its absolute path.
    """
    command = _vent_executable()
    entry = {"mcpServers": {"ventriloquist": {"command": command, "args": ["serve"]}}}
    click.echo(json.dumps(entry, indent=2))
    click.echo(
        "\nPaste this into Claude Desktop's claude_desktop_config.json or "
        "Cursor's ~/.cursor/mcp.json, merging it into an existing mcpServers "
        "block if there is one. For Claude Code, run:\n"
        f"  claude mcp add --scope user ventriloquist -- {shlex.quote(command)} serve\n"
        "Then give the app that starts vent Accessibility permission: Cursor, "
        "or your terminal for Claude Code (see vent doctor).\n\n"
        "Claude Desktop works differently. Edit its config while it is closed, "
        "because it rewrites the file while it runs. It also checks vent's "
        "Python rather than itself, so give this file Accessibility, dragging "
        "it from Finder into System Settings, Privacy & Security, "
        "Accessibility (the + picker greys it out):\n"
        f"  {_vent_python()}",
        err=True,
    )


if __name__ == "__main__":
    main()
