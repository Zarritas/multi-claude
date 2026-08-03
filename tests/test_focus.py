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
    background_sessions,
    find_live_session,
    focus_terminal,
    live_sessions,
    merge_live,
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
# live_sessions                                                                #
# --------------------------------------------------------------------------- #


def test_live_sessions_carries_status_and_timestamp(tmp_path: Path) -> None:
    _write_registry_entry(tmp_path, status="busy", statusUpdatedAt=1785478309605)
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch("multi_claude.focus._proc_start_matches", return_value=True),
    ):
        live = live_sessions(sessions_dir=tmp_path)
    assert set(live) == {"sid-live"}
    entry = live["sid-live"]
    assert entry.status == "busy"
    assert entry.status_updated_at == pytest.approx(1785478309.605)


def test_live_sessions_status_none_when_registry_omits_it(tmp_path: Path) -> None:
    """Older Claude builds wrote no status; the session is still live."""
    _write_registry_entry(tmp_path)
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch("multi_claude.focus._proc_start_matches", return_value=True),
    ):
        live = live_sessions(sessions_dir=tmp_path)
    assert live["sid-live"].status is None
    assert live["sid-live"].status_updated_at is None


def test_live_sessions_falls_back_to_updated_at(tmp_path: Path) -> None:
    _write_registry_entry(tmp_path, status="idle", updatedAt=1785477878784)
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch("multi_claude.focus._proc_start_matches", return_value=True),
    ):
        live = live_sessions(sessions_dir=tmp_path)
    assert live["sid-live"].status_updated_at == pytest.approx(1785477878.784)


def test_live_sessions_applies_staleness_guards(tmp_path: Path) -> None:
    """The same guards as find_live_session: a dead PID is simply not there."""
    _write_registry_entry(tmp_path, status="busy")
    with patch("multi_claude.focus._pid_alive", return_value=False):
        assert live_sessions(sessions_dir=tmp_path) == {}


def test_live_sessions_returns_every_live_entry(tmp_path: Path) -> None:
    _write_registry_entry(tmp_path, pid=1, sessionId="sid-a", status="busy")
    _write_registry_entry(tmp_path, pid=2, sessionId="sid-b", status="idle")
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch("multi_claude.focus._proc_start_matches", return_value=True),
    ):
        live = live_sessions(sessions_dir=tmp_path)
    assert {sid: e.status for sid, e in live.items()} == {"sid-a": "busy", "sid-b": "idle"}


def test_live_sessions_empty_when_dir_missing(tmp_path: Path) -> None:
    assert live_sessions(sessions_dir=tmp_path / "nope") == {}


def test_find_live_session_prefers_a_live_duplicate_over_a_stale_one(tmp_path: Path) -> None:
    """Two entries for one session (a crash left one behind): the live one wins."""
    _write_registry_entry(tmp_path, pid=100, procStart="dead")
    _write_registry_entry(tmp_path, pid=200, procStart="alive")
    with (
        patch("multi_claude.focus._pid_alive", return_value=True),
        patch(
            "multi_claude.focus._proc_start_matches",
            side_effect=lambda pid, proc_start: proc_start == "alive",
        ),
    ):
        live = find_live_session("sid-live", sessions_dir=tmp_path)
    assert live is not None and live.pid == 200


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


# --- `claude agents --json`, the supported source ------------------------------------


def _agents_output(payload: object) -> object:
    """A CompletedProcess-alike carrying ``payload`` as stdout json."""
    from subprocess import CompletedProcess

    return CompletedProcess(args=["claude"], returncode=0, stdout=json.dumps(payload), stderr="")


def test_background_sessions_reads_both_entry_shapes() -> None:
    """Interactive entries carry pid+status; background ones carry state and no pid."""
    payload = [
        {"sessionId": "inter", "pid": 42, "kind": "interactive", "status": "busy"},
        {"sessionId": "bg", "id": "abc", "kind": "background", "state": "failed"},
    ]
    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/claude"),
        patch("multi_claude.focus.subprocess.run", return_value=_agents_output(payload)),
    ):
        result = background_sessions()
    assert result["inter"] == LiveSession("inter", pid=42, status="busy")
    # pid 0 = nothing to focus, which is exactly true of a background session.
    assert result["bg"] == LiveSession("bg", pid=0, status="failed")


def test_background_sessions_is_empty_without_the_cli() -> None:
    with patch("multi_claude.focus.shutil.which", return_value=None):
        assert background_sessions() == {}


@pytest.mark.parametrize(
    "outcome",
    [
        "not json at all",
        json.dumps({"not": "a list"}),
        "",
    ],
)
def test_background_sessions_survives_junk_output(outcome: str) -> None:
    from subprocess import CompletedProcess

    completed = CompletedProcess(args=["claude"], returncode=0, stdout=outcome, stderr="")
    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/claude"),
        patch("multi_claude.focus.subprocess.run", return_value=completed),
    ):
        assert background_sessions() == {}


def test_background_sessions_survives_a_failing_command() -> None:
    """An older build without the subcommand must leave the registry path working."""
    from subprocess import CompletedProcess

    completed = CompletedProcess(args=["claude"], returncode=1, stdout="", stderr="nope")
    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/claude"),
        patch("multi_claude.focus.subprocess.run", return_value=completed),
    ):
        assert background_sessions() == {}


def test_background_sessions_survives_a_timeout() -> None:
    from subprocess import TimeoutExpired

    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/claude"),
        patch("multi_claude.focus.subprocess.run", side_effect=TimeoutExpired("claude", 10)),
    ):
        assert background_sessions() == {}


def test_background_sessions_skips_entries_without_a_session_id() -> None:
    payload = [{"pid": 1, "status": "busy"}, "no soy un objeto", {"sessionId": ""}]
    with (
        patch("multi_claude.focus.shutil.which", return_value="/usr/bin/claude"),
        patch("multi_claude.focus.subprocess.run", return_value=_agents_output(payload)),
    ):
        assert background_sessions() == {}


# --- merging the two sources ---------------------------------------------------------


def test_merge_prefers_the_registry_pid_and_the_agents_status() -> None:
    """The pid is what focusing needs; the state name is what is documented."""
    registry = {"s": LiveSession("s", pid=99, status="busy", status_updated_at=5.0)}
    agents = {"s": LiveSession("s", pid=0, status="needs input", status_updated_at=1.0)}
    (merged,) = merge_live(registry, agents).values()
    assert merged.pid == 99
    assert merged.status == "needs input"
    assert merged.status_updated_at == 5.0


def test_merge_carries_through_what_only_one_side_knows() -> None:
    registry = {"solo-registry": LiveSession("solo-registry", pid=1, status="busy")}
    agents = {"solo-agents": LiveSession("solo-agents", pid=0, status="completed")}
    merged = merge_live(registry, agents)
    assert set(merged) == {"solo-registry", "solo-agents"}


def test_merge_with_no_agents_is_the_registry_unchanged() -> None:
    registry = {"s": LiveSession("s", pid=1, status="busy")}
    assert merge_live(registry, {}) == registry


def test_merge_takes_the_agents_pid_when_the_registry_has_none() -> None:
    registry = {"s": LiveSession("s", pid=0, status=None)}
    agents = {"s": LiveSession("s", pid=7, status="working")}
    (merged,) = merge_live(registry, agents).values()
    assert merged.pid == 7
    assert merged.status == "working"
