"""Rich-powered visual display for token-map."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .models import SessionSummary, Store

console = Console()

# ── palette ──────────────────────────────────────────────────────────────────
C_BRAND   = "bright_cyan"
C_DIM     = "bright_black"
C_INPUT   = "dodger_blue2"
C_OUTPUT  = "medium_orchid"
C_CACHE_R = "green3"
C_CACHE_W = "dark_orange"
C_COST    = "bright_yellow"
C_SAVINGS = "green_yellow"
C_WARN    = "orange_red1"
C_TOOL    = "steel_blue1"
C_HEAD    = "bold bright_white"


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _fmt_cost(usd: float) -> str:
    if usd < 0.01:
        return f"${usd*100:.2f}¢"
    return f"${usd:.4f}"


def _bar(fraction: float, width: int = 20, color: str = C_BRAND) -> Text:
    filled = max(0, min(width, round(fraction * width)))
    empty  = width - filled
    t = Text()
    t.append("█" * filled, style=f"bold {color}")
    t.append("░" * empty,  style=C_DIM)
    return t


def _sparkline(values: list[float], width: int = 20) -> Text:
    chars = "▁▂▃▄▅▆▇█"
    if not values:
        return Text("·" * width, style=C_DIM)
    mx = max(values) or 1
    t = Text()
    sampled = values[-width:]  # last N
    for v in sampled:
        idx = min(len(chars) - 1, int(v / mx * (len(chars) - 1)))
        t.append(chars[idx], style=C_BRAND)
    return t


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


# ── header ────────────────────────────────────────────────────────────────────

def _header(s: SessionSummary) -> Panel:
    started = datetime.fromtimestamp(s.started_at).strftime("%Y-%m-%d %H:%M")
    duration_s = max(0, s.ended_at - s.started_at)
    mins, secs = divmod(int(duration_s), 60)
    dur_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    t = Text(justify="center")
    t.append("  token-map  ", style=f"bold {C_BRAND} on grey11")
    t.append("  ")
    t.append(s.project, style=f"bold {C_HEAD}")
    t.append(f"  ·  {started}  ·  {dur_str}", style=C_DIM)
    t.append(f"  ·  {s.request_count} turns", style=C_DIM)

    return Panel(
        Align.center(t),
        box=box.DOUBLE_EDGE,
        style="grey11",
        padding=(0, 2),
    )


# ── cost overview ─────────────────────────────────────────────────────────────

def _cost_overview(s: SessionSummary) -> Panel:
    total = s.total_cost or 0.0001  # avoid /0

    rows: list[tuple[str, str, float, float, int, str]] = [
        # label, color, cost, fraction, tokens, extra
        ("Input",        C_INPUT,   s.input_tokens * 3.0 / 1_000_000,
                                    s.input_tokens / max(1, s.input_tokens + s.cache_read_tokens),
                                    s.input_tokens, ""),
        ("Output",       C_OUTPUT,  s.output_tokens * 15.0 / 1_000_000,
                                    s.output_tokens * 15 / max(1, total * 1_000_000 / 3),
                                    s.output_tokens, ""),
        ("Cache read",   C_CACHE_R, s.cache_read_tokens * 0.3 / 1_000_000,
                                    s.cache_read_tokens / max(1, s.input_tokens + s.cache_read_tokens),
                                    s.cache_read_tokens,
                                    f"  [dim]saved[/] [{C_SAVINGS}]{_fmt_cost(s.cache_savings)}[/]"),
        ("Cache write",  C_CACHE_W, s.cache_creation_tokens * 3.75 / 1_000_000,
                                    s.cache_creation_tokens * 3.75 / 1_000_000 / total,
                                    s.cache_creation_tokens, ""),
    ]

    table = Table.grid(padding=(0, 1))
    table.add_column(width=12)   # label
    table.add_column(width=8)    # cost
    table.add_column(width=22)   # bar
    table.add_column(width=8)    # tokens
    table.add_column()           # extra

    for label, color, cost, frac, tokens, extra in rows:
        bar = _bar(min(1.0, frac), width=20, color=color)
        table.add_row(
            Text(label, style=f"dim {color}"),
            Text(_fmt_cost(cost), style=f"bold {color}"),
            bar,
            Text(_fmt_tokens(tokens), style=C_DIM),
            Text.from_markup(extra),
        )

    # total row
    table.add_row(Text(""), Text(""), Text(""), Text(""), Text(""))
    total_bar = _bar(1.0, width=20, color=C_COST)
    table.add_row(
        Text("TOTAL", style=f"bold {C_HEAD}"),
        Text(_fmt_cost(s.total_cost), style=f"bold {C_COST}"),
        total_bar,
        Text("", style=C_DIM),
        Text(""),
    )

    return Panel(
        table,
        title=f"[bold {C_BRAND}] Cost Overview [/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(1, 2),
        border_style=C_DIM,
    )


# ── tool breakdown ─────────────────────────────────────────────────────────────

def _tool_breakdown(s: SessionSummary) -> Panel:
    if not s.tool_breakdown:
        return Panel(
            Text("No tool data", style=C_DIM),
            title=f"[bold {C_BRAND}] Tool Breakdown [/]",
            title_align="left",
            box=box.ROUNDED,
            padding=(1, 2),
            border_style=C_DIM,
        )

    max_cost = max(s.tool_breakdown.values()) or 0.0001
    total_tool = sum(s.tool_breakdown.values()) or 0.0001

    # tool → color mapping
    tool_colors = {
        "Bash":    "bright_red",
        "Edit":    "spring_green3",
        "Read":    C_INPUT,
        "Write":   "medium_orchid1",
        "Glob":    "gold3",
        "Grep":    "plum3",
        "Agent":   C_WARN,
        "WebFetch": "sky_blue2",
        "WebSearch": "sky_blue2",
    }

    table = Table.grid(padding=(0, 1))
    table.add_column(width=14)
    table.add_column(width=28)
    table.add_column(width=10)
    table.add_column(width=6)

    for tool, cost in sorted(s.tool_breakdown.items(), key=lambda x: -x[1])[:8]:
        color = tool_colors.get(tool, C_TOOL)
        pct = cost / total_tool
        bar = _bar(cost / max_cost, width=26, color=color)
        table.add_row(
            Text(tool, style=f"bold {color}"),
            bar,
            Text(_fmt_cost(cost), style=f"{color}"),
            Text(f"{pct*100:.0f}%", style=C_DIM),
        )

    return Panel(
        table,
        title=f"[bold {C_BRAND}] Tool Breakdown [/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(1, 2),
        border_style=C_DIM,
    )


# ── expensive turns ────────────────────────────────────────────────────────────

def _expensive_turns(s: SessionSummary) -> Panel:
    if not s.expensive_turns:
        return Panel(
            Text("No turn data", style=C_DIM),
            title=f"[bold {C_BRAND}] Most Expensive Turns [/]",
            title_align="left",
            box=box.ROUNDED,
            padding=(1, 2),
            border_style=C_DIM,
        )

    max_cost = max(t["cost"] for t in s.expensive_turns) or 0.0001

    table = Table.grid(padding=(0, 1))
    table.add_column(width=6)    # time
    table.add_column(width=10)   # cost
    table.add_column(width=12)   # bar
    table.add_column()           # snippet

    for turn in s.expensive_turns:
        ts   = _ts(turn["timestamp"])
        cost = turn["cost"]
        bar  = _bar(cost / max_cost, width=10, color=C_COST)
        snippet = (turn.get("snippet") or turn.get("tool") or "·")
        if len(snippet) > 55:
            snippet = snippet[:52] + "…"
        table.add_row(
            Text(ts, style=C_DIM),
            Text(_fmt_cost(cost), style=f"bold {C_COST}"),
            bar,
            Text(f'"{snippet}"', style="italic dim"),
        )

    return Panel(
        table,
        title=f"[bold {C_BRAND}] Most Expensive Turns [/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(1, 2),
        border_style=C_DIM,
    )


# ── cache efficiency panel ────────────────────────────────────────────────────

def _cache_panel(s: SessionSummary) -> Panel:
    rate = s.cache_hit_rate
    bar  = _bar(rate, width=30, color=C_CACHE_R)

    t = Text()
    t.append(f"  Cache hit rate  ", style=C_DIM)
    t.append(f"{rate*100:.1f}%", style=f"bold {C_CACHE_R}")
    t.append("  ")
    t.append_text(bar)
    t.append(f"  saved {_fmt_cost(s.cache_savings)}", style=f"bold {C_SAVINGS}")

    return Panel(
        t,
        box=box.SIMPLE,
        padding=(0, 1),
    )


# ── sessions list ──────────────────────────────────────────────────────────────

def render_sessions_table(sessions: list[dict]):
    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style=f"bold {C_BRAND}",
        border_style=C_DIM,
        row_styles=["", "dim"],
    )
    table.add_column("Session", style="bold", no_wrap=True, max_width=22)
    table.add_column("Project", style=C_TOOL)
    table.add_column("Started", style=C_DIM, no_wrap=True)
    table.add_column("Cost", style=C_COST, justify="right")
    table.add_column("Turns", justify="right", style=C_DIM)
    table.add_column("Status", justify="center")

    for s in sessions:
        sid  = s["session_id"][-12:]
        proj = s["project"]
        ts   = datetime.fromtimestamp(s["started_at"]).strftime("%m-%d %H:%M")
        cost = _fmt_cost(s["total_cost"])
        reqs = str(s["requests"])
        status = (
            Text("● live", style=f"bold {C_CACHE_R}")
            if not s["ended_at"]
            else Text("✓ done", style=C_DIM)
        )
        table.add_row(sid, proj, ts, cost, reqs, status)

    console.print()
    console.print(
        Panel(table, title=f"[bold {C_BRAND}] Sessions [/]",
              box=box.ROUNDED, border_style=C_DIM)
    )


# ── main report ───────────────────────────────────────────────────────────────

def render_report(s: SessionSummary):
    console.print()
    console.print(_header(s))
    console.print()
    console.print(_cost_overview(s))
    console.print(_cache_panel(s))
    console.print()
    console.print(
        Columns(
            [_tool_breakdown(s), _expensive_turns(s)],
            equal=False, expand=True,
        )
    )
    console.print()


# ── live collector status ─────────────────────────────────────────────────────

def live_collector_status(
    session_id: str,
    project: str,
    store: Store,
    port: int,
    stop_event,
):
    """Rich Live display that updates every 2s while collector is running."""

    progress = Progress(
        SpinnerColumn("dots", style=C_BRAND),
        TextColumn("[bold cyan]Collecting…"),
        TimeElapsedColumn(),
        transient=True,
    )
    task = progress.add_task("collect", total=None)

    def _make_layout(s: Optional[SessionSummary]) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )

        # header
        t = Text(justify="center")
        t.append(" token-map ", style=f"bold {C_BRAND} on grey11")
        t.append("  collecting  ", style=f"italic {C_DIM}")
        t.append(project, style=f"bold {C_HEAD}")
        t.append(f"  ·  port {port}", style=C_DIM)
        layout["header"].update(Panel(Align.center(t), box=box.DOUBLE_EDGE, style="grey11"))

        if s and s.request_count > 0:
            layout["body"].split_row(
                Layout(_cost_overview(s), name="cost"),
                Layout(
                    Columns(
                        [_tool_breakdown(s), _expensive_turns(s)],
                        equal=False, expand=True,
                    ),
                    name="right",
                ),
            )
        else:
            layout["body"].update(
                Panel(
                    Align.center(
                        Text(
                            "\nWaiting for Claude Code…\n\n"
                            "Make sure these env vars are set:\n\n"
                            f"  CLAUDE_CODE_ENABLE_TELEMETRY=1\n"
                            f"  OTEL_METRICS_EXPORTER=otlp\n"
                            f"  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:{port}",
                            style=C_DIM,
                            justify="center",
                        ),
                        vertical="middle",
                    ),
                    box=box.ROUNDED,
                    border_style=C_DIM,
                )
            )

        layout["footer"].update(
            Panel(
                Align.center(
                    Text("Press  Ctrl+C  to stop and view final report", style=C_DIM)
                ),
                box=box.SIMPLE,
            )
        )
        return layout

    with Live(
        _make_layout(None),
        console=console,
        refresh_per_second=0.5,
        screen=True,
    ) as live:
        while not stop_event.is_set():
            summary = store.get_session_summary(session_id)
            live.update(_make_layout(summary))
            stop_event.wait(timeout=2.0)

    # Final report after stopping
    summary = store.get_session_summary(session_id)
    if summary:
        render_report(summary)
