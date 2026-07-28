"""Top-level Textual App. Owns the screen stack, global bindings, prefs and names store."""

from __future__ import annotations

from textual.app import App

from multi_claude.colors import SessionColorsStore
from multi_claude.config import Config, load_config, save_config
from multi_claude.names import NamesStore
from multi_claude.project_folders import ProjectFoldersStore
from multi_claude.project_names import ProjectNamesStore
from multi_claude.remote import RemoteStore, store_from_settings
from multi_claude.tags import TagsStore


def _remote_from(prefs: Config) -> RemoteStore | None:
    """Build the shared-session store described by ``prefs``, if any."""
    return store_from_settings(
        prefs.remote_kind,
        prefs.remote_path,
        host=prefs.remote_api_host(),
        repo=prefs.remote_repo,
        branch=prefs.remote_branch,
    )


class ClaudeBrowserApp(App[None]):
    """Root app. Pushes ProjectsScreen at startup; SessionsScreen is pushed on Enter."""

    CSS_PATH = "styles.tcss"
    TITLE = "multi-claude"

    def __init__(self) -> None:
        super().__init__()
        self.prefs: Config = load_config()
        self.names: NamesStore = NamesStore()
        self.project_names: ProjectNamesStore = ProjectNamesStore()
        self.session_colors: SessionColorsStore = SessionColorsStore()
        self.project_folders: ProjectFoldersStore = ProjectFoldersStore()
        self.tags: TagsStore = TagsStore()
        self.remote: RemoteStore | None = _remote_from(self.prefs)

    def on_mount(self) -> None:
        from multi_claude.screens.projects import ProjectsScreen

        self.push_screen(ProjectsScreen())

    def update_prefs(self, prefs: Config) -> None:
        """Replace in-memory prefs and persist to disk.

        The remote store is rebuilt here too: pointing the config at a different path
        should take effect without a restart.
        """
        self.prefs = prefs
        save_config(prefs)
        self.remote = _remote_from(prefs)
