"""Launch ``claude`` in the right place depending on the surrounding environment.

Three launch modes:

- ``auto`` — multiplexer split > terminator tab > emulator window > suspend.
- ``window`` — emulator window > suspend.
- ``suspend`` — always suspend the TUI and run inline.

Emulators are described declaratively in :data:`EMULATORS`. Adding a new one means
appending an :class:`Emulator` entry: detection via env vars and/or ``TERM_PROGRAM``,
plus an ``argv`` callable that returns the spawn command. Multiplexers are kept
separate because their dispatch (split vs new pane) doesn't fit the same shape.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from multi_claude.config import LaunchMode

if TYPE_CHECKING:
    from textual.app import App

    AppLike = App[object]
else:
    AppLike = object


class LauncherError(RuntimeError):
    """Raised when claude cannot be launched (binary missing, dispatch failed, ...)."""


# --------------------------------------------------------------------------- #
# Multiplexer detection (tmux / zellij / terminator-as-mux)                    #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Emulator table                                                               #
# --------------------------------------------------------------------------- #


ArgvBuilder = Callable[[str, list[str]], list[str]]


@dataclass(frozen=True)
class Emulator:
    """A terminal emulator we know how to spawn a new window in.

    ``argv`` may be ``None`` for emulators we can detect but don't know how to spawn
    (e.g. VS Code's integrated terminal). Detection still helps surface a clear
    error message instead of a silent fallthrough.
    """

    id: str
    env_vars: tuple[str, ...] = ()
    term_programs: tuple[str, ...] = ()
    argv: ArgvBuilder | None = None
    binary: str = field(default="")

    def resolve_binary(self) -> str:
        return self.binary or self.id


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell quoting. Wraps in single quotes and escapes embedded quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


def _argv_kitty(cwd: str, argv: list[str]) -> list[str]:
    return ["kitty", "--directory", cwd, *argv]


def _argv_wezterm(cwd: str, argv: list[str]) -> list[str]:
    return ["wezterm", "start", "--cwd", cwd, "--", *argv]


def _argv_alacritty(cwd: str, argv: list[str]) -> list[str]:
    return ["alacritty", "--working-directory", cwd, "-e", *argv]


def _argv_konsole(cwd: str, argv: list[str]) -> list[str]:
    return ["konsole", "--workdir", cwd, "-e", *argv]


def _argv_gnome_terminal(cwd: str, argv: list[str]) -> list[str]:
    return ["gnome-terminal", f"--working-directory={cwd}", "--", *argv]


def _argv_foot(cwd: str, argv: list[str]) -> list[str]:
    return ["foot", f"--working-directory={cwd}", *argv]


def _argv_terminator(cwd: str, argv: list[str]) -> list[str]:
    return ["terminator", f"--working-directory={cwd}", "-x", *argv]


def _argv_ghostty(cwd: str, argv: list[str]) -> list[str]:
    return ["ghostty", f"--working-directory={cwd}", "-e", *argv]


def _argv_wt(cwd: str, argv: list[str]) -> list[str]:
    # `wt.exe new-tab -d <cwd> -- <cmd...>` opens the command in a new tab of the
    # current Windows Terminal window (or a new window if none is open).
    return ["wt.exe", "new-tab", "-d", cwd, "--", *argv]


def _argv_generic(binary: str) -> ArgvBuilder:
    def build(cwd: str, argv: list[str]) -> list[str]:
        joined = " ".join(_shell_quote(a) for a in argv)
        return [binary, "-e", "sh", "-c", f"cd {_shell_quote(cwd)} && exec {joined}"]

    return build


EMULATORS: tuple[Emulator, ...] = (
    Emulator(
        id="kitty",
        env_vars=("KITTY_PID",),
        term_programs=("kitty",),
        argv=_argv_kitty,
    ),
    Emulator(
        id="wezterm",
        env_vars=("WEZTERM_EXECUTABLE",),
        term_programs=("wezterm", "WezTerm"),
        argv=_argv_wezterm,
    ),
    Emulator(
        id="alacritty",
        env_vars=("ALACRITTY_WINDOW_ID", "ALACRITTY_LOG"),
        term_programs=("alacritty", "Alacritty"),
        argv=_argv_alacritty,
    ),
    Emulator(
        id="konsole",
        env_vars=("KONSOLE_VERSION",),
        argv=_argv_konsole,
    ),
    Emulator(
        id="gnome-terminal",
        env_vars=("GNOME_TERMINAL_SCREEN",),
        argv=_argv_gnome_terminal,
    ),
    Emulator(
        id="foot",
        env_vars=("FOOT_VERSION",),
        argv=_argv_foot,
    ),
    Emulator(
        id="terminator",
        env_vars=("TERMINATOR_UUID",),
        argv=_argv_terminator,
    ),
    Emulator(
        id="ghostty",
        env_vars=("GHOSTTY_RESOURCES_DIR", "GHOSTTY_BIN_DIR"),
        term_programs=("ghostty", "Ghostty"),
        argv=_argv_ghostty,
    ),
    Emulator(
        id="windows-terminal",
        env_vars=("WT_SESSION",),
        argv=_argv_wt,
        binary="wt.exe",
    ),
    # Detected but not supported as standalone windows. Detection still helps surface
    # a clear "not supported" message instead of silently falling through.
    Emulator(
        id="vscode",
        env_vars=("VSCODE_INJECTION",),
        term_programs=("vscode",),
        argv=None,
    ),
    Emulator(
        id="iterm",
        term_programs=("iTerm.app",),
        argv=None,
    ),
    Emulator(
        id="apple-terminal",
        term_programs=("Apple_Terminal",),
        argv=None,
    ),
    Emulator(
        id="tabby",
        term_programs=("tabby", "Tabby"),
        argv=None,
    ),
    Emulator(
        id="warp",
        term_programs=("WarpTerminal",),
        argv=None,
    ),
    Emulator(
        id="conemu",
        env_vars=("ConEmuPID",),
        argv=None,
    ),
)


_GENERIC_FALLBACKS = ("x-terminal-emulator", "xterm")


def detect_terminal_emulator() -> Emulator | None:
    """Return the matched :class:`Emulator` or ``None`` if no detection fires.

    Detection priority:

      1. ``$TERM_PROGRAM`` (canonical signal published by modern emulators).
      2. Emulator-specific env vars.
      3. Generic fallback: ``x-terminal-emulator`` / ``xterm`` if in PATH.

    Each step requires the matching binary to actually be in PATH; if not, we move on.
    """
    term_program = os.environ.get("TERM_PROGRAM", "")
    if term_program:
        tp_lower = term_program.lower()
        for emu in EMULATORS:
            if any(tp.lower() == tp_lower for tp in emu.term_programs) and (
                emu.argv is None or shutil.which(emu.resolve_binary())
            ):
                return emu

    for emu in EMULATORS:
        if not emu.env_vars:
            continue
        if any(os.environ.get(v) for v in emu.env_vars) and (
            emu.argv is None or shutil.which(emu.resolve_binary())
        ):
            return emu

    for fallback in _GENERIC_FALLBACKS:
        if shutil.which(fallback):
            return Emulator(id=fallback, argv=_argv_generic(fallback), binary=fallback)
    return None


# --------------------------------------------------------------------------- #
# Dispatch                                                                     #
# --------------------------------------------------------------------------- #


def launch_claude(
    cwd: Path,
    session_id: str | None = None,
    *,
    display_name: str | None = None,
    app: AppLike | None = None,
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

    _run_suspended(argv, cwd_str, app)


def _try_multiplexer(argv: list[str], cwd_str: str) -> bool:
    mux = detect_multiplexer()
    if mux is None:
        return False

    if mux == "tmux":
        spawn = ["tmux", "split-window", "-h", "-c", cwd_str, *argv]
    elif mux == "zellij":
        spawn = ["zellij", "action", "new-pane", "--cwd", cwd_str, "--", *argv]
    else:  # terminator
        spawn = ["terminator", "--new-tab", f"--working-directory={cwd_str}", "-x", *argv]

    try:
        result = subprocess.run(spawn, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise LauncherError(f"{mux} no encontrado al ejecutar: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else f"exit {result.returncode}"
        raise LauncherError(f"{mux} falló: {tail}")
    return True


def _try_window(argv: list[str], cwd_str: str) -> bool:
    """Spawn ``argv`` in a new window of the detected emulator. Returns True on dispatch."""
    emu = detect_terminal_emulator()
    if emu is None:
        return False
    if emu.argv is None:
        raise LauncherError(
            f"Emulador `{emu.id}` detectado pero no soportado para abrir ventana nueva. "
            f"Usa modo 'suspend' o cambia de emulador."
        )
    spawn = emu.argv(cwd_str, argv)
    # Fully detach so the new window survives if the TUI process exits.
    try:
        subprocess.Popen(
            spawn,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise LauncherError(f"{emu.id} no encontrado al ejecutar: {exc}") from exc
    return True


def _run_suspended(argv: list[str], cwd_str: str, app: AppLike | None) -> None:
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
