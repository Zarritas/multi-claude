"""Filesystem path completion for the add-project input.

Pure functions, no Textual import: keeps the modal small and lets us unit-test
the matching against ``tmp_path`` without spinning up an app.
"""

from __future__ import annotations

import os
from pathlib import Path

SUGGESTION_LIMIT = 12


def expand(prefix: str) -> Path:
    """``~`` and env-var expansion. Plain wrapper so callers don't import os."""
    return Path(os.path.expandvars(prefix)).expanduser()


def list_suggestions(
    prefix: str,
    *,
    limit: int = SUGGESTION_LIMIT,
    include_files: bool = False,
    suffixes: tuple[str, ...] | None = None,
) -> list[Path]:
    """Return up to ``limit`` path candidates matching ``prefix``.

    Rules:
      - Empty prefix returns ``[]`` (don't dump ``/`` at startup).
      - Prefix ending in ``/`` lists every entry of that path.
      - Otherwise lists the entries of ``Path(prefix).parent`` whose name
        starts with ``Path(prefix).name`` (case-insensitive).
      - Directories are always surfaced. Files are surfaced only when
        ``include_files`` is set, and then optionally filtered to ``suffixes``
        (lowercased extensions including the dot, e.g. ``(".zip",)``).
      - Directories sort before files; alphabetical within each group.

    Errors (permission, missing parent, OSError) yield ``[]`` so the input stays
    interactive even when the user types into a non-readable area.
    """
    if not prefix:
        return []
    expanded = expand(prefix)
    if prefix.endswith("/") and expanded.is_dir():
        base = expanded
        name_filter = ""
    else:
        base = expanded.parent
        name_filter = expanded.name.lower()
    if not base.is_dir():
        return []

    norm_suffixes = tuple(s.lower() for s in suffixes) if suffixes is not None else None
    try:
        candidates: list[Path] = []
        for entry in base.iterdir():
            if not entry.name.lower().startswith(name_filter):
                continue
            if entry.is_dir():
                candidates.append(entry)
            elif include_files and entry.is_file():
                if norm_suffixes is None or entry.suffix.lower() in norm_suffixes:
                    candidates.append(entry)
    except (PermissionError, OSError):
        return []

    # Directories first (so Tab keeps descending), then files; alpha within each.
    candidates.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    return candidates[:limit]


def common_prefix_completion(
    prefix: str,
    *,
    include_files: bool = False,
    suffixes: tuple[str, ...] | None = None,
) -> str | None:
    """Return the longest common path prefix of every candidate, or ``None``.

    Used by Tab: if multiple matches share a longer prefix than the user typed,
    we extend the input to that prefix. If only one match exists, we extend all
    the way to it — appending ``/`` only when that match is a directory, so the
    next Tab descends into it (a file is already the final value).
    """
    candidates = list_suggestions(
        prefix, limit=200, include_files=include_files, suffixes=suffixes
    )
    if not candidates:
        return None
    if len(candidates) == 1:
        only = candidates[0]
        return str(only) + ("/" if only.is_dir() else "")
    # commonprefix is string-level (commonpath rounds down to the directory).
    # We want "feature-login" + "feature-logout" → "feature-log".
    common = os.path.commonprefix([str(c) for c in candidates])
    typed = str(expand(prefix))
    if len(common) > len(typed):
        return common
    return None
