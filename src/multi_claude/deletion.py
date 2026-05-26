"""Delete sessions and projects, cleaning every related artefact on disk.

Per-session disk artefacts found in a live Claude install:
- ``~/.claude/projects/<encoded>/<id>.jsonl``   — main log
- ``~/.claude/projects/<encoded>/<id>/``        — directory with subagents data
- ``~/.claude/session-env/<id>``                — env vars (file or dir)
- Entry in ``NamesStore``                       — display name
- Row in the SQLite index (plus the FTS row)

Deleting a project cascades the per-session cleanup over every jsonl inside and
then rmtree's the encoded directory itself.

Both ``delete_session`` and ``delete_project`` refuse to operate on a session
reported as live (``~/.claude/sessions/<host>.json``) unless ``force=True``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from multi_claude.index import SessionIndex, default_index
from multi_claude.names import NamesStore
from multi_claude.tags import TagsStore

CLAUDE_HOME = Path.home() / ".claude"
SESSION_ENV_DIR = CLAUDE_HOME / "session-env"
ACTIVE_SESSIONS_DIR = CLAUDE_HOME / "sessions"


class SessionActiveError(RuntimeError):
    """Raised when a delete targets a session reported as live in ``~/.claude/sessions/``."""

    def __init__(self, active_ids: set[str]) -> None:
        super().__init__(f"sesiones activas: {sorted(active_ids)}")
        self.active_ids = active_ids


def delete_session(
    session_id: str,
    project_dir: Path,
    *,
    names_store: NamesStore | None = None,
    session_env_dir: Path = SESSION_ENV_DIR,
    active_sessions_dir: Path = ACTIVE_SESSIONS_DIR,
    force: bool = False,
    index: SessionIndex | None = None,
    tags_store: TagsStore | None = None,
) -> None:
    """Remove every artefact tied to ``session_id``. Idempotent.

    Raises :class:`SessionActiveError` if the session is reported as live in
    ``~/.claude/sessions/`` and ``force`` is False. Callers (the UI confirm modal)
    pass ``force=True`` after the user acknowledges the warning.
    """
    if not force:
        active = list_active_sessions(sessions_dir=active_sessions_dir)
        if session_id in active:
            raise SessionActiveError({session_id})

    jsonl = project_dir / f"{session_id}.jsonl"
    jsonl.unlink(missing_ok=True)

    subdir = project_dir / session_id
    if subdir.is_dir():
        shutil.rmtree(subdir, ignore_errors=True)

    env_path = session_env_dir / session_id
    if env_path.is_dir():
        shutil.rmtree(env_path, ignore_errors=True)
    elif env_path.exists():
        env_path.unlink(missing_ok=True)

    (names_store or NamesStore()).delete(session_id)
    (tags_store or TagsStore()).delete(session_id)
    (index or default_index()).delete_session(session_id)


def delete_project(
    project_dir: Path,
    *,
    names_store: NamesStore | None = None,
    session_env_dir: Path = SESSION_ENV_DIR,
    active_sessions_dir: Path = ACTIVE_SESSIONS_DIR,
    force: bool = False,
    index: SessionIndex | None = None,
    tags_store: TagsStore | None = None,
) -> None:
    """Remove every session inside ``project_dir`` plus the directory itself.

    Same active-session guard as :func:`delete_session`: refuses upfront if any
    session inside this project is live, unless ``force=True``.
    """
    store = names_store or NamesStore()
    idx = index if index is not None else default_index()
    tags = tags_store or TagsStore()

    if project_dir.is_dir():
        if not force:
            active = list_active_sessions(sessions_dir=active_sessions_dir)
            blocked = {jsonl.stem for jsonl in project_dir.glob("*.jsonl")} & active
            if blocked:
                raise SessionActiveError(blocked)

        for jsonl in project_dir.glob("*.jsonl"):
            delete_session(
                jsonl.stem,
                project_dir,
                names_store=store,
                session_env_dir=session_env_dir,
                active_sessions_dir=active_sessions_dir,
                force=True,  # already gated above
                index=idx,
                tags_store=tags,
            )
        shutil.rmtree(project_dir, ignore_errors=True)


def merge_projects(
    orphan_dir: Path,
    destination_dir: Path,
) -> int:
    """Move every ``.jsonl`` (plus its sibling subdir) from ``orphan_dir`` into
    ``destination_dir`` and rmtree the orphan.

    Returns the number of sessions moved. The destination is created if missing.
    Files with the same session id at the destination are skipped (kept as-is)
    so two real sessions never collide silently.
    """
    if not orphan_dir.is_dir():
        return 0
    destination_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for jsonl in orphan_dir.glob("*.jsonl"):
        target = destination_dir / jsonl.name
        if target.exists():
            continue
        shutil.move(str(jsonl), str(target))
        moved += 1
        subdir = orphan_dir / jsonl.stem
        if subdir.is_dir():
            target_subdir = destination_dir / jsonl.stem
            if not target_subdir.exists():
                shutil.move(str(subdir), str(target_subdir))
    # Best-effort cleanup of whatever remains.
    shutil.rmtree(orphan_dir, ignore_errors=True)
    return moved


def list_active_sessions(
    sessions_dir: Path = ACTIVE_SESSIONS_DIR,
) -> set[str]:
    """Return session ids currently registered as live in ``~/.claude/sessions/``."""
    active: set[str] = set()
    if not sessions_dir.is_dir():
        return active
    for entry in sessions_dir.glob("*.json"):
        try:
            with entry.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        sid = data.get("sessionId") if isinstance(data, dict) else None
        if isinstance(sid, str) and sid:
            active.add(sid)
    return active
