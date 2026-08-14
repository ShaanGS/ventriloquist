"""Ventriloquist CLI: compile Mac apps into MCP servers.

Current commands cover the foundation layer (doctor / apps / inspect).
Explore, compile, and serve land next.
"""

from __future__ import annotations

import sys

import click

from . import ax
from .snapshot import render, snapshot


@click.group()
def main() -> None:
    """Give every Mac app an API."""


@main.command()
def doctor() -> None:
    """Check that Ventriloquist can use the Accessibility API."""
    if ax.is_trusted():
        click.secho("✓ Accessibility permission granted — ready to go.", fg="green")
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
        if text is not None:
            node.element.set_value(text)
            click.secho(f"✓ Set value of #{node_id} {node.role} {node.label!r}", fg="green")
        else:
            node.element.perform(action_name)
            click.secho(f"✓ {action_name} on #{node_id} {node.role} {node.label!r}", fg="green")
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)


if __name__ == "__main__":
    main()
