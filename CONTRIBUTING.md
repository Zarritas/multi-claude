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

## Two READMEs

`README.md` is English and is what GitHub shows; `README.es.md` is Spanish and matches the
language of the interface itself. They are the same document, and they drift the moment one is edited
alone — most of the stale claims found while translating had been true once. **If a change is
user-visible, update both**, and cross-check the anchors: the two files have different heading slugs,
so a link that works in one is not automatically valid in the other.

The interface itself is in Spanish, which is why the English README carries screenshots with Spanish
labels and quotes the cells verbatim (`○ te espera`, `Publicar de todas formas`). Those are what the
user actually sees; translating them in the docs only would be worse than leaving them.

## Commit style

The existing history uses short imperative subjects (`Add configurable launch modes`, `Rename console script from mc to multi-claude`). Follow that.

If your change is user-visible, add an entry under `## [Unreleased]` in `CHANGELOG.md` in the appropriate subsection (`Added` / `Changed` / `Fixed` / `Removed`).

## Cutting a release

Versions come from git tags via `hatch-vcs`, and the tag is the only trigger: pushing `v*`
runs `.github/workflows/release.yml`, which builds the wheel and the sdist and publishes a
GitHub release with the CHANGELOG's notes attached. To cut `1.1.0`:

```bash
# 1. Move the entries under [Unreleased] in CHANGELOG.md into a new [1.1.0] section,
#    dated, and update the link definitions at the foot of the file.
# 2. Check what the release will say — same script the workflow runs, so this is the
#    real thing and not an approximation:
python tools/release_notes.py 1.1.0
# 3. Commit, tag, push.
git tag v1.1.0
git push origin main --tags
```

The workflow reads the notes **before** building anything, so a tag whose version has no
CHANGELOG section fails there instead of publishing a release with the previous version's
notes. It also refuses to publish if the built artefact's version does not match the tag,
which is what a shallow checkout or a tag pushed without its commit looks like.

A wheel installed from the tagged commit reports `1.1.0`; an install from a checkout
between tags reports something like `1.1.0.dev3+gabcdef0`. `multi-claude --version` prints
whichever it is — ask for it in any bug report.

There is no PyPI package: installation is from a git URL, which can be pinned to a tag
(`uv tool install git+https://github.com/Zarritas/multi-claude.git@v1.0.0`). Publishing to
PyPI would only mean adding a `pypa/gh-action-pypi-publish` step with trusted publishing to
the same workflow.

**Since 1.0.0, three things are public surface** and cannot break without a major: the key
bindings, the format of the state files under `~/.config/multi-claude/`, and the published
session manifest (see `remote.py`'s `_READABLE_VERSIONS`).

## Architecture cheatsheet

- `src/multi_claude/app.py` — root Textual app, owns prefs and the names store.
- `src/multi_claude/discovery.py` — scans `~/.claude/projects/`, resolves real cwds.
- `src/multi_claude/session.py` — parses headers from `.jsonl` files cheaply.
- `src/multi_claude/launcher.py` — dispatches `claude --resume` into a multiplexer pane, a tab of the current window, a new emulator window, or the suspended TUI. Adding an emulator = one entry in `EMULATORS`.
- `src/multi_claude/index.py` — SQLite cache + FTS5 search, plus `session_files` behind `file:`. Anything new pulled out of a jsonl means bumping `EXTRACT_VERSION`, or rows written by the older build stay stale forever behind an unchanged mtime — and a filter that silently misses the whole history is worse than one that is not there.
- `src/multi_claude/screens/` — Textual screens (projects, sessions, search, worktrees).
- `src/multi_claude/widgets/` — reusable widgets (preview panel).
- `src/multi_claude/modals.py` — modal dialogs (rename, add project, confirm delete, settings, merge).
- `src/multi_claude/mcp.py` — MCP server over the index (JSON-RPC 2.0 on stdio, no SDK).
- `src/multi_claude/remote.py` — the shared-session transport, and the manifest format. Bumping `VERSION` means adding the new version to `_READABLE_VERSIONS` too: an old manifest must stay readable, or a colleague's published sessions vanish from everyone else's listing on upgrade.
- `src/multi_claude/publish_guard.py` — decides whether a publish would overwrite someone else's version. Pure: no UI, no network, and it never writes. If you touch it, remember the error directions are not symmetric — failing to warn loses a colleague's turns.
- `src/multi_claude/secret_scan.py` — pre-publish credential scan. If you add or loosen a rule, re-measure against real transcripts before believing the tests: the rules were calibrated that way, and `tests/test_secret_scan.py` guards both directions (what must be caught, and the ordinary conversation that must not be). Anywhere you print transcript text back at the user, put it through `redact()` first.
- `src/multi_claude/audit.py` — the same scan over the whole history, behind `multi-claude --audit-secrets`. Exits 1 when it finds something, so it can hang off a hook.

> **Credential-shaped test fixtures must be assembled at runtime** (`"sk" + "_live_…"`), never written out. GitHub's push protection scans the test files too and cannot tell a fixture from the real thing, so a literal one blocks the push of the very test that proves we detect it — which is how this rule was learned. `tests/test_secret_scan.py::_like` is the helper.
- `tests/conftest.py::write_session` — builder for synthetic Claude project trees on `tmp_path`. Reuse it.
- `tests/conftest.py::isolated_index` — autouse; points `XDG_DATA_HOME` at `tmp_path` so tests never write into your own index. Don't disable it.
- `tools/release_notes.py` — pulls one version's body out of the CHANGELOG for the release workflow. Exits 1 when the section is missing, which is what stops a tag from shipping without notes.
- `tools/demo_world.py` — the synthetic machine the docs images are recorded on.
- `tools/screenshots.py` — regenerates the README stills from the real screens.
- `tools/demo.tape` + `tools/demo_app.py` — the README's demo GIF, recorded with vhs.

## Scope hints

- The `.jsonl` files are the source of truth. SQLite is a cache; if it diverges, blow it away.
- Avoid heavy dependencies. `textual` and `rapidfuzz` are the only runtime deps; question anything else.
- Linux is where it is developed and where CI runs, but macOS (iTerm2, Terminal.app via `osascript`) and Windows (Windows Terminal) are implemented and shipped — the classifiers promise all three. Anything touching `launcher.py` or `focus.py` is platform code that CI cannot really exercise: pair it with manual testing on the real OS, and say in the PR which one you tested on.
