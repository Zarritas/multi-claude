"""End-to-end TUI tests for publishing and hydrating shared sessions.

Drives the real screens through ``textual.pilot`` against a synthetic projects tree and
a directory-backed remote, so the wiring (bindings → worker → store → launch) is what is
under test, not a mock of it.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from multi_claude import discovery as discovery_module
from multi_claude.app import ClaudeBrowserApp
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


async def _open_sessions(pilot: object) -> SessionsScreen:
    """Navigate ProjectsScreen → SessionsScreen and return it."""
    from textual.widgets import DataTable

    await pilot.pause()  # type: ignore[attr-defined]
    table = pilot.app.screen.query_one("#projects", DataTable)  # type: ignore[attr-defined]
    table.action_select_cursor()
    await pilot.pause()  # type: ignore[attr-defined]
    screen = pilot.app.screen  # type: ignore[attr-defined]
    assert isinstance(screen, SessionsScreen)
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

        await pilot.press("u")
        await pilot.pause()
        await pilot.press("y")  # ConfirmDeleteModal: y confirms
        await pilot.pause()
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
        await pilot.pause()
        await pilot.press("n")  # decline
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
        await pilot.press("u")
        await pilot.pause()
        await pilot.press("y")
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


async def test_already_local_sessions_are_not_offered_as_shared(world: Path) -> None:
    """Your own publications must not come back as duplicate cloud rows."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)

        await pilot.press("u")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(20):
            await pilot.pause()

        await _open_remote_tab(pilot, screen)

        assert screen._remote_sessions == []


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


async def test_remote_settings_modal_collects_every_field(world: Path) -> None:
    from textual.widgets import Input, RadioButton

    from multi_claude.modals import RemoteSettingsModal
    from multi_claude.project_remotes import RemoteLink

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = RemoteSettingsModal(RemoteLink())
        app.push_screen(modal)
        await pilot.pause()

        modal.query_one("#kind-gitlab", RadioButton).value = True
        modal.query_one("#remote-host", Input).value = "https://git.empresa.com/"
        modal.query_one("#remote-repo", Input).value = "/grupo/sesiones/"
        modal.query_one("#remote-branch", Input).value = "trunk"
        await pilot.pause()

        result = modal.collect()
        assert result.kind == "gitlab"
        # Trailing slashes stripped: they would produce doubled-up URLs.
        assert result.host == "https://git.empresa.com"
        assert result.repo == "grupo/sesiones"
        assert result.branch == "trunk"
        # The tab falls back to the repo's last segment when no label is typed.
        assert result.tab_label() == "sesiones"


async def test_the_token_never_reaches_the_config_file(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.json gets shared and pasted into issues; a credential there leaks."""
    from textual.widgets import Input

    from multi_claude.config import config_path
    from multi_claude.modals import RemoteSettingsModal
    from multi_claude.project_remotes import RemoteLink
    from multi_claude.remote import TokenStore, token_path

    monkeypatch.delenv("MULTI_CLAUDE_REMOTE_TOKEN", raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = RemoteSettingsModal(RemoteLink(kind="gitlab", repo="g/s"))
        app.push_screen(modal)
        await pilot.pause()
        modal.query_one("#remote-token", Input).value = "glpat-muy-secreto"
        await pilot.pause()

        collected = modal.collect()
        token = modal.token_to_save()
        assert token == "glpat-muy-secreto"
        # The token is absent from every field of the config that gets serialised.
        assert "glpat-muy-secreto" not in json.dumps(collected.to_dict())

        TokenStore().set(token or "")
        app.update_prefs(collected)
        await pilot.pause()

        assert "glpat-muy-secreto" not in config_path().read_text(encoding="utf-8")
        assert TokenStore().get() == "glpat-muy-secreto"
        assert stat.S_IMODE(token_path().stat().st_mode) == 0o600


async def test_an_empty_token_field_keeps_the_stored_one(world: Path) -> None:
    """Reopening the modal must not wipe the saved token just by saving again."""
    from multi_claude.modals import RemoteSettingsModal
    from multi_claude.project_remotes import RemoteLink

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = RemoteSettingsModal(RemoteLink(), has_token=True)
        app.push_screen(modal)
        await pilot.pause()
        assert modal.token_to_save() is None  # None means "leave it alone"


async def test_settings_shows_the_remote_summary_and_opens_the_remote_modal(world: Path) -> None:
    from textual.widgets import Button, Label

    from multi_claude.modals import RemoteSettingsModal, SettingsModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        settings = SettingsModal(app.prefs)
        app.push_screen(settings)
        await pilot.pause()

        assert "desactivado" in str(settings.query_one("#remote-summary", Label).content)
        settings.query_one("#configure-remote", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, RemoteSettingsModal)


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


async def test_publishing_from_the_local_tab_needs_an_unambiguous_target(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With several repos linked, guessing could publish a client's session to the wrong one."""
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

        # On the local tab with two candidates: no target, so nothing is published.
        assert screen._publish_target() is None

        # On a remote tab the target is that tab.
        await _open_remote_tab(pilot, screen, index=1)
        target = screen._publish_target()
        assert target is not None and target.label == "b"


async def test_a_single_linked_repo_is_an_unambiguous_target(
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

        target = screen._publish_target()
        assert target is not None and target.path == str(world / "solo")


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
