"""SearchScreen — full-text search across every indexed session, yours and the team's.

Two sources, one list. Local sessions are searched by their **content** (the FTS payload
built from the transcript); team-published ones only by what their manifest carries —
name, first prompt, tags, branch, author — because a manifest holds no transcript. The
column that names the source is what keeps that difference visible instead of implied.

Nothing here touches the network: a remote's rows are whatever the last visit to its tab
cached in the index.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input
from textual.widgets.data_table import RowKey

from multi_claude.discovery import Project, project_remote_key, scan_projects
from multi_claude.formatting import format_relative_time
from multi_claude.index import IndexedRemoteSession, IndexedSession, default_index

# Per source, so a project with hundreds of local hits cannot crowd the team out.
_LIMIT_PER_SOURCE = 200


def _split_file_term(raw: str) -> tuple[str | None, str]:
    """Pull a ``file:`` token out of a raw query, returning it and what is left.

    Only ``file:`` is lifted out, not every ``key:`` the listing's filter understands: the
    rest of the query goes to FTS5 verbatim, exactly as it did before this existed, so a
    session whose transcript literally says ``branch:main`` keeps being findable that way.
    The last ``file:`` wins if someone types two, which is what an input box that is
    re-searched on every keystroke tends to produce.
    """
    term: str | None = None
    rest: list[str] = []
    for token in raw.split():
        head, sep, value = token.partition(":")
        if sep and head.lower() == "file" and value:
            term = value
            continue
        rest.append(token)
    return term, " ".join(rest)


@dataclass(frozen=True)
class _Hit:
    """One painted row: either a local session or a team-published one."""

    local: IndexedSession | None = None
    remote: IndexedRemoteSession | None = None


class SearchScreen(Screen[None]):
    """Type a query, see matching sessions across all projects. Enter to drill in."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[_Hit] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(
            placeholder="busca en todas las sesiones · file:x.py por fichero", id="fts-query"
        )
        yield DataTable(id="fts-results", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "búsqueda global (FTS5)"
        table = self.query_one("#fts-results", DataTable)
        table.add_columns("Sesión", "Dónde", "Proyecto", "Branch", "Última")
        self.query_one("#fts-query", Input).focus()

    @on(Input.Changed, "#fts-query")
    def _on_query_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if not query:
            self._results = []
            self._repaint()
            return
        self._search_worker(query)

    @work(thread=True, exclusive=True, group="fts-search")
    def _search_worker(self, query: str) -> None:
        index = default_index()
        wanted_file, rest = _split_file_term(query)
        if wanted_file is None:
            local = index.fts_search(query, limit=_LIMIT_PER_SOURCE)
            remote = index.fts_search_remote(query, limit=_LIMIT_PER_SOURCE)
            self.app.call_from_thread(self._on_search_complete, local, remote)
            return

        # `file:` is a different question from the FTS one — which conversations edited
        # this file — so it is answered from ``session_files`` and then narrowed by the
        # text, rather than folded into the MATCH. Ordering follows the files table
        # (most recent first): FTS rank means nothing for rows that never matched it.
        local = index.sessions_touching(wanted_file, limit=_LIMIT_PER_SOURCE)
        if rest:
            matching = {s.session_id for s in index.fts_search(rest, limit=_LIMIT_PER_SOURCE)}
            local = [s for s in local if s.session_id in matching]
        # No team rows: a manifest does not carry the files its session edited, so the
        # honest answer is none rather than a half-list that looks like the whole one.
        self.app.call_from_thread(self._on_search_complete, local, [])

    def _on_search_complete(
        self, local: list[IndexedSession], remote: list[IndexedRemoteSession]
    ) -> None:
        # Ranks from two FTS tables are not comparable, so they are not interleaved:
        # each source keeps its own relevance order, yours first.
        self._results = [_Hit(local=s) for s in local] + [_Hit(remote=r) for r in remote]
        self._repaint()
        if remote:
            self.sub_title = f"búsqueda global · {len(local)} tuyas · {len(remote)} del equipo"
        else:
            self.sub_title = "búsqueda global (FTS5)"

    def _repaint(self) -> None:
        table = self.query_one("#fts-results", DataTable)
        table.clear()
        for idx, hit in enumerate(self._results):
            table.add_row(*self._cells(hit), key=str(idx))

    def _cells(self, hit: _Hit) -> tuple[str, str, str, str, str]:
        if hit.local is not None:
            session = hit.local
            return (
                (session.embedded_name or session.first_prompt or session.session_id)[:80],
                "local",
                _project_label(session),
                session.branch or "—",
                format_relative_time(session.mtime),
            )
        remote = hit.remote
        assert remote is not None  # a hit is one or the other
        who = (remote.published_by or "?").split("@")[0]
        return (
            remote.title[:80],
            f"☁ {who}",
            remote.remote_label,
            remote.branch or "—",
            _published_ago(remote.published_at),
        )

    @on(Input.Submitted, "#fts-query")
    def _on_query_submitted(self, event: Input.Submitted) -> None:
        table = self.query_one("#fts-results", DataTable)
        if self._results:
            table.focus()

    @on(DataTable.RowSelected, "#fts-results")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        hit = self._result_for_row(event.row_key)
        if hit is None:
            return
        if hit.local is not None:
            project = self._project_for_session(hit.local)
            if project is None:
                self.notify("No encuentro el proyecto correspondiente", severity="warning")
                return
            self._open(project)
            return

        remote = hit.remote
        assert remote is not None
        project = self._project_for_remote(remote)
        if project is None:
            self.notify(
                f"«{remote.remote_label}» no está enlazado a ningún proyecto de esta máquina",
                severity="warning",
            )
            return
        # Land on the tab that holds it, not just the project: the row the user picked
        # is in a remote listing, and finding it again by hand defeats the search.
        self._open(project, activate_remote=remote.remote_key)

    def _open(self, project: Project, *, activate_remote: str | None = None) -> None:
        from multi_claude.screens.sessions import SessionsScreen

        self.app.pop_screen()  # back to ProjectsScreen
        self.app.push_screen(SessionsScreen(project, activate_remote=activate_remote))

    def _result_for_row(self, row_key: RowKey) -> _Hit | None:
        if row_key.value is None:
            return None
        idx = int(row_key.value)
        if idx >= len(self._results):
            return None
        return self._results[idx]

    def _project_for_session(self, session: IndexedSession) -> Project | None:
        target = Path(session.project_dir)
        for project in scan_projects():
            if project.encoded_path == target:
                return project
        return None

    def _project_for_remote(self, remote: IndexedRemoteSession) -> Project | None:
        """The local project linked to this remote, by the key the links are stored under."""
        if not remote.project_key:
            return None
        for project in scan_projects():
            if project.is_orphan:
                continue
            if project_remote_key(project.path) == remote.project_key:
                return project
        return None

    def action_back(self) -> None:
        self.app.pop_screen()


def _project_label(session: IndexedSession) -> str:
    """The project's name, the way the projects screen shows it.

    The indexed ``project_dir`` is Claude's encoded directory (``-home-me-tienda-api``),
    which is both ugly and long in a column. The recorded ``cwd`` is the real path, so its
    basename is the name the user knows; the encoded directory is only the fallback for a
    session whose first event carried no cwd.
    """
    if session.cwd:
        name = Path(session.cwd).name
        if name:
            return name
    return Path(session.project_dir).name or session.project_dir


def _published_ago(published_at: str | None) -> str:
    """A manifest timestamp as a relative age, or '—' when it is missing or unparseable."""
    if not published_at:
        return "—"
    from datetime import datetime

    try:
        stamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return format_relative_time(stamp.timestamp())
