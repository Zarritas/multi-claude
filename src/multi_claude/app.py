"""Top-level Textual App. Owns the screen stack, global bindings, prefs and names store."""

from __future__ import annotations

import os

from textual.app import App

from multi_claude.colors import SessionColorsStore
from multi_claude.config import Config, load_config, save_config
from multi_claude.discovery import Project, project_remote_key
from multi_claude.names import NamesStore
from multi_claude.project_config import ProjectConfig, ProjectConfigReader
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
        # Cached per working tree by mtime: read on every project open, and a `git pull`
        # that changes the declaration has to take effect without restarting.
        self.project_config: ProjectConfigReader = ProjectConfigReader()

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
        3. What the project's repository declares in ``.multi-claude.json``, which is how a
           team gets the shared tab without every member configuring it by hand.
        4. The global remote, as a fallback for projects with no links of their own.

        Own links win over the global one outright rather than adding to it: a project linked
        to one client's repo must not also quietly publish to the default one. They also win
        over the repo's declaration, because they are a choice this person made deliberately
        and a file in a repository must not be able to override it — the declaration is a
        default for whoever has not chosen, which is exactly the colleague who just cloned.
        """
        env = os.environ.get(REMOTE_DIR_ENV)
        if env:
            # No label: RemoteLink derives one from the folder name, which reads better in a
            # tab and in sentences like "publicada en <label>" than a hardcoded word would.
            return (RemoteLink(kind="directory", path=env),)
        own = self.project_remotes.get(project_remote_key(project.path))
        if own:
            # Resolved here so every consumer sees a complete link: a stored link only names
            # its server, and the server's provider and host live in the config.
            return tuple(link.resolved(self.prefs.remote_servers) for link in own)
        declared = self.declared_links_for(project)
        if declared:
            return declared
        fallback = self.prefs.remote_link()
        return (fallback,) if fallback.is_configured else ()

    def declared_links_for(self, project: Project) -> tuple[RemoteLink, ...]:
        """The usable links the project's repository declares for the team.

        A declaration names a server; resolving it against **this machine's** config is what
        makes the mechanism safe, and it is also what makes an unknown server harmless: the
        link resolves to ``kind="none"``, which is not configured, so it is dropped here
        rather than becoming a tab that cannot be reached. The reason it was dropped is in
        :meth:`project_config_for`, which is what the link manager shows.
        """
        config = self.project_config.read(project.path)
        resolved = (link.resolved(self.prefs.remote_servers) for link in config.links)
        return tuple(link for link in resolved if link.is_configured)

    def project_config_for(self, project: Project) -> ProjectConfig:
        """What the project's repository declares, refusals included."""
        return self.project_config.read(project.path)

    def store_for_link(self, link: RemoteLink) -> RemoteStore | None:
        """Build the store for one link, or None if it is not usable."""
        return store_from_link(link)
