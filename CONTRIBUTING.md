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

## Screenshots and the demo GIF

The images in the README are generated, not cropped by hand — a screenshot goes stale the
moment a column or a binding changes, and a stale one documents a version that no longer
exists. Both come off the same synthetic machine (`tools/demo_world.py`), so the stills and
the recording tell one story. The data is invented on purpose (`/tmp/multi-claude-demo`,
`ana@example.com`): never commit an image with real paths, client names or addresses.

**Stills.** Textual can export a screen to SVG, so this needs nothing installed:

```bash
python tools/screenshots.py       # writes docs/img/*.svg from the real TUI, headless
```

Then render the SVGs to the committed PNGs — the header of `tools/screenshots.py` has the
exact command (the SVGs are not committed: they pull Fira Code from a CDN that GitHub
strips).

**The GIF.** Recorded with [vhs](https://github.com/charmbracelet/vhs), which needs `vhs`,
`ttyd` and `ffmpeg` on `PATH`:

```bash
vhs tools/demo.tape              # writes docs/img/demo.gif
```

`tools/demo.tape` drives `tools/demo_app.py`, which runs the real app with two things
patched: the live-session registry (so the Estado column shows something without real
Claude processes) and `launch_claude` (so pressing Enter reports a placement instead of
spawning a terminal mid-take).

Two things to know if you edit the tape: vhs types keys, it cannot click — every step has
to be reachable from the keyboard — and Textual toasts last about five seconds, so actions
that raise one need spacing or they stack up and cover the table.

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
- `src/multi_claude/mcp.py` — MCP server over the index (JSON-RPC 2.0 on stdio, no SDK).
- `src/multi_claude/remote.py` — the shared-session transport, and the manifest format. Bumping `VERSION` means adding the new version to `_READABLE_VERSIONS` too: an old manifest must stay readable, or a colleague's published sessions vanish from everyone else's listing on upgrade.
- `src/multi_claude/secret_scan.py` — pre-publish credential scan. If you add or loosen a rule, re-measure against real transcripts before believing the tests: the rules were calibrated that way, and `tests/test_secret_scan.py` guards both directions (what must be caught, and the ordinary conversation that must not be). Anywhere you print transcript text back at the user, put it through `redact()` first.
- `src/multi_claude/audit.py` — the same scan over the whole history, behind `multi-claude --audit-secrets`. Exits 1 when it finds something, so it can hang off a hook.

> **Credential-shaped test fixtures must be assembled at runtime** (`"sk" + "_live_…"`), never written out. GitHub's push protection scans the test files too and cannot tell a fixture from the real thing, so a literal one blocks the push of the very test that proves we detect it — which is how this rule was learned. `tests/test_secret_scan.py::_like` is the helper.
- `tests/conftest.py::write_session` — builder for synthetic Claude project trees on `tmp_path`. Reuse it.
- `tests/conftest.py::isolated_index` — autouse; points `XDG_DATA_HOME` at `tmp_path` so tests never write into your own index. Don't disable it.
- `tools/demo_world.py` — the synthetic machine the docs images are recorded on.
- `tools/screenshots.py` — regenerates the README stills from the real screens.
- `tools/demo.tape` + `tools/demo_app.py` — the README's demo GIF, recorded with vhs.

## Scope hints

- The `.jsonl` files are the source of truth. SQLite is a cache; if it diverges, blow it away.
- Avoid heavy dependencies. `textual` and `rapidfuzz` are the only runtime deps; question anything else.
- Linux-only for now. macOS detection (iTerm2, Terminal.app) is welcome — pair it with manual testing on a real Mac.
