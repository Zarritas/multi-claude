"""Typed contract for the root app, used by screens and modals.

Lets screens type ``self.app`` precisely instead of leaking ``# type: ignore``s.
Anything the screens read off the app (prefs, names store) belongs here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from multi_claude.colors import SessionColorsStore
    from multi_claude.config import Config
    from multi_claude.names import NamesStore
    from multi_claude.project_folders import ProjectFoldersStore
    from multi_claude.project_names import ProjectNamesStore
    from multi_claude.tags import TagsStore


class AppProtocol(Protocol):
    prefs: Config
    names: NamesStore
    project_names: ProjectNamesStore
    session_colors: SessionColorsStore
    project_folders: ProjectFoldersStore
    tags: TagsStore

    def update_prefs(self, prefs: Config) -> None: ...
