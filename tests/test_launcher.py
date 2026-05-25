"""Tests for multi_claude.launcher."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from multi_claude.launcher import (
    LauncherError,
    _build_claude_argv,
    detect_multiplexer,
    detect_terminal_emulator,
    launch_claude,
)

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
)


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

    calls = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.run", side_effect=fake_run
    ):
        launch_claude(Path("/work/x"), "sid-1")

    assert calls == [["tmux", "split-window", "-h", "-c", "/work/x", "claude", "--resume", "sid-1"]]


def test_launch_claude_uses_zellij(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("ZELLIJ", "x")
    monkeypatch.delenv("TERMINATOR_UUID", raising=False)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    calls = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.run", side_effect=fake_run
    ):
        launch_claude(Path("/work/y"), None)

    assert calls == [["zellij", "action", "new-pane", "--cwd", "/work/y", "--", "claude"]]


def test_launch_claude_uses_terminator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setenv("TERMINATOR_UUID", "term://x")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    calls = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.run", side_effect=fake_run
    ):
        launch_claude(Path("/work/t"), "sid-3", display_name="Mi feature")

    assert calls == [
        [
            "terminator",
            "--new-tab",
            "--working-directory=/work/t",
            "-x",
            "claude",
            "--resume",
            "sid-3",
            "-n",
            "Mi feature",
        ]
    ]


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

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.run", side_effect=fake_run
    ):
        launch_claude(Path("/work/z"), "sid-2", app=None)

    assert calls == [(["claude", "--resume", "sid-2"], "/work/z")]


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

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.Popen", FakePopen
    ):
        launch_claude(Path("/work/k"), "sid-k", mode="window")

    assert popen_calls == [
        (
            ["kitty", "--directory", "/work/k", "claude", "--resume", "sid-k"],
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

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.run", side_effect=fake_run
    ):
        launch_claude(Path("/work/q"), "sid-q", mode="window")

    assert calls == [(["claude", "--resume", "sid-q"], "/work/q")]


def test_launch_claude_suspend_mode_skips_multiplexer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even inside tmux, mode='suspend' must run inline."""
    monkeypatch.setenv("TMUX", "x")
    _clear_emulator_envs(monkeypatch)

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}"

    calls: list[tuple] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs.get("cwd")))

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.run", side_effect=fake_run
    ):
        launch_claude(Path("/work/s"), None, mode="suspend")

    assert calls == [(["claude"], "/work/s")]


def test_detect_emulator_prefers_term_program_ghostty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "ghostty" else None,
    ):
        assert detect_terminal_emulator() == "ghostty"


def test_detect_emulator_term_program_wezterm(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")  # case-insensitive
    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "wezterm" else None,
    ):
        assert detect_terminal_emulator() == "wezterm"


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
        assert detect_terminal_emulator() == "ghostty"


def test_detect_emulator_ghostty_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """If TERM_PROGRAM is missing but GHOSTTY_RESOURCES_DIR is set, still detect."""
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("GHOSTTY_RESOURCES_DIR", "/snap/ghostty/current/share/ghostty")
    with patch(
        "multi_claude.launcher.shutil.which",
        side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "ghostty" else None,
    ):
        assert detect_terminal_emulator() == "ghostty"


def test_launch_claude_window_mode_uses_ghostty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "ghostty") else None

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.Popen", FakePopen
    ):
        launch_claude(Path("/work/g"), "sid-g", mode="window")

    assert popen_calls == [
        ["ghostty", "--working-directory=/work/g", "-e", "claude", "--resume", "sid-g"]
    ]


def test_launch_claude_auto_falls_through_to_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """No multiplexer + emulator detected → AUTO opens a window."""
    _clear_mux_envs(monkeypatch)
    _clear_emulator_envs(monkeypatch)
    monkeypatch.setenv("WEZTERM_EXECUTABLE", "/usr/bin/wezterm")

    def fake_which(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in ("claude", "wezterm") else None

    popen_calls: list[tuple] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            popen_calls.append(argv)

    with patch("multi_claude.launcher.shutil.which", side_effect=fake_which), patch(
        "multi_claude.launcher.subprocess.Popen", FakePopen
    ):
        launch_claude(Path("/work/w"), None, mode="auto")

    assert popen_calls == [["wezterm", "start", "--cwd", "/work/w", "--", "claude"]]
