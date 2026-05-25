"""User preferences persisted to ``~/.config/multi-claude/config.json``.

Stored settings:

- ``default_mode`` — launch mode for Enter. Shift+Enter uses :func:`alternate_for`.
- ``projects_sort`` / ``sessions_sort`` — column + direction for each screen.
- ``preview_visible`` — whether the session preview panel is shown.
- ``group_worktrees`` — whether to collapse multiple worktrees of the same repo.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from multi_claude.colors import ColorRule

LaunchMode = Literal["auto", "window", "suspend"]
VALID_MODES: tuple[LaunchMode, ...] = ("auto", "window", "suspend")

ProjectSortKey = Literal["name", "path", "session_count", "last_activity"]
VALID_PROJECT_SORT: tuple[ProjectSortKey, ...] = (
    "name",
    "path",
    "session_count",
    "last_activity",
)

SessionSortKey = Literal["prompt", "branch", "messages", "size", "last_activity"]
VALID_SESSION_SORT: tuple[SessionSortKey, ...] = (
    "prompt",
    "branch",
    "messages",
    "size",
    "last_activity",
)


@dataclass
class SortSpec:
    key: str
    descending: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "descending": self.descending}


@dataclass
class Config:
    default_mode: LaunchMode = "auto"
    projects_sort: SortSpec = field(
        default_factory=lambda: SortSpec(key="last_activity", descending=True)
    )
    sessions_sort: SortSpec = field(
        default_factory=lambda: SortSpec(key="last_activity", descending=True)
    )
    preview_visible: bool = True
    group_worktrees: bool = True
    color_rules: list[ColorRule] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "default_mode": self.default_mode,
            "projects_sort": self.projects_sort.to_dict(),
            "sessions_sort": self.sessions_sort.to_dict(),
            "preview_visible": self.preview_visible,
            "group_worktrees": self.group_worktrees,
            "color_rules": [r.to_dict() for r in self.color_rules],
        }


_OPPOSITE: dict[LaunchMode, LaunchMode] = {
    "auto": "suspend",
    "window": "suspend",
    "suspend": "window",
}


def alternate_for(mode: LaunchMode) -> LaunchMode:
    """Return the mode Shift+Enter triggers when ``mode`` is the default."""
    return _OPPOSITE[mode]


def config_path() -> Path:
    """Return the path to the config file (does not create it).

    Resolution order:
      1. ``XDG_CONFIG_HOME`` if set (any platform — explicit opt-in for XDG layout).
      2. ``%APPDATA%`` on Windows (idiomatic per-user roaming config location).
      3. ``~/.config`` everywhere else.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg).expanduser()
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / ".config"
    else:
        base = Path.home() / ".config"
    return base / "multi-claude" / "config.json"


def load_config(path: Path | None = None) -> Config:
    """Load config from ``path`` (default: ``config_path()``). Missing/invalid → defaults."""
    target = path or config_path()
    if not target.exists():
        return Config()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Config()
    if not isinstance(raw, dict):
        return Config()
    return Config(
        default_mode=_coerce_mode(raw.get("default_mode"), "auto"),
        projects_sort=_coerce_sort(raw.get("projects_sort"), VALID_PROJECT_SORT, "last_activity"),
        sessions_sort=_coerce_sort(raw.get("sessions_sort"), VALID_SESSION_SORT, "last_activity"),
        preview_visible=bool(raw.get("preview_visible", True)),
        group_worktrees=bool(raw.get("group_worktrees", True)),
        color_rules=_coerce_color_rules(raw.get("color_rules")),
    )


def save_config(config: Config, path: Path | None = None) -> None:
    """Persist ``config`` to ``path`` (creating parent dirs)."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")


def _coerce_mode(value: object, fallback: LaunchMode) -> LaunchMode:
    if isinstance(value, str) and value in VALID_MODES:
        return value
    return fallback


def _coerce_color_rules(value: object) -> list[ColorRule]:
    if not isinstance(value, list):
        return []
    rules: list[ColorRule] = []
    for item in value:
        rule = ColorRule.from_dict(item)
        if rule is not None:
            rules.append(rule)
    return rules


def _coerce_sort(value: object, valid: tuple[str, ...], fallback_key: str) -> SortSpec:
    if isinstance(value, dict):
        key = value.get("key")
        desc = value.get("descending", True)
        if isinstance(key, str) and key in valid:
            return SortSpec(key=key, descending=bool(desc))
    return SortSpec(key=fallback_key, descending=True)
