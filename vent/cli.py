"""Ventriloquist CLI: compile Mac apps into MCP servers.

Foundation commands (doctor / apps / inspect / act) talk to live apps
directly. Pack commands (anchor / run / serve) exercise the compiled
path: anchor helps author packs by hand, run executes one tool locally,
and serve exposes every pack to MCP clients.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import anchors, ax, packs, runtime
from .snapshot import MAX_TREE_DEPTH, render, snapshot

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"
TRACES_DIR = Path(__file__).resolve().parent.parent / "traces"


@click.group()
def main() -> None:
    """Give every Mac app an API."""


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
        click.echo(
            "  Open System Settings, Privacy & Security, Accessibility, and "
            "enable your terminal (or the app running vent), then rerun."
        )
        sys.exit(1)
    click.secho("✓ Accessibility permission granted. Ready to go.", fg="green")

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


@main.command("harness")
@click.argument("app_name")
@click.option("--cap", default=40, show_default=True, help="How many anchors to record.")
@click.option("--restart", is_flag=True, help="Also quit and relaunch the app (strongest churn).")
@click.option("--reopen", default=None, help="File to reopen after --restart, restoring the app's document state.")
def harness_cmd(app_name: str, cap: int, restart: bool, reopen: str | None) -> None:
    """Measure anchor durability for a running app.

    Records anchors, perturbs the app (zoom resize, and optionally a full
    restart), and reports how many anchors still resolve to the right
    element. WRONG resolutions are the number to watch; the design goal
    is zero.
    """
    from . import harness as harness_mod

    try:
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
        loaded = packs.load_all(PACKS_DIR)
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
            pack_path = PACKS_DIR / pack.bundle_id / "pack.json"
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
def verify(app_name: str) -> None:
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
            try:
                anchors.resolve(root, anchor, low_confidence=low_confidence)
                resolved += 1
            except (anchors.AnchorLost, anchors.AnchorAmbiguous) as exc:
                click.secho(f"  {tool.name} ({op}): {exc}", fg="yellow")

    pct = (resolved / total * 100) if total else 100.0
    color = "green" if resolved == total else "yellow"
    click.secho(f"{pack.app_name}: {resolved}/{total} anchors resolve ({pct:.0f}%)", fg=color)

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
        packs.save(pack, PACKS_DIR / pack.bundle_id / "pack.json")
        click.secho("Pack updated.", fg="green")


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

    path = explorer.save_trace(trace, TRACES_DIR)
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
        trace = explorer.load_trace(TRACES_DIR, app.bundle_id)
    except FileNotFoundError:
        click.secho(f"No trace for {app_name!r}. Run `vent explore {app_name}` first.", fg="red")
        sys.exit(1)
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)

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
    pack_path = PACKS_DIR / pack.bundle_id / "pack.json"
    packs.save(pack, pack_path)
    click.secho(f"✓ Wrote {len(approved)} tool(s) to {pack_path}", fg="green")


@main.command()
def serve() -> None:
    """Serve every compiled pack as MCP tools over stdio.

    High risk tools are refused unless the server was started with
    VENT_ALLOW_HIGH=1 in the environment. See docs/SECURITY.md.
    """
    from .server import serve_stdio

    serve_stdio(PACKS_DIR)


if __name__ == "__main__":
    main()
