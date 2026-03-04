"""Data models and SQLite storage for token-map."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


DB_PATH = Path.home() / ".token-map" / "sessions.db"


@dataclass
class ApiRequest:
    session_id: str
    timestamp: float
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    duration_ms: int
    tool_name: Optional[str] = None
    prompt_snippet: Optional[str] = None


@dataclass
class SessionSummary:
    session_id: str
    project: str
    started_at: float
    ended_at: float
    total_cost: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    request_count: int
    tool_breakdown: dict[str, float] = field(default_factory=dict)
    expensive_turns: list[dict] = field(default_factory=list)

    @property
    def cache_savings(self) -> float:
        """Estimate savings from cache reads vs re-inputting those tokens."""
        # Cache reads cost ~10x less than fresh input tokens
        # Rough model: input ~$3/Mtok, cache read ~$0.30/Mtok
        fresh_cost = self.cache_read_tokens * 3.0 / 1_000_000
        actual_cost = self.cache_read_tokens * 0.30 / 1_000_000
        return fresh_cost - actual_cost

    @property
    def cache_hit_rate(self) -> float:
        total = self.input_tokens + self.cache_read_tokens
        if total == 0:
            return 0.0
        return self.cache_read_tokens / total


class Store:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project TEXT NOT NULL DEFAULT 'unnamed',
                started_at REAL NOT NULL,
                ended_at REAL
            );

            CREATE TABLE IF NOT EXISTS api_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                tool_name TEXT,
                prompt_snippet TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_requests_session
                ON api_requests(session_id);
            CREATE INDEX IF NOT EXISTS idx_requests_timestamp
                ON api_requests(timestamp);
        """)
        self.conn.commit()

    def upsert_session(self, session_id: str, project: str, started_at: float):
        self.conn.execute(
            """INSERT INTO sessions (session_id, project, started_at)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO NOTHING""",
            (session_id, project, started_at),
        )
        self.conn.commit()

    def end_session(self, session_id: str, ended_at: float | None = None):
        self.conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
            (ended_at or time.time(), session_id),
        )
        self.conn.commit()

    def insert_request(self, req: ApiRequest):
        self.conn.execute(
            """INSERT INTO api_requests
               (session_id, timestamp, model, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, cost_usd,
                duration_ms, tool_name, prompt_snippet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                req.session_id, req.timestamp, req.model,
                req.input_tokens, req.output_tokens,
                req.cache_read_tokens, req.cache_creation_tokens,
                req.cost_usd, req.duration_ms,
                req.tool_name, req.prompt_snippet,
            ),
        )
        self.conn.commit()

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        row = self.conn.execute(
            "SELECT project, started_at, ended_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None

        project, started_at, ended_at = row

        agg = self.conn.execute(
            """SELECT
                COALESCE(SUM(cost_usd), 0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_read_tokens), 0),
                COALESCE(SUM(cache_creation_tokens), 0),
                COUNT(*)
               FROM api_requests WHERE session_id = ?""",
            (session_id,),
        ).fetchone()

        total_cost, inp, out, cr, cw, count = agg

        # Tool breakdown
        tool_rows = self.conn.execute(
            """SELECT tool_name, SUM(cost_usd)
               FROM api_requests
               WHERE session_id = ? AND tool_name IS NOT NULL
               GROUP BY tool_name
               ORDER BY 2 DESC""",
            (session_id,),
        ).fetchall()
        tool_breakdown = {r[0]: r[1] for r in tool_rows}

        # Most expensive turns (up to 5)
        turn_rows = self.conn.execute(
            """SELECT timestamp, cost_usd, prompt_snippet, tool_name
               FROM api_requests
               WHERE session_id = ?
               ORDER BY cost_usd DESC
               LIMIT 5""",
            (session_id,),
        ).fetchall()
        expensive_turns = [
            {"timestamp": r[0], "cost": r[1], "snippet": r[2], "tool": r[3]}
            for r in turn_rows
        ]

        return SessionSummary(
            session_id=session_id,
            project=project,
            started_at=started_at,
            ended_at=ended_at or time.time(),
            total_cost=total_cost,
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cr,
            cache_creation_tokens=cw,
            request_count=count,
            tool_breakdown=tool_breakdown,
            expensive_turns=expensive_turns,
        )

    def list_sessions(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.session_id, s.project, s.started_at, s.ended_at,
                      COALESCE(SUM(r.cost_usd), 0) as total_cost,
                      COUNT(r.id) as requests
               FROM sessions s
               LEFT JOIN api_requests r ON s.session_id = r.session_id
               GROUP BY s.session_id
               ORDER BY s.started_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "session_id": r[0], "project": r[1],
                "started_at": r[2], "ended_at": r[3],
                "total_cost": r[4], "requests": r[5],
            }
            for r in rows
        ]

    def close(self):
        self.conn.close()
