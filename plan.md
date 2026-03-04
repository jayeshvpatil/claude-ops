# token-map — Project Plan

Visual token cost tracker for Claude Code development sessions.

---

## Problem

Claude Code sessions have no built-in per-session cost visibility. Developers
don't know which prompts, tools, or features are expensive until they get an
API bill. There's no way to track cost per feature branch, compare models, or
see cache efficiency in real time.

---

## Solution

A CLI tool that:
1. Runs a local OpenTelemetry collector alongside Claude Code
2. Receives per-request token usage via OTEL gRPC (Claude Code's telemetry output)
3. Persists data to SQLite
4. Renders a visually rich terminal dashboard with live and post-session views

---

## Architecture

```
Claude Code
    │
    │  OTEL gRPC (localhost:4317)
    ▼
token-map collector          ~/.token-map/sessions.db
  (grpc server)       ──────►  (SQLite)
                                   │
                              token-map CLI
                                   │
                            Rich terminal UI
```

### Modules

| File | Role |
|---|---|
| `token_map/models.py` | Data models (`ApiRequest`, `SessionSummary`) + SQLite `Store` |
| `token_map/collector.py` | gRPC OTEL server — receives metrics/logs from Claude Code |
| `token_map/display.py` | All Rich rendering: panels, bars, sparklines, live dashboard |
| `token_map/cli.py` | Click commands wiring everything together |

---

## Commands

| Command | Description |
|---|---|
| `token-map dev [-p project]` | Start collector + live dashboard. Prints env vars to set before `claude`. |
| `token-map report [session_id]` | Post-session report for last (or named) session |
| `token-map sessions [-n 10]` | Table of recent sessions with cost totals |
| `eval $(token-map env)` | Shell-sourceable env vars for Claude Code telemetry |
| `token-map hook-install` | Adds a Stop hook to `~/.claude/settings.json` for auto-reports |

---

## Data Collected (per API request)

- `session_id`, `timestamp`, `model`
- `input_tokens`, `output_tokens`
- `cache_read_tokens`, `cache_creation_tokens`
- `cost_usd`, `duration_ms`
- `tool_name` (Bash, Edit, Read, Agent, …)
- `prompt_snippet` (first N chars, for expensive-turns view)

Source: `claude_code.api_request` metric from Claude Code's OTEL export.

---

## Visual Design

All output uses [Rich](https://github.com/Textualize/rich).

### Cost Overview panel
- One row per token type: Input / Output / Cache read / Cache write
- Color-coded fill bars (width proportional to cost share)
- Cache savings shown inline (`saved $0.22`)

### Tool Breakdown panel
- Per-tool cost bars, color-coded by tool (Bash=red, Edit=green, Agent=orange, …)
- Sorted by cost descending

### Most Expensive Turns panel
- Top 5 turns by cost
- Timestamp + cost + mini bar + prompt snippet

### Live dashboard (`token-map dev`)
- Full-screen `rich.Live` layout, refreshes every 2s
- Shows "waiting for Claude Code…" with env var instructions until first span arrives
- Ctrl+C exits and renders final report

---

## Setup (for Claude Code telemetry)

```bash
# Terminal 1 — start collector
token-map dev --project my-feature

# Copy the printed env vars, then Terminal 2:
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
claude
```

Or one-liner:
```bash
eval $(token-map env) && claude
```

Or install the Stop hook for automatic end-of-session reports:
```bash
token-map hook-install
```

---

## Roadmap

### Phase 1 — Core (done)
- [x] OTEL gRPC collector
- [x] SQLite persistence
- [x] Rich live dashboard
- [x] Post-session report
- [x] Sessions history table
- [x] Stop hook installer

### Phase 2 — Analytics
- [ ] `token-map diff <session_a> <session_b>` — compare two sessions
- [ ] `token-map budget --limit $5` — warn / block when cost threshold hit
- [ ] Per-file cost attribution (correlate tool calls with files edited)
- [ ] Model comparison view (same session replayed on Haiku vs Sonnet)

### Phase 3 — Integrations
- [ ] MLflow integration — log sessions as MLflow runs with metrics + artifacts
- [ ] GitHub PR comment with session cost on merge
- [ ] Prometheus metrics endpoint for team dashboards
- [ ] Export to CSV / JSON

---

## Dependencies

```
click>=8.1
rich>=13.7
opentelemetry-sdk>=1.23
opentelemetry-proto>=1.23
grpcio>=1.60
protobuf>=4.25
```

Python 3.10+ required.

---

## Key Design Decisions

**Why SQLite?** Zero-ops, local-first. A dev tool shouldn't require a running
database. Sessions from months ago are still queryable.

**Why gRPC / OTEL?** Claude Code already emits this — it's the only supported
programmatic interface for per-request token data. Hooks don't carry token counts.

**Why Rich?** Best-in-class terminal rendering. The `Live` screen mode gives a
smooth dashboard feel without ncurses complexity.

**Why not wrap `claude`?** Subprocess wrapping is fragile across shells,
PTYs, and Claude Code updates. The OTEL collector approach is stable and
officially supported.
