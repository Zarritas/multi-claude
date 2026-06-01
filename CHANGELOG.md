# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Project identity flipped when a session was moved/resumed across cwds.** `resolve_real_cwd()` took the newest session's first `cwd` event as the project's path. A session moved into a subdir-project and then resumed there records its *original* (parent) cwd in its first event but its *current* cwd in later events; being the newest file, its stale first cwd hijacked the whole project's identity (e.g. the `migrations` project rendered as `gextia-dev`). It now prefers the candidate cwd whose Claude encoding (`encode_cwd()`, every non-alphanumeric char → `-`) matches the project dir's own name — the cwd Claude named that dir after — and only falls back to the newest-first-cwd when nothing matches. No files are moved; the moved session stays a valid session of wherever it now runs.
- **Worktree grouping wrongly folded plain subdirectories of a checkout into the repo's group.** `git rev-parse --git-common-dir` returns the same common dir for *any* path inside a repo, so a subdirectory-cwd (e.g. `repo/projects/migrations`, `repo/projects/audits`) shared the repo root's key and `group_worktrees` collapsed them all under one "repo" entry — making distinct projects appear to merge (e.g. `migrations` showing as `gextia-dev`) and letting the move feature offer sessions to be shuffled between them. `resolve_git_common_dir()` now also runs `--show-toplevel` and returns the common dir **only when the cwd is the worktree root** (`toplevel == path`); subdirectory-cwds return `None` and stay standalone. Real linked worktrees (distinct top-levels sharing a common dir) still group as before. No data was moved by the previous behaviour — it was a grouping/display defect.

### Added

- **Export / import sessions to share with a colleague** (`x` in SessionsScreen, `i` in ProjectsScreen). New `transfer.py` module. **Export** bundles the selected sessions (the `Space`-marked set, or the highlighted row) into a single shareable `.zip`: `manifest.json` (format/version, plus per-session `id`, `cwd`, `branch`, `display_name`, `tags`, `first_prompt`, counts) + `sessions/<id>.jsonl` + the `sessions/<id>/` subagents subdir. A `FilePathModal` proposes a default path under `~/Downloads` (single session → a slugified `<name>.claude-session.zip`; many → `claude-sessions-N.zip`). The SQLite index is left out (cache, rebuilt on scan) and `session-env` is deliberately excluded (machine-local, may hold secrets; Claude recreates it on resume). **Import** reads and validates the manifest (`ArchiveError` on a non-zip, missing/corrupt manifest, wrong format/version, or empty session list), then an `ImportTargetModal` lets you pick which of *your* existing live projects the sessions land in — Claude resumes them under that project's cwd (the jsonl's embedded cwd is historical and isn't rewritten). Display name and tags are restored from the manifest. Guards: an id already present at the destination is skipped (never overwritten); extraction is path-traversal-safe (a crafted `sessions/<id>/../../x` member raises `ArchiveError` instead of escaping the destination); the toast reports imported / skipped-existing / missing-payload counts. Importing needs at least one existing project to target (open Claude somewhere first).
- **Move sessions between worktrees** (`m` in SessionsScreen, with `Space` multi-selection). Mark any number of rows with `Space` (a green `✓` prefixes the marked rows) or act on the highlighted row when nothing is marked, then `m` opens a destination picker scoped to the **other live members of the current repo's worktree group** (the main checkout and sibling worktrees, resolved by shared `git_common_dir`). `move_session()` relocates the `<id>.jsonl` plus its sibling `<id>/` subdir into the destination's encoded-path dir and repoints the session's SQLite index row (`project_dir` + `jsonl_path`) so the global search keeps the right path — Claude then resumes the session under the destination's cwd. Per-session artefacts keyed by id (display name, tags, `session-env`) need no move. Guards: a session reported live in `~/.claude/sessions/` is skipped (`SessionActiveError`) and an id already present at the destination is refused without overwrite (`SessionCollisionError`); the post-move toast reports moved / skipped-active / conflict counts. Destinations only list worktrees that already hold at least one session (a worktree with no sessions yet isn't a discovered project).
- **Session tags** (`t` in SessionsScreen): attach flat, multi-assignment labels to a session. The modal accepts space- and comma-separated input (`bug, urgente cliente-acme`), normalises to lowercase with `-` for internal whitespace, drops reserved chars (`,`, `:`), and offers click-to-toggle plus `Tab`-autocomplete over previously-used tags. Tags render as `#cyan` chips in a new **Tags** column. Filter syntax `tag:bug` (substring) and `tag:bug,urgent` (AND); tag strings also feed the free-text fuzzy haystack so `/refactor` matches `#refactor`. New `3` binding sorts by tag (tagged rows cluster alphabetically; untagged sink to the bottom asc / rise to the top desc). Persists to `~/.config/multi-claude/session-tags.json` keyed by session id (UUID, globally unique). Tag entries are dropped when a session is deleted (single-session delete, bulk cleanup and project-cascade delete all pass the store through).
- **Pick up Claude's `/rename` as a display-name fallback**: `extract_embedded_name()` scans the session jsonl for `system/local_command` events whose stdout matches `<local-command-stdout>Session renamed to: X</local-command-stdout>` and returns the **latest** match (so repeated `/rename`s in Claude let the most recent one win). Precedence in the listing is now `NamesStore` (multi-claude's own rename via `e`) > embedded name (Claude's `/rename`) > first prompt. The embedded name is cached in a new `embedded_name TEXT` column on the `sessions` SQLite table (idempotent `PRAGMA table_info` + `ALTER TABLE` migration; no DB rebuild needed), so cache hits don't re-scan the jsonl. No write-back to Claude — multi-claude's renames stay private to multi-claude, Claude's `/rename` flows in one direction into the listing.
- **macOS support** for spawning new windows in `window`/`auto` mode:
  - **iTerm2** (`TERM_PROGRAM=iTerm.app`) — drives iTerm2 via AppleScript: `tell application "iTerm" to create window with default profile` followed by `write text "cd <cwd> && exec claude [...]"` into the new session. Uses the two-step form because the one-shot `command` parameter is inconsistent across iTerm2 versions.
  - **Terminal.app** (`TERM_PROGRAM=Apple_Terminal`) — `tell application "Terminal" to do script "cd <cwd> && exec claude [...]"` followed by `activate` so the new window comes to the foreground.
  - Both go through `osascript` (always available on macOS). Display names and paths with embedded quotes or backslashes round-trip safely through the POSIX-single-quote + AppleScript-escape layers.
  - Cross-platform emulators (kitty, WezTerm, Ghostty, Alacritty) already worked on macOS without any change — only iTerm2 and Terminal.app needed native AppleScript dispatch.
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
