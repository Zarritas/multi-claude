"""ProjectsScreen — list of all Claude projects."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input
from textual.widgets.data_table import RowKey

from multi_claude.app_protocol import AppProtocol
from multi_claude.colors import ColorRule
from multi_claude.config import Config, SortSpec
from multi_claude.deletion import delete_project, list_active_sessions, merge_projects
from multi_claude.discovery import (
    Project,
    ProjectFolder,
    WorktreeGroup,
    group_into_folders,
    group_worktrees,
    scan_projects,
)
from multi_claude.filtering import FilterQuery, matches_fuzzy, parse_query
from multi_claude.formatting import format_relative_time
from multi_claude.launcher import LauncherError, launch_claude
from multi_claude.modals import (
    AddProjectModal,
    AssignFolderModal,
    ColorRulesEditorModal,
    ConfirmDeleteModal,
    MergeProjectModal,
    RenameModal,
    SettingsModal,
)

# Sort keys exposed via number keys. Order = column order in the table.
_SORT_KEYS_BY_COLUMN: tuple[str, ...] = ("name", "path", "session_count", "last_activity")


class ProjectsScreen(Screen[None]):
    """Top-level screen. DataTable of projects sorted per prefs."""

    BINDINGS = [
        Binding("a", "add_project", "Add"),
        Binding("d", "delete", "Delete"),
        Binding("e", "rename", "Rename"),
        Binding("C", "edit_color_rules", "Reglas color"),
        Binding("s", "settings", "Settings"),
        Binding("slash", "show_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("1", "sort_column('name')", "Sort name", show=False),
        Binding("2", "sort_column('path')", "Sort path", show=False),
        Binding("3", "sort_column('session_count')", "Sort sesiones", show=False),
        Binding("4", "sort_column('last_activity')", "Sort última", show=False),
        Binding("shift+s", "toggle_sort_direction", "Sort dir"),
        Binding("g", "toggle_groups", "Group worktrees"),
        Binding("f", "assign_folder", "Folder"),
        Binding("m", "merge_orphan", "Merge orphan"),
        Binding("question_mark", "search_global", "FTS global", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._projects: list[Project] = []
        # Rows shown to the user can be ``Project``, ``WorktreeGroup`` or ``ProjectFolder``.
        self._rows: list[Project | WorktreeGroup | ProjectFolder] = []
        self._visible_indices: list[int] = []

    @property
    def _claude_app(self) -> AppProtocol:
        return cast(AppProtocol, self.app)

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
        self.sub_title = "escaneando…"
        self._scan_projects_worker()

    @work(thread=True, exclusive=True, group="scan-projects")
    def _scan_projects_worker(self) -> None:
        results = scan_projects()
        self.app.call_from_thread(self._on_scan_complete, results)

    def _on_scan_complete(self, projects: list[Project]) -> None:
        self._projects = projects
        if not self._projects:
            self.sub_title = "sin proyectos en ~/.claude/projects"
        else:
            self.sub_title = f"{len(self._projects)} proyectos"
        self._apply_sort()
        self._repaint()

    def _apply_sort(self) -> None:
        spec = self._claude_app.prefs.projects_sort
        self._projects.sort(key=_project_sort_value(spec.key), reverse=spec.descending)
        base_rows: list[Project | WorktreeGroup]
        if self._claude_app.prefs.group_worktrees:
            base_rows = group_worktrees(self._projects)
        else:
            base_rows = list(self._projects)
        # Pull folder-assigned projects out of base_rows into ProjectFolder rows.
        folder_of = self._claude_app.project_folders.all_assignments()
        if folder_of:
            self._rows = group_into_folders(base_rows, folder_of)
        else:
            self._rows = list(base_rows)

    def _repaint(self) -> None:
        table = self.query_one("#projects", DataTable)
        table.clear()
        raw_query = self.query_one("#filter", Input).value
        query = parse_query(raw_query)
        self._visible_indices = []
        for idx, row_item in enumerate(self._rows):
            if not query.is_empty and not self._matches(row_item, query):
                continue
            row = self._format_row(row_item)
            table.add_row(*row, key=str(idx))
            self._visible_indices.append(idx)
        filter_input = self.query_one("#filter", Input)
        if self._visible_indices and not filter_input.has_focus:
            table.focus()

    def _format_row(
        self, row_item: Project | WorktreeGroup | ProjectFolder
    ) -> tuple[str, str, str, str]:
        store = self._claude_app.project_names
        if isinstance(row_item, ProjectFolder):
            total = row_item.total_member_count
            if row_item.descendant_member_count:
                summary = (
                    f"{len(row_item.members)} directos · "
                    f"{row_item.descendant_member_count} en subcarpetas"
                )
            else:
                summary = f"{total} proyecto(s)"
            return (
                f"📁 {row_item.name}",
                summary,
                str(row_item.session_count),
                format_relative_time(row_item.last_activity),
            )
        if isinstance(row_item, WorktreeGroup):
            members = row_item.members
            alias = store.for_repo(row_item.repo_root)
            base = alias or row_item.repo_root.name or members[0].name
            name = f"{base} (+{len(members) - 1} worktree)"
            return (
                name,
                str(row_item.repo_root),
                str(row_item.session_count),
                format_relative_time(row_item.last_activity),
            )
        project = row_item
        alias = store.for_project(project.encoded_path)
        base = alias or project.name
        name = base + (" (huérfano)" if project.is_orphan else "")
        return (
            name,
            str(project.path),
            str(project.session_count),
            format_relative_time(project.last_activity),
        )

    def _matches(
        self, row_item: Project | WorktreeGroup | ProjectFolder, query: FilterQuery
    ) -> bool:
        store = self._claude_app.project_names
        if isinstance(row_item, ProjectFolder):
            names = " ".join(m.name for m in row_item.members)
            paths = " ".join(str(m.path) for m in row_item.members)
            haystack = f"{row_item.name} {names} {paths}"
        elif isinstance(row_item, WorktreeGroup):
            paths = " ".join(str(m.path) for m in row_item.members)
            names = " ".join(m.name for m in row_item.members)
            group_alias = store.for_repo(row_item.repo_root) or ""
            member_aliases = " ".join(
                store.for_project(m.encoded_path) or "" for m in row_item.members
            )
            haystack = f"{group_alias} {member_aliases} {names} {paths} {row_item.repo_root}"
        else:
            project_alias = store.for_project(row_item.encoded_path) or ""
            haystack = f"{project_alias} {row_item.name} {row_item.path}"

        for key, value in query.constraints.items():
            if key == "path":
                if value not in haystack.lower():
                    return False
            elif key == "branch":
                # Branch isn't surfaced at the project level today.
                return False
        return matches_fuzzy(haystack, query.free_text)

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        row_item = self._row_for_key(event.row_key)
        if row_item is None:
            return
        if isinstance(row_item, ProjectFolder):
            from multi_claude.screens.folder import FolderScreen

            self.app.push_screen(FolderScreen(row_item.name))
            return
        if isinstance(row_item, WorktreeGroup):
            from multi_claude.screens.worktrees import WorktreesScreen

            self.app.push_screen(WorktreesScreen(row_item))
            return
        project = row_item
        if project.is_orphan:
            self.notify(
                f"Proyecto huérfano: {project.path} no existe en disco",
                severity="warning",
            )
            return
        from multi_claude.screens.sessions import SessionsScreen

        self.app.push_screen(SessionsScreen(project))

    def _row_for_key(self, row_key: RowKey) -> Project | WorktreeGroup | ProjectFolder | None:
        if row_key.value is None:
            return None
        idx = int(row_key.value)
        if idx >= len(self._rows):
            return None
        return self._rows[idx]

    def _selected_row(self) -> Project | WorktreeGroup | ProjectFolder | None:
        table = self.query_one("#projects", DataTable)
        if table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._visible_indices):
            return None
        return self._rows[self._visible_indices[table.cursor_row]]

    def _selected_project(self) -> Project | None:
        row = self._selected_row()
        if isinstance(row, Project):
            return row
        return None

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
                mode=self._claude_app.prefs.default_mode,
            )
        except LauncherError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Claude lanzado en {path}. Pulsa `r` para refrescar.")

    def action_settings(self) -> None:
        self.app.push_screen(
            SettingsModal(self._claude_app.prefs),
            self._apply_settings,
        )

    def _apply_settings(self, result: Config | None) -> None:
        if result is None:
            return
        self._claude_app.update_prefs(result)
        self._apply_sort()
        self._repaint()
        self.notify("Ajustes guardados")

    def action_delete(self) -> None:
        project = self._selected_project()
        if project is None:
            return
        active = list_active_sessions()
        live_in_project = {jsonl.stem for jsonl in project.encoded_path.glob("*.jsonl")} & active
        if live_in_project:
            warning = (
                f"⚠ Hay {len(live_in_project)} sesión(es) corriendo ahora mismo en este "
                f"proyecto. Bórralas sólo si sabes lo que haces."
            )
        else:
            warning = "Esto elimina todas las sesiones del proyecto. No afecta al código en disco."
        modal = ConfirmDeleteModal(
            title=f"Borrar proyecto {project.name}",
            details=[
                f"Path: {project.path}",
                f"Sesiones a borrar: {project.session_count}",
                f"Última actividad: {format_relative_time(project.last_activity)}",
            ],
            warning=warning,
        )
        self.app.push_screen(modal, lambda ok: self._apply_delete(project, ok))

    def _apply_delete(self, project: Project, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            delete_project(project.encoded_path, force=True)
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

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide row-dependent bindings when not applicable."""
        if action == "delete" and self._selected_project() is None:
            return False
        if action == "rename" and self._selected_row() is None:
            return False
        if action == "assign_folder" and self._selected_project() is None:
            return False
        if action == "merge_orphan":
            project = self._selected_project()
            if project is None or not project.is_orphan:
                return False
        return True

    def action_sort_column(self, key: str) -> None:
        if key not in _SORT_KEYS_BY_COLUMN:
            return
        spec = self._claude_app.prefs.projects_sort
        if spec.key == key:
            new_spec = SortSpec(key=key, descending=not spec.descending)
        else:
            new_spec = SortSpec(key=key, descending=True)
        new_prefs = Config(
            default_mode=self._claude_app.prefs.default_mode,
            projects_sort=new_spec,
            sessions_sort=self._claude_app.prefs.sessions_sort,
            preview_visible=self._claude_app.prefs.preview_visible,
            group_worktrees=self._claude_app.prefs.group_worktrees,
        )
        self._claude_app.update_prefs(new_prefs)
        self._apply_sort()
        self._repaint()
        self.notify(f"Orden: {key} {'desc' if new_spec.descending else 'asc'}")

    def action_toggle_sort_direction(self) -> None:
        spec = self._claude_app.prefs.projects_sort
        self.action_sort_column(spec.key)

    def action_toggle_groups(self) -> None:
        prefs = self._claude_app.prefs
        new_prefs = Config(
            default_mode=prefs.default_mode,
            projects_sort=prefs.projects_sort,
            sessions_sort=prefs.sessions_sort,
            preview_visible=prefs.preview_visible,
            group_worktrees=not prefs.group_worktrees,
        )
        self._claude_app.update_prefs(new_prefs)
        self._apply_sort()
        self._repaint()
        state = "agrupados" if new_prefs.group_worktrees else "expandidos"
        self.notify(f"Worktrees {state}")

    def action_search_global(self) -> None:
        from multi_claude.screens.search import SearchScreen

        self.app.push_screen(SearchScreen())

    def action_edit_color_rules(self) -> None:
        self.app.push_screen(
            ColorRulesEditorModal(list(self._claude_app.prefs.color_rules)),
            self._apply_color_rules,
        )

    def _apply_color_rules(self, result: list[ColorRule] | None) -> None:
        if result is None:
            return
        prefs = self._claude_app.prefs
        new_prefs = Config(
            default_mode=prefs.default_mode,
            projects_sort=prefs.projects_sort,
            sessions_sort=prefs.sessions_sort,
            preview_visible=prefs.preview_visible,
            group_worktrees=prefs.group_worktrees,
            color_rules=result,
        )
        self._claude_app.update_prefs(new_prefs)
        self.notify(f"Reglas guardadas ({len(result)})")

    def action_rename(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        store = self._claude_app.project_names
        if isinstance(row, ProjectFolder):
            self.app.push_screen(
                RenameModal(
                    subtitle=f"📁 {row.name} · {len(row.members)} proyecto(s)",
                    current_name=row.name,
                    title="Renombrar carpeta",
                    placeholder="nuevo nombre de carpeta",
                ),
                lambda result: self._apply_rename_folder(row, result),
            )
            return
        if isinstance(row, WorktreeGroup):
            current = store.for_repo(row.repo_root)
            self.app.push_screen(
                RenameModal(
                    subtitle=f"repo: {row.repo_root} · {len(row.members)} worktree(s)",
                    current_name=current,
                    title="Renombrar grupo de worktrees",
                    placeholder="alias del repo",
                ),
                lambda result: self._apply_rename_group(row, result),
            )
            return
        # Plain project (may be a worktree shown individually if grouping is off)
        current = store.for_project(row.encoded_path)
        self.app.push_screen(
            RenameModal(
                subtitle=f"{row.name} — {row.path}",
                current_name=current,
                title="Renombrar proyecto",
                placeholder="alias del proyecto",
            ),
            lambda result: self._apply_rename_project(row, result),
        )

    def _apply_rename_folder(self, folder: ProjectFolder, result: str | None) -> None:
        if result is None:
            return
        if result == "":
            # Empty = delete the folder (members become unassigned).
            import contextlib

            with contextlib.suppress(KeyError):
                self._claude_app.project_folders.delete_folder(folder.name)
            self.notify(f"Carpeta {folder.name} eliminada")
        else:
            try:
                self._claude_app.project_folders.rename_folder(folder.name, result)
            except (KeyError, ValueError) as exc:
                self.notify(f"Error: {exc}", severity="error")
                return
            self.notify(f"Carpeta renombrada: {result}")
        self._apply_sort()
        self._repaint()

    def _apply_rename_group(self, group: WorktreeGroup, result: str | None) -> None:
        if result is None:
            return
        store = self._claude_app.project_names
        if result == "":
            store.delete_for_repo(group.repo_root)
            self.notify("Alias borrado")
        else:
            store.set_for_repo(group.repo_root, result)
            self.notify(f"Repo renombrado: {result}")
        self._repaint()

    def _apply_rename_project(self, project: Project, result: str | None) -> None:
        if result is None:
            return
        store = self._claude_app.project_names
        if result == "":
            store.delete_for_project(project.encoded_path)
            self.notify("Alias borrado")
        else:
            store.set_for_project(project.encoded_path, result)
            self.notify(f"Proyecto renombrado: {result}")
        self._repaint()

    def action_assign_folder(self) -> None:
        project = self._selected_project()
        if project is None:
            self.notify(
                "Selecciona un proyecto individual (no un grupo ni una carpeta)",
                severity="warning",
            )
            return
        store = self._claude_app.project_folders
        current = store.folder_of(project.encoded_path)
        modal = AssignFolderModal(
            subtitle=f"{project.name} — {project.path}",
            existing_folders=store.list_folders(),
            current_folder=current,
        )
        self.app.push_screen(modal, lambda r: self._apply_assign_folder(project, r))

    def _apply_assign_folder(self, project: Project, result: str | None) -> None:
        if result is None:
            return
        store = self._claude_app.project_folders
        if result == "":
            store.unassign(project.encoded_path)
            self.notify(f"{project.name} quitado de su carpeta")
        else:
            store.assign(project.encoded_path, result)
            self.notify(f"{project.name} asignado a {result}")
        self._apply_sort()
        self._repaint()

    def action_merge_orphan(self) -> None:
        project = self._selected_project()
        if project is None:
            return
        if not project.is_orphan:
            self.notify("Sólo puedes hacer merge desde un proyecto huérfano", severity="warning")
            return
        candidates = _merge_candidates(project, self._projects)
        modal = MergeProjectModal(project, candidates)
        self.app.push_screen(modal, lambda result: self._apply_merge(project, result))

    def _apply_merge(self, orphan: Project, destination: Project | None) -> None:
        if destination is None:
            return
        try:
            moved = merge_projects(orphan.encoded_path, destination.encoded_path)
        except OSError as exc:
            self.notify(f"Error en merge: {exc}", severity="error")
            return
        # Transfer orphan's alias to the destination if the destination has none yet.
        store = self._claude_app.project_names
        orphan_alias = store.for_project(orphan.encoded_path)
        if orphan_alias is not None and store.for_project(destination.encoded_path) is None:
            store.set_for_project(destination.encoded_path, orphan_alias)
        store.delete_for_project(orphan.encoded_path)
        self.notify(f"Movidas {moved} sesión(es) a {destination.name}")
        self._populate()


def _project_sort_value(key: str) -> Callable[[Project], Any]:
    """Return a key fn for ``list.sort`` using project field ``key``."""
    if key == "name":
        return lambda p: p.name.casefold()
    if key == "path":
        return lambda p: str(p.path).casefold()
    if key == "session_count":
        return lambda p: p.session_count
    return lambda p: p.last_activity


def _merge_candidates(orphan: Project, all_projects: list[Project]) -> list[Project]:
    """Pick projects whose ``name`` matches the orphan and that aren't themselves orphans."""
    return [
        p
        for p in all_projects
        if p.encoded_path != orphan.encoded_path
        and not p.is_orphan
        and (p.name == orphan.name or p.git_common_dir == orphan.git_common_dir)
    ]
