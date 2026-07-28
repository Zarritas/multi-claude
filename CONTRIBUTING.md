# Contributing

Thanks for considering a contribution to `multi-claude`. The project is small and the bar is informal — get the tests green, keep the diff focused.

## Setup

```bash
git clone https://github.com/Zarritas/multi-claude.git
cd multi-claude
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the checks

The CI matrix runs Python 3.10, 3.11 and 3.12 on Ubuntu. Locally, the four commands you need are:

```bash
ruff check .            # lint
ruff format --check .   # format check (use `ruff format .` to apply)
mypy src/multi_claude   # type checking
pytest -q               # tests
```

Run all four before opening a PR. CI will gate on them.

## Commit style

The existing history uses short imperative subjects (`Add configurable launch modes`, `Rename console script from mc to multi-claude`). Follow that.

If your change is user-visible, add an entry under `## [Unreleased]` in `CHANGELOG.md` in the appropriate subsection (`Added` / `Changed` / `Fixed` / `Removed`).

## Cutting a release

Versions come from git tags via `hatch-vcs`. To cut `0.2.0`:

```bash
# 1. Move the entries under [Unreleased] in CHANGELOG.md into a new [0.2.0] section.
# 2. Commit.
git tag v0.2.0
git push origin main --tags
```

A wheel installed from the tagged commit will report `0.2.0`. An install from a checkout between tags reports something like `0.2.0.dev3+gabcdef0`.

## Architecture cheatsheet

- `src/multi_claude/app.py` — root Textual app, owns prefs and the names store.
- `src/multi_claude/discovery.py` — scans `~/.claude/projects/`, resolves real cwds.
- `src/multi_claude/session.py` — parses headers from `.jsonl` files cheaply.
- `src/multi_claude/launcher.py` — dispatches `claude --resume` into a multiplexer pane, a tab of the current window, a new emulator window, or the suspended TUI. Adding an emulator = one entry in `EMULATORS`.
- `src/multi_claude/index.py` — SQLite cache + FTS5 search.
- `src/multi_claude/screens/` — Textual screens (projects, sessions, search, worktrees).
- `src/multi_claude/widgets/` — reusable widgets (preview panel).
- `src/multi_claude/modals.py` — modal dialogs (rename, add project, confirm delete, settings, merge).
- `tests/conftest.py::write_session` — builder for synthetic Claude project trees on `tmp_path`. Reuse it.

## Scope hints

- The `.jsonl` files are the source of truth. SQLite is a cache; if it diverges, blow it away.
- Avoid heavy dependencies. `textual` and `rapidfuzz` are the only runtime deps; question anything else.
- Linux-only for now. macOS detection (iTerm2, Terminal.app) is welcome — pair it with manual testing on a real Mac.
