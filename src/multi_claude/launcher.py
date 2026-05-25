"""Launch ``claude`` in the right place depending on the surrounding environment.

Three launch modes:

- ``auto`` — multiplexer split > terminator tab > emulator window > suspend.
- ``window`` — emulator window > suspend.
- ``suspend`` — always suspend the TUI and run inline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from multi_claude.config import LaunchMode

if TYPE_CHECKING:
    from textual.app import App


class LauncherError(RuntimeError):
    """Raised when claude cannot be launched (e.g. binary not in PATH)."""


def detect_multiplexer() -> str | None:
    """Return 'tmux', 'zellij', 'terminator', or None.

    Terminator is included here because ``--new-tab`` reuses the existing window via
    DBus, so it behaves like a multiplexer split from the user's perspective.
    """
    if os.environ.get("TMUX") and shutil.which("tmux"):
        return "tmux"
    if os.environ.get("ZELLIJ") and shutil.which("zellij"):
        return "zellij"
    if os.environ.get("TERMINATOR_UUID") and shutil.which("terminator"):
        return "terminator"
    return None


# Map values of $TERM_PROGRAM (case-insensitive) to our internal emulator id.
# This is the most canonical signal an emulator can provide, so we check it first.
_TERM_PROGRAM_MAP: dict[str, str] = {
    "ghostty": "ghostty",
    "wezterm": "wezterm",
}

# Emulator-specific env vars: presence means "the user is inside this emulator".
# Order matters only for ties, since each env var is emulator-specific.
_EMULATOR_ENV_CHECKS: tuple[tuple[str, str], ...] = (
    ("KITTY_PID", "kitty"),
    ("WEZTERM_EXECUTABLE", "wezterm"),
    ("ALACRITTY_WINDOW_ID", "alacritty"),
    ("ALACRITTY_LOG", "alacritty"),
    ("KONSOLE_VERSION", "konsole"),
    ("GNOME_TERMINAL_SCREEN", "gnome-terminal"),
    ("FOOT_VERSION", "foot"),
    ("TERMINATOR_UUID", "terminator"),
    ("GHOSTTY_RESOURCES_DIR", "ghostty"),
    ("GHOSTTY_BIN_DIR", "ghostty"),
)


def detect_terminal_emulator() -> str | None:
    """Return a short identifier for the surrounding terminal emulator, or None.

    Used by the ``window`` mode to spawn a new window in the *same* emulator the user
    is already running. Detection priority:

      1. ``$TERM_PROGRAM`` (canonical signal published by modern emulators).
      2. Emulator-specific env vars (one per emulator).
      3. Generic fallback: ``x-terminal-emulator`` / ``xterm`` if present in PATH.

    Each step requires the matching binary to actually be in PATH; if not, we move on.
    """
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    target = _TERM_PROGRAM_MAP.get(term_program)
    if target and shutil.which(target):
        return target

    for env_var, binary in _EMULATOR_ENV_CHECKS:
        if os.environ.get(env_var) and shutil.which(binary):
            return binary

    if shutil.which("x-terminal-emulator"):
        return "x-terminal-emulator"
    if shutil.which("xterm"):
        return "xterm"
    return None


def launch_claude(
    cwd: Path,
    session_id: str | None = None,
    *,
    display_name: str | None = None,
    app: "App | None" = None,
    mode: LaunchMode = "auto",
) -> None:
    """Launch ``claude`` with ``cwd`` and optional ``--resume`` / ``-n``.

    ``mode`` selects the dispatch strategy:
      - ``auto``: try multiplexer first, then emulator window, then suspend.
      - ``window``: emulator window, then suspend.
      - ``suspend``: always suspend.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise LauncherError("`claude` no encontrado en PATH")

    argv = _build_claude_argv(session_id, display_name)
    cwd_str = str(cwd)

    if mode == "auto":
        if _try_multiplexer(argv, cwd_str):
            return
        if _try_window(argv, cwd_str):
            return
        _run_suspended(argv, cwd_str, app)
        return

    if mode == "window":
        if _try_window(argv, cwd_str):
            return
        _run_suspended(argv, cwd_str, app)
        return

    # mode == "suspend"
    _run_suspended(argv, cwd_str, app)


def _try_multiplexer(argv: list[str], cwd_str: str) -> bool:
    mux = detect_multiplexer()
    if mux == "tmux":
        subprocess.run(["tmux", "split-window", "-h", "-c", cwd_str, *argv], check=False)
        return True
    if mux == "zellij":
        subprocess.run(
            ["zellij", "action", "new-pane", "--cwd", cwd_str, "--", *argv],
            check=False,
        )
        return True
    if mux == "terminator":
        subprocess.run(
            ["terminator", "--new-tab", f"--working-directory={cwd_str}", "-x", *argv],
            check=False,
        )
        return True
    return False


def _try_window(argv: list[str], cwd_str: str) -> bool:
    """Spawn ``argv`` in a new window of the detected emulator. Returns True on dispatch."""
    emulator = detect_terminal_emulator()
    if emulator is None:
        return False
    spawn = _emulator_command(emulator, cwd_str, argv)
    if spawn is None:
        return False
    # Fully detach so the new window survives if the TUI process exits.
    subprocess.Popen(  # noqa: S603 — argv is fully controlled
        spawn,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _emulator_command(emulator: str, cwd_str: str, argv: list[str]) -> list[str] | None:
    """Return the argv to spawn ``argv`` in a new window of ``emulator``."""
    if emulator == "kitty":
        return ["kitty", "--directory", cwd_str, *argv]
    if emulator == "wezterm":
        return ["wezterm", "start", "--cwd", cwd_str, "--", *argv]
    if emulator == "alacritty":
        return ["alacritty", "--working-directory", cwd_str, "-e", *argv]
    if emulator == "konsole":
        return ["konsole", "--workdir", cwd_str, "-e", *argv]
    if emulator == "gnome-terminal":
        return ["gnome-terminal", f"--working-directory={cwd_str}", "--", *argv]
    if emulator == "foot":
        return ["foot", f"--working-directory={cwd_str}", *argv]
    if emulator == "terminator":
        # New window (not tab) when explicitly in WINDOW mode.
        return ["terminator", f"--working-directory={cwd_str}", "-x", *argv]
    if emulator == "ghostty":
        return ["ghostty", f"--working-directory={cwd_str}", "-e", *argv]
    if emulator in ("x-terminal-emulator", "xterm"):
        # No portable --cwd flag; wrap in a shell that cds first.
        joined = " ".join(_shell_quote(a) for a in argv)
        return [emulator, "-e", "sh", "-c", f"cd {_shell_quote(cwd_str)} && exec {joined}"]
    return None


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell quoting. Wraps in single quotes and escapes embedded quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


def _run_suspended(argv: list[str], cwd_str: str, app: "App | None") -> None:
    if app is not None:
        with app.suspend():
            subprocess.run(argv, cwd=cwd_str, check=False)
    else:
        subprocess.run(argv, cwd=cwd_str, check=False)


def _build_claude_argv(session_id: str | None, display_name: str | None = None) -> list[str]:
    argv = ["claude"]
    if session_id:
        argv += ["--resume", session_id]
    if display_name:
        argv += ["-n", display_name]
    return argv
