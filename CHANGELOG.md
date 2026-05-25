# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Windows 10/11 support**. The TUI now runs natively on Windows: `Path.home() / ".claude" / "projects"` correctly resolves to `C:\Users\<user>\.claude\projects`, and project rows show real Windows paths (`C:\…`, `D:\…`) extracted from each session's `cwd` field.
  - **Windows Terminal** added to the emulator table — detected via `WT_SESSION` env var. In `window`/`auto` mode the launcher spawns `wt.exe new-tab -d <cwd> -- claude [...]`, opening a new tab in the current WT window (or a new window if none is open).
  - **ConEmu** detected via `ConEmuPID` and surfaced as "not yet supported" with a clear error message (instead of falling through silently).
  - Config file path now prefers `%APPDATA%\multi-claude\config.json` on Windows (typically `C:\Users\<user>\AppData\Roaming\multi-claude\config.json`). `XDG_CONFIG_HOME` is still honoured if set, and `~/.config` remains the fallback when `%APPDATA%` is unavailable.
  - On Windows, `detect_multiplexer()` returns `None` (no tmux/zellij/terminator in the native environment) and `auto` falls through directly to window or suspend mode.
- User-defined project folders (`f` in ProjectsScreen) with **nesting**: paths like `Trabajo/Cliente A/Backend`. ProjectsScreen shows one row per root folder summarising direct members and descendants; `Enter` drills into a FolderScreen that lists subfolders + directly-assigned projects mixed together. Inside a folder, `n` creates a subfolder, `e` renames (cascading to descendants and assignments), `d` deletes (cascade unassigns members), `f` removes a project from the folder. Assignments override worktree-grouping for the assigned members. Persists to `~/.config/multi-claude/project-folders.json`. Filter (`/`) matches folder names. Dangling assignments (folder deleted out-of-band) are auto-cleaned on load.
- Bulk session cleanup (`D`) in SessionsScreen: pick a preset age (1w / 1m / 3m / 6m / 1y) or a custom `YYYY-MM-DD` date, see a live count of how many sessions would be deleted, confirm. Active sessions are skipped automatically.
- Per-session colour override (`c`): pick from a palette; persists to `~/.config/multi-claude/session-colors.json`.
- In-TUI editor for the colour rules (`Shift+C` / `C`): list, add (`a`), edit (`e` or Enter), delete (`d`), reorder (`j`/`k`). Save with `s`, cancel with `Esc`. Available from both ProjectsScreen and SessionsScreen since rules are global.
- Configurable colour rules in `~/.config/multi-claude/config.json` under `color_rules`. Each rule is `{"when": "<condition>", "color": "<rich-style>"}` and the first match wins. Manual overrides still beat any rule. Supported conditions:
  - `branch=main` — exact match (case-insensitive)
  - `branch~=feature/*` — glob over branch (or any field)
  - `prompt~=^/` — regex over the displayed prompt
  - `active=true` — session is reported as live in `~/.claude/sessions`
  - `age<1h` / `age<2d` / `age<3w` — last activity newer than the threshold

### Added

- `AppProtocol` (typed contract for the root app) to remove `# type: ignore[attr-defined]` on `app.prefs` / `app.names`.
- Extensible emulator dispatch table in `launcher.py` (one entry per emulator instead of an `if/elif` chain). Adds detection for `TERM_PROGRAM` values published by iTerm2, Apple Terminal, VS Code, Tabby and Warp (notified clearly when no builder exists).
- Stderr capture for `tmux` / `zellij` / `terminator` invocations: failures now surface as a `notify(severity="error")` instead of being swallowed.
- SQLite-backed session index (`~/.local/share/multi-claude/index.sqlite3`) used as cache plus an FTS5 virtual table for full-text search.
- Background scans via Textual workers; the TUI no longer freezes while parsing large session trees.
- Configurable sort: keys `1`/`2`/`3`/`4` cycle column sort in projects/sessions; direction toggled with `shift+s`. Persisted in `config.json`.
- Per-row preview panel (`p` to toggle) rendering the last turns of the selected session.
- Global FTS search screen (`shift+/`) across all indexed sessions.
- Worktree grouping under the same git repo (`g` to collapse/expand).
- Project merge flow (`m`) to reconcile orphaned projects whose cwd was renamed.
- Yank session id to the clipboard (`y`).
- Fuzzy matching in `/` filter via `rapidfuzz`, plus `key:value` operators (`branch:`, `path:`, `id:`).
- Contextual footer: row-dependent bindings only appear when a row is selected.
- `ruff`, `mypy`, GitHub Actions CI (matrix py3.10/3.11/3.12), `hatch-vcs` versioning, `CHANGELOG.md`, `CONTRIBUTING.md`.

### Changed

- macOS support removed from package classifiers until proper iTerm2 / Terminal.app detection lands.
- Footer hides row-dependent bindings (Rename, Delete, Launch alt) when no session is selected, so the available actions match the cursor state.

### Not done (deferred)

- Differentiating click from Enter on the sessions list: Textual's `DataTable` fires `RowSelected` for both click and Enter, so splitting them cleanly needs a custom widget. Tracked for a follow-up; for now click still launches.

### Fixed

- Deleting a project now refuses (with a confirm-override warning) when one of its sessions is reported as live in `~/.claude/sessions/`.

## [0.1.0] - 2026-05-22

Initial MVP release.

- Two-screen TUI: projects + sessions, sorted by last activity.
- Launch modes: `auto` (multiplexer split → emulator window → suspend), `window` (emulator window → suspend), `suspend`.
- Multiplexer detection: tmux, zellij, terminator.
- Emulator detection: kitty, WezTerm, Ghostty, Alacritty, Konsole, GNOME Terminal, foot, Terminator, x-terminal-emulator, xterm.
- Session rename (`e`), delete (`d`), and persistent display-name store at `~/.config/multi-claude/names.json`.
- Project add via `a` (launches Claude in a new cwd).
- Settings modal (`s`) to choose default / alternate launch mode (Shift+Enter = opposite of default).

[Unreleased]: https://github.com/Zarritas/multi-claude/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Zarritas/multi-claude/releases/tag/v0.1.0
