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
from .snapshot import render, snapshot

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"


@click.group()
def main() -> None:
    """Give every Mac app an API."""


@main.command()
def doctor() -> None:
    """Check that Ventriloquist can use the Accessibility API."""
    if ax.is_trusted():
        click.secho("✓ Accessibility permission granted. Ready to go.", fg="green")
    else:
        click.secho("✗ Accessibility permission missing.", fg="red")
        click.echo(
            "  Open System Settings → Privacy & Security → Accessibility and "
            "enable your terminal (or the app running vent), then rerun."
        )
        sys.exit(1)


@main.command()
def apps() -> None:
    """List running applications vent can attach to."""
    for app in sorted(ax.running_apps(), key=lambda a: a.name.lower()):
        click.echo(f"{app.name:30} pid={app.pid:<8} {app.bundle_id or ''}")


@main.command()
@click.argument("app_name")
@click.option("--depth", default=25, show_default=True, help="Maximum tree depth to walk.")
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
def run(app_name: str, tool_name: str, arg_pairs: tuple[str, ...]) -> None:
    """Execute one compiled tool against the live app, without MCP."""
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
        result = runtime.execute(
            pack, tool_name, args, root, app=app, low_confidence=low_confidence
        )
    except (ax.AXError, runtime.ToolExecutionError, packs.PackError) as exc:
        click.secho(f"✗ {exc}", fg="red")
        sys.exit(1)

    click.secho(f"✓ {pack.app_name}.{tool_name}: {result.detail}", fg="green")
    for value in result.values:
        click.echo(value)


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
