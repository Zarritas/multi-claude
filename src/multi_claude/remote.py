"""Publish sessions to a shared remote and hydrate them back.

Sharing a session today means ``x`` (export to a zip) → send the file → ``i`` (import).
This module is the transport behind doing it without the manual round-trip: a colleague
publishes, and the session shows up in your listing ready to resume.

The remote keeps metadata and payload apart, because they differ in size and in how they
conflict::

    manifest/<uuid>.json                # light metadata, one file per session
    blobs/<uuid>/session.jsonl.gz       # the transcript
    blobs/<uuid>/subagents/...          # subagent transcripts (+ their .meta.json)
    blobs/<uuid>/tool-results/...       # large tool outputs kept outside the jsonl

One manifest **per session** rather than a single global one: two people publishing at the
same moment then write disjoint files and never conflict. Listing costs one directory read.

The session uuid is preserved as the shared key. A spike confirmed Claude resumes a jsonl
whose embedded ``cwd`` points at a ``$HOME`` that does not exist locally, so neither
``sessionId`` nor ``cwd`` need rewriting — see ``docs/REMOTE-SESSIONS.md``.

Two things deliberately never leave the machine: the project's ``memory/`` dir (the
employee's own auto-memory, which lives outside any session's tree) and anything named
``session-env`` (can hold machine-local secrets, and Claude recreates it on resume).
"""

from __future__ import annotations

import gzip
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from multi_claude.session import Session

FORMAT = "multi-claude/remote-session"
VERSION = 1

_MANIFEST_ROOT = "manifest"
_BLOB_ROOT = "blobs"
_MAIN_BLOB = "session.jsonl.gz"

# Compressed on the way up: JSON-per-line and tool output are repetitive text. Measured
# ~3.7:1 on a real 4.6 MB session, so a big one lands near 1 MB.
# ``.meta.json`` siblings are a few hundred bytes, so they travel as-is.
_COMPRESS_SUFFIXES = (".jsonl", ".txt")

# Anything whose path contains this never gets published (see module docstring).
_EXCLUDED_PART = "session-env"

# A session id becomes a path segment on the remote, so it must not be able to escape it.
# Claude's ids are uuid4, but we accept any conservative slug rather than hard-coding uuid.
_SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


class RemoteError(RuntimeError):
    """Raised when the remote is unreachable, or returns something unusable."""


def safe_session_id(session_id: str) -> str:
    """Return ``session_id`` if it is safe to use as a path segment, else raise.

    Guards every path we build from an id that came off the remote: a crafted
    ``../../`` id must not let a manifest write outside the store.
    """
    if not _SAFE_ID_RE.match(session_id):
        raise RemoteError(f"id de sesión no válido: {session_id!r}")
    return session_id


@dataclass(frozen=True)
class RemoteSession:
    """Metadata of one published session, as carried by its manifest.

    ``git_remote``/``git_head`` record the code the conversation was recorded against.
    They are what lets hydration warn that the local checkout has moved on — the
    transcript travels, the repository does not.
    """

    session_id: str
    published_at: str
    published_by: str | None = None
    cwd: str | None = None
    branch: str | None = None
    git_remote: str | None = None
    git_head: str | None = None
    display_name: str | None = None
    tags: tuple[str, ...] = ()
    first_prompt: str | None = None
    message_count: int = 0
    size_bytes: int = 0
    forked_from: str | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "id": self.session_id,
            "published_at": self.published_at,
            "published_by": self.published_by,
            "cwd": self.cwd,
            "branch": self.branch,
            "git_remote": self.git_remote,
            "git_head": self.git_head,
            "display_name": self.display_name,
            "tags": list(self.tags),
            "first_prompt": self.first_prompt,
            "message_count": self.message_count,
            "size_bytes": self.size_bytes,
            "forked_from": self.forked_from,
        }

    @classmethod
    def from_manifest(cls, data: object) -> RemoteSession:
        """Parse and validate a manifest. Raises :class:`RemoteError` on anything odd.

        Unknown keys are ignored so a newer publisher does not break an older reader,
        but an unknown ``version`` is refused: we cannot know what changed.
        """
        if not isinstance(data, dict):
            raise RemoteError("manifest no es un objeto JSON")
        if data.get("format") != FORMAT:
            raise RemoteError("no parece un manifest de multi-claude")
        if data.get("version") != VERSION:
            raise RemoteError(f"versión de manifest no soportada: {data.get('version')!r}")
        session_id = data.get("id")
        if not isinstance(session_id, str):
            raise RemoteError("el manifest no lleva id")
        published_at = data.get("published_at")
        raw_tags = data.get("tags")
        return cls(
            session_id=safe_session_id(session_id),
            published_at=published_at if isinstance(published_at, str) else "",
            published_by=_opt_str(data.get("published_by")),
            cwd=_opt_str(data.get("cwd")),
            branch=_opt_str(data.get("branch")),
            git_remote=_opt_str(data.get("git_remote")),
            git_head=_opt_str(data.get("git_head")),
            display_name=_opt_str(data.get("display_name")),
            tags=tuple(t for t in raw_tags if isinstance(t, str))
            if isinstance(raw_tags, list)
            else (),
            first_prompt=_opt_str(data.get("first_prompt")),
            message_count=_opt_int(data.get("message_count")),
            size_bytes=_opt_int(data.get("size_bytes")),
            forked_from=_opt_str(data.get("forked_from")),
        )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class FetchResult:
    """Outcome of :meth:`RemoteStore.fetch`."""

    session_id: str
    written: tuple[Path, ...]


def collect_session_files(project_dir: Path, session_id: str) -> list[Path]:
    """Return every local file that belongs to ``session_id``, publishable ones only.

    That is the ``<id>.jsonl`` plus everything under its ``<id>/`` subdir — subagent
    transcripts and the ``tool-results/`` that hold outputs too large to inline. Without
    the subdir a fan-out session arrives with its actual work missing.

    Paths containing ``session-env`` are dropped. The project's ``memory/`` dir is not a
    concern here: it sits next to the sessions, not inside one.
    """
    safe_session_id(session_id)
    jsonl = project_dir / f"{session_id}.jsonl"
    if not jsonl.is_file():
        return []
    files = [jsonl]
    subdir = project_dir / session_id
    if subdir.is_dir():
        files.extend(
            path
            for path in sorted(subdir.rglob("*"))
            if path.is_file() and _EXCLUDED_PART not in path.parts
        )
    return files


def blob_name_for(project_dir: Path, session_id: str, path: Path) -> str:
    """Map a local session file to its blob name, relative to ``blobs/<id>/``."""
    if path == project_dir / f"{session_id}.jsonl":
        return _MAIN_BLOB
    rel = path.relative_to(project_dir / session_id).as_posix()
    return f"{rel}.gz" if path.suffix in _COMPRESS_SUFFIXES else rel


def local_path_for(dest_dir: Path, session_id: str, blob_name: str) -> Path:
    """Inverse of :func:`blob_name_for`: where a blob lands when hydrated.

    Rejects blob names that would escape the session's own tree.
    """
    if blob_name == _MAIN_BLOB:
        return dest_dir / f"{session_id}.jsonl"
    rel = blob_name[:-3] if blob_name.endswith(".gz") else blob_name
    root = (dest_dir / session_id).resolve()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise RemoteError(f"ruta sospechosa en el remoto: {blob_name}")
    return target


def _is_compressed(blob_name: str) -> bool:
    return blob_name.endswith(".gz")


def session_to_remote(
    session: Session,
    *,
    published_by: str | None = None,
    git_remote: str | None = None,
    git_head: str | None = None,
    forked_from: str | None = None,
    published_at: str | None = None,
) -> RemoteSession:
    """Build the manifest metadata for a local ``session`` about to be published."""
    return RemoteSession(
        session_id=safe_session_id(session.id),
        published_at=published_at or datetime.now(timezone.utc).isoformat(),
        published_by=published_by,
        cwd=session.cwd,
        branch=session.branch,
        git_remote=git_remote,
        git_head=git_head,
        display_name=session.display_name,
        tags=tuple(session.tags),
        first_prompt=session.first_prompt,
        message_count=session.message_count,
        size_bytes=session.size_bytes,
        forked_from=forked_from,
    )


class RemoteStore(Protocol):
    """What multi-claude needs from a shared session store.

    Three operations, so a backend stays cheap to add: a private GitLab repo over its
    REST API, or a plain directory (see :class:`DirectoryRemote`).
    """

    def list_sessions(self) -> tuple[RemoteSession, ...]:
        """Every published session's metadata. Malformed manifests are skipped, not fatal."""
        ...

    def fetch(self, session_id: str, dest_dir: Path) -> FetchResult:
        """Hydrate ``session_id`` into ``dest_dir``, preserving the uuid."""
        ...

    def publish(self, session: RemoteSession, project_dir: Path) -> None:
        """Upload the session described by ``session``, reading its files from disk."""
        ...


class DirectoryRemote:
    """A remote that is just a directory.

    Doubles as the test backend and as a real one: a shared mount, a Syncthing folder, or
    a read-only NFS export all work without further code. It gives no atomicity across
    files, so :meth:`publish` writes the manifest *last* — a partial failure leaves
    unreferenced blobs (harmless, overwritten next attempt) rather than a manifest
    pointing at a session that isn't fully there.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def __str__(self) -> str:
        """Shown to the user when confirming a publish, so it must name the destination."""
        return str(self.root)

    def list_sessions(self) -> tuple[RemoteSession, ...]:
        manifest_dir = self.root / _MANIFEST_ROOT
        if not manifest_dir.is_dir():
            return ()
        sessions: list[RemoteSession] = []
        for path in sorted(manifest_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                sessions.append(RemoteSession.from_manifest(raw))
            except RemoteError:
                continue  # one bad manifest must not hide the rest
        return tuple(sessions)

    def get_session(self, session_id: str) -> RemoteSession | None:
        """Read a single manifest, or None if it is absent or unusable."""
        path = self.root / _MANIFEST_ROOT / f"{safe_session_id(session_id)}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return RemoteSession.from_manifest(raw)
        except RemoteError:
            return None

    def fetch(self, session_id: str, dest_dir: Path) -> FetchResult:
        safe_session_id(session_id)
        blob_dir = self.root / _BLOB_ROOT / session_id
        if not blob_dir.is_dir():
            raise RemoteError(f"la sesión {session_id} no está en el remoto")
        main = blob_dir / _MAIN_BLOB
        if not main.is_file():
            raise RemoteError(f"la sesión {session_id} no tiene transcript en el remoto")
        if (dest_dir / f"{session_id}.jsonl").exists():
            raise RemoteError(f"la sesión {session_id} ya existe en destino")

        written: list[Path] = []
        for blob in sorted(blob_dir.rglob("*")):
            if not blob.is_file():
                continue
            name = blob.relative_to(blob_dir).as_posix()
            target = local_path_for(dest_dir, session_id, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = blob.read_bytes()
            target.write_bytes(gzip.decompress(payload) if _is_compressed(name) else payload)
            written.append(target)
        return FetchResult(session_id=session_id, written=tuple(written))

    def publish(self, session: RemoteSession, project_dir: Path) -> None:
        files = collect_session_files(project_dir, session.session_id)
        if not files:
            raise RemoteError(f"la sesión {session.session_id} no tiene transcript en disco")

        blob_dir = self.root / _BLOB_ROOT / session.session_id
        for path in files:
            name = blob_name_for(project_dir, session.session_id, path)
            target = blob_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = path.read_bytes()
            _atomic_write(target, gzip.compress(payload) if _is_compressed(name) else payload)

        # Last, so a half-finished upload is invisible rather than broken.
        manifest = self.root / _MANIFEST_ROOT / f"{session.session_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            manifest,
            json.dumps(session.to_manifest(), indent=2, ensure_ascii=False).encode("utf-8"),
        )


REMOTE_DIR_ENV = "MULTI_CLAUDE_REMOTE_DIR"


def store_from_settings(kind: str, path: str) -> RemoteStore | None:
    """Build the configured store, or None when session sharing is off.

    ``$MULTI_CLAUDE_REMOTE_DIR`` wins over the config file. That makes trying a remote a
    one-liner, and lets a second checkout or a test point somewhere else without editing
    state shared with the running app.
    """
    env = os.environ.get(REMOTE_DIR_ENV)
    if env:
        return DirectoryRemote(Path(env).expanduser())
    if kind == "directory" and path:
        return DirectoryRemote(Path(path).expanduser())
    return None


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write ``payload`` to ``target`` via tmp-file + replace, as the other stores do."""
    fd, tmp_str = tempfile.mkstemp(prefix=".remote.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
