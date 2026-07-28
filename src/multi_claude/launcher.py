"""Launch ``claude`` in the right place depending on the surrounding environment.

Launch modes, all of them degrading down the same chain rather than failing:

- ``auto`` — multiplexer split > tab in the current window > new window > suspend.
- ``split`` — multiplexer pane > tab > window > suspend.
- ``tab`` — tab in the current window > window > suspend.
- ``window`` — new emulator window > suspend.
- ``suspend`` — always suspend the TUI and run inline.

Emulators are described declaratively in :data:`EMULATORS`. Adding a new one means
appending an :class:`Emulator` entry: detection via env vars and/or ``TERM_PROGRAM``,
plus an ``argv`` callable for a new window and (when the emulator can do it from the
CLI) a ``tab_argv`` one. Multiplexers are kept separate because their dispatch
(pane vs tab) doesn't fit the same shape.
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


@dataclass(frozen=True)
class LaunchOutcome:
    """What the launcher actually did, so the UI can report degradations.

    ``placement`` is where the session landed (``split``/``tab``/``window``/``suspend``),
    ``target`` the tool that got it there (``tmux``, ``gnome-terminal``, ...), and
    ``fallback_reason`` a human-readable note when the requested placement wasn't
    available and we silently used a different one.
    """

    placement: str
    target: str
    fallback_reason: str | None = None


#: Human-readable names for :attr:`LaunchOutcome.placement`, for UI notifications.
PLACEMENT_LABELS: dict[str, str] = {
    "split": "Panel nuevo",
    "tab": "Pestaña nueva",
    "window": "Ventana nueva",
    "suspend": "Ejecutado en esta terminal",
}


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

    ``tab_argv`` is the equivalent for a new tab in the *current* window; ``None``
    means the emulator has no tabs or no CLI to reach them, and tab launches
    degrade to a new window. ``tab_rpc`` marks tab commands that are remote-control
    clients (``kitty @``, ``wezterm cli``): they exit immediately and fail loudly
    when the feature is disabled, so we can check their exit code and fall back.
    """

    id: str
    env_vars: tuple[str, ...] = ()
    term_programs: tuple[str, ...] = ()
    argv: ArgvBuilder | None = None
    tab_argv: ArgvBuilder | None = None
    tab_rpc: bool = False
    binary: str = field(default="")

    def resolve_binary(self) -> str:
        return self.binary or self.id


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell quoting. Wraps in single quotes and escapes embedded quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


def _argv_kitty(cwd: str, argv: list[str]) -> list[str]:
    return ["kitty", "--directory", cwd, *argv]


def _tab_kitty(cwd: str, argv: list[str]) -> list[str]:
    # Needs `allow_remote_control` in kitty.conf; `kitty @` picks the target up from
    # $KITTY_LISTEN_ON. Exits non-zero when remote control is off, hence tab_rpc.
    return ["kitty", "@", "launch", "--type=tab", "--cwd", cwd, "--", *argv]


def _argv_wezterm(cwd: str, argv: list[str]) -> list[str]:
    return ["wezterm", "start", "--cwd", cwd, "--", *argv]


def _tab_wezterm(cwd: str, argv: list[str]) -> list[str]:
    # `wezterm cli spawn` defaults to a new tab in the current window.
    return ["wezterm", "cli", "spawn", "--cwd", cwd, "--", *argv]


def _argv_alacritty(cwd: str, argv: list[str]) -> list[str]:
    return ["alacritty", "--working-directory", cwd, "-e", *argv]


def _argv_konsole(cwd: str, argv: list[str]) -> list[str]:
    return ["konsole", "--workdir", cwd, "-e", *argv]


def _tab_konsole(cwd: str, argv: list[str]) -> list[str]:
    return ["konsole", "--new-tab", "--workdir", cwd, "-e", *argv]


def _argv_gnome_terminal(cwd: str, argv: list[str]) -> list[str]:
    return ["gnome-terminal", "--window", f"--working-directory={cwd}", "--", *argv]


def _tab_gnome_terminal(cwd: str, argv: list[str]) -> list[str]:
    # `--tab` targets the most recently focused window of the running
    # gnome-terminal-server, which is the one the TUI is sitting in.
    return ["gnome-terminal", "--tab", f"--working-directory={cwd}", "--", *argv]


def _argv_foot(cwd: str, argv: list[str]) -> list[str]:
    return ["foot", f"--working-directory={cwd}", *argv]


def _argv_terminator(cwd: str, argv: list[str]) -> list[str]:
    return ["terminator", f"--working-directory={cwd}", "-x", *argv]


def _tab_terminator(cwd: str, argv: list[str]) -> list[str]:
    return ["terminator", "--new-tab", f"--working-directory={cwd}", "-x", *argv]


def _argv_ghostty(cwd: str, argv: list[str]) -> list[str]:
    return ["ghostty", f"--working-directory={cwd}", "-e", *argv]


def _argv_wt(cwd: str, argv: list[str]) -> list[str]:
    # `-w -1` forces a brand new Windows Terminal window; without it `new-tab`
    # would land in the current one (that's what _tab_wt is for).
    return ["wt.exe", "-w", "-1", "new-tab", "-d", cwd, "--", *argv]


def _tab_wt(cwd: str, argv: list[str]) -> list[str]:
    # `-w 0` addresses the current window.
    return ["wt.exe", "-w", "0", "new-tab", "-d", cwd, "--", *argv]


def _applescript_quote(s: str) -> str:
    """Wrap ``s`` as a double-quoted AppleScript string literal.

    AppleScript double-quoted strings only need ``\\`` and ``"`` escaped. The
    embedded shell command we wrap is already POSIX-single-quoted, so any
    single quotes inside ``s`` need no further treatment here.
    """
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _argv_apple_terminal(cwd: str, argv: list[str]) -> list[str]:
    # Terminal.app has no CLI flag to run a command in a directory, so we drive
    # it via AppleScript. `do script` opens a fresh window; `activate` brings
    # Terminal to the foreground so the user sees the new session immediately.
    shell_cmd = f"cd {_shell_quote(cwd)} && exec " + " ".join(_shell_quote(a) for a in argv)
    as_literal = _applescript_quote(shell_cmd)
    return [
        "osascript",
        "-e",
        f"tell application \"Terminal\" to do script {as_literal}",
        "-e",
        'tell application "Terminal" to activate',
    ]


def _argv_iterm(cwd: str, argv: list[str]) -> list[str]:
    # Two-step form (create window, then `write text` into the new session) works
    # across iTerm2 versions; the one-shot `command` parameter is inconsistent.
    shell_cmd = f"cd {_shell_quote(cwd)} && exec " + " ".join(_shell_quote(a) for a in argv)
    as_literal = _applescript_quote(shell_cmd)
    return [
        "osascript",
        "-e", 'tell application "iTerm"',
        "-e", "  create window with default profile",
        "-e", f"  tell current session of current window to write text {as_literal}",
        "-e", "end tell",
    ]


def _tab_iterm(cwd: str, argv: list[str]) -> list[str]:
    # Same two-step shape as _argv_iterm, but creating a tab inside the current
    # window instead of a new window.
    shell_cmd = f"cd {_shell_quote(cwd)} && exec " + " ".join(_shell_quote(a) for a in argv)
    as_literal = _applescript_quote(shell_cmd)
    return [
        "osascript",
        "-e", 'tell application "iTerm"',
        "-e", "  tell current window to create tab with default profile",
        "-e", f"  tell current session of current window to write text {as_literal}",
        "-e", "end tell",
    ]


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
        tab_argv=_tab_kitty,
        tab_rpc=True,
    ),
    Emulator(
        id="wezterm",
        env_vars=("WEZTERM_EXECUTABLE",),
        term_programs=("wezterm", "WezTerm"),
        argv=_argv_wezterm,
        tab_argv=_tab_wezterm,
        tab_rpc=True,
    ),
    # No tabs on Linux/Windows, and no CLI for the macOS ones: tab launches
    # degrade to a new window.
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
        tab_argv=_tab_konsole,
    ),
    Emulator(
        id="gnome-terminal",
        env_vars=("GNOME_TERMINAL_SCREEN",),
        argv=_argv_gnome_terminal,
        tab_argv=_tab_gnome_terminal,
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
        tab_argv=_tab_terminator,
    ),
    # Ghostty's CLI exposes `+new-window` but no `+new-tab` action yet.
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
        tab_argv=_tab_wt,
        binary="wt.exe",
    ),
    Emulator(
        id="iterm",
        term_programs=("iTerm.app",),
        argv=_argv_iterm,
        tab_argv=_tab_iterm,
        binary="osascript",
    ),
    # Terminal.app can only open tabs by synthesising ⌘T through System Events,
    # which needs accessibility permissions and races with the window focus.
    Emulator(
        id="apple-terminal",
        term_programs=("Apple_Terminal",),
        argv=_argv_apple_terminal,
        binary="osascript",
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
    claude_args: list[str] | None = None,
) -> LaunchOutcome:
    """Launch ``claude`` with ``cwd`` and optional ``--resume`` / ``-n``.

    ``mode`` picks where the session lands; each mode degrades to the next one down
    when its target isn't available (see the module docstring). ``claude_args`` are
    extra user-configured flags, inserted before the ones the TUI owns.

    Returns a :class:`LaunchOutcome` describing what actually happened, so callers
    can tell the user when they asked for a tab and got a window.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise LauncherError("`claude` no encontrado en PATH")

    argv = _build_claude_argv(session_id, display_name, claude_args)
    cwd_str = str(cwd)

    reason: str | None = None

    if mode in ("auto", "split"):
        outcome = _try_multiplexer(argv, cwd_str, want_tab=False)
        if outcome is not None:
            return outcome
        if mode == "split":
            reason = "sin multiplexer activo"
        mode = "tab"

    if mode == "tab":
        outcome = _try_multiplexer(argv, cwd_str, want_tab=True)
        if outcome is not None:
            return _with_reason(outcome, reason)
        outcome, tab_reason = _try_tab(argv, cwd_str)
        if outcome is not None:
            return _with_reason(outcome, reason)
        reason = tab_reason or reason
        mode = "window"

    if mode == "window":
        outcome, window_reason = _try_window(argv, cwd_str)
        if outcome is not None:
            return _with_reason(outcome, reason)
        reason = reason or window_reason or "no se detectó ningún emulador"

    _run_suspended(argv, cwd_str, app)
    return LaunchOutcome(placement="suspend", target="inline", fallback_reason=reason)


def preview_dispatch(mode: LaunchMode) -> LaunchOutcome:
    """Resolve where ``mode`` *would* put a session right now, launching nothing.

    Mirrors the chain in :func:`launch_claude` using detection only, so the settings
    screen can tell the user what their choice means on this machine. In this
    context ``fallback_reason`` also carries caveats that aren't degradations
    (e.g. kitty needing remote control enabled).
    """
    mux = detect_multiplexer()
    emu = detect_terminal_emulator()
    reason: str | None = None

    if mode in ("auto", "split"):
        if mux == "terminator":
            return LaunchOutcome("tab", mux)
        if mux is not None:
            return LaunchOutcome("split", mux)
        if mode == "split":
            reason = "sin multiplexer activo"
        mode = "tab"

    if mode == "tab":
        if mux == "zellij":
            return LaunchOutcome("split", mux, "zellij no abre pestañas con un comando")
        if mux is not None:
            return _with_reason(LaunchOutcome("tab", mux), reason)
        if emu is not None and emu.tab_argv is not None:
            note = "requiere control remoto habilitado" if emu.tab_rpc else None
            return _with_reason(LaunchOutcome("tab", emu.id), reason or note)
        if emu is not None:
            reason = f"`{emu.id}` no sabe abrir pestañas desde la CLI"
        mode = "window"

    if mode == "window":
        if emu is not None and emu.argv is not None:
            return _with_reason(LaunchOutcome("window", emu.id), reason)
        if emu is not None:
            reason = f"`{emu.id}` no puede abrir ventanas desde la CLI"
        else:
            reason = reason or "no se detectó ningún emulador"

    return LaunchOutcome("suspend", "inline", reason)


def _with_reason(outcome: LaunchOutcome, reason: str | None) -> LaunchOutcome:
    if reason is None or outcome.fallback_reason is not None:
        return outcome
    return LaunchOutcome(outcome.placement, outcome.target, reason)


def _try_multiplexer(argv: list[str], cwd_str: str, *, want_tab: bool) -> LaunchOutcome | None:
    """Dispatch into the surrounding multiplexer, or return None if there isn't one."""
    mux = detect_multiplexer()
    if mux is None:
        return None

    placement = "tab" if want_tab else "split"
    reason: str | None = None

    if mux == "tmux":
        verb = ["new-window"] if want_tab else ["split-window", "-h"]
        spawn = ["tmux", *verb, "-c", cwd_str, *argv]
    elif mux == "zellij":
        # `zellij action new-tab` can't take a command (only a layout), so a tab
        # request lands in a pane instead.
        spawn = ["zellij", "action", "new-pane", "--cwd", cwd_str, "--", *argv]
        if want_tab:
            placement = "split"
            reason = "zellij no puede abrir pestañas con un comando; se usó un panel"
    else:  # terminator — only does tabs, never panes, from the CLI
        spawn = ["terminator", "--new-tab", f"--working-directory={cwd_str}", "-x", *argv]
        placement = "tab"

    try:
        result = subprocess.run(spawn, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise LauncherError(f"{mux} no encontrado al ejecutar: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else f"exit {result.returncode}"
        raise LauncherError(f"{mux} falló: {tail}")
    return LaunchOutcome(placement=placement, target=mux, fallback_reason=reason)


def _try_tab(argv: list[str], cwd_str: str) -> tuple[LaunchOutcome | None, str | None]:
    """Open ``argv`` in a new tab of the current window.

    Returns ``(outcome, reason)``: the outcome is None when no tab could be opened,
    and the reason explains why so the caller can report the degradation.
    """
    emu = detect_terminal_emulator()
    if emu is None:
        return None, None
    if emu.tab_argv is None:
        return None, f"`{emu.id}` no sabe abrir pestañas desde la CLI"

    spawn = emu.tab_argv(cwd_str, argv)
    if emu.tab_rpc:
        # Remote-control clients exit right away; a non-zero code means the feature
        # is off (e.g. kitty without allow_remote_control), so fall through.
        try:
            result = subprocess.run(spawn, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise LauncherError(f"{emu.id} no encontrado al ejecutar: {exc}") from exc
        if result.returncode != 0:
            stderr = (result.stderr or "").strip().splitlines()
            tail = stderr[-1] if stderr else f"exit {result.returncode}"
            return None, f"el control remoto de `{emu.id}` falló: {tail}"
        return LaunchOutcome(placement="tab", target=emu.id), None

    _spawn_detached(spawn, emu.id)
    return LaunchOutcome(placement="tab", target=emu.id), None


def _try_window(argv: list[str], cwd_str: str) -> tuple[LaunchOutcome | None, str | None]:
    """Spawn ``argv`` in a new window of the detected emulator.

    Same ``(outcome, reason)`` shape as :func:`_try_tab`. Emulators we can detect
    but not drive (VS Code's integrated terminal, Warp, ...) report a reason and
    let the caller fall through to an inline run.
    """
    emu = detect_terminal_emulator()
    if emu is None:
        return None, None
    if emu.argv is None:
        return None, f"`{emu.id}` no puede abrir ventanas desde la CLI"
    _spawn_detached(emu.argv(cwd_str, argv), emu.id)
    return LaunchOutcome(placement="window", target=emu.id), None


def _spawn_detached(spawn: list[str], emu_id: str) -> None:
    """Fully detach so the new terminal survives if the TUI process exits."""
    try:
        subprocess.Popen(
            spawn,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise LauncherError(f"{emu_id} no encontrado al ejecutar: {exc}") from exc


def _run_suspended(argv: list[str], cwd_str: str, app: AppLike | None) -> None:
    if app is not None:
        with app.suspend():
            subprocess.run(argv, cwd=cwd_str, check=False)
    else:
        subprocess.run(argv, cwd=cwd_str, check=False)


def _build_claude_argv(
    session_id: str | None,
    display_name: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build the claude argv.

    User extras go first so that anything the TUI appends (``--resume``, ``-n``)
    wins if the same flag somehow appears twice.
    """
    argv = ["claude", *(extra_args or [])]
    if session_id:
        argv += ["--resume", session_id]
    if display_name:
        argv += ["-n", display_name]
    return argv
