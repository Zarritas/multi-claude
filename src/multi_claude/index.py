"""SQLite-backed index of sessions.

This is a *cache* on top of the `.jsonl` files, not a source of truth. If the DB
gets corrupted, the next scan rebuilds it from disk. Two tables:

- ``sessions``        — one row per session with header metadata + mtime/size.
- ``sessions_fts``    — FTS5 virtual table over a concatenation of user prompts
                        and assistant text, used by the global search screen.

The index lives at ``$XDG_DATA_HOME/multi-claude/index.sqlite3``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_index_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "multi-claude" / "index.sqlite3"


@dataclass(frozen=True)
class IndexedSession:
    session_id: str
    project_dir: str
    cwd: str | None
    branch: str | None
    first_prompt: str | None
    message_count: int
    size_bytes: int
    mtime: float
    jsonl_path: str
    embedded_name: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    project_dir  TEXT NOT NULL,
    cwd          TEXT,
    branch       TEXT,
    first_prompt TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL,
    jsonl_path   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_dir);
CREATE INDEX IF NOT EXISTS idx_sessions_mtime ON sessions(mtime);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add columns introduced after the initial schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "embedded_name" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN embedded_name TEXT")


class SessionIndex:
    """Thread-safe SQLite handle. One connection per index; queries are short-lived."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_index_path()
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None
            )
            self._conn.executescript(_SCHEMA)
            _ensure_columns(self._conn)
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- writes ------------------------------------------------------------- #

    def upsert_session(self, session: IndexedSession, fts_content: str | None = None) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO sessions(session_id, project_dir, cwd, branch, first_prompt,
                                      message_count, size_bytes, mtime, jsonl_path,
                                      embedded_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    project_dir=excluded.project_dir,
                    cwd=excluded.cwd,
                    branch=excluded.branch,
                    first_prompt=excluded.first_prompt,
                    message_count=excluded.message_count,
                    size_bytes=excluded.size_bytes,
                    mtime=excluded.mtime,
                    jsonl_path=excluded.jsonl_path,
                    embedded_name=excluded.embedded_name
                """,
                (
                    session.session_id,
                    session.project_dir,
                    session.cwd,
                    session.branch,
                    session.first_prompt,
                    session.message_count,
                    session.size_bytes,
                    session.mtime,
                    session.jsonl_path,
                    session.embedded_name,
                ),
            )
            if fts_content is not None:
                conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (session.session_id,))
                conn.execute(
                    "INSERT INTO sessions_fts(session_id, content) VALUES (?, ?)",
                    (session.session_id, fts_content),
                )

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (session_id,))

    # -- reads -------------------------------------------------------------- #

    def get(self, session_id: str) -> IndexedSession | None:
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                """
                SELECT session_id, project_dir, cwd, branch, first_prompt,
                       message_count, size_bytes, mtime, jsonl_path, embedded_name
                FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return _row_to_session(row) if row else None

    def get_mtime(self, session_id: str) -> float | None:
        """Lightweight check used to decide whether to reparse a session."""
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                "SELECT mtime FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return float(row[0]) if row else None

    def list_by_project(self, project_dir: str) -> list[IndexedSession]:
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                """
                SELECT session_id, project_dir, cwd, branch, first_prompt,
                       message_count, size_bytes, mtime, jsonl_path, embedded_name
                FROM sessions WHERE project_dir = ?
                """,
                (project_dir,),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def fts_search(self, query: str, limit: int = 200) -> list[IndexedSession]:
        """Return sessions whose FTS content matches ``query``, ordered by rank."""
        if not query.strip():
            return []
        sanitised = _sanitise_fts_query(query)
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                """
                SELECT s.session_id, s.project_dir, s.cwd, s.branch, s.first_prompt,
                       s.message_count, s.size_bytes, s.mtime, s.jsonl_path,
                       s.embedded_name
                FROM sessions_fts f
                JOIN sessions s ON s.session_id = f.session_id
                WHERE sessions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (sanitised, limit),
            ).fetchall()
        return [_row_to_session(r) for r in rows]


def _row_to_session(row: Any) -> IndexedSession:
    sid, project_dir, cwd, branch, first_prompt, mc, sb, mtime, jp, embedded = row
    return IndexedSession(
        session_id=str(sid),
        project_dir=str(project_dir),
        cwd=str(cwd) if cwd is not None else None,
        branch=str(branch) if branch is not None else None,
        first_prompt=str(first_prompt) if first_prompt is not None else None,
        message_count=int(mc),
        size_bytes=int(sb),
        mtime=float(mtime),
        jsonl_path=str(jp),
        embedded_name=str(embedded) if embedded is not None else None,
    )


def _sanitise_fts_query(query: str) -> str:
    """Escape FTS5 query terms so user input doesn't crash the parser.

    Splits on whitespace, double-quotes each token (FTS5 treats quoted strings as
    literal phrases) and joins with the default AND. Empty after stripping → no match.
    """
    tokens = []
    for raw in query.split():
        cleaned = raw.replace('"', "")
        if cleaned:
            tokens.append(f'"{cleaned}"')
    return " ".join(tokens)


_DEFAULT_INDEX: SessionIndex | None = None
_DEFAULT_INDEX_LOCK = threading.Lock()


def default_index() -> SessionIndex:
    """Process-wide singleton index. Lazy, so tests can use a fresh instance per case."""
    global _DEFAULT_INDEX
    with _DEFAULT_INDEX_LOCK:
        if _DEFAULT_INDEX is None:
            _DEFAULT_INDEX = SessionIndex()
        return _DEFAULT_INDEX


def reset_default_index_for_tests() -> None:
    global _DEFAULT_INDEX
    with _DEFAULT_INDEX_LOCK:
        if _DEFAULT_INDEX is not None:
            _DEFAULT_INDEX.close()
        _DEFAULT_INDEX = None
