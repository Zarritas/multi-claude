"""Tests for multi_claude.focus."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from multi_claude.focus import (
    LiveSession,
    _ancestor_chain,
    _focus_tmux,
    _focus_x11,
    find_live_session,
    focus_terminal,
)


def _write_registry_entry(sessions_dir: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "pid": 4242,
        "sessionId": "sid-live",
        "cwd": "/work/x",
        "procStart": "123456",
    }
    data.update(overrides)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    entry = sessions_dir / f"{data['pid']}.json"
    entry.write_text(json.dumps(data), encoding="utf-8")
    return entry


class _FakeRun:
    """Stand-in for focus._run keyed on the executed binary."""

    def __init__(self, outputs: dict[str, tuple[int, str]]) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]):  # type: ignore[no-untyped-def]
        self.calls.append(argv)
        returncode, stdout = self.outputs.get(argv[0], (1, ""))

        class _Result:
            pass

        result = _Result()
        result.returncode = returncode  # type: ignore[attr-defined]
        result.stdout = stdout  # type: ignore[attr-defined]
        return result


# --------------------------------------------------------------------------- #
# find_live_session                                                            #
# --------------------------------------------------------------------------- #


def test_find_live_session_returns_entry_when_pid_alive(tmp_path: Path) -> None:
    _write_registry_entry(tmp_path)
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch("multi_claude.focus._proc_start_matches", return_value=True),
    ):
        live = find_live_session("sid-live", sessions_dir=tmp_path)
    assert live == LiveSession(session_id="sid-live", pid=4242)


def test_find_live_session_none_when_pid_dead(tmp_path: Path) -> None:
    """A registry file left behind by a crashed process must not count as live."""
    _write_registry_entry(tmp_path)
    with patch("multi_claude.focus._pid_alive", return_value=False):
        assert find_live_session("sid-live", sessions_dir=tmp_path) is None


def test_find_live_session_none_on_proc_start_mismatch(tmp_path: Path) -> None:
    """PID reuse: alive PID but different process start time → stale entry."""
    _write_registry_entry(tmp_path)
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch("multi_claude.focus._proc_start_matches", return_value=False),
    ):
        assert find_live_session("sid-live", sessions_dir=tmp_path) is None


def test_find_live_session_none_for_other_session(tmp_path: Path) -> None:
    _write_registry_entry(tmp_path)
    with patch("multi_claude.focus._pid_alive", return_value=True):
        assert find_live_session("sid-other", sessions_dir=tmp_path) is None


def test_find_live_session_none_when_dir_missing(tmp_path: Path) -> None:
    assert find_live_session("sid", sessions_dir=tmp_path / "nope") is None


def test_find_live_session_ignores_corrupt_json(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    _write_registry_entry(tmp_path)
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch("multi_claude.focus._proc_start_matches", return_value=True),
    ):
        live = find_live_session("sid-live", sessions_dir=tmp_path)
    assert live is not None and live.pid == 4242


# --------------------------------------------------------------------------- #
# _ancestor_chain                                                              #
# --------------------------------------------------------------------------- #


def test_ancestor_chain_walks_to_init() -> None:
    ps_output = "  100 1\n  200 100\n  300 200\n  999 1\n"
    runner = _FakeRun({"ps": (0, ps_output)})
    with patch("multi_claude.focus._run", runner):
        assert _ancestor_chain(300) == [300, 200, 100]


def test_ancestor_chain_degrades_to_pid_when_ps_fails() -> None:
    with patch("multi_claude.focus._run", side_effect=OSError("no ps")):
        assert _ancestor_chain(300) == [300]


def test_ancestor_chain_guards_against_cycles() -> None:
    ps_output = "100 200\n200 100\n"
    runner = _FakeRun({"ps": (0, ps_output)})
    with patch("multi_claude.focus._run", runner):
        assert _ancestor_chain(100) == [100, 200]


# --------------------------------------------------------------------------- #
# _focus_tmux                                                                  #
# --------------------------------------------------------------------------- #


def test_focus_tmux_selects_matching_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-x/default,1,0")
    panes = "111 main @1 %1\n200 work @2 %5\n"
    runner = _FakeRun({"tmux": (0, panes)})
    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/tmux"),
        patch("multi_claude.focus._run", runner),
    ):
        # 300 (claude) → 200 (shell) is the pane pid of window @2 / pane %5.
        assert _focus_tmux([300, 200, 100]) is True

    assert ["tmux", "select-window", "-t", "@2"] in runner.calls
    assert ["tmux", "select-pane", "-t", "%5"] in runner.calls
    assert ["tmux", "switch-client", "-t", "work"] in runner.calls


def test_focus_tmux_no_switch_client_outside_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    panes = "200 work @2 %5\n"
    runner = _FakeRun({"tmux": (0, panes)})
    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/tmux"),
        patch("multi_claude.focus._run", runner),
    ):
        assert _focus_tmux([200]) is True
    assert not any("switch-client" in call for call in runner.calls)


def test_focus_tmux_false_when_no_pane_matches() -> None:
    runner = _FakeRun({"tmux": (0, "111 main @1 %1\n")})
    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/tmux"),
        patch("multi_claude.focus._run", runner),
    ):
        assert _focus_tmux([300, 200]) is False


def test_focus_tmux_false_without_binary() -> None:
    with patch("multi_claude.focus.shutil.which", return_value=None):
        assert _focus_tmux([300]) is False


# --------------------------------------------------------------------------- #
# _focus_x11                                                                   #
# --------------------------------------------------------------------------- #


def test_focus_x11_activates_window_of_closest_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")

    calls: list[list[str]] = []

    def fake_run(argv: list[str]):  # type: ignore[no-untyped-def]
        calls.append(argv)

        class _Result:
            returncode = 0
            stdout = ""

        result = _Result()
        if argv[:2] == ["xdotool", "search"]:
            # Only the terminal ancestor (pid 100) owns a window.
            result.stdout = "77594628\n" if argv[3] == "100" else ""
            result.returncode = 0 if argv[3] == "100" else 1
        return result

    with (
        patch(
            "multi_claude.focus.shutil.which",
            side_effect=lambda cmd: "/usr/bin/xdotool" if cmd == "xdotool" else None,
        ),
        patch("multi_claude.focus._run", side_effect=fake_run),
    ):
        assert _focus_x11([300, 200, 100]) is True

    assert ["xdotool", "windowactivate", "77594628"] in calls


def test_focus_x11_false_without_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _focus_x11([300]) is False


def test_focus_x11_wmctrl_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    listing = "0x04a00003  0 100   host Ghostty\n0x04a00007  0 999   host Other\n"
    runner = _FakeRun({"wmctrl": (0, listing)})
    with (
        patch(
            "multi_claude.focus.shutil.which",
            side_effect=lambda cmd: "/usr/bin/wmctrl" if cmd == "wmctrl" else None,
        ),
        patch("multi_claude.focus._run", runner),
    ):
        assert _focus_x11([300, 100]) is True
    assert ["wmctrl", "-ia", "0x04a00003"] in runner.calls


# --------------------------------------------------------------------------- #
# focus_terminal                                                               #
# --------------------------------------------------------------------------- #


def test_focus_terminal_false_when_every_strategy_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    with (
        patch("multi_claude.focus._ancestor_chain", return_value=[300]),
        patch("multi_claude.focus.shutil.which", return_value=None),
    ):
        assert focus_terminal(300) is False


def test_focus_terminal_stops_at_first_success() -> None:
    with (
        patch("multi_claude.focus._ancestor_chain", return_value=[300]),
        patch("multi_claude.focus._focus_tmux", return_value=True) as tmux,
        patch("multi_claude.focus._focus_x11") as x11,
    ):
        assert focus_terminal(300) is True
    tmux.assert_called_once_with([300])
    x11.assert_not_called()
