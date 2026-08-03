"""Regenerate the README screenshots from the real TUI, with synthetic data.

The point of scripting this rather than cropping a terminal by hand: the shots go stale
every time a column, a binding or a colour changes, and a stale screenshot is worse than
none — it documents a version that no longer exists. This drives the actual app headless
through textual's test pilot and exports each screen, so regenerating is one command.

The machine being shown is built by :mod:`demo_world`, shared with the demo GIF so both
tell the same story. Everything in it is invented.

Usage::

    python tools/screenshots.py [outdir]        # writes <outdir>/*.svg, default docs/img

The SVGs reference Fira Code from a CDN, which GitHub strips, so the committed images are
PNGs rendered from them::

    cd docs/img
    for f in *.svg; do
        read -r w h <<<"$(python -c "
    import re,sys
    m = re.search(r'viewBox=\\"0 0 ([0-9.]+) ([0-9.]+)\\"', open(sys.argv[1]).read())
    print(int(float(m.group(1))), int(float(m.group(2))))" "$f")"
        google-chrome --headless=new --disable-gpu --hide-scrollbars \\
            --force-device-scale-factor=2 --window-size=$w,$h \\
            --screenshot="${f%.svg}.png" "file://$PWD/$f"
    done

Any SVG→PNG renderer works (rsvg-convert, cairosvg, Inkscape); Chrome is used above only
because it loads the webfont, so the PNG matches what the terminal actually looks like.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import demo_world

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
# Anchored to the repo, not the cwd, so it lands in docs/img from wherever it is run.
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _DEFAULT_OUT
# Wide enough that every column fits without the table eliding them.
SIZE = (140, 16)

WORLD = demo_world.build()

from multi_claude.app import ClaudeBrowserApp  # noqa: E402
from multi_claude.index import default_index  # noqa: E402
from multi_claude.screens.projects import ProjectsScreen  # noqa: E402
from multi_claude.screens.sessions import SessionsScreen  # noqa: E402


def save(app: ClaudeBrowserApp, name: str, title: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    svg = app.export_screenshot(title=title)
    (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")
    print(f"  → {name}.svg")


async def main() -> None:
    from textual.widgets import DataTable, Input

    app = ClaudeBrowserApp()
    with patch("multi_claude.screens.sessions.live_sessions", return_value=WORLD.live):
        async with app.run_test(size=SIZE) as pilot:
            for _ in range(30):
                await pilot.pause()
            save(app, "01-proyectos", "multi-claude — proyectos")

            # Sessions of the api project, with the live-status column populated.
            screen = app.screen
            assert isinstance(screen, ProjectsScreen)
            table = screen.query_one("#projects", DataTable)
            wanted = next(i for i, p in enumerate(screen._projects) if p.name == "tienda-api")
            table.move_cursor(row=screen._visible_indices.index(wanted))
            table.action_select_cursor()
            for _ in range(40):
                await pilot.pause()
            sessions = app.screen
            assert isinstance(sessions, SessionsScreen), sessions
            save(app, "02-sesiones", "multi-claude — sesiones y estado en vivo")

            # The preview panel, on the session whose jsonl ends in real turns.
            await pilot.press("p")
            for _ in range(20):
                await pilot.pause()
            save(app, "03-preview", "multi-claude — preview de una sesión (`p`)")
            await pilot.press("p")
            for _ in range(10):
                await pilot.pause()

            # The shared repository's tab, reached the way a user would: ctrl+→.
            await pilot.press("ctrl+right")
            for _ in range(40):
                await pilot.pause()
            save(app, "04-equipo", "multi-claude — sesiones publicadas por el equipo")

            # Global search: yours and the team's in one list.
            await pilot.press("escape")
            for _ in range(20):
                await pilot.pause()
            await pilot.press("question_mark")
            for _ in range(20):
                await pilot.pause()
            search = app.screen
            search.query_one("#fts-query", Input).value = "nginx"
            for _ in range(40):
                await pilot.pause()
            save(app, "05-busqueda", "multi-claude — búsqueda global (`?`)")

    print(
        "índice:",
        default_index().count_sessions(),
        "sesiones ·",
        default_index().count_remote_sessions(),
        "del equipo",
    )


asyncio.run(main())
