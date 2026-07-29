"""User preferences persisted to ``~/.config/multi-claude/config.json``.

Stored settings:

- ``default_mode`` — launch mode for Enter. Shift+Enter uses :func:`alternate_for`.
- ``claude_args`` — extra CLI flags prepended to every ``claude`` invocation.
- ``projects_sort`` / ``sessions_sort`` — column + direction for each screen.
- ``preview_visible`` — whether the session preview panel is shown.
- ``group_worktrees`` — whether to collapse multiple worktrees of the same repo.
- ``remote_*`` — the **global** remote for shared sessions, used when a project has no link
  of its own (see :mod:`multi_claude.project_remotes`). The auth token is deliberately not
  here: see :class:`multi_claude.remote.TokenStore`.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from multi_claude.colors import ColorRule
from multi_claude.project_remotes import RemoteLink

# Where the session lands. ``split``/``tab`` reuse the current window, ``window``
# opens a new one, ``suspend`` runs inline after suspending the TUI, ``auto``
# walks the chain split > tab > window > suspend.
LaunchMode = Literal["auto", "split", "tab", "window", "suspend"]
VALID_MODES: tuple[LaunchMode, ...] = ("auto", "split", "tab", "window", "suspend")

# Flags the TUI owns: allowing them in ``claude_args`` would either fight with the
# session we're resuming or break the interactive launch entirely.
RESERVED_CLAUDE_FLAGS: frozenset[str] = frozenset(
    {
        "-r",
        "--resume",
        "-c",
        "--continue",
        "-n",
        "--name",
        "-p",
        "--print",
        "--bg",
        "--background",
        "--from-pr",
    }
)

# Where shared sessions live. ``none`` disables the feature entirely; ``directory`` points at
# a path (a shared mount, a synced folder); ``gitlab``/``github`` push to a repo over its REST
# API. The auth token is deliberately *not* part of this config — see ``remote.TokenStore``.
RemoteKind = Literal["none", "directory", "gitlab", "github"]
VALID_REMOTE_KINDS: tuple[RemoteKind, ...] = ("none", "directory", "gitlab", "github")

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
    claude_args: list[str] = field(default_factory=list)
    projects_sort: SortSpec = field(
        default_factory=lambda: SortSpec(key="last_activity", descending=True)
    )
    sessions_sort: SortSpec = field(
        default_factory=lambda: SortSpec(key="last_activity", descending=True)
    )
    preview_visible: bool = True
    group_worktrees: bool = True
    color_rules: list[ColorRule] = field(default_factory=list)
    remote_kind: RemoteKind = "none"
    remote_path: str = ""
    remote_host: str = ""
    remote_repo: str = ""
    remote_branch: str = "main"

    def to_dict(self) -> dict[str, object]:
        return {
            "default_mode": self.default_mode,
            "claude_args": list(self.claude_args),
            "projects_sort": self.projects_sort.to_dict(),
            "sessions_sort": self.sessions_sort.to_dict(),
            "preview_visible": self.preview_visible,
            "group_worktrees": self.group_worktrees,
            "color_rules": [r.to_dict() for r in self.color_rules],
            "remote_kind": self.remote_kind,
            "remote_path": self.remote_path,
            "remote_host": self.remote_host,
            "remote_repo": self.remote_repo,
            "remote_branch": self.remote_branch,
        }

    def remote_link(self) -> RemoteLink:
        """The global remote as a :class:`RemoteLink`, the shape everything else speaks."""
        return RemoteLink(
            kind=self.remote_kind,
            path=self.remote_path,
            host=self.remote_host,
            repo=self.remote_repo,
            branch=self.remote_branch,
        )

    def with_remote_link(self, link: RemoteLink) -> Config:
        """This config with its ``remote_*`` fields taken from ``link``."""
        clean = link.normalised()
        return replace(
            self,
            remote_kind=_coerce_remote_kind(clean.kind),
            remote_path=clean.path,
            remote_host=clean.host,
            remote_repo=clean.repo,
            remote_branch=clean.branch,
        )

    def remote_api_host(self) -> str:
        """The API base URL to use: the configured one, or the provider's default."""
        return self.remote_link().api_host

    def remote_summary(self) -> str:
        """One-line description of the configured remote, for the settings screen."""
        return self.remote_link().summary()


_OPPOSITE: dict[LaunchMode, LaunchMode] = {
    "auto": "suspend",
    "split": "window",
    "tab": "window",
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
        claude_args=_coerce_claude_args(raw.get("claude_args")),
        projects_sort=_coerce_sort(raw.get("projects_sort"), VALID_PROJECT_SORT, "last_activity"),
        sessions_sort=_coerce_sort(raw.get("sessions_sort"), VALID_SESSION_SORT, "last_activity"),
        preview_visible=bool(raw.get("preview_visible", True)),
        group_worktrees=bool(raw.get("group_worktrees", True)),
        color_rules=_coerce_color_rules(raw.get("color_rules")),
        remote_kind=_coerce_remote_kind(raw.get("remote_kind")),
        remote_path=_coerce_str(raw.get("remote_path")),
        remote_host=_coerce_str(raw.get("remote_host")),
        remote_repo=_coerce_str(raw.get("remote_repo")),
        remote_branch=_coerce_str(raw.get("remote_branch")) or "main",
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


class ClaudeArgsError(ValueError):
    """Raised when a user-supplied ``claude_args`` string can't be used."""


def parse_claude_args(raw: str) -> list[str]:
    """Split a user-typed flag string into argv, rejecting flags the TUI owns.

    Raises :class:`ClaudeArgsError` on unbalanced quotes or on any flag in
    :data:`RESERVED_CLAUDE_FLAGS` (``--resume`` and friends would collide with the
    session multi-claude is actually launching).
    """
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise ClaudeArgsError(f"No se pudo interpretar la línea: {exc}") from exc
    for part in parts:
        flag = part.split("=", 1)[0]
        if flag in RESERVED_CLAUDE_FLAGS:
            raise ClaudeArgsError(f"`{flag}` lo gestiona multi-claude; quítalo de los extras")
    return parts


def _coerce_claude_args(value: object) -> list[str]:
    """Accept both the canonical list form and a hand-edited string in the JSON."""
    if isinstance(value, str):
        try:
            return parse_claude_args(value)
        except ClaudeArgsError:
            return []
    if not isinstance(value, list):
        return []
    args = [item for item in value if isinstance(item, str)]
    return [a for a in args if a.split("=", 1)[0] not in RESERVED_CLAUDE_FLAGS]


def _coerce_remote_kind(value: object) -> RemoteKind:
    for kind in VALID_REMOTE_KINDS:
        if value == kind:
            return kind
    return "none"


def _coerce_str(value: object) -> str:
    return value if isinstance(value, str) else ""


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
