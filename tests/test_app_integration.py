"""End-to-end TUI test using textual.pilot against a synthetic projects tree."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from multi_claude import discovery as discovery_module
from multi_claude.app import ClaudeBrowserApp
from multi_claude.names import NamesStore

from tests.conftest import write_session


@pytest.fixture
def synthetic_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a projects tree and redirect CLAUDE_PROJECTS_DIR + NamesStore."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    alpha_real = tmp_path / "alpha"
    alpha_real.mkdir()
    write_session(
        projects_root / "-alpha",
        session_id="ses-alpha-1",
        cwd=str(alpha_real),
        branch="main",
        first_prompt="<command-name>/refine-task</command-name><command-args>foo</command-args>",
        mtime=2000.0,
    )

    beta_real = tmp_path / "beta"
    beta_real.mkdir()
    write_session(
        projects_root / "-beta",
        session_id="ses-beta-1",
        cwd=str(beta_real),
        branch="feature/x",
        first_prompt="plain prompt",
        mtime=3000.0,
    )
    write_session(
        projects_root / "-beta",
        session_id="ses-beta-2",
        cwd=str(beta_real),
        branch="main",
        first_prompt="another beta prompt",
        mtime=2500.0,
    )

    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", projects_root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return projects_root


async def test_app_lists_projects_and_navigates(synthetic_world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # ProjectsScreen mounted
        from multi_claude.screens.projects import ProjectsScreen

        projects_screen = app.screen
        assert isinstance(projects_screen, ProjectsScreen)
        assert len(projects_screen._projects) == 2
        # beta is more recent → first row
        assert projects_screen._projects[0].name == "beta"

        # Select first row → SessionsScreen pushed
        from textual.widgets import DataTable

        table = projects_screen.query_one("#projects", DataTable)
        table.action_select_cursor()
        await pilot.pause()

        from multi_claude.screens.sessions import SessionsScreen

        assert isinstance(app.screen, SessionsScreen)
        assert app.screen.project.name == "beta"
        assert len(app.screen._sessions) == 2
        # Newest first → ses-beta-1 (mtime 3000) before ses-beta-2 (mtime 2500)
        assert app.screen._sessions[0].id == "ses-beta-1"

        # Back to ProjectsScreen with escape
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ProjectsScreen)


async def test_app_handles_empty_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", empty)

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from multi_claude.screens.projects import ProjectsScreen

        assert isinstance(app.screen, ProjectsScreen)
        assert app.screen._projects == []


async def test_app_orphan_project_blocks_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    write_session(
        projects_root / "-gone",
        cwd="/this/does/not/exist/anywhere/multi-claude-test",
    )
    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", projects_root)

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from multi_claude.screens.projects import ProjectsScreen
        from textual.widgets import DataTable

        assert isinstance(app.screen, ProjectsScreen)
        assert app.screen._projects[0].is_orphan is True

        table = app.screen.query_one("#projects", DataTable)
        table.action_select_cursor()
        await pilot.pause()

        # Should NOT have navigated away (still on ProjectsScreen)
        assert isinstance(app.screen, ProjectsScreen)


async def test_filter_in_projects_screen(synthetic_world: Path) -> None:
    """`/` opens filter input; typing narrows the visible rows."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        # filter input is now visible and focused
        from textual.widgets import Input

        filter_input = app.screen.query_one("#filter", Input)
        assert filter_input.display is True
        assert filter_input.has_focus

        # type "alpha" → only alpha project visible
        filter_input.value = "alpha"
        await pilot.pause()
        assert app.screen._visible_indices == [
            i for i, p in enumerate(app.screen._projects) if "alpha" in p.name.lower()
        ]
        assert len(app.screen._visible_indices) == 1

        # Escape clears the filter
        await pilot.press("escape")
        await pilot.pause()
        assert filter_input.display is False
        assert len(app.screen._visible_indices) == len(app.screen._projects)


async def test_filter_keeps_focus_on_input_while_typing(synthetic_world: Path) -> None:
    """Regression: filtering on each keystroke must not steal focus from the input."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input

        await pilot.press("slash")
        await pilot.pause()
        filter_input = app.screen.query_one("#filter", Input)
        assert filter_input.has_focus

        # Type a multi-char word one key at a time. Focus must stay on the input.
        for ch in ["a", "l", "p", "h", "a"]:
            await pilot.press(ch)
            await pilot.pause()
            assert filter_input.has_focus, (
                f"focus stolen after typing '{ch}'; value so far: {filter_input.value!r}"
            )
        assert filter_input.value == "alpha"


async def test_filter_in_sessions_screen(synthetic_world: Path) -> None:
    """`/` works inside SessionsScreen too."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable, Input

        # Navigate into beta
        table = app.screen.query_one("#projects", DataTable)
        table.action_select_cursor()
        await pilot.pause()

        from multi_claude.screens.sessions import SessionsScreen
        assert isinstance(app.screen, SessionsScreen)

        await pilot.press("slash")
        await pilot.pause()
        filter_input = app.screen.query_one("#filter", Input)
        filter_input.value = "another"
        await pilot.pause()
        # Only ses-beta-2 has "another" in its prompt
        assert len(app.screen._visible_indices) == 1
        assert app.screen._sessions[app.screen._visible_indices[0]].id == "ses-beta-2"


async def test_rename_session_via_modal(synthetic_world: Path, tmp_path: Path) -> None:
    """`e` opens RenameModal; submitting writes to NamesStore."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable, Input

        # Navigate into beta
        app.screen.query_one("#projects", DataTable).action_select_cursor()
        await pilot.pause()

        from multi_claude.screens.sessions import SessionsScreen
        assert isinstance(app.screen, SessionsScreen)

        # cursor on first row (ses-beta-1). Press e to rename.
        await pilot.press("e")
        await pilot.pause()

        from multi_claude.modals import RenameModal
        assert isinstance(app.screen, RenameModal)
        modal_input = app.screen.query_one("#name-input", Input)
        modal_input.value = "feature/login"
        await pilot.press("enter")
        await pilot.pause()

        # Back in SessionsScreen, store has the name
        assert isinstance(app.screen, SessionsScreen)
        store = NamesStore()  # picks up XDG from monkeypatched env
        assert store.get("ses-beta-1") == "feature/login"
        # Session in memory was reloaded with the name
        named = [s for s in app.screen._sessions if s.id == "ses-beta-1"][0]
        assert named.display_name == "feature/login"


async def test_rename_session_empty_input_deletes_name(synthetic_world: Path) -> None:
    store = NamesStore()
    store.set("ses-beta-1", "old name")

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable, Input
        app.screen.query_one("#projects", DataTable).action_select_cursor()
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        modal_input = app.screen.query_one("#name-input", Input)
        modal_input.value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert NamesStore().get("ses-beta-1") is None


async def test_delete_session_with_confirmation(synthetic_world: Path) -> None:
    """`d` opens ConfirmDeleteModal; `y` deletes the session."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        app.screen.query_one("#projects", DataTable).action_select_cursor()
        await pilot.pause()

        from multi_claude.screens.sessions import SessionsScreen
        sessions_screen = app.screen
        assert isinstance(sessions_screen, SessionsScreen)
        initial_count = len(sessions_screen._sessions)
        first_session = sessions_screen._sessions[0]

        await pilot.press("d")
        await pilot.pause()

        from multi_claude.modals import ConfirmDeleteModal
        assert isinstance(app.screen, ConfirmDeleteModal)

        await pilot.press("y")
        await pilot.pause()

        assert isinstance(app.screen, SessionsScreen)
        assert len(app.screen._sessions) == initial_count - 1
        assert not first_session.path.exists()


async def test_delete_session_cancel_keeps_session(synthetic_world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        app.screen.query_one("#projects", DataTable).action_select_cursor()
        await pilot.pause()

        from multi_claude.screens.sessions import SessionsScreen
        sessions_screen = app.screen
        assert isinstance(sessions_screen, SessionsScreen)
        initial_count = len(sessions_screen._sessions)

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("escape")  # cancel
        await pilot.pause()

        assert isinstance(app.screen, SessionsScreen)
        assert len(app.screen._sessions) == initial_count


async def test_delete_project_with_confirmation(synthetic_world: Path) -> None:
    """`d` on ProjectsScreen wipes the project directory."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        initial_projects = list(app.screen._projects)
        encoded_paths = [p.encoded_path for p in initial_projects]

        await pilot.press("d")
        await pilot.pause()

        from multi_claude.modals import ConfirmDeleteModal
        assert isinstance(app.screen, ConfirmDeleteModal)
        await pilot.press("y")
        await pilot.pause()

        from multi_claude.screens.projects import ProjectsScreen
        assert isinstance(app.screen, ProjectsScreen)
        assert len(app.screen._projects) == len(initial_projects) - 1
        # The cursor was on row 0 (most recent → beta), so beta was deleted
        deleted = [p for p in encoded_paths if not p.exists()]
        remaining = [p for p in encoded_paths if p.exists()]
        assert len(deleted) == 1
        assert len(remaining) == len(initial_projects) - 1


async def test_add_project_invokes_launcher(synthetic_world: Path, tmp_path: Path) -> None:
    """`a` opens AddProjectModal; valid path → launch_claude is called."""
    new_real = tmp_path / "newproj"
    new_real.mkdir()

    captured: dict = {}

    def fake_launch(cwd, session_id, *, display_name=None, app=None, mode="auto"):
        captured["cwd"] = cwd
        captured["session_id"] = session_id
        captured["mode"] = mode

    with patch("multi_claude.screens.projects.launch_claude", side_effect=fake_launch):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

            from multi_claude.modals import AddProjectModal
            from textual.widgets import Input
            assert isinstance(app.screen, AddProjectModal)
            modal_input = app.screen.query_one("#path-input", Input)
            modal_input.value = str(new_real)
            await pilot.press("enter")
            await pilot.pause()

    assert captured["cwd"] == new_real.resolve()
    assert captured["session_id"] is None


async def test_add_project_rejects_missing_path(synthetic_world: Path, tmp_path: Path) -> None:
    """A non-existent path keeps the modal open with an error message."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()

        from multi_claude.modals import AddProjectModal
        from textual.widgets import Input, Label
        assert isinstance(app.screen, AddProjectModal)
        modal_input = app.screen.query_one("#path-input", Input)
        modal_input.value = str(tmp_path / "definitely-not-here")
        await pilot.press("enter")
        await pilot.pause()

        # Still on the modal; error label populated
        assert isinstance(app.screen, AddProjectModal)
        err = app.screen.query_one("#error", Label)
        assert "No existe" in str(err.content)


async def test_ctrl_q_quits_app(synthetic_world: Path) -> None:
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
    # If we reach here without hanging, the app exited cleanly.
    assert app._exit is True or not app.is_running


async def test_enter_uses_default_mode_and_shift_enter_uses_opposite(
    synthetic_world: Path,
) -> None:
    """Enter → prefs.default_mode; Shift+Enter → alternate_for(default)."""
    captured: list[dict] = []

    def fake_launch(cwd, session_id, *, display_name=None, app=None, mode="auto"):
        captured.append({"session_id": session_id, "mode": mode})

    with patch("multi_claude.screens.sessions.launch_claude", side_effect=fake_launch):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # Drill into the first project (beta).
            from textual.widgets import DataTable

            from multi_claude.screens.projects import ProjectsScreen
            from multi_claude.screens.sessions import SessionsScreen

            assert isinstance(app.screen, ProjectsScreen)
            app.screen.query_one("#projects", DataTable).action_select_cursor()
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)

            # Default = "auto" → Enter launches auto.
            app.screen.query_one("#sessions", DataTable).action_select_cursor()
            await pilot.pause()

            # Shift+Enter → opposite of auto = "suspend".
            await pilot.press("shift+enter")
            await pilot.pause()

    assert [c["mode"] for c in captured] == ["auto", "suspend"]


async def test_settings_modal_persists_changes(
    synthetic_world: Path, tmp_path: Path
) -> None:
    """Open settings, switch default to 'window', save → prefs and disk updated."""
    from multi_claude.config import load_config

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

        from multi_claude.modals import SettingsModal
        from textual.widgets import RadioButton

        assert isinstance(app.screen, SettingsModal)
        # Select "window" in the default-mode set.
        app.screen.query_one("#default-window", RadioButton).value = True
        await pilot.pause()
        # Click save.
        from textual.widgets import Button

        app.screen.query_one("#save", Button).press()
        await pilot.pause()

    assert app.prefs.default_mode == "window"
    # And it was persisted to disk under XDG_CONFIG_HOME (set by the fixture).
    persisted = load_config()
    assert persisted.default_mode == "window"
