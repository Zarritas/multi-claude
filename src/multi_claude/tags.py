"""Persistent per-session tag store.

Tags are flat, multi-assignment labels users attach to sessions to slice them
in the listing (``tag:bug``, ``tag:bug,urgent``). Storage is keyed by session
id — session ids are UUIDs, globally unique, so no need to scope by project.

Default location follows the XDG Base Directory spec:
``$XDG_CONFIG_HOME/multi-claude/session-tags.json`` (fallback ``~/.config/...``).

Tag normalisation rules:

- Lowercased.
- Surrounding whitespace stripped; internal whitespace collapsed to ``-``.
- Reserved characters (``,`` and ``:``) are dropped so they never collide with
  the filter syntax (``tag:foo,bar``).
- Empty after normalisation → rejected (``None``).

The store keeps each session's tags ordered by insertion and de-duplicated.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

_WHITESPACE_RE = re.compile(r"\s+")
_RESERVED_RE = re.compile(r"[,:]")


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "multi-claude" / "session-tags.json"


def normalize_tag(raw: str) -> str | None:
    """Canonicalise a user-typed tag. Returns ``None`` for empty/invalid input."""
    if not isinstance(raw, str):
        return None
    stripped = _RESERVED_RE.sub("", raw).strip().lower()
    if not stripped:
        return None
    collapsed = _WHITESPACE_RE.sub("-", stripped)
    return collapsed or None


def parse_tag_list(raw: str) -> list[str]:
    """Split a free-form user string into normalised tags.

    Accepts comma and whitespace as separators so ``"bug urgent, cliente-acme"``
    becomes ``["bug", "urgent", "cliente-acme"]``. Duplicates are removed
    preserving first occurrence.
    """
    out: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,\s]+", raw):
        tag = normalize_tag(chunk)
        if tag is None or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


class TagsStore:
    """File-backed dict of ``session_id -> [tag, ...]``.

    Tolerant to a missing or corrupt file (treated as empty). Writes are atomic
    via tmp-file + os.replace. Reads cache the data in memory; callers can call
    ``reload()`` to force a re-read.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._data: dict[str, list[str]] | None = None

    def _load(self) -> dict[str, list[str]]:
        if self._data is not None:
            return self._data
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            raw = {}
        result: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for sid, tags in raw.items():
                if not isinstance(sid, str) or not isinstance(tags, list):
                    continue
                normalised: list[str] = []
                seen: set[str] = set()
                for item in tags:
                    if not isinstance(item, str):
                        continue
                    canonical = normalize_tag(item)
                    if canonical is None or canonical in seen:
                        continue
                    seen.add(canonical)
                    normalised.append(canonical)
                if normalised:
                    result[sid] = normalised
        self._data = result
        return self._data

    def reload(self) -> None:
        self._data = None
        self._load()

    def get(self, session_id: str) -> tuple[str, ...]:
        return tuple(self._load().get(session_id, ()))

    def set(self, session_id: str, tags: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Replace the tags for ``session_id``. Empty list deletes the entry."""
        data = self._load()
        canonical: list[str] = []
        seen: set[str] = set()
        for raw in tags:
            tag = normalize_tag(raw) if isinstance(raw, str) else None
            if tag is None or tag in seen:
                continue
            seen.add(tag)
            canonical.append(tag)
        if canonical:
            data[session_id] = canonical
        elif session_id in data:
            del data[session_id]
        self._write(data)
        return tuple(canonical)

    def add(self, session_id: str, tag: str) -> tuple[str, ...]:
        """Append ``tag`` to ``session_id`` (idempotent)."""
        canonical = normalize_tag(tag)
        if canonical is None:
            return self.get(session_id)
        data = self._load()
        current = list(data.get(session_id, []))
        if canonical not in current:
            current.append(canonical)
            data[session_id] = current
            self._write(data)
        return tuple(current)

    def remove(self, session_id: str, tag: str) -> tuple[str, ...]:
        """Drop ``tag`` from ``session_id`` (idempotent)."""
        canonical = normalize_tag(tag)
        if canonical is None:
            return self.get(session_id)
        data = self._load()
        current = list(data.get(session_id, []))
        if canonical not in current:
            return tuple(current)
        current = [t for t in current if t != canonical]
        if current:
            data[session_id] = current
        else:
            del data[session_id]
        self._write(data)
        return tuple(current)

    def delete(self, session_id: str) -> None:
        """Remove every tag of ``session_id``. Idempotent."""
        data = self._load()
        if session_id in data:
            del data[session_id]
            self._write(data)

    def all(self) -> dict[str, tuple[str, ...]]:
        return {sid: tuple(tags) for sid, tags in self._load().items()}

    def all_known_tags(self) -> list[str]:
        """Sorted union of every tag in use. Useful for autocompletion."""
        seen: set[str] = set()
        for tags in self._load().values():
            seen.update(tags)
        return sorted(seen)

    def _write(self, data: dict[str, list[str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Drop empty lists defensively before serialising.
        payload = {sid: list(tags) for sid, tags in data.items() if tags}
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=".session-tags.", suffix=".tmp", dir=str(self.path.parent)
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
