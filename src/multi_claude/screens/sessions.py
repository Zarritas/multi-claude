"""SessionsScreen — list of sessions inside one project."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Tab, Tabs

from multi_claude.app_protocol import AppProtocol
from multi_claude.clipboard import ClipboardError, copy_to_clipboard
from multi_claude.colors import ColorRule, resolve_style
from multi_claude.config import Config, LaunchMode, SortSpec, alternate_for
from multi_claude.deletion import (
    SessionActiveError,
    SessionCollisionError,
    delete_session,
    list_active_sessions,
    move_session,
)
from multi_claude.discovery import (
    Project,
    project_remote_key,
    resolve_git_head,
    resolve_git_remote,
    resolve_git_user_email,
    scan_projects,
)
from multi_claude.filtering import FilterQuery, matches_fuzzy, parse_query
from multi_claude.focus import find_live_session, focus_terminal
from multi_claude.formatting import format_relative_time, format_size
from multi_claude.launcher import PLACEMENT_LABELS, LauncherError, launch_claude
from multi_claude.modals import (
    CleanupModal,
    ColorPickerModal,
    ColorRulesEditorModal,
    ConfirmDeleteModal,
    FilePathModal,
    MoveSessionModal,
    ProjectRemotesModal,
    PublishModal,
    RenameModal,
    SettingsModal,
    TagEditorModal,
)
from multi_claude.project_remotes import RemoteLink
from multi_claude.remote import (
    RemoteError,
    RemoteSession,
    collect_session_files,
    session_to_remote,
)
from multi_claude.session import Session, scan_sessions
from multi_claude.transfer import export_sessions, safe_filename
from multi_claude.widgets.preview import SessionPreview

# How many file paths the publish confirmation lists before collapsing the rest into a
# count. Enough to spot a stray tool-results dump, short enough to stay on screen.
_PUBLISH_PREVIEW_LIMIT = 12

# How a published session's row is marked, by how the local copy compares to it. The note is
# spelled out because a glyph alone does not say which side is behind.
_STATE_MARKS: dict[str, tuple[str, str, str]] = {
    "absent": ("☁ ", "bold blue", ""),
    "current": ("✓ ", "bold green", "(descargada)"),
    "stale": ("↻ ", "bold yellow", "(descargada · hay versión más reciente)"),
    "ahead": ("↑ ", "bold magenta", "(descargada · tienes cambios sin publicar)"),
}

# Tab ids. The local listing is always tab 0; each linked sessions repo follows in link order.
_LOCAL_TAB_ID = "tab-local"
_REMOTE_TAB_PREFIX = "tab-remote-"

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
        Binding("space", "toggle_mark", "Marcar"),
        Binding("m", "move", "Mover"),
        Binding("x", "export", "Exportar"),
        Binding("u", "publish", "Publicar"),
        Binding("L", "link_remotes", "Repos sesiones"),
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
        # Which model each painted row came from: (is_remote, index into the matching
        # list). Remote rows are only ever appended, so a local action can never land on
        # one by accident — ``_selected_session`` returns None for them.
        self._rows: list[tuple[bool, int]] = []
        self._active_session_ids: set[str] = set()
        self._marked: set[str] = set()
        self._remote_sessions: list[RemoteSession] = []
        self._remote_links: tuple[RemoteLink, ...] = ()
        # session_id -> (manifest, etiqueta del remoto donde está). Poblado en segundo plano;
        # es lo que permite marcar en la pestaña local qué sesiones están compartidas.
        self._published: dict[str, tuple[RemoteSession, str]] = {}
        self._own_email: str | None = None
        # None while the local tab is selected; otherwise the index into ``_remote_links``.
        self._active_remote: int | None = None

    @property
    def _claude_app(self) -> AppProtocol:
        return cast(AppProtocol, self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        # Built here rather than after mount: the tab bar is part of the initial layout, and
        # populating it from a worker made the screen briefly incomplete.
        self._remote_links = self._claude_app.remote_links_for(self.project)
        yield Tabs(
            Tab("Locales", id=_LOCAL_TAB_ID),
            *(
                Tab(f"☁ {link.tab_label()}", id=f"{_REMOTE_TAB_PREFIX}{index}")
                for index, link in enumerate(self._remote_links)
            ),
            id="session-tabs",
        )
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
        self.query_one("#session-tabs", Tabs).display = bool(self._remote_links)
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
        self._marked &= {s.id for s in sessions}
        self._active_session_ids = list_active_sessions()
        self.sub_title = f"{self.project.name} — {self.project.path}"
        self._apply_sort()
        # Remote rows survive a local rescan; what changes is their ✓/☁ mark, which
        # ``_paint_remote_rows`` derives from disk on every repaint.
        self._repaint()
        if self._remote_links:
            self._load_published_index_worker()

    def _apply_sort(self) -> None:
        spec = self._claude_app.prefs.sessions_sort
        self._sessions.sort(key=_session_sort_value(spec.key), reverse=spec.descending)

    def _repaint(self) -> None:
        from rich.text import Text

        if self._active_remote is not None:
            self._repaint_remote()
            return
        table = self.query_one("#sessions", DataTable)
        table.clear()
        raw_query = self.query_one("#filter", Input).value
        query = parse_query(raw_query)
        self._rows = []
        rules = self._claude_app.prefs.color_rules
        manual = self._claude_app.session_colors
        for idx, session in enumerate(self._sessions):
            if not query.is_empty and not self._matches(session, query):
                continue
            is_active = session.id in self._active_session_ids
            style = resolve_style(session, manual=manual, rules=rules, is_active=is_active)
            label = session.display_name or session.first_prompt
            glyph, glyph_style, note = self._shared_mark(session)
            label_cell: Text | str
            if session.id in self._marked or glyph:
                cell = Text()
                if session.id in self._marked:
                    cell.append("● ", style="bold green")
                if glyph:
                    cell.append(glyph, style=f"bold {glyph_style}")
                cell.append(label, style=style or "")
                if note:
                    cell.append(f"  ({note})", style="dim")
                label_cell = cell
            else:
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
            self._rows.append((False, idx))
        filter_input = self.query_one("#filter", Input)
        if self._rows and not filter_input.has_focus:
            table.focus()

    async def _refresh_remote_tabs(self) -> None:
        """Rebuild the tab bar after the user changes which repos are linked.

        The initial bar is built in ``compose``; this is only for changes made from the link
        modal. Hidden entirely when nothing is linked: a lone "Locales" tab would be chrome
        that explains nothing. Selection returns to local, since the old tabs are gone.

        Async because ``Tabs.clear`` and ``add_tab`` complete on later frames; adding before
        the clear lands raises DuplicateIds.
        """
        self._remote_links = self._claude_app.remote_links_for(self.project)
        self._active_remote = None
        self._remote_sessions = []
        tabs = self.query_one("#session-tabs", Tabs)
        await tabs.clear()
        await tabs.add_tab(Tab("Locales", id=_LOCAL_TAB_ID))
        for index, link in enumerate(self._remote_links):
            await tabs.add_tab(
                Tab(f"☁ {link.tab_label()}", id=f"{_REMOTE_TAB_PREFIX}{index}")
            )
        tabs.display = bool(self._remote_links)

    def _repaint_remote(self) -> None:
        """Paint the selected remote's sessions in place of the local ones."""
        table = self.query_one("#sessions", DataTable)
        table.clear()
        query = parse_query(self.query_one("#filter", Input).value)
        self._rows = []
        self._paint_remote_rows(table, query)
        filter_input = self.query_one("#filter", Input)
        if self._rows and not filter_input.has_focus:
            table.focus()

    def _paint_remote_rows(self, table: DataTable[Any], query: FilterQuery) -> None:
        """Paint everything published to this remote, marking what is already here.

        Sessions you already have are listed rather than hidden: the tab is a view of the
        repo, and seeing your own session in it is how you confirm a publish worked. They are
        marked ``✓`` instead of ``☁``, and Enter resumes them straight from disk.
        """
        from rich.text import Text

        for idx, remote in enumerate(self._remote_sessions):
            if not query.is_empty and not _remote_matches(remote, query):
                continue
            state = self._local_state(remote)
            glyph, glyph_style, note = _STATE_MARKS[state]
            label = Text()
            label.append(glyph, style=glyph_style)
            who = (remote.published_by or "?").split("@")[0]
            label.append(f"{who} · ", style="dim")
            label.append(remote.display_name or remote.first_prompt or remote.session_id)
            if note:
                label.append(f"  {note}", style="dim")
            table.add_row(
                label,
                remote.branch or "—",
                self._format_tags_cell(remote.tags),
                str(remote.message_count),
                format_size(remote.size_bytes),
                _format_published(remote.published_at),
                key=f"remote-{idx}",
            )
            self._rows.append((True, idx))

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
        remote = self._selected_remote()
        if remote is not None:
            self._hydrate_and_launch(remote, self._prefs().default_mode)
            return
        session = self._selected_session()
        if session is None:
            return
        self._launch(session.id, session.display_name, self._prefs().default_mode)

    def _current_row(self) -> tuple[bool, int] | None:
        table = self.query_one("#sessions", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def _selected_session(self) -> Session | None:
        """The local session under the cursor, or None (including on a remote row)."""
        current = self._current_row()
        if current is None or current[0]:
            return None
        return self._sessions[current[1]]

    def _selected_remote(self) -> RemoteSession | None:
        """The published session under the cursor, or None."""
        current = self._current_row()
        if current is None or not current[0]:
            return None
        return self._remote_sessions[current[1]]

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
        # Never open two terminals on the same session: if it's already live,
        # bring its terminal to the foreground (best-effort) instead of resuming
        # a duplicate, which would have two processes writing the same jsonl.
        if session_id is not None:
            live = find_live_session(session_id)
            if live is not None:
                if focus_terminal(live.pid):
                    self.notify("Sesión ya abierta — terminal traída al frente")
                else:
                    self.notify(
                        "Esta sesión ya está abierta en otra terminal "
                        f"(pid {live.pid}); no se abre duplicado",
                        severity="warning",
                    )
                return
        try:
            outcome = launch_claude(
                self.project.path,
                session_id,
                display_name=display_name,
                app=self.app,
                mode=mode,
                claude_args=self._prefs().claude_args,
            )
        except LauncherError as exc:
            self.notify(str(exc), severity="error")
            return
        if outcome.fallback_reason is not None:
            self.notify(
                f"{PLACEMENT_LABELS.get(outcome.placement, outcome.placement)} "
                f"({outcome.target}) — {outcome.fallback_reason}",
                severity="warning",
            )

    def action_toggle_mark(self) -> None:
        session = self._selected_session()
        if session is None:
            return
        table = self.query_one("#sessions", DataTable)
        row = table.cursor_row
        if session.id in self._marked:
            self._marked.discard(session.id)
        else:
            self._marked.add(session.id)
        self._repaint()
        if row is not None and 0 <= row < len(self._rows):
            table.move_cursor(row=row)

    def _selected_sessions(self) -> list[Session]:
        """Bulk-action targets: the marked set, or the current row if none marked."""
        if self._marked:
            return [s for s in self._sessions if s.id in self._marked]
        current = self._selected_session()
        return [current] if current is not None else []

    def action_move(self) -> None:
        targets = self._selected_sessions()
        if not targets:
            self.notify("Selecciona sesión(es) para mover", severity="warning")
            return
        if self.project.git_common_dir is None:
            self.notify(
                "Esta sesión no pertenece a un grupo de worktrees", severity="warning"
            )
            return
        self._gather_move_destinations(targets)

    @work(thread=True, exclusive=True, group="move-destinations")
    def _gather_move_destinations(self, targets: list[Session]) -> None:
        common = self.project.git_common_dir
        candidates = [
            p
            for p in scan_projects()
            if p.git_common_dir == common
            and p.encoded_path != self.project.encoded_path
            and not p.is_orphan
        ]
        self.app.call_from_thread(self._prompt_move, targets, candidates)

    def _prompt_move(self, targets: list[Session], candidates: list[Project]) -> None:
        if not candidates:
            self.notify(
                "No hay otros worktrees del mismo repo a los que mover",
                severity="warning",
            )
            return
        self.app.push_screen(
            MoveSessionModal(len(targets), candidates),
            lambda dest: self._apply_move(targets, dest),
        )

    def _apply_move(self, targets: list[Session], destination: Project | None) -> None:
        if destination is None:
            return  # cancelled
        moved = 0
        blocked = 0
        failed = 0
        for session in targets:
            try:
                move_session(
                    session.id,
                    self.project.encoded_path,
                    destination.encoded_path,
                )
                moved += 1
            except SessionActiveError:
                blocked += 1
            except (SessionCollisionError, OSError):
                failed += 1
        self._marked.clear()
        parts = []
        if moved:
            parts.append(f"Movidas {moved} a {destination.name}")
        if blocked:
            parts.append(f"{blocked} activa(s) omitida(s)")
        if failed:
            parts.append(f"{failed} con conflicto/error")
        self.notify(
            " · ".join(parts) or "Nada que mover",
            severity="warning" if (blocked or failed) else "information",
        )
        self._populate()

    def action_export(self) -> None:
        targets = self._selected_sessions()
        if not targets:
            self.notify("Selecciona sesión(es) para exportar", severity="warning")
            return
        default = self._default_export_path(targets)
        self.app.push_screen(
            FilePathModal(
                title=f"Exportar {len(targets)} sesión(es) a un .zip",
                mode="save",
                default=str(default),
            ),
            lambda path: self._apply_export(targets, path),
        )

    def _default_export_path(self, targets: list[Session]) -> Path:
        base = Path.home() / "Downloads"
        if not base.is_dir():
            base = Path.home()
        if len(targets) == 1:
            stem = safe_filename(
                targets[0].display_name or targets[0].first_prompt or targets[0].id
            )
            return base / f"{stem}.claude-session.zip"
        return base / f"claude-sessions-{len(targets)}.zip"

    def _apply_export(self, targets: list[Session], path: Path | None) -> None:
        if path is None:
            return  # cancelled
        try:
            count = export_sessions(targets, self.project.encoded_path, path)
        except OSError as exc:
            self.notify(f"Error al exportar: {exc}", severity="error")
            return
        if count == 0:
            self.notify("No se exportó nada (ficheros no encontrados)", severity="warning")
            return
        self._marked.clear()
        self._repaint()
        self.notify(f"Exportadas {count} sesión(es) → {path}")

    # --- shared sessions ------------------------------------------------------------

    @work(thread=True, exclusive=True, group="published-index")
    def _load_published_index_worker(self) -> None:
        """Ask every linked remote what it has, so local rows can show if they are shared.

        Runs in the background and repaints when done: the local listing must not wait on the
        network to appear. Failures are silent — not knowing whether a session is published is
        a missing mark, not an error worth interrupting the user for.
        """
        index: dict[str, tuple[RemoteSession, str]] = {}
        for link in self._remote_links:
            store = self._claude_app.store_for_link(link)
            if store is None:
                continue
            try:
                listed = store.list_sessions()
            except (RemoteError, OSError):
                continue
            for remote in listed:
                # First remote wins when a session is published to several: the mark says
                # "shared", and naming one of them is enough for that.
                index.setdefault(remote.session_id, (remote, link.tab_label()))
        email = resolve_git_user_email(self.project.path)
        self.app.call_from_thread(self._on_published_index, index, email)

    def _on_published_index(
        self, index: dict[str, tuple[RemoteSession, str]], email: str | None
    ) -> None:
        self._published = index
        self._own_email = email
        self._repaint()

    def _shared_mark(self, session: Session) -> tuple[str, str, str]:
        """Glyph, style and note for a local row, from what the remotes report.

        Same vocabulary as the remote tabs, so ``↻`` means the same thing on both sides.
        """
        entry = self._published.get(session.id)
        if entry is None:
            return ("", "", "")
        remote, where = entry
        if not remote.size_bytes or session.size_bytes == remote.size_bytes:
            glyph, style, note = "✓ ", "green", f"publicada en {where}"
        elif remote.size_bytes > session.size_bytes:
            glyph, style, note = "↻ ", "yellow", f"{where} tiene una versión más reciente"
        else:
            glyph, style, note = "↑ ", "magenta", f"con cambios sin publicar en {where}"
        author = remote.published_by
        if author and self._own_email and author != self._own_email:
            note = f"{note} · de {author.split('@')[0]}"
        return (glyph, style, note)

    def _is_local(self, session_id: str) -> bool:
        """Whether this session already exists in this project's dir on disk."""
        return (self.project.encoded_path / f"{session_id}.jsonl").exists()

    def _local_state(self, remote: RemoteSession) -> str:
        """How the copy on disk compares to what is published: the row's indicator.

        The jsonl only ever grows, so comparing its size against the ``size_bytes`` recorded
        in the manifest is enough to tell the three cases apart — and all three matter:
        a stale copy means someone continued the session after you fetched it, and an ahead
        copy means you have work nobody else can see yet.

        ``absent`` not downloaded · ``current`` same as published ·
        ``stale`` published is longer · ``ahead`` local is longer
        """
        jsonl = self.project.encoded_path / f"{remote.session_id}.jsonl"
        try:
            local_size = jsonl.stat().st_size
        except OSError:
            return "absent"
        if not remote.size_bytes or local_size == remote.size_bytes:
            return "current"
        return "stale" if remote.size_bytes > local_size else "ahead"

    def _active_link(self) -> RemoteLink | None:
        """The remote whose tab is selected, or None while the local tab is."""
        if self._active_remote is None:
            return None
        if self._active_remote >= len(self._remote_links):
            return None
        return self._remote_links[self._active_remote]

    def _default_destination(self) -> int:
        """Which linked remote the publish dialogue starts on.

        The active tab, when you are on one: publishing from a repo's tab almost always means
        that repo. Otherwise the first, and the dialogue lets you change it.
        """
        return self._active_remote if self._active_remote is not None else 0

    def _notify_error(self, message: str) -> None:
        """Named so worker threads can post an error via ``call_from_thread``.

        ``notify``'s severity is keyword-only, which ``call_from_thread`` cannot pass.
        """
        self.notify(message, severity="error")

    def action_publish(self) -> None:
        if not self._remote_links:
            self.notify(
                "Este proyecto no tiene repositorio de sesiones enlazado (pulsa L)",
                severity="warning",
            )
            return
        targets = self._selected_sessions()
        if not targets:
            self.notify("Selecciona sesión(es) para publicar", severity="warning")
            return
        files = [
            path
            for session in targets
            for path in collect_session_files(self.project.encoded_path, session.id)
        ]
        if not files:
            self.notify("Nada que publicar (ficheros no encontrados)", severity="warning")
            return
        # Show exactly what leaves the machine. tool-results hold raw command output, so
        # a session that once printed a .env would publish it — the user has to be able
        # to see that before confirming, not after.
        listed = [
            f"· {path.relative_to(self.project.encoded_path)}"
            for path in files[:_PUBLISH_PREVIEW_LIMIT]
        ]
        if len(files) > _PUBLISH_PREVIEW_LIMIT:
            listed.append(f"… y {len(files) - _PUBLISH_PREVIEW_LIMIT} más")
        modal = PublishModal(
            session_count=len(targets),
            files=listed,
            destinations=list(self._remote_links),
            preselected=self._default_destination(),
        )
        self.app.push_screen(modal, lambda link: self._apply_publish(targets, link))

    def _apply_publish(self, targets: list[Session], link: RemoteLink | None) -> None:
        if link is None:
            return  # cancelled
        if self._claude_app.store_for_link(link) is None:
            self.notify(f"«{link.tab_label()}» está mal configurado", severity="warning")
            return
        self.notify(f"Publicando {len(targets)} sesión(es) en «{link.tab_label()}»…")
        self._publish_worker(targets, link)

    @work(thread=True, exclusive=True, group="publish-sessions")
    def _publish_worker(self, targets: list[Session], link: RemoteLink) -> None:
        store = self._claude_app.store_for_link(link)
        if store is None:
            return
        published_by = resolve_git_user_email(self.project.path)
        git_remote = resolve_git_remote(self.project.path)
        git_head = resolve_git_head(self.project.path)
        done = 0
        errors: list[str] = []
        for session in targets:
            meta = session_to_remote(
                session,
                published_by=published_by,
                git_remote=git_remote,
                git_head=git_head,
            )
            try:
                store.publish(meta, self.project.encoded_path)
                done += 1
            except (RemoteError, OSError) as exc:
                errors.append(f"{session.id[:8]}: {exc}")
        self.app.call_from_thread(self._on_publish_complete, done, errors)

    def _on_publish_complete(self, done: int, errors: list[str]) -> None:
        self._marked.clear()
        if errors:
            self.notify(
                f"Publicadas {done}; {len(errors)} con error — {errors[0]}",
                severity="warning",
            )
        else:
            self.notify(f"Publicadas {done} sesión(es)")
        if self._active_remote is not None:
            self._load_remote_worker(self._active_remote)
        else:
            self._repaint()
        # What we just uploaded has to show as published on the local tab too.
        self._load_published_index_worker()

    @on(Tabs.TabActivated)
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Switch between the local listing and one remote's listing."""
        tab_id = event.tab.id or ""
        if tab_id == _LOCAL_TAB_ID:
            self._active_remote = None
            self._remote_sessions = []
            self._repaint()
            return
        if not tab_id.startswith(_REMOTE_TAB_PREFIX):
            return
        index = int(tab_id[len(_REMOTE_TAB_PREFIX) :])
        if index >= len(self._remote_links):
            return
        self._active_remote = index
        self._remote_sessions = []
        self._repaint()
        self.notify(f"Cargando «{self._remote_links[index].tab_label()}»…")
        self._load_remote_worker(index)

    @work(thread=True, exclusive=True, group="load-remote")
    def _load_remote_worker(self, index: int) -> None:
        if index >= len(self._remote_links):
            return
        link = self._remote_links[index]
        store = self._claude_app.store_for_link(link)
        if store is None:
            self.app.call_from_thread(
                self._on_remote_failed, index, f"«{link.tab_label()}» está mal configurado"
            )
            return
        try:
            listed = store.list_sessions()
        except (RemoteError, OSError) as exc:
            self.app.call_from_thread(self._on_remote_failed, index, str(exc))
            return
        self.app.call_from_thread(self._on_remote_loaded, index, list(listed))

    def _on_remote_loaded(self, index: int, listed: list[RemoteSession]) -> None:
        # A late reply from a tab the user already left must not overwrite what is on screen.
        if index != self._active_remote:
            return
        self._remote_sessions = listed
        self._repaint()
        states = [self._local_state(r) for r in listed]
        parts = [f"{len(listed)} publicada(s)"]
        downloaded = sum(1 for s in states if s != "absent")
        if downloaded:
            parts.append(f"{downloaded} descargada(s)")
        stale = states.count("stale")
        if stale:
            parts.append(f"{stale} con versión más reciente")
        self.notify(" · ".join(parts))

    def _on_remote_failed(self, index: int, message: str) -> None:
        if index != self._active_remote:
            return
        self._remote_sessions = []
        self._repaint()
        self.notify(f"No se pudo leer el remoto: {message}", severity="error")

    def _hydrate_and_launch(self, remote: RemoteSession, mode: LaunchMode) -> None:
        link = self._active_link()
        if link is None:
            return
        state = self._local_state(remote)
        if state != "absent":
            # Already on disk (we published it, or fetched it earlier). Fetching would refuse
            # to overwrite, so resume the local copy — which is the same session.
            if state == "stale":
                self.notify(
                    "Tu copia es anterior a la publicada; se reanuda la local "
                    "(traer los turnos nuevos aún no está implementado)",
                    severity="warning",
                )
            self._launch(remote.session_id, remote.display_name, mode)
            return
        self.notify(f"Trayendo {remote.session_id[:8]}…")
        self._hydrate_worker(remote, link, mode)

    @work(thread=True, exclusive=True, group="hydrate-session")
    def _hydrate_worker(
        self, remote: RemoteSession, link: RemoteLink, mode: LaunchMode
    ) -> None:
        store = self._claude_app.store_for_link(link)
        if store is None:
            return
        try:
            store.fetch(remote.session_id, self.project.encoded_path)
        except (RemoteError, OSError) as exc:
            self.app.call_from_thread(self._notify_error, f"No se pudo traer la sesión: {exc}")
            return
        local_head = resolve_git_head(self.project.path)
        self.app.call_from_thread(self._on_hydrated, remote, mode, local_head)

    def _on_hydrated(
        self, remote: RemoteSession, mode: LaunchMode, local_head: str | None
    ) -> None:
        # The transcript travels, the repository does not: warn when the conversation
        # was recorded against different code, then launch anyway — it is the user's
        # call whether that matters.
        if remote.git_head and local_head and remote.git_head != local_head:
            self.notify(
                f"Grabada sobre {remote.git_head}, estás en {local_head} — "
                "la conversación puede referirse a otro estado del código",
                severity="warning",
            )
        self._launch(remote.session_id, remote.display_name, mode)
        self._populate()

    def action_link_remotes(self) -> None:
        """Manage which sessions repos this project publishes to."""
        self.app.push_screen(
            ProjectRemotesModal(
                project_name=self.project.name,
                links=list(self._remote_links),
                inherited=not self._claude_app.project_remotes.get(self._remote_key()),
                servers=list(self._claude_app.prefs.remote_servers),
            ),
            self._apply_links,
        )

    def _remote_key(self) -> str:
        return project_remote_key(self.project.path)

    def _apply_links(self, links: list[RemoteLink] | None) -> None:
        if links is None:
            return  # cancelled
        self._claude_app.project_remotes.set_all(self._remote_key(), links)
        self.run_worker(self._refresh_remote_tabs(), exclusive=False)
        self.notify(
            f"{len(links)} repositorio(s) de sesiones enlazado(s)"
            if links
            else "Enlaces borrados (se usa el remoto global)"
        )


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
        new_prefs = replace(prefs, preview_visible=not prefs.preview_visible)
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
        new_prefs = replace(self._claude_app.prefs, color_rules=result)
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
            "toggle_mark",
        }
        # A remote row has no local jsonl yet, so every local action is hidden on it —
        # ``_selected_session`` already returns None there, which handles the set above.
        if action in row_dependent and self._selected_session() is None:
            return False
        if action in ("move", "export", "publish") and not self._selected_sessions():
            return False
        if action == "publish" and not self._remote_links:
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
        new_prefs = replace(self._claude_app.prefs, sessions_sort=new_spec)
        self._claude_app.update_prefs(new_prefs)
        self._apply_sort()
        self._repaint()
        self.notify(f"Orden: {key} {'desc' if new_spec.descending else 'asc'}")

    def action_toggle_sort_direction(self) -> None:
        spec = self._claude_app.prefs.sessions_sort
        self.action_sort_column(spec.key)


def _remote_matches(remote: RemoteSession, query: FilterQuery) -> bool:
    """Same filter semantics as local rows, over the fields a manifest carries.

    ``id:`` matters most here: it is how you land on a session a colleague pasted into
    a chat, since you have its uuid but nothing else.
    """
    for key, value in query.constraints.items():
        if key == "branch" and value not in (remote.branch or "").lower():
            return False
        if key == "path" and value not in (remote.cwd or "").lower():
            return False
        if key == "id" and value not in remote.session_id.lower():
            return False
        if key == "tag":
            needed = [t for t in (s.strip() for s in value.split(",")) if t]
            tags_lower = [t.lower() for t in remote.tags]
            if not all(any(n in t for t in tags_lower) for n in needed):
                return False
    haystack = " ".join(
        filter(
            None,
            [
                remote.display_name or "",
                remote.first_prompt or "",
                remote.branch or "",
                remote.published_by or "",
                " ".join(remote.tags),
            ],
        )
    )
    return matches_fuzzy(haystack, query.free_text)


def _format_published(published_at: str) -> str:
    """Render a manifest's ISO timestamp like the local rows' relative times."""
    from datetime import datetime

    try:
        return format_relative_time(datetime.fromisoformat(published_at).timestamp())
    except (TypeError, ValueError):
        return "—"


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
