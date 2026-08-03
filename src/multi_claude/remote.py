"""Publish sessions to a shared remote and hydrate them back.

Sharing a session today means ``x`` (export to a zip) → send the file → ``i`` (import).
This module is the transport behind doing it without the manual round-trip: a colleague
publishes, and the session shows up in your listing ready to resume.

The remote keeps metadata and payload apart, because they differ in size and in how they
conflict::

    manifest/<uuid>.json                # light metadata, one file per session
    search/<uuid>.txt.gz                # just the conversation's text, for searching
    blobs/<uuid>/session.jsonl.gz       # the transcript
    blobs/<uuid>/subagents/...          # subagent transcripts (+ their .meta.json)
    blobs/<uuid>/tool-results/...       # large tool outputs kept outside the jsonl

One manifest **per session** rather than a single global one: two people publishing at the
same moment then write disjoint files and never conflict. Listing costs one directory read.

The **search blob** is why a colleague's session can be searched by what was said inside it
without fetching it. It could not live in the manifest: listing reads every manifest, so
half a megabyte of text per session would turn opening a tab into a multi-megabyte download
(and, over the REST backends, into a much larger response per session). Kept apart it is
downloaded once per session, lazily, and it is *small* — measured over 35 real sessions,
the search payloads compress to 0,5 MB against 18,5 MB for the same sessions' transcripts:
**36 times less** to be able to search them.

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
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from multi_claude.index import IndexedRemoteSession
from multi_claude.project_remotes import RemoteLink
from multi_claude.session import Session

FORMAT = "multi-claude/remote-session"

# 2 adds ``search_bytes``, telling a reader that ``search/<uuid>.txt.gz`` exists. Readers
# accept 1 as well: a session published by an older build simply has no search blob and
# stays searchable by its metadata, which is what it always was.
VERSION = 2
_READABLE_VERSIONS = frozenset({1, 2})

MANIFEST_ROOT = "manifest"
SEARCH_ROOT = "search"
BLOB_ROOT = "blobs"
MAIN_BLOB = "session.jsonl.gz"

# A search payload above this is not uploaded: past a point it stops being the cheap
# alternative to fetching the session, which is the only reason it exists. Sessions over it
# stay searchable by metadata. 512 KB is also the cap the local FTS payload uses.
MAX_SEARCH_BYTES = 512_000

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
    # Size of the plain-text search payload on the remote, 0 when there is none. Only a
    # size: the text itself lives in ``search/<uuid>.txt.gz`` precisely so that listing
    # does not drag it along.
    search_bytes: int = 0

    @property
    def has_search_text(self) -> bool:
        return self.search_bytes > 0

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
            "search_bytes": self.search_bytes,
        }

    @classmethod
    def from_manifest(cls, data: object) -> RemoteSession:
        """Parse and validate a manifest. Raises :class:`RemoteError` on anything odd.

        Unknown keys are ignored so a newer publisher does not break an older reader, and
        every version we have ever written is accepted — a v1 manifest just has no search
        blob. An unknown *future* version is still refused: we cannot know what changed.
        """
        if not isinstance(data, dict):
            raise RemoteError("manifest no es un objeto JSON")
        if data.get("format") != FORMAT:
            raise RemoteError("no parece un manifest de multi-claude")
        if data.get("version") not in _READABLE_VERSIONS:
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
            search_bytes=_opt_int(data.get("search_bytes")),
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
        return MAIN_BLOB
    rel = path.relative_to(project_dir / session_id).as_posix()
    return f"{rel}.gz" if path.suffix in _COMPRESS_SUFFIXES else rel


def local_path_for(dest_dir: Path, session_id: str, blob_name: str) -> Path:
    """Inverse of :func:`blob_name_for`: where a blob lands when hydrated.

    Rejects blob names that would escape the session's own tree.
    """
    if blob_name == MAIN_BLOB:
        return dest_dir / f"{session_id}.jsonl"
    rel = blob_name[:-3] if blob_name.endswith(".gz") else blob_name
    root = (dest_dir / session_id).resolve()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise RemoteError(f"ruta sospechosa en el remoto: {blob_name}")
    return target


def is_compressed_blob(blob_name: str) -> bool:
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


def with_search_size(session: RemoteSession, payload: bytes | None) -> RemoteSession:
    """The manifest as written: it records the payload's *plain* size, or 0 when absent."""
    from dataclasses import replace as _replace

    if payload is None:
        return _replace(session, search_bytes=0)
    text = decode_search_payload(payload) or ""
    return _replace(session, search_bytes=len(text.encode("utf-8")))


def search_blob_name(session_id: str) -> str:
    """Where a session's search payload lives on the remote."""
    return f"{SEARCH_ROOT}/{safe_session_id(session_id)}.txt.gz"


def search_payload_for(project_dir: Path, session_id: str) -> bytes | None:
    """The gzipped search payload for a local session, or None when there is nothing to send.

    The same text the local FTS index uses — user prompts and assistant replies, no tool
    calls and no tool output. That matters twice: it is what makes the blob small, and it
    keeps the most likely place for a stray credential (a command's output) out of what
    gets uploaded for searching.
    """
    from multi_claude.session import extract_fts_content

    jsonl = project_dir / f"{safe_session_id(session_id)}.jsonl"
    if not jsonl.is_file():
        return None
    try:
        text = extract_fts_content(jsonl)
    except OSError:
        return None
    if not text.strip() or len(text.encode("utf-8")) > MAX_SEARCH_BYTES:
        return None
    return gzip.compress(text.encode("utf-8"))


def decode_search_payload(payload: bytes) -> str | None:
    """Undo :func:`search_payload_for`. None when the blob is not readable as one."""
    try:
        return gzip.decompress(payload).decode("utf-8", errors="replace")
    except (OSError, gzip.BadGzipFile, EOFError):
        return None


def remote_to_indexed(
    session: RemoteSession,
    *,
    remote_key: str,
    remote_label: str,
    project_key: str | None,
) -> IndexedRemoteSession:
    """Turn a listed manifest into the row the session index caches.

    The inverse direction of :func:`session_to_remote`, and deliberately lossy: only
    what a manifest already carries travels, so nothing here reads a transcript.
    """
    return IndexedRemoteSession(
        remote_key=remote_key,
        session_id=session.session_id,
        remote_label=remote_label,
        project_key=project_key,
        published_by=session.published_by,
        published_at=session.published_at or None,
        cwd=session.cwd,
        branch=session.branch,
        display_name=session.display_name,
        first_prompt=session.first_prompt,
        tags=tuple(session.tags),
        message_count=session.message_count,
        size_bytes=session.size_bytes,
    )


class RemoteStore(Protocol):
    """What multi-claude needs from a shared session store.

    Three operations, so a backend stays cheap to add: a private GitLab repo over its
    REST API, or a plain directory (see :class:`DirectoryRemote`).
    """

    def list_sessions(self) -> tuple[RemoteSession, ...]:
        """Every published session's metadata. Malformed manifests are skipped, not fatal."""
        ...

    def get_session(self, session_id: str) -> RemoteSession | None:
        """One session's manifest, or None if it is not published here.

        Part of the contract because republishing needs it: it is how a publish checks
        whether someone else has published on top since this copy was fetched.
        """
        ...

    def fetch(self, session_id: str, dest_dir: Path) -> FetchResult:
        """Hydrate ``session_id`` into ``dest_dir``, preserving the uuid."""
        ...

    def fetch_search_text(self, session_id: str) -> str | None:
        """The session's search payload, or None if it has none on this remote.

        Deliberately separate from :meth:`fetch`: the point is to be able to search a
        colleague's conversation without downloading it, and the payload is ~36x smaller
        than the transcript. A backend that cannot do it may return None.
        """
        ...

    def publish(self, session: RemoteSession, project_dir: Path) -> None:
        """Upload the session described by ``session``, reading its files from disk."""
        ...

    def unpublish(self, session_id: str) -> None:
        """Remove a published session from the remote. The local copy is untouched."""
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
        manifest_dir = self.root / MANIFEST_ROOT
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
        path = self.root / MANIFEST_ROOT / f"{safe_session_id(session_id)}.json"
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
        blob_dir = self.root / BLOB_ROOT / session_id
        if not blob_dir.is_dir():
            raise RemoteError(f"la sesión {session_id} no está en el remoto")
        main = blob_dir / MAIN_BLOB
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
            target.write_bytes(gzip.decompress(payload) if is_compressed_blob(name) else payload)
            written.append(target)
        return FetchResult(session_id=session_id, written=tuple(written))

    def publish(self, session: RemoteSession, project_dir: Path) -> None:
        files = collect_session_files(project_dir, session.session_id)
        if not files:
            raise RemoteError(f"la sesión {session.session_id} no tiene transcript en disco")

        blob_dir = self.root / BLOB_ROOT / session.session_id
        for path in files:
            name = blob_name_for(project_dir, session.session_id, path)
            target = blob_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = path.read_bytes()
            _atomic_write(target, gzip.compress(payload) if is_compressed_blob(name) else payload)

        # The search payload, so colleagues can search this by content without fetching it.
        search = search_payload_for(project_dir, session.session_id)
        if search is not None:
            target = self.root / search_blob_name(session.session_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, search)

        # Last, so a half-finished upload is invisible rather than broken.
        manifest = self.root / MANIFEST_ROOT / f"{session.session_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            manifest,
            json.dumps(
                with_search_size(session, search).to_manifest(), indent=2, ensure_ascii=False
            ).encode("utf-8"),
        )

    def fetch_search_text(self, session_id: str) -> str | None:
        path = self.root / search_blob_name(session_id)
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        return decode_search_payload(payload)

    def unpublish(self, session_id: str) -> None:
        """Remove the manifest first, then the blobs.

        The reverse of publishing, and for the same reason: the manifest is what makes a
        session visible, so dropping it first means an interrupted delete leaves unreferenced
        blobs (invisible, harmless) rather than a manifest pointing at missing payload.
        """
        safe_session_id(session_id)
        manifest = self.root / MANIFEST_ROOT / f"{session_id}.json"
        blob_dir = self.root / BLOB_ROOT / session_id
        search = self.root / search_blob_name(session_id)
        if not manifest.exists() and not blob_dir.exists():
            raise RemoteError(f"la sesión {session_id} no está publicada aquí")
        manifest.unlink(missing_ok=True)
        search.unlink(missing_ok=True)  # or it lingers as an orphan nothing references
        shutil.rmtree(blob_dir, ignore_errors=True)


REMOTE_DIR_ENV = "MULTI_CLAUDE_REMOTE_DIR"
REMOTE_TOKEN_ENV = "MULTI_CLAUDE_REMOTE_TOKEN"


def token_path() -> Path:
    """Where API tokens live: next to the config, but in their own file."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "multi-claude" / "remote-tokens.json"


def legacy_token_path() -> Path:
    """The single-token file used before tokens were per server."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "multi-claude" / "remote-token"


class TokenStore:
    """API tokens, one per configured server, kept out of ``config.json`` on purpose.

    ``config.json`` gets pasted into issues and shared between machines, so a credential in it
    leaks by accident. This is a separate file created ``0600``.

    Keyed by server name because a company has more than one host and they do not share
    credentials. ``$MULTI_CLAUDE_REMOTE_TOKEN`` still overrides everything, so CI or a one-off
    run never has to write a secret to disk; the old single-token file is read as a fallback so
    an existing setup keeps working.
    """

    def __init__(self, path: Path | None = None, *, legacy: Path | None = None) -> None:
        self.path = path or token_path()
        self.legacy = legacy if legacy is not None else legacy_token_path()

    def _load(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

    def _legacy_token(self) -> str | None:
        try:
            token = self.legacy.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None
        return token or None

    def get(self, server: str | None = None) -> str | None:
        env = os.environ.get(REMOTE_TOKEN_ENV)
        if env:
            return env
        tokens = self._load()
        if server:
            found = tokens.get(server)
            if found:
                return found
        elif len(tokens) == 1:
            # No server named and exactly one token stored: it can only mean that one.
            return next(iter(tokens.values()))
        return self._legacy_token()

    def has_token(self, server: str | None = None) -> bool:
        """Whether a token is available, without handing the value out.

        The settings screen needs to say "saved" without rendering the secret.
        """
        return self.get(server) is not None

    def set(self, token: str, server: str | None = None) -> None:
        """Store ``token`` for ``server`` with owner-only permissions, atomically."""
        tokens = self._load()
        tokens[server or ""] = token.strip()
        self._write(tokens)

    def delete(self, server: str | None = None) -> None:
        tokens = self._load()
        if tokens.pop(server or "", None) is not None:
            self._write(tokens)
        if server is None:
            self.legacy.unlink(missing_ok=True)

    def _write(self, tokens: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(prefix=".tokens.", dir=str(self.path.parent))
        tmp = Path(tmp_str)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tokens, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


def store_from_link(link: RemoteLink, *, token: str | None = None) -> RemoteStore | None:
    """Build a store for ``link``, or None when it is off or half-filled.

    Half-filled yields None rather than a store that raises on every call: the UI can then
    say "not configured" instead of surfacing a network error.
    """
    if not link.is_configured:
        return None
    if link.kind == "directory":
        return DirectoryRemote(Path(link.path).expanduser())
    if link.kind == "ssh":
        from multi_claude.remote_git import GitSshRemote

        return GitSshRemote(link)
    from multi_claude.remote_http import GitHubRemote, GitLabRemote

    driver = GitLabRemote if link.kind == "gitlab" else GitHubRemote
    return driver(
        link.api_host,
        link.repo,
        link.branch,
        token if token is not None else TokenStore().get(link.server or None),
    )


def store_from_settings(
    kind: str,
    path: str,
    *,
    host: str = "",
    repo: str = "",
    branch: str = "main",
    token: str | None = None,
) -> RemoteStore | None:
    """Build the configured store, or None when session sharing is off.

    ``$MULTI_CLAUDE_REMOTE_DIR`` wins over everything else. That makes trying a remote a
    one-liner, and lets a second checkout or a test point somewhere else without editing
    state shared with the running app.

    A hosted provider with no ``repo`` yields None rather than a store that fails on every
    call: half-configured is the same as off, and the settings screen says so.

    The HTTP drivers are imported here rather than at module scope: they import this module
    for the shared layout, so a top-level import would be circular.
    """
    env = os.environ.get(REMOTE_DIR_ENV)
    if env:
        return DirectoryRemote(Path(env).expanduser())
    if kind == "directory" and path:
        return DirectoryRemote(Path(path).expanduser())
    if kind in ("gitlab", "github") and repo and host:
        from multi_claude.remote_http import GitHubRemote, GitLabRemote

        driver = GitLabRemote if kind == "gitlab" else GitHubRemote
        return driver(host, repo, branch, token if token is not None else TokenStore().get())
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
