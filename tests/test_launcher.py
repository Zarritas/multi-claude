"""Tests for multi_claude.launcher."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from multi_claude.launcher import (
    LauncherError,
    LaunchOutcome,
    _argv_ghostty,
    _build_claude_argv,
    detect_multiplexer,
    detect_terminal_emulator,
    launch_claude,
    preview_dispatch,
)


def _p(posix_path: str) -> str:
    """Return the platform-native string form of a POSIX-style test path.

    Tests build paths from literal POSIX strings (e.g. ``/work/x``) for readability,
    but the launcher passes them through ``str(Path(...))`` which yields ``\\work\\x``
    on Windows. This helper produces the expected on-disk representation.
    """
    return str(Path(posix_path))


# Env vars that signal a terminal emulator. Cleared in tests that want
# detect_terminal_emulator() to return None so we can isolate other branches.
_EMULATOR_ENVS = (
    "KITTY_PID",
    "WEZTERM_EXECUTABLE",
    "ALACRITTY_WINDOW_ID",
    "ALACRITTY_LOG",
    "KONSOLE_VERSION",
    "GNOME_TERMINAL_SCREEN",
    "FOOT_VERSION",
    "TERM_PROGRAM",
    "GHOSTTY_RESOURCES_DIR",
    "GHOSTTY_BIN_DIR",
    "VSCODE_INJECTION",
    "WT_SESSION",
    "ConEmuPID",
)


class _MuxRun:
    """Stand-in for ``subprocess.run`` that records calls and reports success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(argv)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()


def _clear_mux_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TMUX", "ZELLIJ", "TERMINATOR_UUID"):
        monkeypatch.delenv(var, raising=False)


def _clear_emulator_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _EMULATOR_ENVS:
        monkeypatch.delenv(var, raising=False)
    # TERMINATOR_UUID is shared with mux detection but also used by emulator detection.
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)


def test_build_argv_without_session() -> None:
    assert _build_claude_argv(None) == ["claude"]


def test_build_argv_with_session() -> None:
    assert _build_claude_argv("abc-123") == ["claude", "--resume", "abc-123"]


def test_build_argv_with_display_name() -> None:
    assert _build_claude_argv(None, "Mi feature") == ["claude", "-n", "Mi feature"]


def test_build_argv_with_session_and_display_name() -> None:
    assert _build_claude_argv("abc", "X") == ["claude", "--resume", "abc", "-n", "X"]


def test_detect_multiplexer_prefers_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-x/default,123,0")
    monkeypatch.setenv("ZELLIJ", "1")
    with patch("multi_claude.launcher.shutil.which", return_value="/usr/bin/tmux"):
        assert detect_multiplexer() == "tmux"


def test_detect_multiplexer_zellij_when_no_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("ZELLIJ", "1")
    with patch("multi_claude.launcher.shutil.which", return_value="/usr/bin/zellij"):
        assert detect_multiplexer() == "zellij"


def test_detect_multiplexer_terminator_when_no_tmux_no_zellij(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")
    with patch("multi_claude.launcher.shutil.which", return_value="/usr/bin/terminator"):
        assert detect_multiplexer() == "terminator"


def test_detect_multiplexer_tmux_wins_over_terminator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")
    with patch("multi_claude.launcher.shutil.which", return_value="/usr/bin/tmux"):
        assert detect_multiplexer() == "tmux"


def test_detect_multiplexer_none_when_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)
    assert detect_multiplexer() is None


def test_detect_multiplexer_none_when_env_set_but_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)
    with patch("multi_claude.launcher.shutil.which", return_value=None):
        assert detect_multiplexer() is None


def test_launch_claude_raises_when_no_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("multi_claude.launcher.shutil.which", return_value=None):
        with pytest.raises(LauncherError):
            launch_claude(Path("/tmp"), "id")


def test_launch_claude_uses_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        launch_claude(Path("/work/x"), "sid-1")

    assert runner.calls == [
        ["tmux", "split-window", "-h", "-c", _p("/work/x"), "claude", "--resume", "sid-1"]
    ]


def test_launch_claude_uses_zellij(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("ZELLIJ", "x")
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        launch_claude(Path("/work/y"), None)

    assert runner.calls == [
        ["zellij", "action", "new-pane", "--cwd", _p("/work/y"), "--", "claude"]
    ]


def test_launch_claude_terminator_splits_with_remotinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto`/`split` inside Terminator opens a real pane over DBus, not a tab."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        outcome = launch_claude(Path("/work/t"), "sid-3", display_name="Mi feature")

    assert runner.calls == [
        [
            "remotinator",
            "vsplit",
            "-x",
            f"cd '{_p('/work/t')}' && exec 'claude' '--resume' 'sid-3' '-n' 'Mi feature'",
        ]
    ]
    assert outcome == LaunchOutcome("split", "terminator")


def test_launch_claude_uses_terminator_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tab` mode still goes through the CLI, which reuses the window over DBus."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        outcome = launch_claude(Path("/work/t"), "sid-3", display_name="Mi feature", mode="tab")

    assert runner.calls == [
        [
            "terminator",
            "--new-tab",
            f"--working-directory={_p('/work/t')}",
            "-x",
            "claude",
            "--resume",
            "sid-3",
            "-n",
            "Mi feature",
        ]
    ]
    assert outcome == LaunchOutcome("tab", "terminator")


def test_launch_claude_terminator_without_remotinator_falls_back_to_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `remotinator` in PATH → the pane degrades to a tab and says so."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")

    def fake_which(cmd: str) -> str | None:
        return None if cmd == "remotinator" else f"/usr/bin/{cmd}"

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        outcome = launch_claude(Path("/work/t"), "sid-3", mode="split")

    assert runner.calls == [
        [
            "terminator",
            "--new-tab",
            f"--working-directory={_p('/work/t')}",
            "-x",
            "claude",
            "--resume",
            "sid-3",
        ]
    ]
    assert outcome.placement == "tab"
    assert outcome.fallback_reason is not None and "remotinator" in outcome.fallback_reason


def test_launch_claude_terminator_pane_error_on_stdout_falls_back_to_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remotinator reports some failures as `ERROR: ...` on stdout with exit 0."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        rejected = argv[0] == "remotinator"

        class _Result:
            returncode = 0
            stdout = "ERROR: Terminal with supplied UUID not found\n" if rejected else ""
            stderr = ""

        return _Result()

    with (
        patch(
            "multi_claude.launcher.shutil.which",
            side_effect=lambda cmd: f"/usr/bin/{cmd}",
        ),
        patch("multi_claude.launcher.subprocess.run", side_effect=fake_run),
    ):
        outcome = launch_claude(Path("/work/t"), "sid-3", mode="split")

    assert [c[0] for c in calls] == ["remotinator", "terminator"]
    assert outcome.placement == "tab"
    assert outcome.fallback_reason is not None and "UUID" in outcome.fallback_reason


def test_launch_claude_tmux_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If tmux exits non-zero, the launcher reports the error instead of silently failing."""
    monkeypatch.setenv("TMUX", "x")
    _clear_emulator_envs(monkeypatch)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    def failing_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        class _Result:
            returncode = 1
            stderr = "tmux: server not running\n"

        return _Result()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=failing_run),
        pytest.raises(LauncherError, match="tmux: server not running"),
    ):
        launch_claude(Path("/work"), "sid")


def test_launch_claude_fallback_runs_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No multiplexer, no detectable emulator → suspend (inline run)."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)

    def fake_which(cmd: str) -> str | None:
        # Only `claude` exists; nothing else (no emulator binaries either).
        return "/usr/bin/claude" if cmd == "claude" else None

    calls = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs.get("cwd")))

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=fake_run),
    ):
        launch_claude(Path("/work/z"), "sid-2", app=None)

    assert calls == [(["claude", "--resume", "sid-2"], _p("/work/z"))]


def test_launch_claude_window_mode_uses_kitty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("KITTY_PID", "1234")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "kitty") else None

    popen_calls: list[tuple] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append((argv, kwargs))

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        launch_claude(Path("/work/k"), "sid-k", mode="window")

    assert popen_calls == [
        (
            ["kitty", "--directory", _p("/work/k"), "claude", "--resume", "sid-k"],
            {
                "start_new_session": True,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]


def test_launch_claude_window_mode_falls_back_to_suspend_when_no_emulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)

    def fake_which(cmd: str) -> str | None:
        return "/usr/bin/claude" if cmd == "claude" else None

    calls: list[tuple] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs.get("cwd")))

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=fake_run),
    ):
        launch_claude(Path("/work/q"), "sid-q", mode="window")

    assert calls == [(["claude", "--resume", "sid-q"], _p("/work/q"))]


def test_launch_claude_suspend_mode_skips_multiplexer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even inside tmux, mode='suspend' must run inline."""
    monkeypatch.setenv("TMUX", "x")
    _clear_emulator_envs(monkeypatch)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    calls: list[tuple] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs.get("cwd")))

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=fake_run),
    ):
        launch_claude(Path("/work/s"), None, mode="suspend")

    assert calls == [(["claude"], _p("/work/s"))]


def test_detect_emulator_prefers_term_program_ghostty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "ghostty" else None,
    ):
        emu = detect_terminal_emulator()
        assert emu is not None and emu.id == "ghostty"


def test_detect_emulator_term_program_wezterm(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")  # case-insensitive
    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "wezterm" else None,
    ):
        emu = detect_terminal_emulator()
        assert emu is not None and emu.id == "wezterm"


def test_detect_emulator_term_program_beats_xterm_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if x-terminal-emulator/xterm exist, TERM_PROGRAM=ghostty wins."""
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda cmd: f"/usr/bin/{cmd}",  # everything exists
    ):
        emu = detect_terminal_emulator()
        assert emu is not None and emu.id == "ghostty"


def test_detect_emulator_ghostty_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """If TERM_PROGRAM is missing but GHOSTTY_RESOURCES_DIR is set, still detect."""
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("GHOSTTY_RESOURCES_DIR", "/snap/ghostty/current/share/ghostty")
    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "ghostty" else None,
    ):
        emu = detect_terminal_emulator()
        assert emu is not None and emu.id == "ghostty"


def test_detect_emulator_vscode_degrades_to_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VS Code's integrated terminal is detectable but cannot spawn new windows."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")

    calls: list[tuple] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs.get("cwd")))

    with (
        patch(
            "multi_claude.launcher.shutil.which",
            side_effect=lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
        ),
        patch("multi_claude.launcher.subprocess.run", side_effect=fake_run),
    ):
        outcome = launch_claude(Path("/work/v"), "sid-v", mode="window")

    assert calls == [(["claude", "--resume", "sid-v"], _p("/work/v"))]
    assert outcome.placement == "suspend"
    assert outcome.fallback_reason is not None and "vscode" in outcome.fallback_reason


def test_launch_claude_window_mode_uses_ghostty_ipc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ghostty +new-window` asks the running instance instead of spawning a second one."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.setattr("multi_claude.launcher._IS_MACOS", False)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "ghostty") else None

    popen_calls: list[list[str]] = []
    runner = _MuxRun()

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        outcome = launch_claude(Path("/work/g"), "sid-g", mode="window")

    assert runner.calls == [
        [
            "ghostty",
            "+new-window",
            f"--working-directory={_p('/work/g')}",
            "-e",
            "claude",
            "--resume",
            "sid-g",
        ]
    ]
    assert popen_calls == []
    assert outcome == LaunchOutcome("window", "ghostty")


def test_launch_claude_ghostty_ipc_failure_spawns_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No reachable instance (or a Ghostty without the action) → plain `ghostty`."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.setattr("multi_claude.launcher._IS_MACOS", False)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "ghostty") else None

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        class _Result:
            returncode = 1
            stdout = ""
            stderr = "D-Bus method call returned an error\n"

        return _Result()

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=fake_run),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        outcome = launch_claude(Path("/work/g"), "sid-g", mode="window")

    assert popen_calls == [
        [
            "ghostty",
            f"--working-directory={_p('/work/g')}",
            "-e",
            "claude",
            "--resume",
            "sid-g",
        ]
    ]
    # Same placement either way, so the user isn't told about the internal fallback.
    assert outcome == LaunchOutcome("window", "ghostty")


def test_argv_ghostty_on_macos_goes_through_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ghostty's CLI refuses to launch the emulator on macOS; `open -na` is the way in."""
    monkeypatch.setattr("multi_claude.launcher._IS_MACOS", True)

    assert _argv_ghostty("/work/g", ["claude", "--resume", "sid-g"]) == [
        "open",
        "-na",
        "Ghostty.app",
        "--args",
        "--working-directory=/work/g",
        "-e",
        "claude",
        "--resume",
        "sid-g",
    ]


def test_launch_claude_auto_falls_through_to_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """No multiplexer + emulator with tabs → AUTO opens a tab, not a window."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("WEZTERM_EXECUTABLE", "/usr/bin/wezterm")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "wezterm") else None

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        outcome = launch_claude(Path("/work/w"), None, mode="auto")

    assert runner.calls == [["wezterm", "cli", "spawn", "--cwd", _p("/work/w"), "--", "claude"]]
    assert outcome == LaunchOutcome("tab", "wezterm")


def test_launch_claude_auto_falls_through_to_window_without_tabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emulator without CLI tabs (alacritty) → AUTO ends up opening a window."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("ALACRITTY_WINDOW_ID", "9")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "alacritty") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        outcome = launch_claude(Path("/work/a"), None, mode="tab")

    assert popen_calls == [["alacritty", "--working-directory", _p("/work/a"), "-e", "claude"]]
    assert outcome.placement == "window"
    assert outcome.fallback_reason is not None and "alacritty" in outcome.fallback_reason


def test_launch_claude_window_mode_uses_windows_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside Windows Terminal, window mode targets a brand new window (`-w -1`)."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("WT_SESSION", "abc-123")

    def fake_which(cmd: str) -> str | None:
        return f"/fake/{cmd}" if cmd in ("claude", "wt.exe") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        launch_claude(Path("/work/wt"), "sid-wt", mode="window")

    expected = ["wt.exe", "-w", "-1", "new-tab", "-d", _p("/work/wt")]
    expected += ["--", "claude", "--resume", "sid-wt"]
    assert popen_calls == [expected]


def test_detect_emulator_conemu_degrades_to_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConEmu is detectable but not yet supported for spawning new windows."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("ConEmuPID", "4321")

    calls: list[tuple] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs.get("cwd")))

    with (
        patch(
            "multi_claude.launcher.shutil.which",
            side_effect=lambda cmd: "/fake/claude" if cmd == "claude" else None,
        ),
        patch("multi_claude.launcher.subprocess.run", side_effect=fake_run),
    ):
        outcome = launch_claude(Path("/work/c"), "sid-c", mode="window")

    assert calls == [(["claude", "--resume", "sid-c"], _p("/work/c"))]
    assert outcome.placement == "suspend"
    assert outcome.fallback_reason is not None and "conemu" in outcome.fallback_reason


def test_launch_claude_window_mode_uses_apple_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside Apple Terminal, window mode drives Terminal.app via AppleScript."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "osascript") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        launch_claude(Path("/Users/jane/proj"), "sid-mac", mode="window")

    # The cwd flows through two layers: POSIX single-quoting for the shell command,
    # then AppleScript escaping (backslashes doubled, double-quotes backslash-escaped)
    # when embedded as a string literal in the `do script` call. On macOS the path
    # has no backslashes so the AS escape is a no-op; on Windows runners we must
    # mirror both layers in the expected value.
    cwd_str = _p("/Users/jane/proj")
    shell_cmd = f"cd '{cwd_str}' && exec 'claude' '--resume' 'sid-mac'"
    as_escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    assert popen_calls == [
        [
            "osascript",
            "-e",
            f'tell application "Terminal" to do script "{as_escaped}"',
            "-e",
            'tell application "Terminal" to activate',
        ]
    ]


def test_launch_claude_window_mode_uses_iterm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside iTerm2, window mode spawns a new window via AppleScript `write text`."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "osascript") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        launch_claude(Path("/Users/jane/proj"), None, mode="window")

    cwd_str = _p("/Users/jane/proj")
    shell_cmd = f"cd '{cwd_str}' && exec 'claude'"
    as_escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    assert popen_calls == [
        [
            "osascript",
            "-e",
            'tell application "iTerm"',
            "-e",
            "  create window with default profile",
            "-e",
            f'  tell current session of current window to write text "{as_escaped}"',
            "-e",
            "end tell",
        ]
    ]


def test_iterm_applescript_escapes_embedded_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Display names containing quotes or backslashes must round-trip safely."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "osascript") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        launch_claude(Path("/work"), None, display_name='say "hi" \\n', mode="window")

    # The display name passes through two escaping layers (POSIX shell single-quotes
    # for the shell command, then AppleScript escaping for backslashes and quotes).
    # We assert end-to-end that no literal `"` or unescaped `\` leak into the final argv.
    [argv] = popen_calls
    write_text_line = next(line for line in argv if "write text" in line)
    # AppleScript-level escapes — backslash escaped as \\, double quote as \"
    assert '\\"hi\\"' in write_text_line
    assert "\\\\n" in write_text_line


# --------------------------------------------------------------------------- #
# Tab placement                                                                #
# --------------------------------------------------------------------------- #


def test_tab_mode_uses_gnome_terminal_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported case: GNOME Terminal must reuse the current window."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("GNOME_TERMINAL_SCREEN", "/org/gnome/Terminal/screen/x")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "gnome-terminal") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        outcome = launch_claude(Path("/work/g"), "sid-g", mode="tab")

    assert popen_calls == [
        [
            "gnome-terminal",
            "--tab",
            f"--working-directory={_p('/work/g')}",
            "--",
            "claude",
            "--resume",
            "sid-g",
        ]
    ]
    assert outcome == LaunchOutcome("tab", "gnome-terminal")


def test_window_mode_uses_gnome_terminal_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """`window` must be explicit about wanting a window, not the server's default."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("GNOME_TERMINAL_SCREEN", "/org/gnome/Terminal/screen/x")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "gnome-terminal") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        launch_claude(Path("/work/g"), None, mode="window")

    assert popen_calls == [
        ["gnome-terminal", "--window", f"--working-directory={_p('/work/g')}", "--", "claude"]
    ]


def test_tab_mode_uses_konsole_new_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("KONSOLE_VERSION", "230800")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "konsole") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        launch_claude(Path("/work/k"), None, mode="tab")

    assert popen_calls == [["konsole", "--new-tab", "--workdir", _p("/work/k"), "-e", "claude"]]


def test_tab_mode_kitty_without_remote_control_falls_back_to_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`kitty @` exits non-zero when remote control is off → open a window instead."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("KITTY_PID", "1234")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "kitty") else None

    def failing_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        class _Result:
            returncode = 1
            stderr = "Remote control is disabled\n"

        return _Result()

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.run", side_effect=failing_run),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        outcome = launch_claude(Path("/work/k"), None, mode="tab")

    assert popen_calls == [["kitty", "--directory", _p("/work/k"), "claude"]]
    assert outcome.placement == "window"
    assert outcome.fallback_reason is not None
    assert "Remote control is disabled" in outcome.fallback_reason


def test_tab_mode_uses_tmux_new_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        outcome = launch_claude(Path("/work/t"), "sid-t", mode="tab")

    assert runner.calls == [
        ["tmux", "new-window", "-c", _p("/work/t"), "claude", "--resume", "sid-t"]
    ]
    assert outcome == LaunchOutcome("tab", "tmux")


def test_tab_mode_zellij_degrades_to_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    """zellij can't run a command in a new tab, so it reports the degradation."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("ZELLIJ", "x")
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        outcome = launch_claude(Path("/work/z"), None, mode="tab")

    assert runner.calls == [
        ["zellij", "action", "new-pane", "--cwd", _p("/work/z"), "--", "claude"]
    ]
    assert outcome.placement == "split"
    assert outcome.fallback_reason is not None and "zellij" in outcome.fallback_reason


def test_split_mode_without_multiplexer_reports_and_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("GNOME_TERMINAL_SCREEN", "/screen/x")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "gnome-terminal") else None

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            pass

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=fake_which),
        patch("multi_claude.launcher.subprocess.Popen", FakePopen),
    ):
        outcome = launch_claude(Path("/work/s"), None, mode="split")

    assert outcome.placement == "tab"
    assert outcome.fallback_reason == "sin multiplexer activo"


# --------------------------------------------------------------------------- #
# Extra claude args                                                            #
# --------------------------------------------------------------------------- #


def test_build_argv_prepends_extra_args() -> None:
    argv = _build_claude_argv("abc", "X", ["--dangerously-skip-permissions", "--model", "opus"])
    assert argv == [
        "claude",
        "--dangerously-skip-permissions",
        "--model",
        "opus",
        "--resume",
        "abc",
        "-n",
        "X",
    ]


def test_extra_args_reach_the_spawned_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)

    runner = _MuxRun()

    with (
        patch("multi_claude.launcher.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"),
        patch("multi_claude.launcher.subprocess.run", side_effect=runner),
    ):
        launch_claude(
            Path("/work/x"),
            "sid",
            mode="split",
            claude_args=["--dangerously-skip-permissions"],
        )

    assert runner.calls == [
        [
            "tmux",
            "split-window",
            "-h",
            "-c",
            _p("/work/x"),
            "claude",
            "--dangerously-skip-permissions",
            "--resume",
            "sid",
        ]
    ]


# --------------------------------------------------------------------------- #
# Dry-run preview                                                              #
# --------------------------------------------------------------------------- #


def test_preview_dispatch_matches_tab_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("GNOME_TERMINAL_SCREEN", "/screen/x")

    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda c: f"/usr/bin/{c}" if c == "gnome-terminal" else None,
    ):
        assert preview_dispatch("tab") == LaunchOutcome("tab", "gnome-terminal")
        assert preview_dispatch("auto") == LaunchOutcome("tab", "gnome-terminal")
        assert preview_dispatch("window") == LaunchOutcome("window", "gnome-terminal")


def test_preview_dispatch_reports_missing_tab_support(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")

    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda c: f"/usr/bin/{c}" if c == "ghostty" else None,
    ):
        outcome = preview_dispatch("tab")

    assert outcome.placement == "window"
    assert outcome.fallback_reason is not None and "ghostty" in outcome.fallback_reason


def test_preview_dispatch_terminator_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    """With remotinator around, `split`/`auto` in Terminator previews as a pane."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")

    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda c: f"/usr/bin/{c}" if c in ("terminator", "remotinator") else None,
    ):
        assert preview_dispatch("split") == LaunchOutcome("split", "terminator")
        assert preview_dispatch("auto") == LaunchOutcome("split", "terminator")
        assert preview_dispatch("tab") == LaunchOutcome("tab", "terminator")


def test_preview_dispatch_terminator_without_remotinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")

    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda c: f"/usr/bin/{c}" if c == "terminator" else None,
    ):
        outcome = preview_dispatch("split")

    assert outcome.placement == "tab"
    assert outcome.fallback_reason is not None and "remotinator" in outcome.fallback_reason


def test_preview_dispatch_suspends_without_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)

    with patch("multi_claude.launcher.shutil.which", return_value=None):
        assert preview_dispatch("auto") == LaunchOutcome(
            "suspend", "inline", "no se detectó ningún emulador"
        )
