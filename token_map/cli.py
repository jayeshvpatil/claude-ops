"""Click-based CLI entry point for token-map."""

from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path

import click
from rich.console import Console
from rich.text import Text

from .display import console, render_report, render_sessions_table, live_collector_status
from .models import Store

err = Console(stderr=True)


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve_session(store: Store, session_id: str | None) -> str | None:
    if session_id:
        return session_id
    sessions = store.list_sessions(limit=1)
    if sessions:
        return sessions[0]["session_id"]
    return None


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option()
def main():
    """
    \b
      ████████╗ ██████╗ ██╗  ██╗███████╗███╗  ██╗      ███╗   ███╗ █████╗ ██████╗
         ██╔══╝██╔═══██╗██║ ██╔╝██╔════╝████╗ ██║      ████╗ ████║██╔══██╗██╔══██╗
         ██║   ██║   ██║█████╔╝ █████╗  ██╔██╗██║      ██╔████╔██║███████║██████╔╝
         ██║   ██║   ██║██╔═██╗ ██╔══╝  ██║╚████║      ██║╚██╔╝██║██╔══██║██╔═══╝
         ██║   ╚██████╔╝██║  ██╗███████╗██║ ╚███║      ██║ ╚═╝ ██║██║  ██║██║
         ╚═╝    ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚══╝      ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝

    Visual token cost tracker for Claude Code sessions.
    """


@main.command("dev")
@click.option("--project", "-p", default=None, help="Project name tag")
@click.option("--port",    "-P", default=4317,  show_default=True,
              help="OTLP gRPC port to listen on")
@click.option("--session", "-s", default=None,
              help="Reuse an existing session ID (resume tracking)")
def dev(project: str | None, port: int, session: str | None):
    """Start a live token-tracking session for Claude Code.

    \b
    Launches a local OTEL collector, then prints the env vars you need
    to set before running `claude`. Hit Ctrl+C to stop and see the report.

    \b
    Example:
        token-map dev --project my-feature
        # copy+paste the env vars shown, then:
        claude
    """
    try:
        from .collector import Collector, HAS_GRPC
    except ImportError:
        err.print("[red]Missing dependencies.[/] Run: pip install token-map[all]")
        sys.exit(1)

    if not HAS_GRPC:
        err.print(
            "[yellow]grpcio / opentelemetry-proto not installed.[/]\n"
            "Run: pip install grpcio opentelemetry-proto"
        )
        sys.exit(1)

    store = Store()
    session_id = session or str(uuid.uuid4())
    proj = project or Path.cwd().name

    collector = Collector(store, session_id, proj, port=port)
    collector.start()

    env = collector.env_vars()

    console.print()
    console.print(
        Text(" token-map dev ", style="bold bright_cyan on grey11")
        + Text(f"  project: {proj}  ·  session: {session_id[:8]}…", style="dim")
    )
    console.print()
    console.print(
        "  [dim]Set these env vars before launching[/] [bold]claude[/][dim]:[/]"
    )
    console.print()
    for k, v in env.items():
        console.print(f"  [bold bright_yellow]export[/] "
                      f"[bright_cyan]{k}[/]=[green]{v}[/]")
    console.print()

    stop_event = threading.Event()
    try:
        live_collector_status(session_id, proj, store, port, stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        collector.stop()
        store.close()


@main.command("report")
@click.argument("session_id", required=False)
def report(session_id: str | None):
    """Show the cost report for a session.

    SESSION_ID defaults to the most recent session.
    """
    store = Store()
    sid = _resolve_session(store, session_id)
    if not sid:
        err.print("[red]No sessions found.[/] Run [bold]token-map dev[/] first.")
        store.close()
        sys.exit(1)

    summary = store.get_session_summary(sid)
    store.close()

    if not summary:
        err.print(f"[red]Session not found:[/] {sid}")
        sys.exit(1)

    render_report(summary)


@main.command("sessions")
@click.option("--limit", "-n", default=10, show_default=True,
              help="Number of sessions to list")
def sessions(limit: int):
    """List recent sessions."""
    store = Store()
    rows = store.list_sessions(limit=limit)
    store.close()

    if not rows:
        console.print("[dim]No sessions recorded yet.[/]")
        return

    render_sessions_table(rows)


@main.command("env")
@click.option("--port", "-P", default=4317, show_default=True)
def env(port: int):
    """Print env vars to enable Claude Code telemetry (shell-sourceable).

    \b
    Usage:
        eval $(token-map env)
        claude
    """
    vars_ = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://localhost:{port}",
    }
    for k, v in vars_.items():
        # Use plain print so eval $(...) works cleanly
        print(f"export {k}={v}")


@main.command("hook-install")
def hook_install():
    """Install a Stop hook in ~/.claude/settings.json to auto-report sessions."""
    import json

    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            pass

    hooks = data.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])

    hook_entry = {
        "hooks": [
            {
                "type": "command",
                "command": "token-map report",
            }
        ]
    }

    # Idempotent: don't add duplicates
    already = any(
        any(h.get("command") == "token-map report"
            for h in entry.get("hooks", []))
        for entry in stop_hooks
    )

    if already:
        console.print("[dim]Hook already installed.[/]")
        return

    stop_hooks.append(hook_entry)
    settings_path.write_text(json.dumps(data, indent=2))
    console.print(
        f"[bold bright_cyan]✓[/] Hook installed in [dim]{settings_path}[/]\n"
        "[dim]token-map report will run automatically after each Claude Code session.[/]"
    )
