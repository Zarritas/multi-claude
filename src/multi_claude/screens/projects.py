"""ProjectsScreen — list of all Claude projects."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input

from multi_claude.config import Config
from multi_claude.deletion import delete_project
from multi_claude.discovery import Project, scan_projects
from multi_claude.formatting import format_relative_time
from multi_claude.launcher import LauncherError, launch_claude
from multi_claude.modals import AddProjectModal, ConfirmDeleteModal, SettingsModal


class ProjectsScreen(Screen):
    """Top-level screen. DataTable of projects sorted by last_activity desc."""

    BINDINGS = [
        Binding("a", "add_project", "Add"),
        Binding("d", "delete", "Delete"),
        Binding("s", "settings", "Settings"),
        Binding("slash", "show_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._projects: list[Project] = []
        self._visible_indices: list[int] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="projects", cursor_type="row", zebra_stripes=True)
        filter_input = Input(placeholder="filtro (Esc cierra)", id="filter")
        filter_input.display = False
        yield filter_input
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#projects", DataTable)
        table.add_columns("Proyecto", "Path", "Sesiones", "Última")
        self._populate()

    def _populate(self) -> None:
        self._projects = scan_projects()
        self._repaint()
        if not self._projects:
            self.sub_title = "sin proyectos en ~/.claude/projects"
        else:
            self.sub_title = f"{len(self._projects)} proyectos"

    def _repaint(self) -> None:
        table = self.query_one("#projects", DataTable)
        table.clear()
        query = self.query_one("#filter", Input).value.strip().lower()
        self._visible_indices = []
        for idx, project in enumerate(self._projects):
            if query and not self._matches(project, query):
                continue
            name = project.name + (" (huérfano)" if project.is_orphan else "")
            row = (
                name,
                str(project.path),
                str(project.session_count),
                format_relative_time(project.last_activity),
            )
            table.add_row(*row, key=str(idx))
            self._visible_indices.append(idx)
        filter_input = self.query_one("#filter", Input)
        if self._visible_indices and not filter_input.has_focus:
            table.focus()

    @staticmethod
    def _matches(project: Project, query: str) -> bool:
        haystack = f"{project.name} {project.path}".lower()
        return query in haystack

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        project = self._project_for_row(event.row_key)
        if project is None:
            return
        if project.is_orphan:
            self.notify(
                f"Proyecto huérfano: {project.path} no existe en disco",
                severity="warning",
            )
            return
        from multi_claude.screens.sessions import SessionsScreen

        self.app.push_screen(SessionsScreen(project))

    def _project_for_row(self, row_key) -> Project | None:
        if row_key.value is None:
            return None
        idx = int(row_key.value)
        if idx >= len(self._projects):
            return None
        return self._projects[idx]

    def _selected_project(self) -> Project | None:
        table = self.query_one("#projects", DataTable)
        if table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._visible_indices):
            return None
        return self._projects[self._visible_indices[table.cursor_row]]

    def action_add_project(self) -> None:
        self.app.push_screen(AddProjectModal(), self._apply_add_project)

    def _apply_add_project(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            launch_claude(
                path,
                None,
                app=self.app,
                mode=self.app.prefs.default_mode,  # type: ignore[attr-defined]
            )
        except LauncherError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Claude lanzado en {path}. Pulsa `r` para refrescar.")

    def action_settings(self) -> None:
        self.app.push_screen(
            SettingsModal(self.app.prefs),  # type: ignore[attr-defined]
            self._apply_settings,
        )

    def _apply_settings(self, result: Config | None) -> None:
        if result is None:
            return
        self.app.update_prefs(result)  # type: ignore[attr-defined]
        self.notify("Ajustes guardados")

    def action_delete(self) -> None:
        project = self._selected_project()
        if project is None:
            return
        modal = ConfirmDeleteModal(
            title=f"Borrar proyecto {project.name}",
            details=[
                f"Path: {project.path}",
                f"Sesiones a borrar: {project.session_count}",
                f"Última actividad: {format_relative_time(project.last_activity)}",
            ],
            warning="Esto elimina todas las sesiones del proyecto. No afecta al código en disco.",
        )
        self.app.push_screen(modal, lambda ok: self._apply_delete(project, ok))

    def _apply_delete(self, project: Project, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            delete_project(project.encoded_path)
        except OSError as exc:
            self.notify(f"Error al borrar: {exc}", severity="error")
            return
        self.notify(f"Proyecto {project.name} borrado")
        self._populate()

    def action_show_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        filter_input.display = True
        filter_input.focus()

    @on(Input.Changed, "#filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._repaint()

    @on(Input.Submitted, "#filter")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#projects", DataTable).focus()

    def action_clear_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        if filter_input.display:
            filter_input.value = ""
            filter_input.display = False
            self._repaint()

    def action_refresh(self) -> None:
        self._populate()
        self.notify("Proyectos re-escaneados")
