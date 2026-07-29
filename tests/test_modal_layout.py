"""Every modal must stay readable on a small terminal.

Modals set their own width and grow with their content, so on a terminal narrower or shorter
than they expect text used to be silently clipped: long help text cut off mid-word, and the
bottom of a tall modal — buttons included — never drawn, with nothing on screen to hint at it.

These tests read what is actually painted (via the compositor, not the widget tree) and assert
that the parts a user needs in order to act are present: the title, the keys or buttons that
accept and dismiss, and any warning. Sizes go down to 80x18, well below a normal terminal.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from textual.screen import ModalScreen

from multi_claude import modals as M
from multi_claude.app import ClaudeBrowserApp
from multi_claude.colors import ColorRule
from multi_claude.config import Config
from multi_claude.project_remotes import RemoteLink

# Box-drawing and scrollbar glyphs that sit between words once the screen is flattened.
_CHROME = re.compile(r"[█▀▄▔▁▊▎▆▃▂▅▇░▒▓│─┌┐└┘├┤┬┴┼]")

SIZES = [(80, 18), (80, 24), (100, 30), (140, 45)]


def _visible_text(app: ClaudeBrowserApp) -> str:
    """Flatten what is on screen into one searchable string.

    Chrome is stripped and whitespace collapsed so a phrase still matches after it wraps
    across lines — otherwise every wrapped sentence would look like a missing one.
    """
    lines = [
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    ]
    return re.sub(r"\s+", " ", _CHROME.sub(" ", "\n".join(lines)))


# Each case: what to build, what must ALWAYS be readable (how to act), and what must be
# readable once there is room. A long form cannot show every field at once on a short
# terminal — that is what the scroll is for — but it must never hide how to accept or
# dismiss it, nor a warning about what is about to happen.
CASES: dict[str, tuple[Callable[[], ModalScreen], list[str], list[str]]] = {
    "settings": (
        lambda: M.SettingsModal(Config()),
        ["Ajustes", "Guardar", "Cancelar"],
        ["Enter (predeterminado)", "Argumentos para"],
    ),
    "remote-settings-gitlab": (
        lambda: M.RemoteSettingsModal(RemoteLink(kind="gitlab")),
        ["Ctrl+T prueba la conexión", "Guardar", "Probar", "Cancelar"],
        ["Dónde se publican", "Repositorio de sesiones", "permisos 0600"],
    ),
    "remote-settings-directory": (
        lambda: M.RemoteSettingsModal(RemoteLink(kind="directory")),
        ["Guardar", "Cancelar"],
        ["Carpeta compartida", "Nombre de la pestaña"],
    ),
    "project-remotes": (
        lambda: M.ProjectRemotesModal(
            project_name="gextia-dev",
            links=[
                RemoteLink(kind="gitlab", repo="grupo/sesiones-cliente-x"),
                RemoteLink(kind="directory", path="/mnt/equipo/sesiones"),
            ],
            inherited=True,
        ),
        ["Repositorios de sesiones", "Añadir", "Guardar"],
        ["vale para todos sus worktrees", "sesiones-cliente-x"],
    ),
    "publish": (
        lambda: M.PublishModal(
            session_count=2,
            files=[f"· fichero-{i}.jsonl" for i in range(12)] + ["… y 2 más"],
            destinations=[
                RemoteLink(kind="gitlab", repo="grupo/sesiones-cliente-x", label="cliente-x"),
                RemoteLink(kind="directory", path="/mnt/equipo/sesiones"),
            ],
        ),
        # The warning is in the always-list on purpose: it is the whole point of the dialog.
        ["Publicar 2", "Revisa que no haya secretos", "Publicar", "Cancelar"],
        ["Repositorio de destino", "cliente-x"],
    ),
    "delete-confirmation": (
        lambda: M.ConfirmDeleteModal(
            title="Borrar sesión abc123…",
            details=["Prompt: refactor del exporter", "Mensajes: 412"],
            warning="Esta sesión está corriendo ahora mismo",
        ),
        ["Borrar sesión", "Esta sesión está corriendo", "confirma", "Cancelar"],
        ["Prompt: refactor del exporter"],
    ),
    "cleanup": (
        lambda: M.CleanupModal(session_activities=[1000.0] * 8, active_count=2),
        ["Cancelar"],
        [],
    ),
    "colour-rules": (
        lambda: M.ColorRulesEditorModal([ColorRule(when="branch=main", color="bold red")]),
        ["Editor de reglas de color"],
        [],
    ),
    "tags": (
        lambda: M.TagEditorModal(subtitle="una sesión", current_tags=("bug",), known_tags=["bug"]),
        ["Etiquetas"],
        [],
    ),
    "rename": (
        lambda: M.RenameModal(subtitle="id: abc123", current_name=None, title="Renombrar sesión"),
        ["Renombrar sesión"],
        [],
    ),
}


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
async def test_modal_always_shows_how_to_act(case: str, size: tuple[int, int]) -> None:
    """Title, warning and the accept/dismiss controls, at every size down to 80x18."""
    make, always, _ = CASES[case]
    app = ClaudeBrowserApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.push_screen(make())
        for _ in range(8):
            await pilot.pause()

        visible = _visible_text(app)
        missing = [p for p in always if re.sub(r"\s+", " ", p) not in visible]
        assert not missing, f"{case} at {size[0]}x{size[1]} hides: {missing}"


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
async def test_modal_content_is_all_readable_when_there_is_room(case: str) -> None:
    """On a roomy terminal nothing may be clipped — that would be a wrap bug, not scrolling."""
    make, always, readable = CASES[case]
    app = ClaudeBrowserApp()
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.push_screen(make())
        for _ in range(8):
            await pilot.pause()

        visible = _visible_text(app)
        missing = [p for p in [*always, *readable] if re.sub(r"\s+", " ", p) not in visible]
        assert not missing, f"{case} hides: {missing}"


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_no_modal_is_wider_or_taller_than_the_terminal(size: tuple[int, int]) -> None:
    """A modal drawn past the edge loses whatever falls outside, with no scrollbar to say so."""
    from textual.containers import Vertical

    width, height = size
    app = ClaudeBrowserApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for case, (make, _always, _readable) in CASES.items():
            modal = make()
            app.push_screen(modal)
            for _ in range(6):
                await pilot.pause()
            box = modal.query_one(Vertical)
            assert box.outer_size.width <= width, f"{case} desborda a lo ancho"
            assert box.outer_size.height <= height, f"{case} desborda a lo alto"
            app.pop_screen()
            await pilot.pause()


async def test_a_long_help_line_wraps_instead_of_being_cut() -> None:
    """Regression: Labels sized themselves to their content and got clipped mid-word."""
    app = ClaudeBrowserApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            M.ProjectRemotesModal(project_name="p", links=[], inherited=False)
        )
        for _ in range(8):
            await pilot.pause()
        # The full sentence is only readable if it wrapped; clipped, the tail is gone.
        assert "vale para todos sus worktrees" in _visible_text(app)
