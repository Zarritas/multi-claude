"""User-defined nested folders for grouping projects in ProjectsScreen.

Folders form a tree. Each folder is identified by its full slash-separated
path from the root (``"Trabajo"``, ``"Trabajo/Cliente A"``,
``"Trabajo/Cliente A/Backend"``). Each project (by its encoded
``~/.claude/projects/<encoded>`` path) can be assigned to at most one folder.
A project not assigned to anything lives at the root level, as before.

Storage:

    ~/.config/multi-claude/project-folders.json

    {
        "folders": ["Trabajo", "Trabajo/Cliente A"],
        "assignments": {
            "/home/x/.claude/projects/-work-acme": "Trabajo/Cliente A"
        }
    }

Rules:

- Names are case-insensitive but original casing is preserved for display.
- A path segment cannot contain ``/`` or be empty after trimming.
- Adding ``A/B/C`` creates ``A`` and ``A/B`` as well if missing.
- Renaming the leaf segment of ``A/B`` to ``X`` produces ``A/X`` and cascades
  to descendants and assignments.
- Deleting a folder cascades to every descendant; their assignments become
  unassigned (the projects are never deleted).
- Assignments referencing a missing folder are auto-cleaned on load.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SEPARATOR = "/"


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "multi-claude" / "project-folders.json"


def _split(path: str) -> list[str]:
    return [seg for seg in path.split(SEPARATOR) if seg]


def _join(segments: list[str]) -> str:
    return SEPARATOR.join(segments)


def _validate_segment(segment: str) -> str:
    trimmed = segment.strip()
    if not trimmed:
        raise ValueError("folder name segment cannot be empty")
    if SEPARATOR in trimmed:
        raise ValueError(f"folder name segment cannot contain {SEPARATOR!r}: {segment!r}")
    return trimmed


def _normalise_path(raw: str) -> str:
    """Trim each segment, drop empty ones, reject invalid ones."""
    parts = [_validate_segment(p) for p in raw.split(SEPARATOR) if p.strip()]
    if not parts:
        raise ValueError("folder path cannot be empty")
    return _join(parts)


def parent_of(path: str) -> str | None:
    """Return the parent path of ``path``, or ``None`` if it's a root folder."""
    segs = _split(path)
    if len(segs) <= 1:
        return None
    return _join(segs[:-1])


def leaf_of(path: str) -> str:
    segs = _split(path)
    return segs[-1] if segs else ""


def is_descendant(candidate: str, ancestor: str) -> bool:
    """``True`` if ``candidate`` is ``ancestor`` itself or below it."""
    a = _split(ancestor)
    c = _split(candidate)
    if len(c) < len(a):
        return False
    return [s.casefold() for s in c[: len(a)]] == [s.casefold() for s in a]


class ProjectFoldersStore:
    """File-backed store of folder tree + project→folder assignments."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._folders: list[str] | None = None
        self._assignments: dict[str, str] | None = None

    # -- loading / saving --------------------------------------------------- #

    def _load(self) -> None:
        if self._folders is not None and self._assignments is not None:
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            raw = {}
        folders_raw = raw.get("folders") if isinstance(raw, dict) else None
        assignments_raw = raw.get("assignments") if isinstance(raw, dict) else None

        folders: list[str] = []
        seen: set[str] = set()
        if isinstance(folders_raw, list):
            for item in folders_raw:
                if not isinstance(item, str):
                    continue
                try:
                    canonical = _normalise_path(item)
                except ValueError:
                    continue
                key = canonical.casefold()
                if key in seen:
                    continue
                seen.add(key)
                folders.append(canonical)

        # Ensure every ancestor of every loaded folder exists (defensive load).
        all_paths: set[str] = set()
        for folder_path in folders:
            segs = _split(folder_path)
            for i in range(1, len(segs) + 1):
                ancestor = _join(segs[:i])
                all_paths.add(ancestor)
        # Re-derive the ordered list (insertion order, ancestor-first).
        ordered: list[str] = []
        for folder_path in folders:
            segs = _split(folder_path)
            for i in range(1, len(segs) + 1):
                ancestor = _join(segs[:i])
                if ancestor not in ordered:
                    ordered.append(ancestor)
        # All previously-known should now appear in ordered.
        assert all_paths == set(ordered)
        folders = ordered

        assignments: dict[str, str] = {}
        if isinstance(assignments_raw, dict):
            valid = {folder_path.casefold(): folder_path for folder_path in folders}
            for encoded, folder in assignments_raw.items():
                if not isinstance(encoded, str) or not isinstance(folder, str):
                    continue
                try:
                    candidate = _normalise_path(folder)
                except ValueError:
                    continue
                matched = valid.get(candidate.casefold())
                if matched is None:
                    # Dangling assignment — drop silently.
                    continue
                assignments[encoded] = matched

        self._folders = folders
        self._assignments = assignments

    def reload(self) -> None:
        self._folders = None
        self._assignments = None
        self._load()

    def _save(self) -> None:
        assert self._folders is not None and self._assignments is not None
        payload = {"folders": list(self._folders), "assignments": dict(self._assignments)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=".project-folders.", suffix=".tmp", dir=str(self.path.parent)
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- folder management ------------------------------------------------- #

    def list_folders(self) -> list[str]:
        self._load()
        assert self._folders is not None
        return list(self._folders)

    def add_folder(self, path: str) -> str:
        """Create folder at ``path``, creating missing ancestors as needed.

        Returns the canonical (case-preserving) path. Idempotent.
        """
        self._load()
        assert self._folders is not None
        canonical_input = _normalise_path(path)
        segs = _split(canonical_input)
        canon_index = {f.casefold(): f for f in self._folders}

        running: list[str] = []
        for seg in segs:
            running.append(seg)
            current = _join(running)
            existing = canon_index.get(current.casefold())
            if existing is None:
                self._folders.append(current)
                canon_index[current.casefold()] = current
                running[-1] = seg  # keep the casing the caller used
            else:
                # Use the casing already on file for the ancestor chain.
                running[-1] = _split(existing)[-1]

        self._save()
        return canon_index[canonical_input.casefold()]

    def rename_folder(self, old_path: str, new_leaf: str) -> str:
        """Rename the leaf of ``old_path`` to ``new_leaf``. Descendants follow.

        Returns the new canonical path. Raises ``KeyError`` if ``old_path``
        does not exist, ``ValueError`` if the new path would collide with an
        existing folder or contains an illegal segment.
        """
        self._load()
        assert self._folders is not None and self._assignments is not None
        old_canonical = _normalise_path(old_path)
        leaf = _validate_segment(new_leaf)
        old_segs = _split(old_canonical)
        new_segs = [*old_segs[:-1], leaf]
        new_canonical = _join(new_segs)

        canon_index = {f.casefold(): f for f in self._folders}
        if old_canonical.casefold() not in canon_index:
            raise KeyError(f"folder not found: {old_path!r}")
        if (
            new_canonical.casefold() != old_canonical.casefold()
            and new_canonical.casefold() in canon_index
        ):
            raise ValueError(f"folder already exists: {new_canonical!r}")

        def _rewrite(folder_path: str) -> str:
            if folder_path.casefold() == old_canonical.casefold():
                return new_canonical
            if is_descendant(folder_path, old_canonical):
                tail = _split(folder_path)[len(old_segs) :]
                return _join(new_segs + tail)
            return folder_path

        self._folders = [_rewrite(f) for f in self._folders]
        self._assignments = {
            encoded: _rewrite(folder) for encoded, folder in self._assignments.items()
        }
        self._save()
        return new_canonical

    def delete_folder(self, path: str) -> None:
        """Remove ``path`` and every descendant; affected assignments are dropped."""
        self._load()
        assert self._folders is not None and self._assignments is not None
        target = _normalise_path(path)
        canon_index = {f.casefold(): f for f in self._folders}
        if target.casefold() not in canon_index:
            raise KeyError(f"folder not found: {path!r}")

        self._folders = [f for f in self._folders if not is_descendant(f, target)]
        self._assignments = {
            encoded: folder
            for encoded, folder in self._assignments.items()
            if not is_descendant(folder, target)
        }
        self._save()

    # -- assignments ------------------------------------------------------- #

    def assign(self, encoded_path: Path, folder_path: str) -> None:
        """Move ``encoded_path`` into ``folder_path`` (creating the folder if missing)."""
        canonical = self.add_folder(folder_path)
        self._load()
        assert self._assignments is not None
        self._assignments[str(encoded_path)] = canonical
        self._save()

    def unassign(self, encoded_path: Path) -> None:
        self._load()
        assert self._assignments is not None
        if str(encoded_path) in self._assignments:
            del self._assignments[str(encoded_path)]
            self._save()

    def folder_of(self, encoded_path: Path) -> str | None:
        self._load()
        assert self._assignments is not None
        return self._assignments.get(str(encoded_path))

    def members_of(self, folder_path: str, *, recursive: bool = False) -> list[str]:
        """Encoded paths assigned to ``folder_path`` (directly, or recursively)."""
        self._load()
        assert self._assignments is not None
        target = _normalise_path(folder_path)
        if recursive:
            return [
                encoded
                for encoded, folder in self._assignments.items()
                if is_descendant(folder, target)
            ]
        return [
            encoded
            for encoded, folder in self._assignments.items()
            if folder.casefold() == target.casefold()
        ]

    def all_assignments(self) -> dict[str, str]:
        self._load()
        assert self._assignments is not None
        return dict(self._assignments)

    def children_folders(self, parent_path: str | None) -> list[str]:
        """Direct subfolders of ``parent_path`` (None = root level)."""
        self._load()
        assert self._folders is not None
        if parent_path is None or not parent_path.strip():
            return [f for f in self._folders if SEPARATOR not in f]
        parent_canonical = _normalise_path(parent_path)
        parent_segs = _split(parent_canonical)
        depth = len(parent_segs)
        out: list[str] = []
        for f in self._folders:
            segs = _split(f)
            if len(segs) != depth + 1:
                continue
            if [s.casefold() for s in segs[:depth]] == [s.casefold() for s in parent_segs]:
                out.append(f)
        return out
