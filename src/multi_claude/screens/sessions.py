"""SessionsScreen — list of sessions inside one project."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input

from multi_claude.config import Config, LaunchMode, alternate_for
from multi_claude.deletion import delete_session, list_active_sessions
from multi_claude.discovery import Project
from multi_claude.formatting import format_relative_time, format_size
from multi_claude.launcher import LauncherError, launch_claude
from multi_claude.modals import ConfirmDeleteModal, RenameModal, SettingsModal
from multi_claude.names import NamesStore
from multi_claude.session import Session, scan_sessions


class SessionsScreen(Screen):
    """DataTable of sessions for a single project, sorted by last_activity desc."""

    BINDINGS = [
        Binding("n", "new_session", "New"),
        Binding("shift+enter", "launch_alternate", "Launch alt"),
        Binding("e", "rename", "Rename"),
        Binding("d", "delete", "Delete"),
        Binding("s", "settings", "Settings"),
        Binding("slash", "show_filter", "Filter"),
        Binding("escape", "back_or_clear", "Back"),
        Binding("left", "back_or_clear", "Back", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self._sessions: list[Session] = []
        self._visible_indices: list[int] = []
        self._names = NamesStore()

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
        filter_input = Input(placeholder="filtro (Esc cierra)", id="filter")
        filter_input.display = False
        yield filter_input
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.project.name} — {self.project.path}"
        table = self.query_one("#sessions", DataTable)
        table.add_columns("Prompt", "Branch", "Msgs", "Tamaño", "Última")
        self._populate()

    def _populate(self) -> None:
        self._sessions = scan_sessions(self.project.encoded_path, names_store=self._names)
        self._repaint()

    def _repaint(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.clear()
        query = self.query_one("#filter", Input).value.strip().lower()
        self._visible_indices = []
        for idx, session in enumerate(self._sessions):
            if query and not self._matches(session, query):
                continue
            row = (
                session.display_name or session.first_prompt,
                session.branch or "—",
                str(session.message_count),
                format_size(session.size_bytes),
                format_relative_time(session.last_activity),
            )
            table.add_row(*row, key=str(idx))
            self._visible_indices.append(idx)
        filter_input = self.query_one("#filter", Input)
        if self._visible_indices and not filter_input.has_focus:
            table.focus()

    @staticmethod
    def _matches(session: Session, query: str) -> bool:
        haystack = " ".join(
            filter(
                None,
                [
                    session.display_name or "",
                    session.first_prompt or "",
                    session.branch or "",
                ],
            )
        ).lower()
        return query in haystack

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        session = self._session_for_row(event.row_key)
        if session is None:
            return
        self._launch(session.id, session.display_name, self._prefs().default_mode)

    def _session_for_row(self, row_key) -> Session | None:
        if row_key.value is None:
            return None
        idx = int(row_key.value)
        if idx >= len(self._sessions):
            return None
        return self._sessions[idx]

    def _selected_session(self) -> Session | None:
        table = self.query_one("#sessions", DataTable)
        if table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._visible_indices):
            return None
        return self._sessions[self._visible_indices[table.cursor_row]]

    def action_new_session(self) -> None:
        self._launch(None, None, self._prefs().default_mode)

    def action_launch_alternate(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        self._launch(
            session.id,
            session.display_name,
            alternate_for(self._prefs().default_mode),
        )

    def action_settings(self) -> None:
        self.app.push_screen(SettingsModal(self._prefs()), self._apply_settings)

    def _apply_settings(self, result: Config | None) -> None:
        if result is None:
            return
        self.app.update_prefs(result)  # type: ignore[attr-defined]
        self.notify("Ajustes guardados")

    def _prefs(self) -> Config:
        return self.app.prefs  # type: ignore[attr-defined,no-any-return]

    def _launch(
        self,
        session_id: str | None,
        display_name: str | None,
        mode: LaunchMode,
    ) -> None:
        try:
            launch_claude(
                self.project.path,
                session_id,
                display_name=display_name,
                app=self.app,
                mode=mode,
            )
        except LauncherError as exc:
            self.notify(str(exc), severity="error")

    def action_rename(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        self.app.push_screen(
            RenameModal(session.id, session.display_name),
            lambda result: self._apply_rename(session.id, result),
        )

    def _apply_rename(self, session_id: str, result: str | None) -> None:
        if result is None:
            return  # cancelled
        if result == "":
            self._names.delete(session_id)
            self.notify("Nombre borrado")
        else:
            self._names.set(session_id, result)
            self.notify(f"Renombrado: {result}")
        self._populate()

    def action_delete(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        active = list_active_sessions()
        warning = "Esta sesión está corriendo ahora mismo" if session.id in active else None
        modal = ConfirmDeleteModal(
            title=f"Borrar sesión {session.id[:8]}…",
            details=[
                f"Prompt: {(session.display_name or session.first_prompt)[:80]}",
                f"Mensajes: {session.message_count}  ·  Tamaño: {format_size(session.size_bytes)}",
                f"Última actividad: {format_relative_time(session.last_activity)}",
            ],
            warning=warning,
        )
        self.app.push_screen(modal, lambda ok: self._apply_delete(session, ok))

    def _apply_delete(self, session: Session, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            delete_session(session.id, self.project.encoded_path, names_store=self._names)
        except OSError as exc:
            self.notify(f"Error al borrar: {exc}", severity="error")
            return
        self.notify("Sesión borrada")
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
        self.query_one("#sessions", DataTable).focus()

    def action_back_or_clear(self) -> None:
        filter_input = self.query_one("#filter", Input)
        if filter_input.display:
            filter_input.value = ""
            filter_input.display = False
            self._repaint()
            return
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._populate()
        self.notify("Sesiones re-escaneadas")
