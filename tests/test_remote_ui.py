"""End-to-end TUI tests for publishing and hydrating shared sessions.

Drives the real screens through ``textual.pilot`` against a synthetic projects tree and
a directory-backed remote, so the wiring (bindings → worker → store → launch) is what is
under test, not a mock of it.
"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from multi_claude import discovery as discovery_module
from multi_claude.app import ClaudeBrowserApp
from multi_claude.index import default_index
from multi_claude.launcher import LaunchOutcome
from multi_claude.remote import REMOTE_DIR_ENV, DirectoryRemote, RemoteSession
from multi_claude.screens.sessions import SessionsScreen
from tests.conftest import write_session


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One project with two sessions, plus an empty remote wired via the env var."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    real = tmp_path / "repo"
    real.mkdir()
    write_session(
        projects_root / "-repo",
        session_id="ses-1",
        cwd=str(real),
        branch="main",
        first_prompt="arreglar el exporter",
        mtime=3000.0,
    )
    write_session(
        projects_root / "-repo",
        session_id="ses-2",
        cwd=str(real),
        branch="main",
        first_prompt="otra cosa",
        mtime=2000.0,
    )
    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", projects_root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(REMOTE_DIR_ENV, str(tmp_path / "remote"))
    return tmp_path


def _fake_token() -> str:
    """A GitHub-shaped token, assembled at runtime.

    Written literally it is a secret as far as GitHub's push protection is concerned,
    and it would block the push of the test that proves we detect it.
    """
    return "ghp" + "_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456"


async def _open_sessions(pilot: object) -> SessionsScreen:
    """Navigate ProjectsScreen → SessionsScreen and return it.

    Waits for the push to land instead of assuming one frame is enough: the projects scan runs
    in a worker, so how long it takes depends on how much the fixture put on disk.
    """
    from textual.widgets import DataTable

    for _ in range(20):
        await pilot.pause()  # type: ignore[attr-defined]
        if isinstance(pilot.app.screen, SessionsScreen):  # type: ignore[attr-defined]
            break
        try:
            table = pilot.app.screen.query_one("#projects", DataTable)  # type: ignore[attr-defined]
        except Exception:
            continue
        if table.row_count:
            table.action_select_cursor()
    screen = pilot.app.screen  # type: ignore[attr-defined]
    assert isinstance(screen, SessionsScreen), f"no se abrió la pantalla: {screen}"
    # Wait for rows too: row-dependent bindings like u are disabled while the list is empty.
    for _ in range(20):
        if screen._rows:
            break
        await pilot.pause()  # type: ignore[attr-defined]
    return screen


async def _remote_store(app: ClaudeBrowserApp, project_index: int = 0) -> DirectoryRemote:
    """The store behind the first remote linked to the open project."""
    from multi_claude.screens.sessions import SessionsScreen

    screen = app.screen
    assert isinstance(screen, SessionsScreen)
    (link,) = screen._remote_links
    store = app.store_for_link(link)
    assert isinstance(store, DirectoryRemote)
    return store


async def _publish(pilot: object, *, dest_index: int | None = None) -> None:
    """Press u, optionally pick a destination, and confirm."""
    from textual.widgets import RadioButton

    from multi_claude.modals import PublishModal

    await pilot.press("u")  # type: ignore[attr-defined]
    for _ in range(6):
        await pilot.pause()  # type: ignore[attr-defined]
    modal = pilot.app.screen  # type: ignore[attr-defined]
    assert isinstance(modal, PublishModal), f"no se abrió el diálogo: {modal}"
    if dest_index is not None:
        modal.query_one(f"#dest-{dest_index}", RadioButton).value = True
        await pilot.pause()  # type: ignore[attr-defined]
    modal.query_one("#publish").press()
    for _ in range(6):
        await pilot.pause()  # type: ignore[attr-defined]


async def _open_remote_tab(pilot: object, screen: SessionsScreen, index: int = 0) -> None:
    """Select the tab of the ``index``-th linked remote and wait for it to load."""
    from textual.widgets import Tabs

    tabs = screen.query_one("#session-tabs", Tabs)
    tabs.active = f"tab-remote-{index}"
    for _ in range(30):
        await pilot.pause()  # type: ignore[attr-defined]


async def test_publish_writes_the_session_to_the_remote(world: Path) -> None:
    """`u` → confirm → the manifest and blobs land on the remote."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        store = await _remote_store(app)

        await _publish(pilot)
        for _ in range(20):  # let the publish worker finish
            await pilot.pause()

        published = store.list_sessions()
        assert [s.session_id for s in published] == ["ses-1"]
        assert (world / "remote" / "blobs" / "ses-1" / "session.jsonl.gz").is_file()
        assert screen._rows  # screen still usable afterwards


async def test_publish_can_be_cancelled(world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        store = await _remote_store(app)

        await pilot.press("u")
        for _ in range(6):
            await pilot.pause()
        await pilot.press("escape")
        for _ in range(6):
            await pilot.pause()

        assert store.list_sessions() == ()


async def test_publishing_marked_sessions_publishes_all_of_them(world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        store = await _remote_store(app)

        await pilot.press("space")  # mark row 1
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("space")  # mark row 2
        await pilot.pause()
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()

        assert sorted(s.session_id for s in store.list_sessions()) == ["ses-1", "ses-2"]


async def test_a_colleagues_session_shows_up_and_hydrates_on_enter(world: Path) -> None:
    """The whole point: a session published elsewhere resumes with Enter."""
    remote = DirectoryRemote(world / "remote")
    other_project = world / "otro-proyecto"
    write_session(other_project, session_id="ses-de-carlos", cwd="/home/carlos/repo")
    remote.publish(
        RemoteSession(
            session_id="ses-de-carlos",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="carlos@example.com",
            branch="fl-v16-9269",
            first_prompt="lo que hablé yo",
            git_head="abc1234",
        ),
        other_project,
    )

    launched: list[tuple[Path, str | None]] = []

    def fake_launch(cwd: Path, session_id: str | None = None, **kwargs: object) -> LaunchOutcome:
        launched.append((cwd, session_id))
        return LaunchOutcome("window", "fake-emulator")

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)

        await _open_remote_tab(pilot, screen)
        assert [r.session_id for r in screen._remote_sessions] == ["ses-de-carlos"]
        # The remote tab shows only that remote's sessions, not the local ones.
        assert screen._rows == [(True, 0)]

        from textual.widgets import DataTable

        table = screen.query_one("#sessions", DataTable)
        table.move_cursor(row=len(screen._rows) - 1)
        await pilot.pause()
        assert screen._selected_remote() is not None
        assert screen._selected_session() is None  # local actions cannot touch it

        with patch("multi_claude.screens.sessions.launch_claude", side_effect=fake_launch):
            table.action_select_cursor()
            for _ in range(30):
                await pilot.pause()

        # Hydrated into *this* project's dir, under the original uuid...
        assert (world / "projects" / "-repo" / "ses-de-carlos.jsonl").is_file()
        # ...and resumed by that same uuid.
        assert launched and launched[-1][1] == "ses-de-carlos"


async def test_opening_a_remote_tab_makes_its_sessions_searchable(world: Path) -> None:
    """Visiting a remote caches its listing, which is what lets `?` find a teammate's work.

    The search screen never talks to a remote, so if this wiring breaks the team's
    sessions simply stop existing for global search — silently.
    """
    remote = DirectoryRemote(world / "remote")
    other_project = world / "otro-proyecto"
    write_session(other_project, session_id="ses-de-carlos", cwd="/home/carlos/repo")
    remote.publish(
        RemoteSession(
            session_id="ses-de-carlos",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="carlos@example.com",
            branch="fl-v16-9269",
            display_name="el exporter de facturas se atraganta",
            first_prompt="lo que hablé yo",
            tags=("infra",),
        ),
        other_project,
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)

        index = default_index()
        for query in ("exporter", "carlos", "fl-v16-9269", "infra"):
            hits = index.fts_search_remote(query)
            assert [h.session_id for h in hits] == ["ses-de-carlos"], query
        (hit,) = index.fts_search_remote("exporter")
        assert hit.published_by == "carlos@example.com"
        assert hit.remote_label == screen._remote_links[0].tab_label()
        # The key is the link's identity, so relabelling the tab cannot duplicate rows.
        assert hit.remote_key == screen._remote_links[0].identity_key()
        # And it points back at the project whose tab holds it.
        assert hit.project_key == screen._remote_key()


async def test_global_search_lists_a_teammates_session_and_opens_its_tab(world: Path) -> None:
    """`?` shows the team's sessions marked with their author, and Enter lands on the tab."""
    from textual.widgets import DataTable, Input

    from multi_claude.screens.search import SearchScreen
    from multi_claude.screens.sessions import SessionsScreen

    remote = DirectoryRemote(world / "remote")
    other_project = world / "otro-proyecto"
    write_session(other_project, session_id="ses-de-carlos", cwd="/home/carlos/repo")
    remote.publish(
        RemoteSession(
            session_id="ses-de-carlos",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="carlos@example.com",
            branch="fl-v16-9269",
            display_name="el exporter de facturas se atraganta",
            first_prompt="lo que hablé yo",
        ),
        other_project,
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)  # this is what caches the listing
        await pilot.press("escape")  # back to ProjectsScreen
        await pilot.pause()

        await pilot.press("question_mark")
        for _ in range(10):
            await pilot.pause()
        search = pilot.app.screen
        assert isinstance(search, SearchScreen), f"no se abrió la búsqueda: {search}"

        search.query_one("#fts-query", Input).value = "exporter"
        for _ in range(20):
            await pilot.pause()

        table = search.query_one("#fts-results", DataTable)
        rows = [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]
        # The fixture's own session also says "exporter", and local hits come first, so
        # both sources are in this list — which is the point of the "Dónde" column.
        assert rows[0][1] == "local"
        team = [i for i, cells in enumerate(rows) if cells[1].startswith("☁")]
        assert team, f"la sesión del equipo no aparece: {rows}"
        assert rows[team[0]][1] == "☁ carlos"  # who, not just "remote"
        assert "exporter" in rows[team[0]][0]

        table.move_cursor(row=team[0])
        table.action_select_cursor()
        for _ in range(30):
            await pilot.pause()

        landed = pilot.app.screen
        assert isinstance(landed, SessionsScreen)
        # Not merely the project: the remote's own tab, with the row in it.
        assert landed._active_remote == 0
        assert [r.session_id for r in landed._remote_sessions] == ["ses-de-carlos"]


async def test_secrets_filter_isolates_the_sensitive_sessions(world: Path) -> None:
    """`/secrets:yes` answers "what should not leave this machine", in one keystroke."""
    from textual.widgets import Input

    jsonl = world / "projects" / "-repo" / "ses-1.jsonl"
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": "GITHUB_TOKEN=" + _fake_token()},
                    "sessionId": "ses-1",
                }
            )
            + "\n"
        )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        for _ in range(40):
            await pilot.pause()
            if screen._secret_counts:
                break

        async def shown(query: str) -> list[str]:
            screen.query_one("#filter", Input).value = query
            for _ in range(10):
                await pilot.pause()
            return [screen._sessions[i].id for is_remote, i in screen._rows if not is_remote]

        await pilot.press("slash")
        await pilot.pause()
        assert await shown("secrets:yes") == ["ses-1"]
        assert await shown("secrets:no") == ["ses-2"]
        # Both were scanned, so nothing is unknown…
        assert await shown("secrets:unknown") == []
        # …and a value the filter does not understand lets nothing through.
        assert await shown("secrets:quizá") == []


async def test_hydrating_a_session_with_a_credential_warns_you(world: Path) -> None:
    """Scanning in the other direction: what just landed came from someone else's disk.

    Whoever published it may not have had a scanner in front of them, and now it is here.
    """
    remote = DirectoryRemote(world / "remote")
    other_project = world / "otro-proyecto"
    other_project.mkdir(exist_ok=True)
    (other_project / "ses-ajena.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "GITHUB_TOKEN=" + _fake_token()},
                "sessionId": "ses-ajena",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    remote.publish(
        RemoteSession(
            session_id="ses-ajena",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="carlos@example.com",
            display_name="lo que hizo carlos",
        ),
        other_project,
    )

    warnings: list[str] = []

    def fake_launch(cwd: Path, session_id: str | None = None, **kwargs: object) -> LaunchOutcome:
        return LaunchOutcome("window", "fake")

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        original_notify = screen.notify

        def capture(message: str, **kwargs: object) -> None:
            warnings.append(message)
            original_notify(message, **kwargs)  # type: ignore[arg-type]

        screen.notify = capture  # type: ignore[method-assign]

        await _open_remote_tab(pilot, screen)
        from textual.widgets import DataTable

        table = screen.query_one("#sessions", DataTable)
        table.move_cursor(row=0)
        with patch("multi_claude.screens.sessions.launch_claude", side_effect=fake_launch):
            table.action_select_cursor()
            for _ in range(40):
                await pilot.pause()

        assert (world / "projects" / "-repo" / "ses-ajena.jsonl").is_file()  # fetched
        assert any("credencial" in w for w in warnings), warnings
        # And it is already marked, without waiting for the next full scan.
        assert screen._secret_counts.get("ses-ajena") == 1


async def test_a_session_with_a_credential_is_marked_in_the_listing(world: Path) -> None:
    """The ⚠ answers "should this leave the machine?" before you try to publish it."""
    from textual.widgets import DataTable

    from multi_claude.index import default_index

    jsonl = world / "projects" / "-repo" / "ses-1.jsonl"
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "GITHUB_TOKEN=" + _fake_token(),
                    },
                    "sessionId": "ses-1",
                }
            )
            + "\n"
        )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        for _ in range(40):
            await pilot.pause()
            if screen._secret_counts:
                break
        assert screen._secret_counts.get("ses-1") == 1
        assert screen._secret_counts.get("ses-2") == 0  # scanned and clean

        table = screen.query_one("#sessions", DataTable)
        rendered = {str(table.get_row_at(i)[0]): i for i in range(table.row_count)}
        marked = [text for text in rendered if text.startswith("⚠")]
        assert len(marked) == 1, rendered
        # The scan result is cached, so a second visit costs nothing.
        assert default_index().secret_scan_is_fresh("ses-1", jsonl.stat().st_mtime)


async def test_publishing_a_session_with_a_credential_warns_and_defends_itself(
    world: Path,
) -> None:
    """The whole point of the scan: the dialogue changes shape when it finds something.

    Not just a louder label — the accept button moves out of reflex range, because the
    failure this guards against is someone pressing Enter on autopilot.
    """
    from textual.widgets import Button

    from multi_claude.modals import PublishModal

    jsonl = world / "projects" / "-repo" / "ses-1.jsonl"
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "GITHUB_TOKEN=" + _fake_token(),
                    },
                    "sessionId": "ses-1",
                }
            )
            + "\n"
        )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        await pilot.press("u")
        for _ in range(30):
            await pilot.pause()
            if isinstance(pilot.app.screen, PublishModal):
                break
        modal = pilot.app.screen
        assert isinstance(modal, PublishModal), f"no se abrió el diálogo: {modal}"
        assert modal.suspicious
        (exposure,) = modal.exposures
        assert exposure.rule == "token de GitHub"
        # And it says what to do about it, which is the point of showing it at all.
        assert "revócalo" in exposure.rotation
        assert "ses-1.jsonl" in exposure.where()
        # The secret itself never reaches the dialogue.
        rendered = f"{exposure.headline()} {exposure.rotation} {exposure.where()}"
        assert "aBcDeFgHiJkLmNoPqRsTuVwXyZ" not in rendered
        # Cancel has the focus and the accept button says what it is doing.
        assert modal.focused is modal.query_one("#cancel", Button)
        assert "todas formas" in str(modal.query_one("#publish", Button).label)

        # Enter must not publish: it presses the focused Cancelar instead.
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        store = await _remote_store(app)
        assert store.list_sessions() == ()


async def test_publishing_a_clean_session_keeps_the_fast_path(world: Path) -> None:
    """No findings → the dialogue stays as it was, Enter included."""
    from textual.widgets import Button

    from multi_claude.modals import PublishModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        await pilot.press("u")
        for _ in range(30):
            await pilot.pause()
            if isinstance(pilot.app.screen, PublishModal):
                break
        modal = pilot.app.screen
        assert isinstance(modal, PublishModal)
        assert not modal.suspicious
        assert modal.exposures == []
        assert modal.focused is modal.query_one("#publish", Button)

        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
        store = await _remote_store(app)
        assert [s.session_id for s in store.list_sessions()] == ["ses-1"]


async def test_the_remote_tab_is_reachable_with_the_keyboard(world: Path) -> None:
    """ctrl+→ / ctrl+← cycle the tabs, wrapping around.

    Before these bindings the tab bar was mouse-only: `tab` moves focus to the preview and
    the Tabs widget never received the arrow keys, so shared sessions were unreachable for
    anyone driving by keyboard (or for a screen recorder, which is how this surfaced).
    """
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        assert screen._active_remote is None

        await pilot.press("ctrl+right")
        for _ in range(30):
            await pilot.pause()
        assert screen._active_remote == 0

        # One more wraps back to the local listing (one remote → two tabs).
        await pilot.press("ctrl+right")
        for _ in range(20):
            await pilot.pause()
        assert screen._active_remote is None

        # And backwards from local lands on the last remote.
        await pilot.press("ctrl+left")
        for _ in range(30):
            await pilot.pause()
        assert screen._active_remote == 0


async def test_tab_switching_is_inert_without_linked_remotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No remotes → the tab bar is hidden, and the binding must not blow up."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    real = tmp_path / "repo"
    real.mkdir()
    write_session(projects_root / "-repo", session_id="solo", cwd=str(real))
    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", projects_root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        assert screen._remote_links == ()
        await pilot.press("ctrl+right")
        await pilot.press("ctrl+left")
        for _ in range(10):
            await pilot.pause()
        assert screen._active_remote is None


async def test_author_filters_a_remote_tab_by_who_published(world: Path) -> None:
    """`author:` on a remote tab: two colleagues published, you want one of them."""
    from textual.widgets import Input

    remote = DirectoryRemote(world / "remote")
    for who, sid, name in (
        ("carlos@example.com", "ses-de-carlos", "el exporter de facturas"),
        ("ana@example.com", "ses-de-ana", "el deploy de staging"),
    ):
        project = world / f"proyecto-{sid}"
        write_session(project, session_id=sid, cwd="/home/otro/repo")
        remote.publish(
            RemoteSession(
                session_id=sid,
                published_at="2026-07-28T10:00:00+00:00",
                published_by=who,
                display_name=name,
            ),
            project,
        )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)
        assert len(screen._rows) == 2

        await pilot.press("slash")
        await pilot.pause()
        screen.query_one("#filter", Input).value = "author:ana"
        for _ in range(10):
            await pilot.pause()

        assert [screen._remote_sessions[i].session_id for _, i in screen._rows] == ["ses-de-ana"]


async def test_author_finds_a_session_you_hydrated_from_a_colleague(world: Path) -> None:
    """The local-tab case: which of the sessions on my disk came from someone else."""
    from textual.widgets import Input

    remote = DirectoryRemote(world / "remote")
    other_project = world / "otro-proyecto"
    write_session(other_project, session_id="ses-de-carlos", cwd="/home/carlos/repo")
    remote.publish(
        RemoteSession(
            session_id="ses-de-carlos",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="carlos@example.com",
            display_name="el exporter de facturas",
        ),
        other_project,
    )
    # Already on disk, as if hydrated earlier: the local tab lists it like any other.
    write_session(
        world / "projects" / "-repo",
        session_id="ses-de-carlos",
        cwd=str(world / "repo"),
        first_prompt="el exporter de facturas",
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        # The published index is what supplies the author, and it loads in a worker.
        for _ in range(30):
            if screen._published:
                break
            await pilot.pause()
        assert "ses-de-carlos" in screen._published

        await pilot.press("slash")
        await pilot.pause()
        screen.query_one("#filter", Input).value = "author:carlos"
        for _ in range(10):
            await pilot.pause()

        shown = [screen._sessions[i].id for is_remote, i in screen._rows if not is_remote]
        assert shown == ["ses-de-carlos"]

        screen.query_one("#filter", Input).value = "author:ana"
        for _ in range(10):
            await pilot.pause()
        assert screen._rows == []


async def test_publishing_over_someone_elses_version_is_blocked(world: Path) -> None:
    """The data-loss case: Ana published on top, and publishing would replace her turns.

    Nothing is written; the dialogue reports it and overwriting has to be chosen.
    """
    from multi_claude.index import default_index
    from multi_claude.modals import PublishConflictModal

    store = DirectoryRemote(world / "remote")
    other = world / "de-ana"
    write_session(other, session_id="ses-1", cwd="/home/ana/repo", first_prompt="lo de ana")
    ana = RemoteSession(
        session_id="ses-1",
        published_at="2026-08-02T09:00:00+00:00",
        published_by="ana@example.com",
        message_count=455,
    )
    store.publish(ana, other)

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        # Our copy derives from an older publish of the same session.
        default_index().record_publish_base(
            screen._remote_links[0].identity_key(), "ses-1", "2026-07-30T08:00:00+00:00"
        )

        await _publish(pilot)
        for _ in range(40):
            await pilot.pause()
            if isinstance(pilot.app.screen, PublishConflictModal):
                break
        modal = pilot.app.screen
        assert isinstance(modal, PublishConflictModal), f"no avisó del conflicto: {modal}"
        assert any("ana" in line for line in modal.lines)

        # Cancelling leaves Ana's version exactly as it was.
        await pilot.press("escape")
        for _ in range(20):
            await pilot.pause()
        (still,) = store.list_sessions()
        assert still.published_by == "ana@example.com"
        assert still.message_count == 455


async def test_overwriting_a_conflict_is_possible_on_purpose(world: Path) -> None:
    """Sometimes replacing is right — a stray republish — so the door exists."""
    from multi_claude.index import default_index
    from multi_claude.modals import PublishConflictModal

    store = DirectoryRemote(world / "remote")
    other = world / "de-ana"
    write_session(other, session_id="ses-1", cwd="/home/ana/repo")
    store.publish(
        RemoteSession(
            session_id="ses-1",
            published_at="2026-08-02T09:00:00+00:00",
            published_by="ana@example.com",
        ),
        other,
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        default_index().record_publish_base(
            screen._remote_links[0].identity_key(), "ses-1", "vieja"
        )
        await _publish(pilot)
        for _ in range(40):
            await pilot.pause()
            if isinstance(pilot.app.screen, PublishConflictModal):
                break
        modal = pilot.app.screen
        assert isinstance(modal, PublishConflictModal)
        modal.query_one("#overwrite").press()
        for _ in range(40):
            await pilot.pause()

        (now,) = store.list_sessions()
        assert now.published_by != "ana@example.com"  # ours replaced hers, deliberately


async def test_a_fast_forward_publishes_without_asking(world: Path) -> None:
    """Nobody touched it since we published, so republishing is routine."""
    from multi_claude.index import default_index
    from multi_claude.modals import PublishConflictModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _publish(pilot)  # first publish records the base
        for _ in range(30):
            await pilot.pause()
        store = await _remote_store(app)
        (published,) = store.list_sessions()
        remote_key = screen._remote_links[0].identity_key()
        assert default_index().publish_base(remote_key, "ses-1") == published.published_at

        # Publish the same session again: same stamp on the remote, so no dialogue.
        await _publish(pilot)
        for _ in range(30):
            await pilot.pause()
        assert not isinstance(pilot.app.screen, PublishConflictModal)
        assert len(store.list_sessions()) == 1


async def test_fetching_records_the_base_so_a_later_publish_is_safe(world: Path) -> None:
    """Hydrating is the other moment our copy starts deriving from a published version."""
    from multi_claude.index import default_index

    store = DirectoryRemote(world / "remote")
    other = world / "de-ana"
    write_session(other, session_id="ses-de-ana", cwd="/home/ana/repo")
    store.publish(
        RemoteSession(
            session_id="ses-de-ana",
            published_at="2026-08-02T09:00:00+00:00",
            published_by="ana@example.com",
        ),
        other,
    )

    def fake_launch(cwd: Path, session_id: str | None = None, **kwargs: object) -> LaunchOutcome:
        return LaunchOutcome("window", "fake")

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)
        from textual.widgets import DataTable

        table = screen.query_one("#sessions", DataTable)
        table.move_cursor(row=0)
        with patch("multi_claude.screens.sessions.launch_claude", side_effect=fake_launch):
            table.action_select_cursor()
            for _ in range(40):
                await pilot.pause()

        remote_key = screen._remote_links[0].identity_key()
        assert default_index().publish_base(remote_key, "ses-de-ana") == "2026-08-02T09:00:00+00:00"


async def test_a_teammates_session_becomes_searchable_by_its_content(world: Path) -> None:
    """The whole point of manifest v2: found by a phrase from inside, without fetching it.

    Before this, a colleague's session could only be found by its name, tags, branch or
    author — the metadata a manifest carries.
    """
    from multi_claude.index import default_index

    remote = DirectoryRemote(world / "remote")
    other_project = world / "otro-proyecto"
    write_session(other_project, session_id="ses-de-ana", cwd="/home/ana/repo")
    # A real conversation, so there is a search payload to publish.
    (other_project / "ses-de-ana.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "type": role,
                    "message": {"role": role, "content": [{"type": "text", "text": text}]},
                    "sessionId": "ses-de-ana",
                }
            )
            for role, text in (
                ("user", "el exportador se corta a los 60 segundos"),
                ("assistant", "es el proxy_read_timeout del vhost de nginx"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    remote.publish(
        RemoteSession(
            session_id="ses-de-ana",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="ana@example.com",
            display_name="algo que no menciona el timeout",
        ),
        other_project,
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)
        index = default_index()
        for _ in range(40):
            await pilot.pause()
            if index.count_remote_with_text():
                break

        # A phrase that appears only inside the conversation, never in its metadata.
        hits = index.fts_search_remote("proxy_read_timeout")
        assert [h.session_id for h in hits] == ["ses-de-ana"]
        # And the session was never downloaded.
        assert not (world / "projects" / "-repo" / "ses-de-ana.jsonl").exists()
        # Metadata still matches too: the text is added, not swapped in.
        assert index.fts_search_remote("ana")


async def test_the_search_text_is_not_downloaded_twice(world: Path) -> None:
    """Re-listing a tab must not re-fetch what is already indexed."""
    from multi_claude.index import default_index

    remote = DirectoryRemote(world / "remote")
    other = world / "otro"
    write_session(other, session_id="ses-x", cwd="/home/otra/repo")
    (other / "ses-x.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": "buscable"}]},
                "sessionId": "ses-x",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    remote.publish(
        RemoteSession(
            session_id="ses-x",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="otra@example.com",
        ),
        other,
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)
        for _ in range(40):
            await pilot.pause()
            if default_index().count_remote_with_text():
                break
        assert default_index().count_remote_with_text() == 1

        calls: list[str] = []
        store = await _remote_store(app)
        original = store.fetch_search_text

        def counting(session_id: str) -> str | None:
            calls.append(session_id)
            return original(session_id)

        store.fetch_search_text = counting  # type: ignore[method-assign]
        with patch.object(app, "store_for_link", return_value=store):
            screen._load_remote_worker(0)
            for _ in range(40):
                await pilot.pause()

        assert calls == []  # already indexed, so nothing was asked for again
        assert default_index().count_remote_with_text() == 1


async def test_unpublishing_drops_the_session_from_search(world: Path) -> None:
    """A re-listing is a replace: what is gone from the remote stops being a search hit."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        await _open_remote_tab(pilot, screen)
        assert default_index().fts_search_remote("exporter")

        store = await _remote_store(app)
        store.unpublish("ses-1")
        screen._load_remote_worker(0)
        for _ in range(30):
            await pilot.pause()

        assert default_index().fts_search_remote("exporter") == []


async def test_a_published_session_shows_up_in_its_tab(world: Path) -> None:
    """The confirmation that a publish worked: your session appears in the repo's tab."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)

        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()

        await _open_remote_tab(pilot, screen)

        assert [r.session_id for r in screen._remote_sessions] == ["ses-1"]
        assert screen._rows == [(True, 0)]


async def test_a_session_you_already_have_is_marked_not_hidden(world: Path) -> None:
    """It is listed with ✓ instead of ☁, because the tab is a view of the repo."""
    from textual.widgets import DataTable

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)

        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        await _open_remote_tab(pilot, screen)

        table = screen.query_one("#sessions", DataTable)
        rendered = str(table.get_row_at(0)[0])
        assert "✓" in rendered
        assert "descargada" in rendered


async def test_the_row_says_when_the_published_version_is_newer(world: Path) -> None:
    """Someone continued the session after you fetched it: the row has to say so."""
    from textual.widgets import DataTable

    remote = DirectoryRemote(world / "remote")
    other = world / "otro"
    jsonl = write_session(other, session_id="ses-compartida", cwd="/home/otro/repo")
    # Publish a manifest claiming more bytes than the copy that will land locally.
    remote.publish(
        RemoteSession(
            session_id="ses-compartida",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="ana@example.com",
            size_bytes=jsonl.stat().st_size * 4,
        ),
        other,
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)

        # Not downloaded yet → cloud, no note about the local copy.
        assert screen._local_state(screen._remote_sessions[0]) == "absent"
        assert "☁" in str(screen.query_one("#sessions", DataTable).get_row_at(0)[0])

        # Fetch it: the local copy is now shorter than what the manifest advertises.
        remote.fetch("ses-compartida", screen.project.encoded_path)
        screen._repaint()
        await pilot.pause()

        assert screen._local_state(screen._remote_sessions[0]) == "stale"
        rendered = str(screen.query_one("#sessions", DataTable).get_row_at(0)[0])
        assert "↻" in rendered
        assert "versión más reciente" in rendered


async def test_the_row_says_when_you_have_unpublished_turns(world: Path) -> None:
    """You continued a session you published; nobody else can see that work yet."""
    from textual.widgets import DataTable

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        await _open_remote_tab(pilot, screen)
        assert screen._local_state(screen._remote_sessions[0]) == "current"

        # Simulate resuming it: Claude appends to the same jsonl.
        jsonl = screen.project.encoded_path / "ses-1.jsonl"
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "sessionId": "ses-1"}) + "\n")
        screen._repaint()
        await pilot.pause()

        assert screen._local_state(screen._remote_sessions[0]) == "ahead"
        rendered = str(screen.query_one("#sessions", DataTable).get_row_at(0)[0])
        assert "↑" in rendered
        assert "sin publicar" in rendered


async def test_enter_on_a_session_you_already_have_resumes_it_locally(world: Path) -> None:
    """Fetching would refuse to overwrite, so it must resume the copy on disk instead."""
    launched: list[str | None] = []

    def fake_launch(cwd: Path, session_id: str | None = None, **kwargs: object) -> LaunchOutcome:
        launched.append(session_id)
        return LaunchOutcome("window", "fake-emulator")

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        await _open_remote_tab(pilot, screen)

        from textual.widgets import DataTable

        table = screen.query_one("#sessions", DataTable)
        table.move_cursor(row=0)
        await pilot.pause()
        with patch("multi_claude.screens.sessions.launch_claude", side_effect=fake_launch):
            table.action_select_cursor()
            for _ in range(20):
                await pilot.pause()

        assert launched == ["ses-1"]


async def test_going_back_to_the_local_tab_clears_the_remote_rows(world: Path) -> None:
    remote = DirectoryRemote(world / "remote")
    other = world / "otro"
    write_session(other, session_id="ses-ajena", cwd="/home/otro/repo")
    remote.publish(
        RemoteSession(session_id="ses-ajena", published_at="2026-07-28T10:00:00+00:00"),
        other,
    )

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _open_remote_tab(pilot, screen)
        assert len(screen._remote_sessions) == 1

        from textual.widgets import Tabs

        screen.query_one("#session-tabs", Tabs).active = "tab-local"
        for _ in range(10):
            await pilot.pause()
        assert screen._remote_sessions == []
        assert all(not is_remote for is_remote, _ in screen._rows)


async def test_publish_is_unavailable_without_a_remote(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sharing off the binding hides rather than failing on use."""
    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        assert screen._remote_links == ()
        assert screen.check_action("publish", ()) is False


# --- configuring the remote from the UI ---------------------------------------------


async def test_linking_a_repo_only_asks_what_is_repo_specific(world: Path) -> None:
    """The server supplies provider and host, so the link only needs repo, branch and label."""
    from textual.widgets import Input, RadioButton

    from multi_claude.modals import RepoLinkModal
    from multi_claude.project_remotes import RemoteLink, RemoteServer

    servers = [RemoteServer(name="FactorLibre", kind="gitlab", host="https://git.empresa.com")]
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = RepoLinkModal(RemoteLink(), servers=servers)
        app.push_screen(modal)
        for _ in range(6):
            await pilot.pause()

        modal.query_one("#target-0", RadioButton).value = True
        modal.query_one("#link-repo", Input).value = "/grupo/sesiones/"
        modal.query_one("#link-branch", Input).value = "trunk"
        await pilot.pause()

        result = modal.collect()
        assert result.server == "FactorLibre"
        assert result.kind == "gitlab"
        assert result.host == "https://git.empresa.com"
        assert result.repo == "grupo/sesiones"  # trailing slashes stripped
        assert result.branch == "trunk"
        assert result.tab_label() == "sesiones"


async def test_a_configured_server_appears_when_linking_a_repo(world: Path) -> None:
    """The whole point: servers set up in Ajustes are offered here by name."""
    from dataclasses import replace

    from textual.widgets import Button, RadioButton

    from multi_claude.modals import ProjectRemotesModal, RepoLinkModal
    from multi_claude.project_remotes import RemoteServer

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        app.update_prefs(
            replace(
                app.prefs,
                remote_servers=[
                    RemoteServer(name="FactorLibre", host="https://git.factorlibre.com"),
                    RemoteServer(name="GitHub propio", kind="github"),
                ],
            )
        )

        await pilot.press("L")
        for _ in range(8):
            await pilot.pause()
        manage = app.screen
        assert isinstance(manage, ProjectRemotesModal)
        manage.query_one("#add", Button).press()
        for _ in range(8):
            await pilot.pause()

        link_modal = app.screen
        assert isinstance(link_modal, RepoLinkModal)
        offered = [str(button.label) for button in link_modal.query(RadioButton)]
        assert any("FactorLibre" in text for text in offered)
        assert any("GitHub propio" in text for text in offered)
        assert any("Carpeta compartida" in text for text in offered)


async def test_the_token_never_reaches_the_config_file(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.json gets shared and pasted into issues; a credential there leaks."""
    from dataclasses import replace

    from textual.widgets import Input

    from multi_claude.config import config_path
    from multi_claude.modals import ServerEditModal
    from multi_claude.project_remotes import RemoteServer
    from multi_claude.remote import TokenStore, token_path

    monkeypatch.delenv("MULTI_CLAUDE_REMOTE_TOKEN", raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = ServerEditModal(RemoteServer(name="FactorLibre"))
        app.push_screen(modal)
        for _ in range(6):
            await pilot.pause()
        modal.query_one("#server-token", Input).value = "glpat-muy-secreto"
        await pilot.pause()

        server = modal.collect()
        token = modal.typed_token()
        assert token == "glpat-muy-secreto"
        # Absent from the server object and from the config it gets stored in.
        assert "glpat-muy-secreto" not in json.dumps(server.to_dict())
        assert "glpat-muy-secreto" not in json.dumps(
            replace(app.prefs, remote_servers=[server]).to_dict()
        )

        TokenStore().set(token or "", server.name)
        app.update_prefs(replace(app.prefs, remote_servers=[server]))
        await pilot.pause()

        assert "glpat-muy-secreto" not in config_path().read_text(encoding="utf-8")
        assert TokenStore().get("FactorLibre") == "glpat-muy-secreto"
        assert stat.S_IMODE(token_path().stat().st_mode) == 0o600


async def test_an_empty_token_field_keeps_the_stored_one(world: Path) -> None:
    """Reopening the modal must not wipe the saved token just by saving again."""
    from multi_claude.modals import ServerEditModal
    from multi_claude.project_remotes import RemoteServer

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = ServerEditModal(RemoteServer(name="FactorLibre"), has_token=True)
        app.push_screen(modal)
        for _ in range(6):
            await pilot.pause()
        assert modal.typed_token() is None  # None means "leave it alone"


async def test_settings_opens_the_server_list_and_the_global_remote(world: Path) -> None:
    from textual.widgets import Button, Label

    from multi_claude.modals import RepoLinkModal, ServersModal, SettingsModal

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        settings = SettingsModal(app.prefs)
        app.push_screen(settings)
        for _ in range(6):
            await pilot.pause()

        assert "desactivado" in str(settings.query_one("#remote-summary", Label).content)
        assert "ninguno" in str(settings.query_one("#servers-summary", Label).content)

        settings.query_one("#configure-servers", Button).press()
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.screen, ServersModal)
        app.pop_screen()
        for _ in range(6):
            await pilot.pause()

        settings.query_one("#configure-remote", Button).press()
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.screen, RepoLinkModal)


async def test_the_env_var_overrides_every_configured_remote(world: Path) -> None:
    """It is the escape hatch: a throwaway run must not touch a real sessions repo."""
    from dataclasses import replace

    from multi_claude.project_remotes import RemoteLink

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)

        # Link the project to a GitLab repo, which would otherwise win over the global one.
        app.project_remotes.set_all(
            screen._remote_key(),
            [RemoteLink(kind="gitlab", host="https://git.example.com", repo="g/s")],
        )
        app.update_prefs(replace(app.prefs, remote_kind="github", remote_repo="o/r"))

        (link,) = app.remote_links_for(screen.project)
        assert link.kind == "directory"
        assert link.path == str(world / "remote")


# --- per-project links and settings tabs --------------------------------------------


async def test_linking_a_project_creates_one_tab_per_repo(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several sessions repos on one project, each with its own tab."""
    from textual.widgets import Tabs

    from multi_claude.project_remotes import RemoteLink

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)  # env var would override the links
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        assert screen._remote_links == ()

        app.project_remotes.set_all(
            screen._remote_key(),
            [
                RemoteLink(kind="directory", path=str(world / "r-cliente"), label="cliente-x"),
                RemoteLink(kind="directory", path=str(world / "r-producto")),
            ],
        )
        await screen._refresh_remote_tabs()
        await pilot.pause()

        labels = [str(tab.label) for tab in screen.query_one("#session-tabs", Tabs).query("Tab")]
        assert labels == ["Locales", "☁ cliente-x", "☁ r-producto"]


async def test_the_publish_dialogue_lets_you_choose_between_repos(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With several repos linked you pick the destination in the dialogue itself."""
    from multi_claude.project_remotes import RemoteLink

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.project_remotes.set_all(
            screen._remote_key(),
            [
                RemoteLink(kind="directory", path=str(world / "a"), label="a"),
                RemoteLink(kind="directory", path=str(world / "b"), label="b"),
            ],
        )
        await screen._refresh_remote_tabs()
        await pilot.pause()

        # From the local tab the dialogue offers both and starts on the first.
        assert screen._default_destination() == 0
        await _publish(pilot, dest_index=1)
        for _ in range(20):
            await pilot.pause()
        assert [s.session_id for s in DirectoryRemote(world / "b").list_sessions()] == ["ses-1"]
        assert DirectoryRemote(world / "a").list_sessions() == ()

        # From a repo's tab it starts on that repo.
        await _open_remote_tab(pilot, screen, index=1)
        assert screen._default_destination() == 1


async def test_a_single_linked_repo_needs_no_choosing(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from multi_claude.project_remotes import RemoteLink

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.project_remotes.set_all(
            screen._remote_key(), [RemoteLink(kind="directory", path=str(world / "solo"))]
        )
        await screen._refresh_remote_tabs()
        await pilot.pause()

        # A single destination needs no choosing: the dialogue just states it.
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        assert [s.session_id for s in DirectoryRemote(world / "solo").list_sessions()] == ["ses-1"]


async def test_project_links_win_over_the_global_remote(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project linked to a client's repo must not also publish to the default one."""
    from dataclasses import replace

    from multi_claude.project_remotes import RemoteLink

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.update_prefs(
            replace(app.prefs, remote_kind="directory", remote_path=str(world / "global"))
        )
        # With no links, the global remote is the fallback.
        (fallback,) = app.remote_links_for(screen.project)
        assert fallback.path == str(world / "global")

        app.project_remotes.set_all(
            screen._remote_key(), [RemoteLink(kind="directory", path=str(world / "propio"))]
        )
        (own,) = app.remote_links_for(screen.project)
        assert own.path == str(world / "propio")


async def test_a_repo_declaration_is_used_when_nothing_is_linked(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the whole feature: clone the repo, and the team's tab is already there."""
    import json
    from dataclasses import replace

    from multi_claude.project_config import PROJECT_CONFIG_NAME
    from multi_claude.project_remotes import RemoteServer

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    (world / "repo" / PROJECT_CONFIG_NAME).write_text(
        json.dumps({"sessions_repos": [{"server": "Empresa", "repo": "equipo/sesiones"}]}),
        encoding="utf-8",
    )
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.update_prefs(
            replace(
                app.prefs,
                remote_servers=[RemoteServer(name="Empresa", kind="gitlab", host="https://git.x")],
            )
        )
        app.project_config.clear()
        (link,) = app.remote_links_for(screen.project)
        assert link.repo == "equipo/sesiones"
        assert link.kind == "gitlab"


async def test_a_declaration_naming_an_unconfigured_server_is_inert(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety property, end to end: the repo says which repository, you say which
    server. Without the server configured here, the declaration reaches nothing."""
    import json

    from multi_claude.project_config import PROJECT_CONFIG_NAME

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    (world / "repo" / PROJECT_CONFIG_NAME).write_text(
        json.dumps({"sessions_repos": [{"server": "Desconocido", "repo": "equipo/sesiones"}]}),
        encoding="utf-8",
    )
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.project_config.clear()
        assert app.declared_links_for(screen.project) == ()
        assert app.remote_links_for(screen.project) == ()


async def test_own_links_win_over_the_repo_declaration(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file in a repository must not override a choice this person made deliberately."""
    import json

    from multi_claude.project_config import PROJECT_CONFIG_NAME
    from multi_claude.project_remotes import RemoteLink

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    (world / "repo" / PROJECT_CONFIG_NAME).write_text(
        json.dumps({"sessions_repos": [{"server": "Empresa", "repo": "equipo/sesiones"}]}),
        encoding="utf-8",
    )
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.project_config.clear()
        app.project_remotes.set_all(
            screen._remote_key(), [RemoteLink(kind="directory", path=str(world / "propio"))]
        )
        (own,) = app.remote_links_for(screen.project)
        assert own.path == str(world / "propio")


async def test_the_repo_declaration_wins_over_the_global_remote(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More specific than a machine-wide default, and it is what the team agreed on."""
    import json
    from dataclasses import replace

    from multi_claude.project_config import PROJECT_CONFIG_NAME
    from multi_claude.project_remotes import RemoteServer

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    (world / "repo" / PROJECT_CONFIG_NAME).write_text(
        json.dumps({"sessions_repos": [{"server": "Empresa", "repo": "equipo/sesiones"}]}),
        encoding="utf-8",
    )
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.update_prefs(
            replace(
                app.prefs,
                remote_kind="directory",
                remote_path=str(world / "global"),
                remote_servers=[RemoteServer(name="Empresa", kind="gitlab", host="https://git.x")],
            )
        )
        app.project_config.clear()
        (link,) = app.remote_links_for(screen.project)
        assert link.repo == "equipo/sesiones"


async def test_the_env_override_beats_a_repo_declaration(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a total override, so a throwaway run never touches a real repo."""
    import json

    from multi_claude.project_config import PROJECT_CONFIG_NAME

    (world / "repo" / PROJECT_CONFIG_NAME).write_text(
        json.dumps({"sessions_repos": [{"server": "Empresa", "repo": "equipo/sesiones"}]}),
        encoding="utf-8",
    )
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.project_config.clear()
        (link,) = app.remote_links_for(screen.project)
        assert link.kind == "directory"


async def test_settings_has_a_tab_per_configuration_area(world: Path) -> None:
    from textual.widgets import TabbedContent

    from multi_claude.modals import SettingsModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        settings = SettingsModal(app.prefs)
        app.push_screen(settings)
        await pilot.pause()

        tabs = settings.query_one("#settings-tabs", TabbedContent)
        titles = [str(tab.label) for tab in tabs.query("ContentTab")]
        assert titles == ["Lanzamiento", "Sesiones compartidas", "Colores"]


async def test_editing_colour_rules_from_settings_keeps_them(world: Path) -> None:
    """The rules editor is reachable from settings now, not only from C."""
    from textual.widgets import Button

    from multi_claude.colors import ColorRule
    from multi_claude.modals import ColorRulesEditorModal, SettingsModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        settings = SettingsModal(app.prefs)
        app.push_screen(settings)
        await pilot.pause()

        settings.query_one("#edit-rules", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, ColorRulesEditorModal)

        settings._on_rules_edited([ColorRule(when="branch=main", color="bold red")])
        await pilot.pause()
        assert len(settings._collect().color_rules) == 1


# --- the local tab knows what is shared ---------------------------------------------


async def _wait(pilot: object, turns: int = 25) -> None:
    for _ in range(turns):
        await pilot.pause()  # type: ignore[attr-defined]


async def _wait_until(pilot: object, condition: Callable[[], bool], turns: int = 60) -> None:
    """Pump frames until ``condition`` holds.

    Background workers finish whenever they finish; a fixed number of frames passes on an idle
    machine and fails under load, which is a flaky test rather than a real signal.
    """
    for _ in range(turns):
        await pilot.pause()  # type: ignore[attr-defined]
        if condition():
            return
    raise AssertionError("la condición no se cumplió a tiempo")


async def test_local_rows_show_which_sessions_are_published(world: Path) -> None:
    """After publishing, the local row says so — you should not have to switch tabs."""
    from textual.widgets import DataTable

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        table = screen.query_one("#sessions", DataTable)

        # Nothing published yet: no mark.
        assert "✓" not in str(table.get_row_at(0)[0])

        await _publish(pilot)
        await _wait_until(pilot, lambda: "ses-1" in screen._published)

        row = str(table.get_row_at(0)[0])
        assert "✓" in row
        assert "publicada en" in row


async def test_an_unpublished_session_stays_unmarked(world: Path) -> None:
    """Only one of the two sessions is published; the other must not be marked."""
    from textual.widgets import DataTable

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _publish(pilot)
        await _wait_until(pilot, lambda: bool(screen._published))

        table = screen.query_one("#sessions", DataTable)
        marked = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert sum("publicada en" in row for row in marked) == 1


async def test_a_local_row_says_when_it_has_unpublished_turns(world: Path) -> None:
    from textual.widgets import DataTable

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _publish(pilot)
        await _wait_until(pilot, lambda: "ses-1" in screen._published)

        # Continuing the session appends to its jsonl, making it longer than what is published.
        jsonl = screen.project.encoded_path / "ses-1.jsonl"
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "sessionId": "ses-1"}) + "\n")
        screen._populate()
        await _wait_until(
            pilot, lambda: "↑" in str(screen.query_one("#sessions", DataTable).get_row_at(0)[0])
        )

        row = str(screen.query_one("#sessions", DataTable).get_row_at(0)[0])
        assert "↑" in row
        assert "cambios sin publicar" in row


async def test_a_local_row_says_when_it_came_from_someone_else(world: Path) -> None:
    """A session fetched from a colleague should be recognisable among your own."""
    from textual.widgets import DataTable

    remote = DirectoryRemote(world / "remote")
    other = world / "otro"
    write_session(other, session_id="de-ana", cwd="/home/ana/repo", first_prompt="lo de Ana")
    remote.publish(
        RemoteSession(
            session_id="de-ana",
            published_at="2026-07-28T09:00:00+00:00",
            published_by="ana@factorlibre.com",
            size_bytes=(other / "de-ana.jsonl").stat().st_size,
        ),
        other,
    )
    # Simulate having fetched it: same bytes, so it is "current", but published by Ana.
    remote.fetch("de-ana", world / "projects" / "-repo")

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        await _wait_until(pilot, lambda: "de-ana" in screen._published)

        table = screen.query_one("#sessions", DataTable)
        rows = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        theirs = [r for r in rows if "de ana" in r]
        assert theirs, f"ninguna fila atribuida a Ana: {rows}"
        assert "✓" in theirs[0]


async def test_the_published_index_survives_a_remote_that_fails(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable remote must cost a missing mark, not a broken listing."""
    from textual.widgets import DataTable

    from multi_claude.project_remotes import RemoteLink

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.project_remotes.set_all(
            screen._remote_key(),
            [RemoteLink(kind="gitlab", host="http://127.0.0.1:1", repo="g/s", label="caido")],
        )
        await screen._refresh_remote_tabs()
        screen._populate()
        await _wait(pilot)

        # Sessions still listed, just without any shared mark.
        table = screen.query_one("#sessions", DataTable)
        assert table.row_count == 2
        assert screen._published == {}


# --- the publish dialogue ------------------------------------------------------------


async def test_the_publish_dialogue_offers_publish_not_delete(world: Path) -> None:
    """Regression: it reused the delete modal, so the accept button read "Borrar" in red."""
    from textual.widgets import Button

    from multi_claude.modals import PublishModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        await pilot.press("u")
        for _ in range(6):
            await pilot.pause()

        modal = app.screen
        assert isinstance(modal, PublishModal)
        confirm = modal.query_one("#publish", Button)
        assert str(confirm.label) == "Publicar"
        assert confirm.variant == "primary"  # not "error": nothing is being destroyed
        assert not modal.query("#confirm")  # the delete modal's button id is absent


async def test_the_publish_dialogue_lists_the_files_and_the_warning(world: Path) -> None:
    from multi_claude.modals import PublishModal

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_sessions(pilot)
        await pilot.press("u")
        for _ in range(6):
            await pilot.pause()

        modal = app.screen
        assert isinstance(modal, PublishModal)
        assert modal.files == ["· ses-1.jsonl"]

        # Read what is actually painted, once the modal has had frames to draw itself.
        for _ in range(10):
            await pilot.pause()
        rendered = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )
        flat = re.sub(r"[█▀▄▔▁▊▎▆▃]", " ", rendered)
        flat = re.sub(r"\s+", " ", flat)
        assert "Revisa que no haya secretos" in flat
        assert "ses-1.jsonl" in flat
        assert "Publicar" in flat


async def test_the_dialogue_starts_on_the_active_tabs_repo(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing from a repo's tab almost always means that repo."""
    from textual.widgets import RadioSet

    from multi_claude.modals import PublishModal
    from multi_claude.project_remotes import RemoteLink

    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        app.project_remotes.set_all(
            screen._remote_key(),
            [
                RemoteLink(kind="directory", path=str(world / "uno"), label="uno"),
                RemoteLink(kind="directory", path=str(world / "dos"), label="dos"),
            ],
        )
        await screen._refresh_remote_tabs()
        await pilot.pause()
        # Mark a local session first: a repo tab has no local rows to act on, and space
        # survives the tab switch.
        await pilot.press("space")
        await pilot.pause()
        await _open_remote_tab(pilot, screen, index=1)

        await pilot.press("u")
        for _ in range(6):
            await pilot.pause()
        modal = app.screen
        assert isinstance(modal, PublishModal)
        assert modal.preselected == 1
        pressed = modal.query_one("#publish-destination", RadioSet).pressed_button
        assert pressed is not None and pressed.id == "dest-1"
        assert modal.chosen().label == "dos"


async def test_a_single_destination_shows_no_chooser(world: Path) -> None:
    from multi_claude.modals import PublishModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        await pilot.press("u")
        for _ in range(6):
            await pilot.pause()

        modal = app.screen
        assert isinstance(modal, PublishModal)
        assert not modal.query("#publish-destination")  # nothing to choose between
        assert modal.chosen() is modal.destinations[0]


async def test_testing_a_server_does_not_freeze_the_ui(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the check ran on the UI thread and hung the app until it timed out.

    The probe is made deliberately slow, because the bug only shows when it takes a while —
    which over SSH meant up to two minutes of frozen terminal.
    """
    import time

    from textual.widgets import Input, Label, RadioButton

    from multi_claude import modals as modals_module
    from multi_claude.modals import ServerEditModal
    from multi_claude.project_remotes import RemoteServer

    def slow_probe(server: RemoteServer, token: str | None) -> tuple[str, bool]:
        time.sleep(1.0)
        return ("OK · tardío", True)

    monkeypatch.setattr(modals_module, "_probe_server", slow_probe)

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        # SSH on purpose: that is the case that froze, and it needs no token.
        modal = ServerEditModal(
            RemoteServer(name="lento", host="https://git.example.com", auth="ssh")
        )
        app.push_screen(modal)
        for _ in range(6):
            await pilot.pause()
        modal.query_one("#server-auth-ssh", RadioButton).value = True
        await pilot.pause()

        started = time.monotonic()
        modal.action_test()
        elapsed = time.monotonic() - started
        # Returned without waiting for the probe: that is the whole fix.
        assert elapsed < 0.5, f"action_test bloqueó {elapsed:.2f}s"

        await pilot.pause()
        assert "Probando" in str(modal.query_one("#server-status", Label).content)

        # And the UI still takes input while the probe is in flight.
        modal.query_one("#server-name", Input).value = "sigo escribiendo"
        await pilot.pause()
        assert modal.query_one("#server-name", Input).value == "sigo escribiendo"
        assert app.screen is modal

        # Eventually the result lands.
        for _ in range(60):
            await pilot.pause()
            if "tardío" in str(modal.query_one("#server-status", Label).content):
                break
        assert "tardío" in str(modal.query_one("#server-status", Label).content)


async def test_a_server_can_be_edited_and_renamed(world: Path) -> None:
    """Editing was only on a keybinding, and a rename added a second server instead."""
    from textual.widgets import Button, Input, RadioButton

    from multi_claude.modals import ServerEditModal, ServersModal
    from multi_claude.project_remotes import RemoteServer
    from multi_claude.remote import TokenStore

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        TokenStore().set("glpat-viejo", "Antiguo")
        servers = ServersModal([RemoteServer(name="Antiguo", host="https://git.viejo.com")])
        app.push_screen(servers)
        for _ in range(6):
            await pilot.pause()

        # There is a visible button, not just a keybinding.
        servers.query_one("#server-0", RadioButton).value = True
        await pilot.pause()
        servers.query_one("#edit", Button).press()
        for _ in range(8):
            await pilot.pause()

        editor = app.screen
        assert isinstance(editor, ServerEditModal)
        # It opened with the existing values, not empty.
        assert editor.query_one("#server-name", Input).value == "Antiguo"
        assert editor.query_one("#server-host", Input).value == "https://git.viejo.com"

        editor.query_one("#server-name", Input).value = "Nuevo nombre"
        await pilot.pause()
        editor.query_one("#save", Button).press()
        for _ in range(10):
            await pilot.pause()

        # Replaced, not duplicated.
        assert [s.name for s in servers._servers] == ["Nuevo nombre"]
        # And the token followed the rename, so existing links keep working.
        assert TokenStore().get("Nuevo nombre") == "glpat-viejo"
        assert TokenStore().get("Antiguo") is None


async def test_editing_without_a_selection_says_so(world: Path) -> None:
    from textual.widgets import Button

    from multi_claude.modals import ServersModal

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        servers = ServersModal([])
        app.push_screen(servers)
        for _ in range(6):
            await pilot.pause()

        servers.query_one("#edit", Button).press()
        await pilot.pause()
        # Nothing to edit and nothing crashed.
        assert isinstance(app.screen, ServersModal)
        assert servers._servers == []


# --- unpublishing from the UI ---------------------------------------------------------


async def test_d_on_a_published_row_unpublishes_it(world: Path) -> None:
    """`d` means delete-where-you-are: locally it deletes, on a published row it unpublishes."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        store = await _remote_store(app)
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        await _open_remote_tab(pilot, screen)
        assert len(store.list_sessions()) == 1

        from textual.widgets import DataTable

        table = screen.query_one("#sessions", DataTable)
        table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press("d")
        for _ in range(6):
            await pilot.pause()
        await pilot.press("y")  # confirm
        for _ in range(25):
            await pilot.pause()

        assert store.list_sessions() == ()
        # The local transcript is untouched.
        assert (screen.project.encoded_path / "ses-1.jsonl").is_file()


async def test_unpublishing_can_be_cancelled(world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        store = await _remote_store(app)
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        await _open_remote_tab(pilot, screen)

        from textual.widgets import DataTable

        screen.query_one("#sessions", DataTable).move_cursor(row=0)
        await pilot.pause()
        await pilot.press("d")
        for _ in range(6):
            await pilot.pause()
        await pilot.press("n")  # decline
        for _ in range(10):
            await pilot.pause()

        assert len(store.list_sessions()) == 1


async def test_the_unpublish_dialogue_says_the_local_copy_survives(world: Path) -> None:
    """People expect a delete to take their own copy with it; it must say otherwise."""
    import re as _re

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = await _open_sessions(pilot)
        await _publish(pilot)
        for _ in range(20):
            await pilot.pause()
        await _open_remote_tab(pilot, screen)

        from textual.widgets import DataTable

        screen.query_one("#sessions", DataTable).move_cursor(row=0)
        await pilot.pause()
        await pilot.press("d")
        for _ in range(10):
            await pilot.pause()

        painted = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )
        flat = _re.sub(r"\s+", " ", _re.sub(r"[█▀▄▔▁▊▎▆▃]", " ", painted))
        assert "Despublicar" in flat
        assert "copia local NO se borra" in flat


async def test_the_global_remote_can_be_switched_off(world: Path) -> None:
    """Once configured there was no way back: the dialogue offered no "off"."""
    from dataclasses import replace

    from textual.widgets import RadioButton

    from multi_claude.modals import RepoLinkModal

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        configured = replace(app.prefs, remote_kind="directory", remote_path=str(world / "global"))
        modal = RepoLinkModal(configured.remote_link(), servers=[], allow_none=True)
        app.push_screen(modal)
        for _ in range(8):
            await pilot.pause()

        modal.query_one("#target-none", RadioButton).value = True
        await pilot.pause()
        modal._try_save()
        for _ in range(6):
            await pilot.pause()

        # What it returns switches sharing off in the config.
        from multi_claude.project_remotes import RemoteLink

        off = configured.with_remote_link(RemoteLink())
        assert off.remote_kind == "none"
        assert not off.remote_link().is_configured


async def test_a_project_link_dialogue_offers_no_off_switch(world: Path) -> None:
    """A project link is removed from its list instead, where "Quitar" says exactly that."""
    from multi_claude.modals import RepoLinkModal
    from multi_claude.project_remotes import RemoteLink

    app = ClaudeBrowserApp()
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        modal = RepoLinkModal(RemoteLink(), servers=[])
        app.push_screen(modal)
        for _ in range(8):
            await pilot.pause()
        assert not modal.query("#target-none")
