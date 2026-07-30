"""Export sessions to a shareable ``.zip`` and import them back.

A session lives as ``<id>.jsonl`` (plus an optional ``<id>/`` subagents subdir)
inside a project's encoded-path dir. To share one with a colleague we bundle those
files plus the per-session metadata that does *not* live in the jsonl (multi-claude's
own display name and tags) into a single zip:

    manifest.json
    sessions/<id>.jsonl
    sessions/<id>/...          # subagents subdir, if present

``manifest.json``::

    {
      "format": "multi-claude/session-export",
      "version": 1,
      "exported_at": "<iso8601>",
      "sessions": [
        {"id", "cwd", "branch", "display_name", "tags",
         "first_prompt", "message_count", "size_bytes"}
      ]
    }

On import the colleague picks one of *their* existing projects as the destination;
the files land in that project's encoded dir and Claude resumes the session under
that project's cwd (the jsonl's embedded cwd is historical and does not need
rewriting). The SQLite index is a cache rebuilt on the next scan, so it is not part
of the archive. ``session-env`` is deliberately excluded: it can hold machine-local
secrets and Claude recreates it on resume.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from multi_claude.names import NamesStore
from multi_claude.session import Session
from multi_claude.tags import TagsStore

FORMAT = "multi-claude/session-export"
VERSION = 1
_ARCHIVE_ROOT = "sessions"
_MANIFEST_NAME = "manifest.json"
_FILENAME_MAX = 60


class ArchiveError(RuntimeError):
    """Raised when an archive is missing, malformed, or fails its format/version check."""


@dataclass(frozen=True)
class ManifestSession:
    """One session entry parsed from an archive manifest (metadata only)."""

    session_id: str
    cwd: str | None
    branch: str | None
    display_name: str | None
    tags: tuple[str, ...]
    first_prompt: str | None


@dataclass(frozen=True)
class ImportResult:
    """Outcome of :func:`import_archive`."""

    imported: tuple[str, ...]
    skipped_existing: tuple[str, ...]
    skipped_missing: tuple[str, ...]


def safe_filename(text: str, fallback: str = "session") -> str:
    """Turn arbitrary label text into a filesystem-safe stem (no separators)."""
    cleaned = re.sub(r"[^\w.-]+", "-", text.strip(), flags=re.UNICODE).strip("-.")
    cleaned = cleaned[:_FILENAME_MAX].strip("-.")
    return cleaned or fallback


def export_sessions(sessions: list[Session], project_dir: Path, dest_zip: Path) -> int:
    """Bundle ``sessions`` (from ``project_dir``) into ``dest_zip``. Returns count written.

    Sessions whose jsonl is missing on disk are skipped. If none remain, no file is
    created and 0 is returned. The destination's parent dir is created if needed.
    """
    present = [s for s in sessions if (project_dir / f"{s.id}.jsonl").is_file()]
    if not present:
        return 0

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "sessions": [
            {
                "id": s.id,
                "cwd": s.cwd,
                "branch": s.branch,
                "display_name": s.display_name,
                "tags": list(s.tags),
                "first_prompt": s.first_prompt,
                "message_count": s.message_count,
                "size_bytes": s.size_bytes,
            }
            for s in present
        ],
    }

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2, ensure_ascii=False))
        for session in present:
            jsonl = project_dir / f"{session.id}.jsonl"
            zf.write(jsonl, f"{_ARCHIVE_ROOT}/{session.id}.jsonl")
            subdir = project_dir / session.id
            if subdir.is_dir():
                for path in sorted(subdir.rglob("*")):
                    if path.is_file():
                        rel = path.relative_to(project_dir).as_posix()
                        zf.write(path, f"{_ARCHIVE_ROOT}/{rel}")
    return len(present)


def read_manifest(zip_path: Path) -> tuple[ManifestSession, ...]:
    """Read and validate the manifest of ``zip_path``. Raises :class:`ArchiveError`."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                raw = zf.read(_MANIFEST_NAME)
            except KeyError as exc:
                raise ArchiveError("el archivo no contiene manifest.json") from exc
    except zipfile.BadZipFile as exc:
        raise ArchiveError("el archivo no es un .zip válido") from exc
    except OSError as exc:
        raise ArchiveError(f"no se pudo leer el archivo: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArchiveError("manifest.json corrupto (JSON inválido)") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ArchiveError("no parece un export de multi-claude")
    if data.get("version") != VERSION:
        raise ArchiveError(f"versión de formato no soportada: {data.get('version')!r}")

    entries = data.get("sessions")
    if not isinstance(entries, list) or not entries:
        raise ArchiveError("el manifest no lista ninguna sesión")

    sessions: list[ManifestSession] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        raw_tags = entry.get("tags")
        tags = (
            tuple(t for t in raw_tags if isinstance(t, str)) if isinstance(raw_tags, list) else ()
        )
        sessions.append(
            ManifestSession(
                session_id=entry["id"],
                cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
                branch=entry.get("branch") if isinstance(entry.get("branch"), str) else None,
                display_name=(
                    entry.get("display_name")
                    if isinstance(entry.get("display_name"), str)
                    else None
                ),
                tags=tags,
                first_prompt=(
                    entry.get("first_prompt")
                    if isinstance(entry.get("first_prompt"), str)
                    else None
                ),
            )
        )
    if not sessions:
        raise ArchiveError("el manifest no contiene sesiones válidas")
    return tuple(sessions)


def import_archive(
    zip_path: Path,
    dest_dir: Path,
    *,
    names_store: NamesStore | None = None,
    tags_store: TagsStore | None = None,
) -> ImportResult:
    """Extract every session in ``zip_path`` into ``dest_dir`` and restore name/tags.

    Sessions whose id already exists at the destination are skipped (never overwritten)
    so two real sessions don't collide silently; sessions listed in the manifest whose
    payload is absent from the archive are reported separately. The destination's index
    row is left to rebuild on the next scan.
    """
    manifest_sessions = read_manifest(zip_path)
    names = names_store or NamesStore()
    tags = tags_store or TagsStore()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()

    imported: list[str] = []
    skipped_existing: list[str] = []
    skipped_missing: list[str] = []

    with zipfile.ZipFile(zip_path) as zf:
        names_in_zip = set(zf.namelist())
        for ms in manifest_sessions:
            sid = ms.session_id
            jsonl_member = f"{_ARCHIVE_ROOT}/{sid}.jsonl"
            if jsonl_member not in names_in_zip:
                skipped_missing.append(sid)
                continue
            if (dest_dir / f"{sid}.jsonl").exists():
                skipped_existing.append(sid)
                continue

            members = [
                n
                for n in names_in_zip
                if n == jsonl_member or n.startswith(f"{_ARCHIVE_ROOT}/{sid}/")
            ]
            for member in members:
                _extract_member(zf, member, dest_dir, dest_root)

            if ms.display_name:
                names.set(sid, ms.display_name)
            if ms.tags:
                tags.set(sid, list(ms.tags))
            imported.append(sid)

    return ImportResult(
        imported=tuple(imported),
        skipped_existing=tuple(skipped_existing),
        skipped_missing=tuple(skipped_missing),
    )


def _extract_member(zf: zipfile.ZipFile, member: str, dest_dir: Path, dest_root: Path) -> None:
    """Extract ``member`` (a ``sessions/...`` arcname) into ``dest_dir`` safely.

    Strips the ``sessions/`` prefix and guards against path traversal: a crafted
    archive cannot write outside ``dest_dir``.
    """
    rel = member[len(_ARCHIVE_ROOT) + 1 :]
    if not rel:
        return
    target = (dest_dir / rel).resolve()
    if dest_root != target and dest_root not in target.parents:
        raise ArchiveError(f"ruta sospechosa en el archivo: {member}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src:
        target.write_bytes(src.read())
