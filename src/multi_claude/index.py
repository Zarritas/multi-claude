"""SQLite-backed index of sessions.

This is a *cache* on top of the `.jsonl` files, not a source of truth. If the DB
gets corrupted, the next scan rebuilds it from disk. The tables that matter:

- ``sessions``        — one row per session with header metadata + mtime/size.
- ``sessions_fts``    — FTS5 virtual table over a concatenation of user prompts
                        and assistant text, used by the global search screen.
- ``session_files``   — the files each session edited, behind the ``file:`` filter.

The index lives at ``$XDG_DATA_HOME/multi-claude/index.sqlite3``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_index_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "multi-claude" / "index.sqlite3"


def basename(path: str) -> str:
    """The last segment of a path, lowercased, whichever separator it uses.

    Not ``Path(path).name``: the index is read on the machine that wrote it, but a session
    published by a colleague can carry Windows paths onto a Linux box, where ``Path`` would
    hand back ``C:\\src\\app.py`` whole and hide the basename `file:` is meant to match.
    """
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


@dataclass(frozen=True)
class IndexedRemoteSession:
    """A team-published session as the index remembers it.

    Mirrors what a manifest carries, plus where it was seen: ``remote_key`` is the
    remote's identity (:meth:`RemoteLink.identity_key`), ``remote_label`` the tab name
    to show, and ``project_key`` the linked repo's origin, which is how a search hit
    leads back to the project whose tab holds it.
    """

    remote_key: str
    session_id: str
    remote_label: str
    project_key: str | None
    published_by: str | None
    published_at: str | None
    cwd: str | None
    branch: str | None
    display_name: str | None
    first_prompt: str | None
    tags: tuple[str, ...]
    message_count: int
    size_bytes: int

    @property
    def title(self) -> str:
        """What to show as the session's name, best available first."""
        return self.display_name or self.first_prompt or self.session_id


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

-- Sessions published by the team, as last listed from each remote. Cached so the
-- global search can offer a teammate's session without a network round trip; the
-- content indexed is only what a manifest carries (name, first prompt, tags,
-- branch, author) — the transcript itself never leaves the machine that has it.
CREATE TABLE IF NOT EXISTS remote_sessions (
    remote_key    TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    remote_label  TEXT NOT NULL DEFAULT '',
    project_key   TEXT,
    published_by  TEXT,
    published_at  TEXT,
    cwd           TEXT,
    branch        TEXT,
    display_name  TEXT,
    first_prompt  TEXT,
    tags          TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    listed_at     REAL    NOT NULL DEFAULT 0,
    -- 1 once the session's search blob has been downloaded and folded into the FTS row.
    -- Until then the row is searchable by its manifest's metadata only, and this is what
    -- tells the downloader which ones are still missing.
    has_text      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (remote_key, session_id)
);
CREATE INDEX IF NOT EXISTS idx_remote_sessions_project ON remote_sessions(project_key);

CREATE VIRTUAL TABLE IF NOT EXISTS remote_sessions_fts USING fts5(
    remote_key UNINDEXED,
    session_id UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Which published version of a session this machine is working from: the manifest's
-- ``published_at`` at the moment it was fetched, or last published successfully. It is the
-- base of a fast-forward — if the remote's manifest now carries a different stamp, someone
-- published on top and overwriting would drop their work. Without this the two cases are
-- indistinguishable: a jsonl only grows, so "mine is bigger" says nothing about whether
-- theirs grew too.
CREATE TABLE IF NOT EXISTS session_base (
    remote_key   TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    published_at TEXT NOT NULL,
    recorded_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (remote_key, session_id)
);

-- Result of the credential scan per session, so the listing can mark what looks
-- sensitive without re-grepping megabytes on every repaint. Keyed by mtime: a session
-- that grew since it was scanned counts as unscanned. Only the count is stored —
-- never a finding, never a value: this file is not the place to keep a copy of a secret.
CREATE TABLE IF NOT EXISTS session_secrets (
    session_id   TEXT PRIMARY KEY,
    mtime        REAL    NOT NULL,
    finding_count INTEGER NOT NULL DEFAULT 0,
    scanned_at   REAL    NOT NULL DEFAULT 0
);

-- Which files each session edited, so `file:` can answer "in which conversation did we
-- touch this". It is not in the FTS table on purpose: paths tokenise badly (a tokenizer
-- splits `src/multi_claude/index.py` into words and then `index` matches prose about an
-- index), and the question here is not full-text at all — it is a substring over a short
-- list. `name` is the lowercased basename, indexed, because that is what people type:
-- `file:index.py` far more often than the whole path.
CREATE TABLE IF NOT EXISTS session_files (
    session_id TEXT NOT NULL,
    path       TEXT NOT NULL,
    name       TEXT NOT NULL,
    PRIMARY KEY (session_id, path)
);
CREATE INDEX IF NOT EXISTS idx_session_files_name ON session_files(name);
"""


# Bumped whenever :mod:`multi_claude.session` starts pulling something new out of a
# jsonl, so rows extracted by an older build are reparsed once instead of staying
# stale forever behind an unchanged mtime.
#
# 2: the FTS payload caps went from 64 KB / 2.000 lines to 512 KB / 20.000, so every row
#    written before that is missing the tail of its conversation and has to be reparsed.
# 3: the files each session edited are extracted now (``session_files``), and a row written
#    before that has none — which would read as "this conversation touched nothing" and
#    make `file:` quietly miss most of the history.
# 4: token counts and active time are pulled out too, and a row from before reports zero —
#    which in a total would silently under-report the work instead of admitting it is unknown.
EXTRACT_VERSION = 4


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add columns introduced after the initial schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "embedded_name" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN embedded_name TEXT")
    if "extract_version" not in existing:
        # Default 0 = "extracted before this was tracked", so every pre-existing row
        # looks stale and gets reparsed on its next scan.
        conn.execute("ALTER TABLE sessions ADD COLUMN extract_version INTEGER NOT NULL DEFAULT 0")
    # What a session spent, as its own events reported it. Four token counts and not one
    # total: cache reads run three orders of magnitude above everything else, so a single
    # number would be a cache-read count under a misleading name.
    for column, kind in (
        ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("active_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("first_at", "TEXT NOT NULL DEFAULT ''"),
        ("last_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {kind}")


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

    def upsert_session(
        self,
        session: IndexedSession,
        fts_content: str | None = None,
        touched_files: tuple[str, ...] | None = None,
        usage: object | None = None,
    ) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO sessions(session_id, project_dir, cwd, branch, first_prompt,
                                      message_count, size_bytes, mtime, jsonl_path,
                                      embedded_name, extract_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    project_dir=excluded.project_dir,
                    cwd=excluded.cwd,
                    branch=excluded.branch,
                    first_prompt=excluded.first_prompt,
                    message_count=excluded.message_count,
                    size_bytes=excluded.size_bytes,
                    mtime=excluded.mtime,
                    jsonl_path=excluded.jsonl_path,
                    embedded_name=excluded.embedded_name,
                    extract_version=excluded.extract_version
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
                    EXTRACT_VERSION,
                ),
            )
            if fts_content is not None:
                conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (session.session_id,))
                conn.execute(
                    "INSERT INTO sessions_fts(session_id, content) VALUES (?, ?)",
                    (session.session_id, fts_content),
                )
            if usage is not None:
                # Duck-typed rather than imported: session.py imports this module, so taking
                # its Usage as a type here would close the loop.
                conn.execute(
                    """
                    UPDATE sessions SET input_tokens=?, output_tokens=?, cache_read_tokens=?,
                        cache_creation_tokens=?, active_seconds=?, first_at=?, last_at=?
                    WHERE session_id=?
                    """,
                    (
                        getattr(usage, "input_tokens", 0),
                        getattr(usage, "output_tokens", 0),
                        getattr(usage, "cache_read_tokens", 0),
                        getattr(usage, "cache_creation_tokens", 0),
                        getattr(usage, "active_seconds", 0),
                        getattr(usage, "first_at", ""),
                        getattr(usage, "last_at", ""),
                        session.session_id,
                    ),
                )
            if touched_files is not None:
                # Replaced wholesale rather than merged: a reparse sees the whole session,
                # so what it did not find is not there any more (an edit undone by a
                # rewritten transcript, a file the cap cut off). Merging would accumulate
                # paths that no version of the conversation touches.
                conn.execute(
                    "DELETE FROM session_files WHERE session_id = ?", (session.session_id,)
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO session_files(session_id, path, name) VALUES (?, ?, ?)",
                    [(session.session_id, path, basename(path)) for path in touched_files],
                )

    def delete_session(self, session_id: str) -> None:
        """Drop every trace of a session, the scan result included.

        Leaving ``session_secrets`` behind would let a deleted id come back marked as
        sensitive if the same uuid is ever hydrated again.
        """
        with self._lock:
            conn = self._connection()
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session_secrets WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session_files WHERE session_id = ?", (session_id,))

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

    def is_fresh(self, session_id: str, mtime: float) -> bool:
        """Whether the cached row can be trusted for a file with this ``mtime``.

        Two ways to be stale: the file changed, or the row was extracted by a build
        that pulled less out of the jsonl than the current one does (see
        ``EXTRACT_VERSION``). Both mean reparse.
        """
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                "SELECT mtime, extract_version FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return False
        return float(row[0]) == mtime and int(row[1]) == EXTRACT_VERSION

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

    def replace_remote_sessions(
        self, remote_key: str, sessions: list[IndexedRemoteSession], *, listed_at: float
    ) -> None:
        """Make the cache for ``remote_key`` match this listing exactly.

        The listing *is* the remote's truth at that moment, so a session someone
        unpublished has to disappear from search instead of lingering as a hit nobody can
        fetch. But this cannot be a wholesale delete-and-reinsert: that would throw away
        every downloaded search payload on each visit to the tab and re-download them all.
        So it is a diff — rows that are gone are deleted, rows that are still there keep
        their indexed text unless the manifest says it was republished (a different
        ``published_at``), which is exactly when the text is stale.
        """
        with self._lock:
            conn = self._connection()
            known = {
                str(sid): (str(stamp or ""), int(flag))
                for sid, stamp, flag in conn.execute(
                    "SELECT session_id, published_at, has_text FROM remote_sessions "
                    "WHERE remote_key = ?",
                    (remote_key,),
                ).fetchall()
            }
            incoming = {s.session_id for s in sessions}
            conn.execute("BEGIN")
            try:
                for gone in set(known) - incoming:
                    conn.execute(
                        "DELETE FROM remote_sessions WHERE remote_key = ? AND session_id = ?",
                        (remote_key, gone),
                    )
                    conn.execute(
                        "DELETE FROM remote_sessions_fts WHERE remote_key = ? AND session_id = ?",
                        (remote_key, gone),
                    )
                for session in sessions:
                    previous = known.get(session.session_id)
                    keeps_text = (
                        previous is not None
                        and previous[1] == 1
                        and previous[0] == (session.published_at or "")
                    )
                    conn.execute(
                        "DELETE FROM remote_sessions WHERE remote_key = ? AND session_id = ?",
                        (remote_key, session.session_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO remote_sessions(
                            remote_key, session_id, remote_label, project_key, published_by,
                            published_at, cwd, branch, display_name, first_prompt, tags,
                            message_count, size_bytes, listed_at, has_text)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            remote_key,
                            session.session_id,
                            session.remote_label,
                            session.project_key,
                            session.published_by,
                            session.published_at,
                            session.cwd,
                            session.branch,
                            session.display_name,
                            session.first_prompt,
                            ",".join(session.tags),
                            session.message_count,
                            session.size_bytes,
                            listed_at,
                            1 if keeps_text else 0,
                        ),
                    )
                    if keeps_text:
                        continue  # its FTS row already holds metadata + downloaded text
                    conn.execute(
                        "DELETE FROM remote_sessions_fts WHERE remote_key = ? AND session_id = ?",
                        (remote_key, session.session_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO remote_sessions_fts(remote_key, session_id, content)
                        VALUES (?, ?, ?)
                        """,
                        (remote_key, session.session_id, _remote_fts_content(session)),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def remote_sessions_without_text(self, remote_key: str) -> list[str]:
        """Ids under ``remote_key`` whose search blob has not been indexed yet."""
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                "SELECT session_id FROM remote_sessions WHERE remote_key = ? AND has_text = 0",
                (remote_key,),
            ).fetchall()
        return [str(sid) for (sid,) in rows]

    def add_remote_search_text(self, remote_key: str, session_id: str, text: str) -> None:
        """Fold a downloaded search payload into that session's FTS row.

        The metadata stays in the indexed content: someone looking for "lo de Ana sobre
        nginx" is as likely to remember the author or the branch as a phrase from inside
        the conversation, and dropping them to make room would trade one kind of hit for
        another. ``has_text`` flips so the row is not downloaded again.
        """
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                """
                SELECT display_name, first_prompt, tags, branch, published_by
                FROM remote_sessions WHERE remote_key = ? AND session_id = ?
                """,
                (remote_key, session_id),
            ).fetchone()
            if row is None:
                return  # unpublished between listing and download; nothing to attach to
            metadata = "\n".join(str(value) for value in row if value)
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "DELETE FROM remote_sessions_fts WHERE remote_key = ? AND session_id = ?",
                    (remote_key, session_id),
                )
                conn.execute(
                    """
                    INSERT INTO remote_sessions_fts(remote_key, session_id, content)
                    VALUES (?, ?, ?)
                    """,
                    (remote_key, session_id, f"{metadata}\n{text}"),
                )
                conn.execute(
                    """
                    UPDATE remote_sessions SET has_text = 1
                    WHERE remote_key = ? AND session_id = ?
                    """,
                    (remote_key, session_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def count_remote_with_text(self) -> int:
        """How many cached team sessions are searchable by their content, not just metadata."""
        with self._lock:
            conn = self._connection()
            row = conn.execute("SELECT COUNT(*) FROM remote_sessions WHERE has_text = 1").fetchone()
        return int(row[0]) if row else 0

    def fts_search_remote(self, query: str, limit: int = 200) -> list[IndexedRemoteSession]:
        """Team-published sessions whose manifest metadata matches ``query``.

        Note what this does *not* search: a manifest carries no transcript, so this
        finds sessions by name, first prompt, tags, branch and author — never by
        something said mid-conversation. The local :meth:`fts_search` does that.
        """
        if not query.strip():
            return []
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                """
                SELECT r.remote_key, r.session_id, r.remote_label, r.project_key,
                       r.published_by, r.published_at, r.cwd, r.branch, r.display_name,
                       r.first_prompt, r.tags, r.message_count, r.size_bytes
                FROM remote_sessions_fts f
                JOIN remote_sessions r
                  ON r.remote_key = f.remote_key AND r.session_id = f.session_id
                WHERE remote_sessions_fts MATCH :match
                ORDER BY rank
                LIMIT :limit
                """,
                {"match": _sanitise_fts_query(query), "limit": limit},
            ).fetchall()
        return [_row_to_remote_session(r) for r in rows]

    def purge_missing(self) -> int:
        """Drop rows whose jsonl is no longer on disk. Returns how many went.

        The index never forgot anything, so it outlived what it described: sessions Claude
        purged by ``cleanupPeriodDays``, sessions deleted from another machine's copy of a
        shared repo, sessions moved elsewhere. Those rows still answered `?`, offering hits
        that cannot be opened. Cheap to do — one ``stat`` per row — and safe: the index is a
        cache, and anything still on disk gets re-added by the next scan.
        """
        with self._lock:
            conn = self._connection()
            rows = conn.execute("SELECT session_id, jsonl_path FROM sessions").fetchall()
        gone = [str(sid) for sid, path in rows if not Path(str(path)).is_file()]
        if not gone:
            return 0
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN")
            try:
                for session_id in gone:
                    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM session_secrets WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM session_files WHERE session_id = ?", (session_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(gone)

    # -- which published version we are working from ------------------------- #

    def record_publish_base(self, remote_key: str, session_id: str, published_at: str) -> None:
        """Remember the published version this machine's copy derives from."""
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO session_base(remote_key, session_id, published_at, recorded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(remote_key, session_id) DO UPDATE SET
                    published_at=excluded.published_at,
                    recorded_at=excluded.recorded_at
                """,
                (remote_key, session_id, published_at, time.time()),
            )

    def publish_base(self, remote_key: str, session_id: str) -> str | None:
        """The ``published_at`` this copy derives from, or None if we never recorded one."""
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                "SELECT published_at FROM session_base WHERE remote_key = ? AND session_id = ?",
                (remote_key, session_id),
            ).fetchone()
        return str(row[0]) if row else None

    def forget_publish_base(self, remote_key: str, session_id: str) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                "DELETE FROM session_base WHERE remote_key = ? AND session_id = ?",
                (remote_key, session_id),
            )

    # -- credential scan results -------------------------------------------- #

    def record_secret_scan(self, session_id: str, mtime: float, finding_count: int) -> None:
        """Remember how many suspected credentials a session had when last scanned."""
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO session_secrets(session_id, mtime, finding_count, scanned_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    mtime=excluded.mtime,
                    finding_count=excluded.finding_count,
                    scanned_at=excluded.scanned_at
                """,
                (session_id, mtime, finding_count, time.time()),
            )

    def secret_counts(self, session_ids: list[str]) -> dict[str, int]:
        """``session_id -> finding count`` for the ids that have a cached scan.

        No entry means "not scanned yet", which the caller must not confuse with "clean":
        the listing shows nothing until a scan has actually happened.
        """
        if not session_ids:
            return {}
        placeholders = ",".join("?" * len(session_ids))
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                f"SELECT session_id, finding_count FROM session_secrets "
                f"WHERE session_id IN ({placeholders})",
                session_ids,
            ).fetchall()
        return {str(sid): int(count) for sid, count in rows}

    def secret_scan_is_fresh(self, session_id: str, mtime: float) -> bool:
        """Whether the cached scan still describes the file on disk."""
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                "SELECT mtime FROM session_secrets WHERE session_id = ?", (session_id,)
            ).fetchone()
        return bool(row) and float(row[0]) == mtime

    def files_for_sessions(self, session_ids: list[str]) -> dict[str, tuple[str, ...]]:
        """``session_id -> the paths it edited``, for the ids that have any.

        A missing id means "this session edited nothing we could see", which includes a row
        the current build has not reparsed yet. The listing treats both the same way,
        because from the filter's side they are the same answer: no match.
        """
        if not session_ids:
            return {}
        placeholders = ",".join("?" * len(session_ids))
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                f"SELECT session_id, path FROM session_files WHERE session_id IN ({placeholders})",
                session_ids,
            ).fetchall()
        found: dict[str, list[str]] = {}
        for sid, path in rows:
            found.setdefault(str(sid), []).append(str(path))
        return {sid: tuple(paths) for sid, paths in found.items()}

    def sessions_touching(self, term: str, limit: int = 200) -> list[IndexedSession]:
        """Sessions that edited a file matching ``term``, most recent first.

        A term with a separator in it is matched against the whole path, one without
        against the basename — which is how people ask: `index.py` when they mean the file,
        `multi_claude/index.py` when they mean *that* one and not some other index.py.

        ``LIKE`` needs its wildcards escaped: a path is exactly the kind of string that
        contains ``_``, and unescaped that matches any character — `file:test_x.py` would
        quietly also return `test-x.py`.
        """
        term = term.strip()
        if not term:
            return []
        column = "path" if ("/" in term or "\\" in term) else "name"
        pattern = f"%{_escape_like(term.lower())}%"
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                f"""
                SELECT DISTINCT s.session_id, s.project_dir, s.cwd, s.branch, s.first_prompt,
                       s.message_count, s.size_bytes, s.mtime, s.jsonl_path, s.embedded_name
                FROM session_files sf
                JOIN sessions s ON s.session_id = sf.session_id
                WHERE lower(sf.{column}) LIKE :pattern ESCAPE '\\'
                ORDER BY s.mtime DESC
                LIMIT :limit
                """,
                {"pattern": pattern, "limit": limit},
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def usage_rows(self) -> list[tuple[str, str, int, int, int, int, int, str]]:
        """``(session_id, project_dir, in, out, cache_read, cache_creation, active_s, last_at)``.

        Raw rows for the report to group however it likes. Sessions the current build has not
        reparsed yet report zeros, which is why the report says how many of those there are
        instead of quietly adding nothing to the totals.
        """
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                """
                SELECT session_id, project_dir, input_tokens, output_tokens, cache_read_tokens,
                       cache_creation_tokens, active_seconds, last_at
                FROM sessions
                """
            ).fetchall()
        return [
            (str(a), str(b), int(c), int(d), int(e), int(f), int(g), str(h or ""))
            for a, b, c, d, e, f, g, h in rows
        ]

    def forget_secret_scan(self, session_id: str) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute("DELETE FROM session_secrets WHERE session_id = ?", (session_id,))

    def count_remote_sessions(self) -> int:
        """How many team-published sessions are cached, across every remote."""
        with self._lock:
            conn = self._connection()
            row = conn.execute("SELECT COUNT(*) FROM remote_sessions").fetchone()
        return int(row[0]) if row else 0

    def count_sessions(self) -> int:
        """How many sessions the index knows about. Zero means it was never populated."""
        with self._lock:
            conn = self._connection()
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return int(row[0]) if row else 0

    def fts_search(
        self, query: str, limit: int = 200, *, cwd_prefix: str | None = None
    ) -> list[IndexedSession]:
        """Return sessions whose FTS content matches ``query``, ordered by rank.

        ``cwd_prefix`` restricts the result to sessions recorded at that path or
        below it. The comparison is done with ``substr`` rather than ``LIKE`` on
        purpose: a path containing ``_`` or ``%`` would otherwise act as a wildcard.
        """
        if not query.strip():
            return []
        params: dict[str, object] = {
            "match": _sanitise_fts_query(query),
            "limit": limit,
        }
        scope = ""
        if cwd_prefix:
            params["cwd"] = cwd_prefix.rstrip("/") or "/"
            scope = "AND (s.cwd = :cwd OR substr(s.cwd, 1, length(:cwd) + 1) = :cwd || '/')"
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                f"""
                SELECT s.session_id, s.project_dir, s.cwd, s.branch, s.first_prompt,
                       s.message_count, s.size_bytes, s.mtime, s.jsonl_path,
                       s.embedded_name
                FROM sessions_fts f
                JOIN sessions s ON s.session_id = f.session_id
                WHERE sessions_fts MATCH :match
                {scope}
                ORDER BY rank
                LIMIT :limit
                """,
                params,
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


def _remote_fts_content(session: IndexedRemoteSession) -> str:
    """The searchable text of a manifest: everything a person might remember about it."""
    parts = [
        session.display_name,
        session.first_prompt,
        " ".join(session.tags),
        session.branch,
        session.published_by,
    ]
    return "\n".join(p for p in parts if p)


def _row_to_remote_session(row: Any) -> IndexedRemoteSession:
    (
        remote_key,
        session_id,
        remote_label,
        project_key,
        published_by,
        published_at,
        cwd,
        branch,
        display_name,
        first_prompt,
        tags,
        message_count,
        size_bytes,
    ) = row
    return IndexedRemoteSession(
        remote_key=str(remote_key),
        session_id=str(session_id),
        remote_label=str(remote_label or ""),
        project_key=str(project_key) if project_key is not None else None,
        published_by=str(published_by) if published_by is not None else None,
        published_at=str(published_at) if published_at is not None else None,
        cwd=str(cwd) if cwd is not None else None,
        branch=str(branch) if branch is not None else None,
        display_name=str(display_name) if display_name is not None else None,
        first_prompt=str(first_prompt) if first_prompt is not None else None,
        tags=tuple(t for t in str(tags or "").split(",") if t),
        message_count=int(message_count),
        size_bytes=int(size_bytes),
    )


def _escape_like(term: str) -> str:
    """Neutralise LIKE's wildcards in a literal term, for use with ``ESCAPE '\\'``.

    Paths are full of ``_``, which LIKE reads as "any character": unescaped,
    `file:test_x.py` would also match `test-x.py` and nobody would notice it lying.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
