"""Detect an already-running claude session and bring its terminal to the front.

The active-session registry (``~/.claude/sessions/<pid>.json``) gives us the PID of
every live ``claude`` process. Focusing the terminal that hosts that PID is platform
dependent and strictly best-effort — strategies are tried in order and the first
success wins:

- **tmux**: find the pane whose process tree contains the PID, then select its
  window/pane (and ``switch-client`` when the TUI itself runs inside tmux).
- **X11 / XWayland**: walk the PID's ancestor chain and ask ``xdotool`` (or
  ``wmctrl``) for a window owned by one of those PIDs, then activate it.
- **GNOME Wayland**: native-Wayland windows are invisible to xdotool; we can only
  activate them through the third-party "Window Calls" shell extension, if present.
- **macOS**: ``osascript`` + System Events to raise the app owning an ancestor PID.
- **Windows**: not supported yet (callers fall back to a notice).

Callers use :func:`find_live_session` to decide whether a session is already open
(with stale-registry guards: dead PID, or PID reuse detected via ``procStart``),
:func:`live_sessions` to read the whole registry in one pass (for a listing that
shows what each session is doing right now), and :func:`focus_terminal` to
attempt the foreground switch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ACTIVE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

_SUBPROCESS_TIMEOUT = 3.0  # all helpers are quick local IPC; never hang the UI

# `claude agents --json` starts a node process, so it is an order of magnitude slower than
# the rest: ~350 ms measured. Generous enough for a loaded machine, short enough that a
# hung CLI does not keep a worker busy.
_AGENTS_TIMEOUT = 10.0


@dataclass(frozen=True)
class LiveSession:
    """A session registered as live in ``~/.claude/sessions/`` with a verified PID.

    ``status`` is whatever Claude Code last wrote to the registry (``"busy"`` while
    it works, and the entry is rewritten as the session changes state). The
    vocabulary is not a documented contract, so it is carried through as the raw
    string and callers render unknown values generically rather than guessing.
    """

    session_id: str
    pid: int
    status: str | None = None
    status_updated_at: float | None = None  # epoch seconds, from the registry's millis


def find_live_session(
    session_id: str,
    sessions_dir: Path = ACTIVE_SESSIONS_DIR,
) -> LiveSession | None:
    """Return the live registry entry for ``session_id``, or None.

    Registry files can outlive their process (crashes, power loss), so an entry
    only counts if its PID is still alive — and, on Linux, if the process start
    time recorded as ``procStart`` still matches ``/proc/<pid>/stat`` (guards
    against PID reuse after a reboot or long uptime).
    """
    return live_sessions(sessions_dir).get(session_id)


def live_sessions(
    sessions_dir: Path = ACTIVE_SESSIONS_DIR,
) -> dict[str, LiveSession]:
    """Return every verified-live registry entry, keyed by session id.

    One pass over the directory, so a listing can label all its rows without
    re-globbing per session. Entries that fail the staleness guards of
    :func:`find_live_session` are left out, exactly as if they weren't there.
    """
    result: dict[str, LiveSession] = {}
    if not sessions_dir.is_dir():
        return result
    for entry in sessions_dir.glob("*.json"):
        try:
            with entry.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = data.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            continue
        proc_start = data.get("procStart")
        if isinstance(proc_start, str) and not _proc_start_matches(pid, proc_start):
            continue
        status = data.get("status")
        result[session_id] = LiveSession(
            session_id=session_id,
            pid=pid,
            status=status if isinstance(status, str) and status else None,
            status_updated_at=_epoch_seconds(data.get("statusUpdatedAt") or data.get("updatedAt")),
        )
    return result


def background_sessions() -> dict[str, LiveSession]:
    """Sessions that ``claude agents --json`` knows about, keyed by session id.

    The supported way to ask, and the only way to see **background** sessions: those are
    dispatched from agent view and recorded under ``~/.claude/jobs/``, not in the per-PID
    registry, so :func:`live_sessions` is blind to them. Their state also comes with a
    documented vocabulary (``working``, ``needs input``, ``idle``, ``completed``,
    ``failed``, ``stopped``) instead of the two values observed in the registry.

    Why this does not simply replace :func:`live_sessions`: the command takes ~350 ms
    (measured, five runs, warm) because it starts a node process. At the two-second cadence
    the Estado column refreshes at, that would mean a subprocess spawn every two seconds
    forever. So the registry stays the fast path and this runs on a slow tick, merged over
    it — the interactive rows keep their latency, the background ones appear a few seconds
    late, and nothing spawns a process ten times a minute.

    Returns an empty dict on any failure (no ``claude`` on PATH, an older build without the
    subcommand, a timeout): callers fall back to the registry, which is what they had.
    """
    if shutil.which("claude") is None:
        return {}
    try:
        completed = subprocess.run(
            ["claude", "agents", "--json", "--all"],
            capture_output=True,
            text=True,
            timeout=_AGENTS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {}
    try:
        entries = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(entries, list):
        return {}

    result: dict[str, LiveSession] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        # Interactive entries carry `status` and a pid; background ones carry `state` and
        # an id. Both shapes are collapsed into LiveSession, with pid 0 meaning "not a
        # process we can focus" — which is exactly true of a background session.
        raw_state = entry.get("status") or entry.get("state")
        pid = entry.get("pid")
        result[session_id] = LiveSession(
            session_id=session_id,
            pid=pid if isinstance(pid, int) else 0,
            status=raw_state if isinstance(raw_state, str) and raw_state else None,
            status_updated_at=_epoch_seconds(entry.get("startedAt")),
        )
    return result


def merge_live(
    registry: dict[str, LiveSession], agents: dict[str, LiveSession]
) -> dict[str, LiveSession]:
    """Overlay ``agents`` on ``registry``, keeping whichever knows more about each session.

    The registry wins on ``pid`` because that is what focusing a terminal needs and a
    background session has none; ``claude agents`` wins on ``status`` because its vocabulary
    is documented and the registry's is not. Sessions only one of them knows about are
    carried through as they are.
    """
    merged = dict(registry)
    for session_id, from_agents in agents.items():
        known = merged.get(session_id)
        if known is None:
            merged[session_id] = from_agents
            continue
        merged[session_id] = LiveSession(
            session_id=session_id,
            pid=known.pid or from_agents.pid,
            status=from_agents.status or known.status,
            status_updated_at=known.status_updated_at or from_agents.status_updated_at,
        )
    return merged


def _epoch_seconds(raw: object) -> float | None:
    """Convert the registry's millisecond timestamps to epoch seconds."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw / 1000.0


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a probe on Windows (it terminates). Assume alive;
        # the registry file existing is the best signal we have there.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    return True


def _proc_start_matches(pid: int, proc_start: str) -> bool:
    """Compare the registry's ``procStart`` with ``/proc/<pid>/stat`` field 22.

    Only meaningful where procfs exists (Linux); elsewhere we trust the PID check.
    """
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return True
    try:
        stat = stat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    # comm (field 2) may contain spaces/parens; everything after the last ')' is
    # whitespace-separated, starting at field 3. starttime is field 22 → index 19.
    fields = stat.rsplit(")", 1)[-1].split()
    if len(fields) < 20:
        return True
    return fields[19] == proc_start


# --------------------------------------------------------------------------- #
# Focus strategies                                                             #
# --------------------------------------------------------------------------- #


def focus_terminal(pid: int) -> bool:
    """Try to bring the terminal hosting ``pid`` to the foreground. Best-effort."""
    ancestors = _ancestor_chain(pid)
    return (
        _focus_tmux(ancestors)
        or _focus_x11(ancestors)
        or _focus_gnome_wayland(ancestors)
        or _focus_macos(ancestors)
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _ancestor_chain(pid: int) -> list[int]:
    """Return ``pid`` plus its ancestors, closest first (excluding pid 0/1).

    Uses ``ps -eo pid=,ppid=`` which works on both Linux and macOS. On failure
    (Windows, ps missing) the chain degrades to just ``[pid]``.
    """
    parent_of: dict[int, int] = {}
    try:
        result = _run(["ps", "-eo", "pid=,ppid="])
    except (OSError, subprocess.TimeoutExpired):
        return [pid]
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            parent_of[int(parts[0])] = int(parts[1])
        except ValueError:
            continue

    chain = [pid]
    current = pid
    while current in parent_of:
        current = parent_of[current]
        if current <= 1 or current in chain:  # stop at init / guard cycles
            break
        chain.append(current)
    return chain


def _focus_tmux(ancestors: list[int]) -> bool:
    """Select the tmux window/pane whose process tree contains the session."""
    if not shutil.which("tmux"):
        return False
    try:
        result = _run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_pid} #{session_name} #{window_id} #{pane_id}",
            ]
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False

    ancestor_set = set(ancestors)
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        pane_pid_raw, session_name, window_id, pane_id = parts
        try:
            pane_pid = int(pane_pid_raw)
        except ValueError:
            continue
        if pane_pid not in ancestor_set:
            continue
        try:
            _run(["tmux", "select-window", "-t", window_id])
            _run(["tmux", "select-pane", "-t", pane_id])
            if os.environ.get("TMUX"):
                _run(["tmux", "switch-client", "-t", session_name])
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True
    return False


def _focus_x11(ancestors: list[int]) -> bool:
    """Activate the X11 window owned by the closest ancestor PID that has one.

    Works for X11 sessions and for XWayland windows under Wayland; native
    Wayland windows are invisible here (handled by the GNOME strategy below).
    """
    if not os.environ.get("DISPLAY"):
        return False
    if shutil.which("xdotool"):
        for pid in ancestors:
            try:
                search = _run(["xdotool", "search", "--pid", str(pid)])
            except (OSError, subprocess.TimeoutExpired):
                return False
            window_ids = search.stdout.split()
            if search.returncode != 0 or not window_ids:
                continue
            try:
                activate = _run(["xdotool", "windowactivate", window_ids[-1]])
            except (OSError, subprocess.TimeoutExpired):
                return False
            if activate.returncode == 0:
                return True
        return False
    if shutil.which("wmctrl"):
        try:
            listing = _run(["wmctrl", "-lp"])
        except (OSError, subprocess.TimeoutExpired):
            return False
        if listing.returncode != 0:
            return False
        windows_by_pid: dict[int, str] = {}
        for line in listing.stdout.splitlines():
            parts = line.split(None, 4)  # win_id desktop pid host title
            if len(parts) < 3:
                continue
            try:
                windows_by_pid.setdefault(int(parts[2]), parts[0])
            except ValueError:
                continue
        for pid in ancestors:
            win_id = windows_by_pid.get(pid)
            if win_id is None:
                continue
            try:
                activate = _run(["wmctrl", "-ia", win_id])
            except (OSError, subprocess.TimeoutExpired):
                return False
            return activate.returncode == 0
    return False


def _focus_gnome_wayland(ancestors: list[int]) -> bool:
    """Activate via the GNOME Shell "Window Calls" extension, when installed.

    Stock GNOME Wayland exposes no way to focus another app's window; the
    extension (https://github.com/ickyicky/window-calls) adds List/Activate
    methods on the session bus. If it's absent the List call simply errors.
    """
    if sys.platform != "linux" or os.environ.get("XDG_SESSION_TYPE") != "wayland":
        return False
    if not shutil.which("gdbus"):
        return False
    try:
        listing = _run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell",
                "--object-path",
                "/org/gnome/Shell/Extensions/Windows",
                "--method",
                "org.gnome.Shell.Extensions.Windows.List",
            ]
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if listing.returncode != 0:
        return False
    # Output shape: ('[{"id":..., "pid":..., ...}, ...]',)
    raw = listing.stdout.strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return False
    try:
        windows = json.loads(raw[start : end + 1].replace("\\'", "'"))
    except json.JSONDecodeError:
        return False
    by_pid = {
        w["pid"]: w["id"] for w in windows if isinstance(w, dict) and "pid" in w and "id" in w
    }
    for pid in ancestors:
        win_id = by_pid.get(pid)
        if win_id is None:
            continue
        try:
            activate = _run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.gnome.Shell",
                    "--object-path",
                    "/org/gnome/Shell/Extensions/Windows",
                    "--method",
                    "org.gnome.Shell.Extensions.Windows.Activate",
                    str(win_id),
                ]
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return activate.returncode == 0
    return False


def _focus_macos(ancestors: list[int]) -> bool:
    """Raise the macOS app owning an ancestor PID via System Events.

    Terminal apps on macOS are single-process (all windows share one PID), so
    this brings the right app forward but cannot pick the exact window/tab.
    """
    if sys.platform != "darwin":
        return False
    for pid in ancestors:
        script = (
            'tell application "System Events" to set frontmost of '
            f"(first process whose unix id is {pid}) to true"
        )
        try:
            result = _run(["osascript", "-e", script])
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode == 0:
            return True
    return False
