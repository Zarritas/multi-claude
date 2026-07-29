"""Top-level Textual App. Owns the screen stack, global bindings, prefs and names store."""

from __future__ import annotations

import os

from textual.app import App

from multi_claude.colors import SessionColorsStore
from multi_claude.config import Config, load_config, save_config
from multi_claude.discovery import Project, project_remote_key
from multi_claude.names import NamesStore
from multi_claude.project_folders import ProjectFoldersStore
from multi_claude.project_names import ProjectNamesStore
from multi_claude.project_remotes import ProjectRemotesStore, RemoteLink
from multi_claude.remote import REMOTE_DIR_ENV, RemoteStore, store_from_link
from multi_claude.tags import TagsStore


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
        self.project_remotes: ProjectRemotesStore = ProjectRemotesStore()

    def on_mount(self) -> None:
        from multi_claude.screens.projects import ProjectsScreen

        self.push_screen(ProjectsScreen())

    def update_prefs(self, prefs: Config) -> None:
        """Replace in-memory prefs and persist to disk."""
        self.prefs = prefs
        save_config(prefs)

    # --- shared-session remotes ---------------------------------------------------------

    def remote_links_for(self, project: Project) -> tuple[RemoteLink, ...]:
        """Every sessions repo this project publishes to, in tab order.

        Resolution order, first match wins:

        1. ``$MULTI_CLAUDE_REMOTE_DIR`` — a total override to one scratch folder, so a
           throwaway run or a test never touches stored config or a real repo.
        2. The project's own links, keyed by its git ``origin``.
        3. The global remote, as a fallback for projects with no links of their own.

        Own links win over the global one outright rather than adding to it: a project linked
        to one client's repo must not also quietly publish to the default one.
        """
        env = os.environ.get(REMOTE_DIR_ENV)
        if env:
            return (RemoteLink(kind="directory", path=env, label="local"),)
        own = self.project_remotes.get(project_remote_key(project.path))
        if own:
            return own
        fallback = self.prefs.remote_link()
        return (fallback,) if fallback.is_configured else ()

    def store_for_link(self, link: RemoteLink) -> RemoteStore | None:
        """Build the store for one link, or None if it is not usable."""
        return store_from_link(link)
