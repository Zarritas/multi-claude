"""End-to-end TUI test using textual.pilot against a synthetic projects tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from multi_claude import discovery as discovery_module
from multi_claude.app import ClaudeBrowserApp
from multi_claude.launcher import LaunchOutcome
from multi_claude.names import NamesStore
from multi_claude.screens.projects import ProjectsScreen
from tests.conftest import settle, write_session


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
        edited_files=(str(beta_real / "src" / "widget.py"),),
    )

    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", projects_root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return projects_root


async def _enter_project(pilot: object) -> None:
    """Open the project under the cursor and wait until its sessions have arrived.

    The scan runs in a worker, so the screen exists before its rows do: asserting on
    ``_sessions`` after a single ``pause()`` is a race that passes or fails on how fast the
    machine is. It went unnoticed until the worker grew one more index read and CI, on a
    different Python than the one used locally, started losing it.

    Waiting for the rows instead of counting pauses is what makes these deterministic. The
    cap is there so a genuine regression fails the assertion below rather than hanging.
    """
    from textual.widgets import DataTable

    from multi_claude.screens.sessions import SessionsScreen

    app = pilot.app  # type: ignore[attr-defined]
    app.screen.query_one("#projects", DataTable).action_select_cursor()
    for _ in range(40):
        await settle(pilot, rounds=1)
        if isinstance(app.screen, SessionsScreen) and app.screen._sessions:
            return


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
        await _enter_project(pilot)

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
        from textual.widgets import DataTable

        from multi_claude.screens.projects import ProjectsScreen

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


async def test_every_project_gets_indexed_without_being_opened(
    synthetic_world: Path,
) -> None:
    """Global search used to answer only for the projects you happened to have entered.

    That is the one failure mode a search must not have: you cannot tell a result that is
    missing from a result that does not exist.
    """
    from multi_claude.index import default_index

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        for _ in range(60):
            await pilot.pause()
            if default_index().count_sessions() >= len(app.screen._projects):
                break
        # Never navigated into a single project, yet every session is in the index.
        assert isinstance(app.screen, ProjectsScreen)
        assert default_index().count_sessions() >= len(app.screen._projects)


async def test_the_background_pass_purges_rows_whose_file_is_gone(
    synthetic_world: Path,
) -> None:
    from multi_claude.index import IndexedSession, default_index

    index = default_index()
    index.upsert_session(
        IndexedSession(
            session_id="fantasma",
            project_dir="/nope",
            cwd=None,
            branch=None,
            first_prompt="ya no existo",
            message_count=1,
            size_bytes=1,
            mtime=1.0,
            jsonl_path="/nope/fantasma.jsonl",
        ),
        fts_content="ya no existo",
    )
    assert index.get("fantasma") is not None

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        for _ in range(60):
            await pilot.pause()
            if index.get("fantasma") is None:
                break
        assert index.get("fantasma") is None


async def test_session_only_filter_keys_match_nothing_in_projects(
    synthetic_world: Path,
) -> None:
    """`author:`/`tag:`/`id:`/`secrets:`/`file:` ask about a session; over projects, nothing.

    Showing every project would read as "none of these has that author" instead of "the
    question does not apply at this level".
    """
    from textual.widgets import Input

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        filter_input = app.screen.query_one("#filter", Input)
        keys = ("author:ana", "tag:infra", "id:abc", "branch:main", "secrets:yes", "file:a.py")
        for query in keys:
            filter_input.value = query
            await pilot.pause()
            assert app.screen._visible_indices == [], query


async def test_file_filter_finds_the_session_that_edited_it(synthetic_world: Path) -> None:
    """The listing's half of `file:`, end to end: scan, index, filter, rows on screen.

    ``ses-beta-2`` is the only synthetic session with an edit in it, so a correct filter
    leaves exactly one row and a broken one leaves either zero or both.
    """
    from textual.widgets import Input

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # -beta sorts first, and has the two sessions
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert len(screen._sessions) == 2

        await pilot.press("slash")
        await pilot.pause()
        filter_input = screen.query_one("#filter", Input)

        def shown() -> list[str]:
            return [screen._sessions[i].id for is_remote, i in screen._rows if not is_remote]

        filter_input.value = "file:widget.py"
        await pilot.pause()
        assert shown() == ["ses-beta-2"]

        filter_input.value = "file:src/widget.py"
        await pilot.pause()
        assert shown() == ["ses-beta-2"]

        filter_input.value = "file:nothing.py"
        await pilot.pause()
        assert shown() == []


async def test_a_background_repaint_does_not_move_the_cursor(synthetic_world: Path) -> None:
    """A scan finishing must not move the selection under the person using the list.

    Repaints are not only the user's doing: the credential scan and the published-sessions
    lookup land in the background and repaint when they do. ``DataTable.clear()`` sends the
    cursor to row 0, so without keeping it deliberately, a scan completing while someone is
    working the list moves their selection — a nuisance for ``Enter`` and a hazard for ``d``,
    which would then delete a session they were not looking at.
    """
    from textual.widgets import DataTable

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _enter_project(pilot)
        screen = app.screen
        table = screen.query_one("#sessions", DataTable)

        table.move_cursor(row=1)
        await pilot.pause()
        selected = screen._sessions[screen._rows[1][1]].id

        # What a worker does when it lands.
        screen._repaint()
        await pilot.pause()

        assert table.cursor_row == 1
        assert screen._sessions[screen._rows[table.cursor_row][1]].id == selected


async def test_marking_a_row_leaves_the_cursor_on_it(synthetic_world: Path) -> None:
    """`space` toggles, so a cursor that jumped would make the next one unmark the wrong row."""
    from textual.widgets import DataTable

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _enter_project(pilot)
        screen = app.screen
        table = screen.query_one("#sessions", DataTable)

        table.move_cursor(row=1)
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()

        assert table.cursor_row == 1
        assert len(screen._marked) == 1


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
        from textual.widgets import Input

        # Navigate into beta
        await _enter_project(pilot)

        from multi_claude.screens.sessions import SessionsScreen

        assert isinstance(app.screen, SessionsScreen)

        await pilot.press("slash")
        await pilot.pause()
        filter_input = app.screen.query_one("#filter", Input)
        filter_input.value = "another"
        await pilot.pause()
        # Only ses-beta-2 has "another" in its prompt
        assert len(app.screen._rows) == 1
        is_remote, index = app.screen._rows[0]
        assert not is_remote
        assert app.screen._sessions[index].id == "ses-beta-2"


async def test_rename_session_via_modal(synthetic_world: Path, tmp_path: Path) -> None:
    """`e` opens RenameModal; submitting writes to NamesStore."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input

        # Navigate into beta
        await _enter_project(pilot)

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
        named = next(s for s in app.screen._sessions if s.id == "ses-beta-1")
        assert named.display_name == "feature/login"


async def test_rename_session_empty_input_deletes_name(synthetic_world: Path) -> None:
    store = NamesStore()
    store.set("ses-beta-1", "old name")

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Input

        await _enter_project(pilot)
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

        await _enter_project(pilot)

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

        await _enter_project(pilot)

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

    def fake_launch(cwd, session_id, *, display_name=None, app=None, mode="auto", claude_args=None):
        captured["cwd"] = cwd
        captured["session_id"] = session_id
        captured["mode"] = mode
        captured["claude_args"] = claude_args
        return LaunchOutcome("window", "fake-emulator")

    with patch("multi_claude.screens.projects.launch_claude", side_effect=fake_launch):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

            from textual.widgets import Input

            from multi_claude.modals import AddProjectModal

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

        from textual.widgets import Input, Label

        from multi_claude.modals import AddProjectModal

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

    def fake_launch(cwd, session_id, *, display_name=None, app=None, mode="auto", claude_args=None):
        captured.append({"session_id": session_id, "mode": mode})
        return LaunchOutcome("window", "fake-emulator")

    with patch("multi_claude.screens.sessions.launch_claude", side_effect=fake_launch):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            # Drill into the first project (beta).
            from textual.widgets import DataTable

            from multi_claude.screens.projects import ProjectsScreen
            from multi_claude.screens.sessions import SessionsScreen

            assert isinstance(app.screen, ProjectsScreen)
            await _enter_project(pilot)
            assert isinstance(app.screen, SessionsScreen)
            # Wait for scan worker to populate sessions before activating a row.
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Default = "auto" → activating the row launches with auto.
            app.screen.query_one("#sessions", DataTable).action_select_cursor()
            await pilot.pause()

            # Shift+Enter → opposite of auto = "suspend".
            await pilot.press("shift+enter")
            await pilot.pause()

    assert [c["mode"] for c in captured] == ["auto", "suspend"]


async def test_settings_modal_persists_changes(synthetic_world: Path, tmp_path: Path) -> None:
    """Open settings, switch default to 'window', save → prefs and disk updated."""
    from multi_claude.config import load_config

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

        from textual.widgets import RadioButton

        from multi_claude.modals import SettingsModal

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


async def test_settings_modal_keeps_unrelated_prefs(synthetic_world: Path) -> None:
    """Regression: saving the launch mode must not reset sorts, preview or colours."""
    from dataclasses import replace

    from multi_claude.colors import ColorRule
    from multi_claude.config import SortSpec, load_config

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        rule = ColorRule(when="branch:hotfix", color="red")
        app.update_prefs(
            replace(
                app.prefs,
                projects_sort=SortSpec(key="name", descending=False),
                preview_visible=False,
                group_worktrees=False,
                color_rules=[rule],
            )
        )

        await pilot.press("s")
        await pilot.pause()

        from textual.widgets import Button, RadioButton

        from multi_claude.modals import SettingsModal

        assert isinstance(app.screen, SettingsModal)
        app.screen.query_one("#default-tab", RadioButton).value = True
        await pilot.pause()
        app.screen.query_one("#save", Button).press()
        await pilot.pause()

    persisted = load_config()
    assert persisted.default_mode == "tab"
    assert persisted.projects_sort == SortSpec(key="name", descending=False)
    assert persisted.preview_visible is False
    assert persisted.group_worktrees is False
    assert persisted.color_rules == [rule]


async def test_settings_modal_stores_claude_args(synthetic_world: Path) -> None:
    """The checkbox and the free-text field end up in prefs.claude_args."""
    from multi_claude.config import load_config

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

        from textual.widgets import Button, Checkbox, Input

        from multi_claude.modals import SettingsModal

        assert isinstance(app.screen, SettingsModal)
        app.screen.query_one("#skip-permissions", Checkbox).value = True
        app.screen.query_one("#claude-args", Input).value = "--model opus"
        await pilot.pause()
        app.screen.query_one("#save", Button).press()
        await pilot.pause()

    assert app.prefs.claude_args == ["--model", "opus", "--dangerously-skip-permissions"]
    assert load_config().claude_args == ["--model", "opus", "--dangerously-skip-permissions"]


async def test_settings_modal_rejects_reserved_flag(synthetic_world: Path) -> None:
    """Typing `--resume` keeps the modal open with an error instead of saving."""
    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

        from textual.widgets import Button, Input, Label

        from multi_claude.modals import SettingsModal

        assert isinstance(app.screen, SettingsModal)
        app.screen.query_one("#claude-args", Input).value = "--resume abc"
        await pilot.pause()
        app.screen.query_one("#save", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, SettingsModal)  # still open
        error = app.screen.query_one("#args-error", Label)
        assert "--resume" in str(error.render())

        await pilot.press("escape")
        await pilot.pause()

    assert app.prefs.claude_args == []


async def test_settings_modal_checkbox_reflects_stored_flag(synthetic_world: Path) -> None:
    """Reopening the modal shows the bypass flag as a checkbox, not as raw text."""
    from dataclasses import replace

    app = ClaudeBrowserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.update_prefs(
            replace(
                app.prefs,
                claude_args=["--permission-mode", "bypassPermissions", "--model", "opus"],
            )
        )
        await pilot.press("s")
        await pilot.pause()

        from textual.widgets import Checkbox, Input

        from multi_claude.modals import SettingsModal

        assert isinstance(app.screen, SettingsModal)
        assert app.screen.query_one("#skip-permissions", Checkbox).value is True
        assert app.screen.query_one("#claude-args", Input).value == "--model opus"


async def test_launch_passes_claude_args(synthetic_world: Path) -> None:
    """Configured extras reach launch_claude when a session is resumed."""
    from dataclasses import replace

    captured: list[dict] = []

    def fake_launch(cwd, session_id, *, display_name=None, app=None, mode="auto", claude_args=None):
        captured.append({"claude_args": claude_args})
        return LaunchOutcome("tab", "fake-emulator")

    with patch("multi_claude.screens.sessions.launch_claude", side_effect=fake_launch):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.update_prefs(replace(app.prefs, claude_args=["--dangerously-skip-permissions"]))

            from textual.widgets import DataTable

            from multi_claude.screens.sessions import SessionsScreen

            await _enter_project(pilot)
            assert isinstance(app.screen, SessionsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.screen.query_one("#sessions", DataTable).action_select_cursor()
            await pilot.pause()

    assert captured == [{"claude_args": ["--dangerously-skip-permissions"]}]


async def test_sessions_screen_shows_live_status(synthetic_world: Path) -> None:
    """The Estado column reports what the live registry says each session is doing."""
    from multi_claude.focus import LiveSession

    registry = {
        "ses-beta-1": LiveSession(session_id="ses-beta-1", pid=11, status="busy"),
        "ses-beta-2": LiveSession(session_id="ses-beta-2", pid=22, status="waiting"),
    }
    with patch("multi_claude.screens.sessions.live_sessions", return_value=registry):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            from multi_claude.screens.sessions import SessionsScreen

            await _enter_project(pilot)
            assert isinstance(app.screen, SessionsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = app.screen.query_one("#sessions", DataTable)
            statuses = {
                app.screen._sessions[idx].id: _cell_text(table, row, 1)
                for row, (is_remote, idx) in enumerate(app.screen._rows)
                if not is_remote
            }
    assert statuses == {"ses-beta-1": "● trabajando", "ses-beta-2": "○ te espera"}


async def test_sessions_screen_labels_an_unknown_status_generically(
    synthetic_world: Path,
) -> None:
    """Claude Code's status vocabulary can grow; an unmapped value still reads as live."""
    from multi_claude.focus import LiveSession

    registry = {"ses-beta-1": LiveSession(session_id="ses-beta-1", pid=11, status="compacting")}
    with patch("multi_claude.screens.sessions.live_sessions", return_value=registry):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            await _enter_project(pilot)
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one("#sessions", DataTable)
            row = next(r for r, (_, idx) in enumerate(app.screen._rows) if idx == 0)
            assert app.screen._sessions[0].id == "ses-beta-1"
            assert _cell_text(table, row, 1) == "● abierta"


async def test_sessions_screen_marks_unregistered_sessions_as_not_running(
    synthetic_world: Path,
) -> None:
    with patch("multi_claude.screens.sessions.live_sessions", return_value={}):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            await _enter_project(pilot)
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one("#sessions", DataTable)
            assert _cell_text(table, 0, 1) == "—"


async def test_live_status_refresh_updates_the_cell_in_place(synthetic_world: Path) -> None:
    """A status change rewrites its cell without repainting (the cursor must survive)."""
    from multi_claude.focus import LiveSession

    def _registry(status: str, sessions: Any) -> dict[str, LiveSession]:
        return {s.id: LiveSession(session_id=s.id, pid=1, status=status) for s in sessions}

    with patch("multi_claude.screens.sessions.live_sessions", return_value={}):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable

            from multi_claude.screens.sessions import SessionsScreen

            await _enter_project(pilot)
            screen = app.screen
            assert isinstance(screen, SessionsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = screen.query_one("#sessions", DataTable)
            # Both sessions start out working, then the cursor is parked on the second row.
            screen._on_live_refresh(_registry("busy", screen._sessions))
            await pilot.pause()
            table.move_cursor(row=1)
            await pilot.pause()
            assert _cell_text(table, 0, 1) == "● trabajando"

            # Same set of live sessions, new status → cells rewritten, cursor untouched.
            screen._on_live_refresh(_registry("waiting", screen._sessions))
            await pilot.pause()
            assert _cell_text(table, 0, 1) == "○ te espera"
            assert table.cursor_row == 1

            # A session going away does change row colours and ordering, so that repaints.
            screen._on_live_refresh({})
            await pilot.pause()
            assert _cell_text(table, 0, 1) == "—"


async def test_sort_by_status_puts_waiting_sessions_first(synthetic_world: Path) -> None:
    from multi_claude.focus import LiveSession

    registry = {"ses-beta-2": LiveSession(session_id="ses-beta-2", pid=22, status="waiting")}
    with patch("multi_claude.screens.sessions.live_sessions", return_value=registry):
        app = ClaudeBrowserApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            from multi_claude.screens.sessions import SessionsScreen

            await _enter_project(pilot)
            screen = app.screen
            assert isinstance(screen, SessionsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            # Default order is by recency: ses-beta-1 (mtime 3000) leads.
            assert screen._sessions[0].id == "ses-beta-1"

            await pilot.press("2")
            await pilot.pause()

            assert app.prefs.sessions_sort.key == "status"
            assert screen._sessions[0].id == "ses-beta-2"

            # While sorted by status, a status change has to reorder the rows, not
            # just rewrite a cell: leaving them in place would be a listing that
            # claims an order it no longer has.
            screen._on_live_refresh(
                {"ses-beta-1": LiveSession(session_id="ses-beta-1", pid=11, status="waiting")}
            )
            await pilot.pause()
            assert screen._sessions[0].id == "ses-beta-1"


def _cell_text(table: Any, row: int, column: int) -> str:
    """Plain text of a painted cell, whether it was styled or not."""
    from textual.coordinate import Coordinate

    value = table.get_cell_at(Coordinate(row, column))
    return str(value.plain) if hasattr(value, "plain") else str(value)
