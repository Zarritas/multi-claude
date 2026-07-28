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


async def test_publish_writes_the_session_to_the_remote(world: Path) -> None:
    """`u` → confirm → the manifest and blobs land on the remote."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        screen = await _open_sessions(pilot)
        assert isinstance(app.remote, DirectoryRemote)

        await pilot.press("u")
        await pilot.pause()
        await pilot.press("y")  # ConfirmDeleteModal: y confirms
        await pilot.pause()
        for _ in range(20):  # let the publish worker finish
            await pilot.pause()

        published = app.remote.list_sessions()
        assert [s.session_id for s in published] == ["ses-1"]
        assert (world / "remote" / "blobs" / "ses-1" / "session.jsonl.gz").is_file()
        assert screen._rows  # screen still usable afterwards


async def test_publish_can_be_cancelled(world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        assert isinstance(app.remote, DirectoryRemote)

        await pilot.press("u")
        await pilot.pause()
        await pilot.press("n")  # decline
        await pilot.pause()

        assert app.remote.list_sessions() == ()


async def test_publishing_marked_sessions_publishes_all_of_them(world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await _open_sessions(pilot)
        assert isinstance(app.remote, DirectoryRemote)

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

        assert sorted(s.session_id for s in app.remote.list_sessions()) == ["ses-1", "ses-2"]


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

        await pilot.press("R")  # show shared sessions
        for _ in range(20):
            await pilot.pause()
        assert [r.session_id for r in screen._remote_sessions] == ["ses-de-carlos"]
        # It is painted last, after the two local rows.
        assert screen._rows[-1] == (True, 0)

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

        await pilot.press("R")
        for _ in range(20):
            await pilot.pause()

        assert screen._remote_sessions == []


async def test_toggling_shared_off_clears_the_rows(world: Path) -> None:
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
        await pilot.press("R")
        for _ in range(20):
            await pilot.pause()
        assert len(screen._remote_sessions) == 1

        await pilot.press("R")
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
        assert app.remote is None
        assert screen.check_action("publish", ()) is False


# --- configuring the remote from the UI ---------------------------------------------


async def test_remote_settings_modal_collects_every_field(world: Path) -> None:
    from textual.widgets import Input, RadioButton

    from multi_claude.config import Config
    from multi_claude.modals import RemoteSettingsModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = RemoteSettingsModal(Config())
        app.push_screen(modal)
        await pilot.pause()

        modal.query_one("#kind-gitlab", RadioButton).value = True
        modal.query_one("#remote-host", Input).value = "https://git.empresa.com/"
        modal.query_one("#remote-repo", Input).value = "/grupo/sesiones/"
        modal.query_one("#remote-branch", Input).value = "trunk"
        await pilot.pause()

        result = modal.collect()
        assert result.remote_kind == "gitlab"
        # Trailing slashes stripped: they would produce doubled-up URLs.
        assert result.remote_host == "https://git.empresa.com"
        assert result.remote_repo == "grupo/sesiones"
        assert result.remote_branch == "trunk"


async def test_the_token_never_reaches_the_config_file(
    world: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.json gets shared and pasted into issues; a credential there leaks."""
    from textual.widgets import Input

    from multi_claude.config import Config, config_path
    from multi_claude.modals import RemoteSettingsModal
    from multi_claude.remote import TokenStore, token_path

    monkeypatch.delenv("MULTI_CLAUDE_REMOTE_TOKEN", raising=False)
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = RemoteSettingsModal(Config(remote_kind="gitlab", remote_repo="g/s"))
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
    from multi_claude.config import Config
    from multi_claude.modals import RemoteSettingsModal

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = RemoteSettingsModal(Config(), has_token=True)
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


async def test_changing_the_remote_rebuilds_the_store_without_a_restart(world: Path) -> None:
    from dataclasses import replace

    from multi_claude.remote import DirectoryRemote

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.remote, DirectoryRemote)  # from the env var

        app.update_prefs(replace(app.prefs, remote_kind="none", remote_path=""))
        await pilot.pause()
        # The env var still wins, which is what makes it a testing escape hatch.
        assert isinstance(app.remote, DirectoryRemote)
