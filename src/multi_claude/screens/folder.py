"""FolderScreen — drill into a user-defined folder, showing subfolders + members."""

from __future__ import annotations

import contextlib
from typing import cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input
from textual.widgets.data_table import RowKey

from multi_claude.app_protocol import AppProtocol
from multi_claude.discovery import Project, scan_projects
from multi_claude.formatting import format_relative_time
from multi_claude.modals import AssignFolderModal, RenameModal


class FolderScreen(Screen[None]):
    """Lists subfolders + directly-assigned projects of a folder.

    The folder is identified by its **path** (e.g. ``"Trabajo/Cliente A"``).
    Rebuilds its contents on each mount from the live store + ``scan_projects``,
    so changes elsewhere are reflected when you re-enter.
    """

    BINDINGS = [
        Binding("n", "new_subfolder", "Nueva subcarpeta"),
        Binding("e", "rename", "Renombrar"),
        Binding("f", "unassign", "Quitar de carpeta"),
        Binding("d", "delete_folder", "Borrar carpeta"),
        Binding("escape", "back", "Back"),
        Binding("left", "back", "Back", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, folder_path: str) -> None:
        super().__init__()
        self.folder_path = folder_path
        # ``_rows`` is a heterogeneous list of subfolder labels and assigned projects.
        # Subfolders are represented by their FULL path string; projects by the dataclass.
        self._rows: list[str | Project] = []

    @property
    def _claude_app(self) -> AppProtocol:
        return cast(AppProtocol, self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="folder-contents", cursor_type="row", zebra_stripes=True)
        filter_input = Input(placeholder="filtro (Esc cierra)", id="filter")
        filter_input.display = False
        yield filter_input
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#folder-contents", DataTable)
        table.add_columns("Contenido", "Path", "Sesiones", "Última")
        self._refresh_rows()
        self._repaint()
        table.focus()

    def _refresh_rows(self) -> None:
        store = self._claude_app.project_folders
        subfolders = store.children_folders(self.folder_path)
        members_encoded = set(store.members_of(self.folder_path, recursive=False))
        # Resolve members against the live project scan.
        all_projects = {str(p.encoded_path): p for p in scan_projects()}
        direct_members = [all_projects[e] for e in members_encoded if e in all_projects]
        # Sort: subfolders first (alphabetically), then projects by last_activity desc.
        subfolders.sort(key=str.casefold)
        direct_members.sort(key=lambda p: p.last_activity, reverse=True)
        self._rows = [*subfolders, *direct_members]
        self.sub_title = (
            f"📁 {self.folder_path} — {len(subfolders)} subcarpeta(s), "
            f"{len(direct_members)} proyecto(s)"
        )

    def _repaint(self) -> None:
        table = self.query_one("#folder-contents", DataTable)
        table.clear()
        store = self._claude_app.project_folders
        for idx, row in enumerate(self._rows):
            if isinstance(row, str):
                # Subfolder
                descendant_count = len(store.members_of(row, recursive=True))
                child_count = len(store.children_folders(row))
                summary = ""
                if descendant_count:
                    summary = f"{descendant_count} proyecto(s)"
                if child_count:
                    if summary:
                        summary += f" · {child_count} subcarpeta(s)"
                    else:
                        summary = f"{child_count} subcarpeta(s)"
                leaf = row.rsplit("/", 1)[-1]
                table.add_row(
                    f"📁 {leaf}",
                    row,
                    summary or "—",
                    "—",
                    key=f"folder:{idx}",
                )
            else:
                table.add_row(
                    row.name,
                    str(row.path),
                    str(row.session_count),
                    format_relative_time(row.last_activity),
                    key=f"project:{idx}",
                )

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = self._index_from_row_key(event.row_key)
        if idx is None:
            return
        target = self._rows[idx]
        if isinstance(target, str):
            self.app.push_screen(FolderScreen(target))
            return
        # Project row
        if target.is_orphan:
            self.notify(
                f"Proyecto huérfano: {target.path} no existe en disco",
                severity="warning",
            )
            return
        from multi_claude.screens.sessions import SessionsScreen

        self.app.push_screen(SessionsScreen(target))

    def _index_from_row_key(self, row_key: RowKey) -> int | None:
        if row_key.value is None or ":" not in row_key.value:
            return None
        try:
            return int(row_key.value.split(":", 1)[1])
        except ValueError:
            return None

    def _selected_row(self) -> str | Project | None:
        table = self.query_one("#folder-contents", DataTable)
        if table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._rows):
            return None
        return self._rows[table.cursor_row]

    # -- actions ------------------------------------------------------------ #

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._refresh_rows()
        self._repaint()
        self.notify("Contenido actualizado")

    def action_new_subfolder(self) -> None:
        modal = AssignFolderModal(
            subtitle=f"Crear subcarpeta dentro de 📁 {self.folder_path}",
            existing_folders=[],  # only the "create new" path makes sense here
            current_folder=None,
        )
        self.app.push_screen(modal, self._apply_new_subfolder)

    def _apply_new_subfolder(self, result: str | None) -> None:
        if result is None or result == "":
            return
        store = self._claude_app.project_folders
        full = f"{self.folder_path}/{result}"
        try:
            store.add_folder(full)
        except ValueError as exc:
            self.notify(f"Error: {exc}", severity="error")
            return
        self.notify(f"Subcarpeta creada: {full}")
        self._refresh_rows()
        self._repaint()

    def action_rename(self) -> None:
        target = self._selected_row()
        if target is None:
            return
        if isinstance(target, str):
            current_leaf = target.rsplit("/", 1)[-1]
            self.app.push_screen(
                RenameModal(
                    subtitle=f"📁 {target}",
                    current_name=current_leaf,
                    title="Renombrar subcarpeta",
                    placeholder="nuevo nombre",
                ),
                lambda r: self._apply_rename_subfolder(target, r),
            )
            return
        # Project alias
        store = self._claude_app.project_names
        current = store.for_project(target.encoded_path)
        self.app.push_screen(
            RenameModal(
                subtitle=f"{target.name} — {target.path}",
                current_name=current,
                title="Renombrar proyecto",
                placeholder="alias del proyecto",
            ),
            lambda r: self._apply_rename_project(target, r),
        )

    def _apply_rename_subfolder(self, old_path: str, result: str | None) -> None:
        if result is None:
            return
        store = self._claude_app.project_folders
        if result == "":
            # Empty = delete subfolder (cascade).
            with contextlib.suppress(KeyError):
                store.delete_folder(old_path)
            self.notify(f"Subcarpeta {old_path} eliminada")
        else:
            try:
                store.rename_folder(old_path, result)
            except (KeyError, ValueError) as exc:
                self.notify(f"Error: {exc}", severity="error")
                return
            self.notify(f"Renombrada a {result}")
        self._refresh_rows()
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
        self._refresh_rows()
        self._repaint()

    def action_unassign(self) -> None:
        target = self._selected_row()
        if not isinstance(target, Project):
            return
        self._claude_app.project_folders.unassign(target.encoded_path)
        self.notify(f"{target.name} quitado de {self.folder_path}")
        self._refresh_rows()
        self._repaint()
        if not self._rows:
            self.app.pop_screen()

    def action_delete_folder(self) -> None:
        target = self._selected_row()
        if not isinstance(target, str):
            self.notify("Selecciona una subcarpeta para borrar", severity="warning")
            return
        with contextlib.suppress(KeyError):
            self._claude_app.project_folders.delete_folder(target)
        self.notify(f"Subcarpeta {target} eliminada (proyectos vuelven a root)")
        self._refresh_rows()
        self._repaint()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        target = self._selected_row()
        if action == "rename" and target is None:
            return False
        if action == "unassign" and not isinstance(target, Project):
            return False
        return not (action == "delete_folder" and not isinstance(target, str))
