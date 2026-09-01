"""TranscriptScreen — read a whole conversation without resuming it, and take it with you.

The preview panel (`p`) answers "is this the session I mean?" in three lines. This answers
the other question, the one the shared archive exists for: **what did they actually do**.
Reading a colleague's session by resuming it is a bad trade — it starts a Claude process,
puts the transcript in a context that is not yours, and leaves you inside a conversation you
only wanted to read.

Exporting matters for the same reason. A conversation that explains why something is the way
it is belongs in the MR that changes it, not in a terminal only you can see; `x` writes it to
a Markdown file and `y` copies it, so it can be pasted where the discussion is happening.
"""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Static

from multi_claude.clipboard import ClipboardError, copy_to_clipboard
from multi_claude.transcript import read_all_turns, to_markdown


class TranscriptScreen(Screen[None]):
    """The whole conversation, scrollable, filterable, and exportable."""

    BINDINGS = [
        Binding("escape", "back", "Volver"),
        Binding("slash", "show_filter", "Buscar"),
        Binding("x", "export", "Exportar .md"),
        Binding("y", "copy", "Copiar"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    DEFAULT_CSS = """
    TranscriptScreen #transcript-filter {
        display: none;
        border: tall $accent;
    }
    TranscriptScreen #transcript-filter.visible {
        display: block;
    }
    TranscriptScreen .turn-user {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }
    TranscriptScreen .turn-assistant {
        color: $text;
        margin-top: 1;
    }
    TranscriptScreen .meta {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        jsonl_path: Path,
        title: str,
        session_id: str,
        cwd: str | None = None,
        branch: str | None = None,
    ) -> None:
        super().__init__()
        self.jsonl_path = jsonl_path
        self.session_title = title
        self.session_id = session_id
        self.cwd = cwd
        self.branch = branch
        self._turns: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="buscar en la conversación (Esc cierra)", id="transcript-filter")
        yield VerticalScroll(Static("Leyendo…", id="transcript-body"), id="transcript-scroll")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.session_title
        # Focus the scroll, not just for the arrow keys: without something focused inside,
        # key presses do not reach this screen's bindings at all, so `x` and `y` silently
        # do nothing — the kind of dead key nobody reports, they just stop trying.
        self.query_one("#transcript-scroll", VerticalScroll).focus()
        self._load_worker()

    # Reading a whole transcript is disk work on a file that can run to megabytes; doing it
    # on the UI thread would freeze the app for exactly as long as the session is long.
    @work(thread=True, exclusive=True, group="transcript")
    def _load_worker(self) -> None:
        turns = read_all_turns(self.jsonl_path)
        self.app.call_from_thread(self._on_loaded, turns)

    def _on_loaded(self, turns: list[tuple[str, str]]) -> None:
        self._turns = turns
        self._paint()

    def _paint(self, needle: str = "") -> None:
        body = self.query_one("#transcript-body", Static)
        turns = self._matching(needle)
        if not self._turns:
            body.update("Esta sesión no tiene turnos legibles.")
            return
        if not turns:
            body.update(f"Ningún turno contiene «{needle}».")
            return
        parts: list[str] = []
        for role, text in turns:
            parts.append(f"[bold]{'Tú' if role == 'user' else 'Claude'}[/bold]")
            # Markup off: transcripts are full of square brackets, and Rich would eat them
            # as tags — or fail on a malformed one, taking the whole screen with it.
            parts.append(text.replace("[", "\\["))
            parts.append("")
        body.update("\n".join(parts))
        if needle:
            self.sub_title = f"{self.session_title} — {len(turns)}/{len(self._turns)} turnos"
        else:
            self.sub_title = f"{self.session_title} — {len(self._turns)} turnos"

    def _matching(self, needle: str) -> list[tuple[str, str]]:
        """Turns containing ``needle``. Filtering, not highlighting, on purpose.

        The question this answers is "where in this conversation did we talk about X", and a
        conversation is long: showing only the turns that mention it *is* the answer, while
        highlighting would leave you scrolling a wall of text looking for a colour.
        """
        if not needle.strip():
            return self._turns
        low = needle.lower()
        return [(role, text) for role, text in self._turns if low in text.lower()]

    def _markdown(self) -> str:
        return to_markdown(
            self._turns,
            title=self.session_title,
            session_id=self.session_id,
            cwd=self.cwd,
            branch=self.branch,
        )

    # --- actions ----------------------------------------------------------------------

    def action_show_filter(self) -> None:
        field = self.query_one("#transcript-filter", Input)
        field.add_class("visible")
        field.focus()

    @on(Input.Changed, "#transcript-filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._paint(event.value)

    def action_back(self) -> None:
        field = self.query_one("#transcript-filter", Input)
        if field.has_class("visible"):
            field.value = ""
            field.remove_class("visible")
            self._paint()
            return
        self.dismiss(None)

    def action_copy(self) -> None:
        if not self._turns:
            self.notify("No hay nada que copiar", severity="warning")
            return
        try:
            backend = copy_to_clipboard(self._markdown())
        except ClipboardError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"{len(self._turns)} turnos copiados como Markdown vía {backend}")

    def action_export(self) -> None:
        if not self._turns:
            self.notify("No hay nada que exportar", severity="warning")
            return
        target = Path.cwd() / f"{self.session_id}.md"
        try:
            target.write_text(self._markdown(), encoding="utf-8")
        except OSError as exc:
            self.notify(f"No se pudo escribir: {exc}", severity="error")
            return
        self.notify(f"Exportado a {target}")

    def action_quit(self) -> None:
        self.app.exit()
