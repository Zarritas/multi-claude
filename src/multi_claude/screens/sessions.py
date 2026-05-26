"""SessionsScreen — list of sessions inside one project."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input

from multi_claude.app_protocol import AppProtocol
from multi_claude.clipboard import ClipboardError, copy_to_clipboard
from multi_claude.colors import ColorRule, resolve_style
from multi_claude.config import Config, LaunchMode, SortSpec, alternate_for
from multi_claude.deletion import delete_session, list_active_sessions
from multi_claude.discovery import Project
from multi_claude.filtering import FilterQuery, matches_fuzzy, parse_query
from multi_claude.formatting import format_relative_time, format_size
from multi_claude.launcher import LauncherError, launch_claude
from multi_claude.modals import (
    CleanupModal,
    ColorPickerModal,
    ColorRulesEditorModal,
    ConfirmDeleteModal,
    RenameModal,
    SettingsModal,
    TagEditorModal,
)
from multi_claude.session import Session, scan_sessions
from multi_claude.widgets.preview import SessionPreview

_SORT_KEYS_BY_COLUMN: tuple[str, ...] = (
    "prompt",
    "branch",
    "tags",
    "messages",
    "size",
    "last_activity",
)


class SessionsScreen(Screen[None]):
    """DataTable of sessions for a single project, sorted by last_activity desc."""

    BINDINGS = [
        Binding("n", "new_session", "New"),
        Binding("shift+enter", "launch_alternate", "Launch alt"),
        Binding("e", "rename", "Rename"),
        Binding("t", "edit_tags", "Etiquetas"),
        Binding("c", "set_color", "Color"),
        Binding("C", "edit_color_rules", "Reglas color"),
        Binding("d", "delete", "Delete"),
        Binding("D", "cleanup", "Limpieza"),
        Binding("y", "yank_id", "Copiar id"),
        Binding("p", "toggle_preview", "Preview"),
        Binding("s", "settings", "Settings"),
        Binding("slash", "show_filter", "Filter"),
        Binding("escape", "back_or_clear", "Back"),
        Binding("left", "back_or_clear", "Back", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("1", "sort_column('prompt')", "Sort prompt", show=False),
        Binding("2", "sort_column('branch')", "Sort branch", show=False),
        Binding("3", "sort_column('tags')", "Sort tags", show=False),
        Binding("4", "sort_column('messages')", "Sort msgs", show=False),
        Binding("5", "sort_column('size')", "Sort tamaño", show=False),
        Binding("6", "sort_column('last_activity')", "Sort última", show=False),
        Binding("shift+s", "toggle_sort_direction", "Sort dir"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self._sessions: list[Session] = []
        self._visible_indices: list[int] = []
        self._active_session_ids: set[str] = set()

    @property
    def _claude_app(self) -> AppProtocol:
        return cast(AppProtocol, self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="sessions-body"):
            yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
            yield SessionPreview(id="preview")
        filter_input = Input(placeholder="filtro (Esc cierra)", id="filter")
        filter_input.display = False
        yield filter_input
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.project.name} — {self.project.path}"
        table = self.query_one("#sessions", DataTable)
        table.add_columns("Prompt", "Branch", "Tags", "Msgs", "Tamaño", "Última")
        self._apply_preview_visibility()
        self._populate()

    def _apply_preview_visibility(self) -> None:
        preview = self.query_one("#preview", SessionPreview)
        preview.display = self._claude_app.prefs.preview_visible

    def _populate(self) -> None:
        self.sub_title = f"{self.project.name} — escaneando…"
        self._scan_sessions_worker()

    @work(thread=True, exclusive=True, group="scan-sessions")
    def _scan_sessions_worker(self) -> None:
        results = scan_sessions(
            self.project.encoded_path,
            names_store=self._claude_app.names,
            tags_store=self._claude_app.tags,
        )
        self.app.call_from_thread(self._on_scan_complete, results)

    def _on_scan_complete(self, sessions: list[Session]) -> None:
        self._sessions = sessions
        self._active_session_ids = list_active_sessions()
        self.sub_title = f"{self.project.name} — {self.project.path}"
        self._apply_sort()
        self._repaint()

    def _apply_sort(self) -> None:
        spec = self._claude_app.prefs.sessions_sort
        self._sessions.sort(key=_session_sort_value(spec.key), reverse=spec.descending)

    def _repaint(self) -> None:
        from rich.text import Text

        table = self.query_one("#sessions", DataTable)
        table.clear()
        raw_query = self.query_one("#filter", Input).value
        query = parse_query(raw_query)
        self._visible_indices = []
        rules = self._claude_app.prefs.color_rules
        manual = self._claude_app.session_colors
        for idx, session in enumerate(self._sessions):
            if not query.is_empty and not self._matches(session, query):
                continue
            is_active = session.id in self._active_session_ids
            style = resolve_style(session, manual=manual, rules=rules, is_active=is_active)
            label = session.display_name or session.first_prompt
            label_cell = Text(label, style=style) if style else label
            tags_cell = self._format_tags_cell(session.tags)
            row = (
                label_cell,
                session.branch or "—",
                tags_cell,
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
    def _matches(session: Session, query: FilterQuery) -> bool:
        for key, value in query.constraints.items():
            if key == "branch" and value not in (session.branch or "").lower():
                return False
            if key == "path" and value not in (session.cwd or "").lower():
                return False
            if key == "id" and value not in session.id.lower():
                return False
            if key == "tag":
                needed = [t for t in (s.strip() for s in value.split(",")) if t]
                tags_lower = [t.lower() for t in session.tags]
                if not all(any(n in t for t in tags_lower) for n in needed):
                    return False
        haystack = " ".join(
            filter(
                None,
                [
                    session.display_name or "",
                    session.first_prompt or "",
                    session.branch or "",
                    " ".join(session.tags),
                ],
            )
        )
        return matches_fuzzy(haystack, query.free_text)

    @staticmethod
    def _format_tags_cell(tags: tuple[str, ...]) -> Any:
        if not tags:
            return "—"
        from rich.text import Text

        text = Text()
        for i, tag in enumerate(tags):
            if i:
                text.append(" ")
            text.append(f"#{tag}", style="bold cyan")
        return text

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_launch_default()

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self._claude_app.prefs.preview_visible:
            return
        session = self._selected_session()
        preview = self.query_one("#preview", SessionPreview)
        preview.show_session(session.path if session is not None else None)

    def action_launch_default(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        self._launch(session.id, session.display_name, self._prefs().default_mode)

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
        self._claude_app.update_prefs(result)
        self._apply_sort()
        self._repaint()
        self.notify("Ajustes guardados")

    def _prefs(self) -> Config:
        return self._claude_app.prefs

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

    def action_edit_tags(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        store = self._claude_app.tags
        subtitle = f"{(session.display_name or session.first_prompt)[:60]}"
        self.app.push_screen(
            TagEditorModal(
                subtitle=subtitle,
                current_tags=session.tags,
                known_tags=store.all_known_tags(),
            ),
            lambda result: self._apply_tags(session.id, result),
        )

    def _apply_tags(self, session_id: str, result: list[str] | None) -> None:
        if result is None:
            return  # cancelled
        store = self._claude_app.tags
        new_tags = store.set(session_id, result)
        if new_tags:
            self.notify(f"Etiquetas: {' '.join('#' + t for t in new_tags)}")
        else:
            self.notify("Etiquetas borradas")
        self._populate()

    def action_rename(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        self.app.push_screen(
            RenameModal(
                subtitle=f"id: {session.id}",
                current_name=session.display_name,
                title="Renombrar sesión",
            ),
            lambda result: self._apply_rename(session.id, result),
        )

    def _apply_rename(self, session_id: str, result: str | None) -> None:
        if result is None:
            return  # cancelled
        if result == "":
            self._claude_app.names.delete(session_id)
            self.notify("Nombre borrado")
        else:
            self._claude_app.names.set(session_id, result)
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
            delete_session(
                session.id,
                self.project.encoded_path,
                names_store=self._claude_app.names,
                tags_store=self._claude_app.tags,
                force=True,  # user already confirmed the warning in the modal
            )
        except OSError as exc:
            self.notify(f"Error al borrar: {exc}", severity="error")
            return
        self.notify("Sesión borrada")
        self._populate()

    def action_cleanup(self) -> None:
        if not self._sessions:
            self.notify("No hay sesiones que limpiar", severity="warning")
            return
        active_in_project = {s.id for s in self._sessions} & self._active_session_ids
        modal = CleanupModal(
            session_activities=[s.last_activity for s in self._sessions],
            active_count=len(active_in_project),
        )
        self.app.push_screen(modal, self._apply_cleanup)

    def _apply_cleanup(self, threshold: float | None) -> None:
        if threshold is None:
            return
        targets = [
            s
            for s in self._sessions
            if s.last_activity < threshold and s.id not in self._active_session_ids
        ]
        if not targets:
            self.notify("Nada que borrar (todo lo viejo está activo)", severity="warning")
            return
        deleted = 0
        errors = 0
        for session in targets:
            try:
                delete_session(
                    session.id,
                    self.project.encoded_path,
                    names_store=self._claude_app.names,
                    tags_store=self._claude_app.tags,
                    force=True,
                )
                deleted += 1
            except OSError:
                errors += 1
        if errors:
            self.notify(f"Borradas {deleted}, {errors} errores", severity="warning")
        else:
            self.notify(f"Borradas {deleted} sesión(es)")
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

    def action_toggle_preview(self) -> None:
        prefs = self._claude_app.prefs
        new_prefs = Config(
            default_mode=prefs.default_mode,
            projects_sort=prefs.projects_sort,
            sessions_sort=prefs.sessions_sort,
            preview_visible=not prefs.preview_visible,
            group_worktrees=prefs.group_worktrees,
        )
        self._claude_app.update_prefs(new_prefs)
        self._apply_preview_visibility()
        if new_prefs.preview_visible:
            session = self._selected_session()
            self.query_one("#preview", SessionPreview).show_session(
                session.path if session else None
            )
        self.notify(f"Preview {'visible' if new_prefs.preview_visible else 'oculto'}")

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
        self._repaint()
        self.notify(f"Reglas guardadas ({len(result)})")

    def action_set_color(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        store = self._claude_app.session_colors
        current = store.get(session.id)
        subtitle = f"{(session.display_name or session.first_prompt)[:60]}"
        self.app.push_screen(
            ColorPickerModal(subtitle=subtitle, current_style=current),
            lambda result: self._apply_color(session.id, result),
        )

    def _apply_color(self, session_id: str, result: str | None) -> None:
        if result is None:
            return  # cancelled
        store = self._claude_app.session_colors
        if result == "":
            store.delete(session_id)
            self.notify("Color borrado")
        else:
            store.set(session_id, result)
            self.notify("Color asignado")
        self._repaint()

    def action_yank_id(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        try:
            backend = copy_to_clipboard(session.id)
        except ClipboardError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"{session.id} copiado vía {backend}")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide row-dependent bindings when no row is selected; hide cleanup if empty."""
        row_dependent = {
            "rename",
            "delete",
            "launch_alternate",
            "yank_id",
            "set_color",
            "edit_tags",
        }
        if action in row_dependent and self._selected_session() is None:
            return False
        return not (action == "cleanup" and not self._sessions)

    def action_sort_column(self, key: str) -> None:
        if key not in _SORT_KEYS_BY_COLUMN:
            return
        spec = self._claude_app.prefs.sessions_sort
        if spec.key == key:
            new_spec = SortSpec(key=key, descending=not spec.descending)
        else:
            new_spec = SortSpec(key=key, descending=True)
        new_prefs = Config(
            default_mode=self._claude_app.prefs.default_mode,
            projects_sort=self._claude_app.prefs.projects_sort,
            sessions_sort=new_spec,
            preview_visible=self._claude_app.prefs.preview_visible,
            group_worktrees=self._claude_app.prefs.group_worktrees,
        )
        self._claude_app.update_prefs(new_prefs)
        self._apply_sort()
        self._repaint()
        self.notify(f"Orden: {key} {'desc' if new_spec.descending else 'asc'}")

    def action_toggle_sort_direction(self) -> None:
        spec = self._claude_app.prefs.sessions_sort
        self.action_sort_column(spec.key)


def _session_sort_value(key: str) -> Callable[[Session], Any]:
    """Return a key fn for ``list.sort`` using session field ``key``."""
    if key == "prompt":
        return lambda s: (s.display_name or s.first_prompt or "").casefold()
    if key == "branch":
        return lambda s: (s.branch or "").casefold()
    if key == "tags":
        # (no_tags_flag, joined_tags_casefolded) → tagged rows cluster together
        # alphabetically; untagged sessions fall at the bottom (asc) or top (desc).
        return lambda s: (not s.tags, " ".join(s.tags).casefold())
    if key == "messages":
        return lambda s: s.message_count
    if key == "size":
        return lambda s: s.size_bytes
    return lambda s: s.last_activity
