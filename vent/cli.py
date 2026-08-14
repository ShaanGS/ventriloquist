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
def inspect(app_name: str, depth: int, max_nodes: int) -> None:
    """Print the semantic accessibility snapshot of a running app."""
    try:
        app = ax.find_app(app_name)
        root = ax.app_element(app)
        nodes = snapshot(root, max_depth=depth, max_nodes=max_nodes)
    except ax.NotTrustedError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)
    except ax.AXError as exc:
        click.secho(str(exc), fg="red")
        sys.exit(1)

    click.secho(f"{app.name} (pid {app.pid}) — {len(nodes)} elements", bold=True)
    click.echo(render(nodes))


if __name__ == "__main__":
    main()
