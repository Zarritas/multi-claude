"""Session preview widget — renders the last N turns of a session jsonl."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from multi_claude.transcript import read_last_turns

PREVIEW_LAST_LINES = 60
PREVIEW_TURN_LIMIT = 12
PREVIEW_TEXT_LIMIT = 800


class SessionPreview(Widget):
    """Read-only panel that renders the last few turns of a session."""

    DEFAULT_CSS = """
    SessionPreview {
        border: round $primary;
        padding: 0 1;
        background: $boost;
        height: 1fr;
        width: 1fr;
    }
    SessionPreview > VerticalScroll {
        height: 1fr;
    }
    SessionPreview .turn-user {
        color: $accent;
        text-style: bold;
    }
    SessionPreview .turn-assistant {
        color: $text;
    }
    SessionPreview .placeholder {
        color: $text-muted;
        text-style: italic;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="preview-scroll"):
            yield Static("Selecciona una sesión.", id="preview-body", classes="placeholder")

    def clear(self, placeholder: str = "Selecciona una sesión.") -> None:
        body = self.query_one("#preview-body", Static)
        body.remove_class("turn-user")
        body.remove_class("turn-assistant")
        body.add_class("placeholder")
        body.update(placeholder)

    def show_session(self, jsonl_path: Path | None) -> None:
        body = self.query_one("#preview-body", Static)
        if jsonl_path is None or not jsonl_path.exists():
            self.clear("No hay preview disponible.")
            return
        try:
            turns = read_last_turns(
                jsonl_path,
                tail_lines=PREVIEW_LAST_LINES,
                turn_limit=PREVIEW_TURN_LIMIT,
                text_limit=PREVIEW_TEXT_LIMIT,
            )
        except OSError as exc:
            self.clear(f"Error leyendo {jsonl_path.name}: {exc}")
            return
        if not turns:
            self.clear("Sin turnos de texto en esta sesión.")
            return
        rendered = "\n\n".join(_format_turn(role, text) for role, text in turns)
        body.remove_class("placeholder")
        body.update(rendered)


def _format_turn(role: str, text: str) -> str:
    icon = "▶" if role == "user" else "◆"
    label = "Usuario" if role == "user" else "Claude"
    return f"{icon} {label}\n{text}"
