"""Modal screens: rename session, add project, confirm delete.

Each modal completes via ``self.dismiss(<result>)``. Callers use
``await self.app.push_screen(Modal(...), callback)`` and react in ``callback``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static

from multi_claude.colors import PALETTE, ColorRule
from multi_claude.config import VALID_MODES, Config, LaunchMode, alternate_for
from multi_claude.discovery import Project


def _stop_event(event: object) -> None:
    """Best-effort stop+prevent_default on a Textual key event."""
    stop = getattr(event, "stop", None)
    if callable(stop):
        stop()
    prevent_default = getattr(event, "prevent_default", None)
    if callable(prevent_default):
        prevent_default()


class RenameModal(ModalScreen[str | None]):
    """Ask for a new display name. Empty string + Enter ⇒ delete the name.

    Dismisses with:
      - ``None`` → cancel (no change)
      - ``""``   → delete the existing name
      - ``"x"``  → set name to "x"

    Generic over the entity being renamed: caller passes a title (e.g. "Renombrar
    sesión" / "Renombrar proyecto") and a short subtitle (id, path) for context.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    RenameModal {
        align: center middle;
    }
    RenameModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 70;
        height: auto;
    }
    RenameModal Label.title {
        text-style: bold;
    }
    RenameModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        subtitle: str,
        current_name: str | None,
        *,
        title: str = "Renombrar",
        placeholder: str = "nuevo nombre",
    ) -> None:
        super().__init__()
        self.subtitle = subtitle
        self.current_name = current_name or ""
        self._title_text = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title_text, classes="title")
            yield Static(self.subtitle)
            yield Input(value=self.current_name, placeholder=self._placeholder, id="name-input")
            yield Label("Enter guarda · vacío borra el nombre · Esc cancela", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#name-input", Input).focus()

    @on(Input.Submitted, "#name-input")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditRuleModal(ModalScreen[ColorRule | None]):
    """Edit or create a single colour rule.

    Dismisses with the new :class:`ColorRule` on submit, ``None`` on cancel.
    Lightweight validation only (both fields non-empty) — semantic typos in
    ``when`` will silently fail to match at render time, never crash.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    EditRuleModal {
        align: center middle;
    }
    EditRuleModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    EditRuleModal Label.title {
        text-style: bold;
    }
    EditRuleModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    EditRuleModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    EditRuleModal Label.error {
        color: $error;
        margin-top: 1;
    }
    """

    def __init__(self, rule: ColorRule | None = None) -> None:
        super().__init__()
        self.rule = rule

    def compose(self) -> ComposeResult:
        title = "Editar regla" if self.rule else "Nueva regla"
        with Vertical():
            yield Label(title, classes="title")
            yield Label("Condición (when)", classes="section")
            yield Input(
                value=self.rule.when if self.rule else "",
                placeholder="branch=main · branch~=feature/* · active=true · age<1h",
                id="when-input",
            )
            yield Label("Color (estilo Rich)", classes="section")
            yield Input(
                value=self.rule.color if self.rule else "",
                placeholder="bold green · bold #ff8800 · dim white · black on yellow",
                id="color-input",
            )
            yield Label("", id="error", classes="error")
            yield Label("Enter en cualquier campo guarda · Esc cancela", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#when-input", Input).focus()

    @on(Input.Submitted)
    def _submit_any(self, event: Input.Submitted) -> None:
        when = self.query_one("#when-input", Input).value.strip()
        color = self.query_one("#color-input", Input).value.strip()
        if not when:
            self._set_error("Indica una condición")
            self.query_one("#when-input", Input).focus()
            return
        if not color:
            self._set_error("Indica un color")
            self.query_one("#color-input", Input).focus()
            return
        self.dismiss(ColorRule(when=when, color=color))

    def _set_error(self, msg: str) -> None:
        self.query_one("#error", Label).update(msg)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ColorRulesEditorModal(ModalScreen[list[ColorRule] | None]):
    """Edit the global colour rules list.

    Dismisses with the new list on save (``s``), ``None`` on cancel (``Esc``).
    Operations:
      - ``a`` — open EditRuleModal to append a new rule
      - ``e`` / Enter on a row — edit selected rule
      - ``d`` — delete selected rule
      - ``j`` / ``k`` — move selected rule down / up (priority changes!)
    """

    BINDINGS = [
        Binding("a", "add_rule", "Añadir"),
        Binding("e", "edit_rule", "Editar"),
        Binding("d", "delete_rule", "Borrar"),
        Binding("j", "move_down", "Bajar"),
        Binding("k", "move_up", "Subir"),
        Binding("s", "save", "Guardar"),
        Binding("escape", "cancel", "Cancelar"),
    ]

    DEFAULT_CSS = """
    ColorRulesEditorModal {
        align: center middle;
    }
    ColorRulesEditorModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 100;
        height: auto;
    }
    ColorRulesEditorModal Label.title {
        text-style: bold;
    }
    ColorRulesEditorModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    ColorRulesEditorModal OptionList#rules-list {
        max-height: 16;
        border: round $accent;
        margin-top: 1;
    }
    """

    def __init__(self, initial_rules: list[ColorRule]) -> None:
        super().__init__()
        self.rules: list[ColorRule] = list(initial_rules)

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList

        with Vertical():
            yield Label("Editor de reglas de color", classes="title")
            yield Static("Primera regla que matchea gana. Manual (c) siempre tiene preferencia.")
            yield OptionList(id="rules-list")
            yield Label(
                "a añadir · e editar · d borrar · j/k reordenar · s guardar · Esc cancelar",
                classes="hint",
            )

    def on_mount(self) -> None:
        from textual.widgets import OptionList

        self._refresh_list()
        self.query_one("#rules-list", OptionList).focus()

    def _refresh_list(self) -> None:
        from rich.text import Text
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        opt_list = self.query_one("#rules-list", OptionList)
        previous = opt_list.highlighted
        opt_list.clear_options()
        if not self.rules:
            opt_list.add_option(
                Option(Text("(sin reglas) — pulsa 'a' para añadir", style="dim"), id="empty"),
            )
            return
        for i, rule in enumerate(self.rules):
            try:
                preview = Text(f"  ● {rule.when}   →   {rule.color}", style=rule.color)
            except Exception:
                # Invalid style strings should not block rendering.
                preview = Text(f"  ● {rule.when}   →   {rule.color}")
            opt_list.add_option(Option(preview, id=str(i)))
        # Restore cursor (or clamp to last row if we deleted the last one).
        if previous is None:
            opt_list.highlighted = 0
        else:
            opt_list.highlighted = max(0, min(previous, len(self.rules) - 1))

    def _selected_index(self) -> int | None:
        from textual.widgets import OptionList

        if not self.rules:
            return None
        opt_list = self.query_one("#rules-list", OptionList)
        idx = opt_list.highlighted
        if idx is None or idx < 0 or idx >= len(self.rules):
            return None
        return idx

    # -- actions ------------------------------------------------------------- #

    def action_add_rule(self) -> None:
        self.app.push_screen(EditRuleModal(), self._on_added)

    def _on_added(self, rule: ColorRule | None) -> None:
        if rule is None:
            return
        self.rules.append(rule)
        self._refresh_list()

    def action_edit_rule(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self.app.push_screen(
            EditRuleModal(self.rules[idx]),
            lambda r: self._on_edited(idx, r),
        )

    def _on_edited(self, idx: int, rule: ColorRule | None) -> None:
        if rule is None:
            return
        self.rules[idx] = rule
        self._refresh_list()

    def action_delete_rule(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        del self.rules[idx]
        self._refresh_list()

    def action_move_down(self) -> None:
        idx = self._selected_index()
        if idx is None or idx >= len(self.rules) - 1:
            return
        self.rules[idx], self.rules[idx + 1] = self.rules[idx + 1], self.rules[idx]
        self._refresh_list()
        from textual.widgets import OptionList

        self.query_one("#rules-list", OptionList).highlighted = idx + 1

    def action_move_up(self) -> None:
        idx = self._selected_index()
        if idx is None or idx == 0:
            return
        self.rules[idx], self.rules[idx - 1] = self.rules[idx - 1], self.rules[idx]
        self._refresh_list()
        from textual.widgets import OptionList

        self.query_one("#rules-list", OptionList).highlighted = idx - 1

    def action_save(self) -> None:
        self.dismiss(self.rules)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # Enter on a row → open the edit modal for that rule.
    def on_option_list_option_selected(self, event: object) -> None:
        option = getattr(event, "option", None)
        option_id = getattr(option, "id", None) if option is not None else None
        if option_id == "empty":
            # Empty placeholder → behave like 'a'
            self.action_add_rule()
            return
        if isinstance(option_id, str) and option_id.isdigit():
            idx = int(option_id)
            if 0 <= idx < len(self.rules):
                self.app.push_screen(
                    EditRuleModal(self.rules[idx]),
                    lambda r: self._on_edited(idx, r),
                )


class ColorPickerModal(ModalScreen[str | None]):
    """Pick a colour for a session.

    Dismisses with:
      - ``None`` → cancel (no change)
      - ``""``   → remove the current colour (back to default / rule)
      - ``"bold red"`` (or any palette style) → assign that style
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    ColorPickerModal {
        align: center middle;
    }
    ColorPickerModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    ColorPickerModal Label.title {
        text-style: bold;
    }
    ColorPickerModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(self, subtitle: str, current_style: str | None) -> None:
        super().__init__()
        self.subtitle = subtitle
        self.current_style = current_style or ""

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        with Vertical():
            yield Label("Color de la sesión", classes="title")
            yield Static(self.subtitle)
            options = [Option("Sin color (usar reglas)", id="none")]
            for label, style in PALETTE:
                from rich.text import Text

                rendered = Text(f"● {label}", style=style)
                options.append(Option(rendered, id=style))
            opt_list = OptionList(*options, id="color-list")
            # Highlight whatever's currently set, if anything.
            initial = 0
            if self.current_style:
                for idx, (_, style) in enumerate(PALETTE, start=1):
                    if style == self.current_style:
                        initial = idx
                        break
            opt_list.highlighted = initial
            yield opt_list
            yield Label("Enter aplica · Esc cancela", classes="hint")

    def on_mount(self) -> None:
        from textual.widgets import OptionList

        self.query_one("#color-list", OptionList).focus()

    def on_option_list_option_selected(self, event: object) -> None:
        option = getattr(event, "option", None)
        option_id = getattr(option, "id", None) if option is not None else None
        if option_id == "none":
            self.dismiss("")
            return
        if isinstance(option_id, str):
            self.dismiss(option_id)
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddProjectModal(ModalScreen[Path | None]):
    """Ask for a project path with shell-like autocomplete.

    - Typing updates a list of matching subdirectories below the input.
    - ``Tab``  → extend the input to the longest common prefix of candidates.
    - ``↓``    → move focus into the suggestion list; ``Enter`` picks one.
    - ``Enter`` on the input → submit and resolve the path.
    - Returns a resolved :class:`Path` on submit, ``None`` on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "focus_suggestions", "Elegir sugerencia", priority=True),
    ]

    DEFAULT_CSS = """
    AddProjectModal {
        align: center middle;
    }
    AddProjectModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    AddProjectModal Label.title {
        text-style: bold;
    }
    AddProjectModal Label.error {
        color: $error;
        margin-top: 1;
    }
    AddProjectModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    AddProjectModal OptionList#suggestions {
        max-height: 12;
        border: round $accent;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList

        with Vertical():
            yield Label("Añadir proyecto — lanzar Claude en un cwd nuevo", classes="title")
            yield Input(placeholder="/ruta/al/proyecto", id="path-input")
            suggestions = OptionList(id="suggestions")
            suggestions.display = False
            yield suggestions
            yield Label("", id="error", classes="error")
            yield Label("Enter lanza · Tab completa · ↓ elige · Esc cancela", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    # -- typing + suggestions ------------------------------------------------ #

    @on(Input.Changed, "#path-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_suggestions(event.value)

    def _refresh_suggestions(self, prefix: str) -> None:
        from textual.widgets import OptionList

        from multi_claude.path_complete import list_suggestions

        suggestions = list_suggestions(prefix)
        opt_list = self.query_one("#suggestions", OptionList)
        opt_list.clear_options()
        if not suggestions:
            opt_list.display = False
            return
        opt_list.display = True
        for path in suggestions:
            opt_list.add_option(str(path))

    # -- keys ---------------------------------------------------------------- #

    def on_key(self, event: object) -> None:
        key = getattr(event, "key", None)
        if key == "tab":
            self._tab_complete()
            _stop_event(event)
            return
        # Escape hatches when focus is inside the suggestion list.
        if self._suggestions_have_focus():
            if key == "escape":
                self._focus_input()
                _stop_event(event)
                return
            if key == "up" and self._suggestions_at_top():
                self._focus_input()
                _stop_event(event)

    def _suggestions_have_focus(self) -> bool:
        from textual.widgets import OptionList

        try:
            opt_list = self.query_one("#suggestions", OptionList)
        except Exception:
            return False
        return bool(opt_list.has_focus)

    def _suggestions_at_top(self) -> bool:
        from textual.widgets import OptionList

        try:
            opt_list = self.query_one("#suggestions", OptionList)
        except Exception:
            return False
        # highlighted is None when nothing is selected; treat that as "at top".
        return opt_list.highlighted in (None, 0)

    def _focus_input(self) -> None:
        input_w = self.query_one("#path-input", Input)
        input_w.focus()
        input_w.cursor_position = len(input_w.value)

    def action_focus_suggestions(self) -> None:
        """Move focus into the suggestion list (priority binding so Input doesn't eat ↓)."""
        from textual.widgets import OptionList

        input_w = self.query_one("#path-input", Input)
        opt_list = self.query_one("#suggestions", OptionList)
        if not input_w.has_focus:
            return
        if not opt_list.display or opt_list.option_count == 0:
            return
        opt_list.focus()
        opt_list.highlighted = 0

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Disable the ↓ priority binding once focus is inside the suggestion list.

        Without this, ``Binding("down", ..., priority=True)`` would keep swallowing
        every ↓ and the OptionList could never advance the highlight.
        """
        if action == "focus_suggestions":
            try:
                input_w = self.query_one("#path-input", Input)
            except Exception:
                return False
            if not input_w.has_focus:
                return False
            from textual.widgets import OptionList

            try:
                opt_list = self.query_one("#suggestions", OptionList)
            except Exception:
                return False
            if not opt_list.display or opt_list.option_count == 0:
                return False
        return True

    def _tab_complete(self) -> None:
        from multi_claude.path_complete import common_prefix_completion

        input_w = self.query_one("#path-input", Input)
        completion = common_prefix_completion(input_w.value)
        if completion is None or completion == input_w.value:
            return
        input_w.value = completion
        input_w.cursor_position = len(completion)
        self._refresh_suggestions(completion)

    # -- option picked ------------------------------------------------------- #

    def _handle_suggestion_selected(self, prompt: str) -> None:
        if not prompt:
            return
        if not prompt.endswith("/"):
            prompt = prompt + "/"
        input_w = self.query_one("#path-input", Input)
        input_w.value = prompt
        input_w.cursor_position = len(prompt)
        input_w.focus()
        self._refresh_suggestions(prompt)

    def on_option_list_option_selected(self, event: object) -> None:
        # Filter by widget id (Textual delivers the OptionSelected message to the screen).
        control = getattr(event, "control", None) or getattr(event, "option_list", None)
        if control is not None and getattr(control, "id", None) != "suggestions":
            return
        option = getattr(event, "option", None)
        prompt = str(getattr(option, "prompt", "")) if option is not None else ""
        self._handle_suggestion_selected(prompt)

    # -- submit / cancel ----------------------------------------------------- #

    @on(Input.Submitted, "#path-input")
    def _submit(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw:
            self._set_error("Indica una ruta")
            return
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            self._set_error(f"Ruta inválida: {exc}")
            return
        if not resolved.exists():
            self._set_error(f"No existe: {resolved}")
            return
        if not resolved.is_dir():
            self._set_error(f"No es un directorio: {resolved}")
            return
        self.dismiss(resolved)

    def _set_error(self, msg: str) -> None:
        self.query_one("#error", Label).update(msg)

    def action_cancel(self) -> None:
        self.dismiss(None)


_MODE_LABELS: dict[LaunchMode, str] = {
    "auto": "Auto — multiplexer > ventana nueva > suspend",
    "window": "Ventana nueva del emulador (suspend si no se detecta)",
    "suspend": "Suspender la TUI",
}


class SettingsModal(ModalScreen[Config | None]):
    """Edit the default launch mode. Shift+Enter mode is derived (see alternate_for)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
    }
    SettingsModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 80;
        height: auto;
    }
    SettingsModal Label.title {
        text-style: bold;
    }
    SettingsModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    SettingsModal Label.alt-preview {
        margin-top: 1;
        color: $text-muted;
    }
    SettingsModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    SettingsModal Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    SettingsModal Button {
        margin: 0 1;
    }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._initial = config

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Vertical():
            yield Label("Ajustes — modo de lanzamiento", classes="title")

            yield Label("Enter (predeterminado)", classes="section")
            with RadioSet(id="default-mode"):
                for mode in VALID_MODES:
                    yield RadioButton(
                        _MODE_LABELS[mode],
                        value=(mode == self._initial.default_mode),
                        id=f"default-{mode}",
                    )

            yield Label(
                self._alt_preview_text(self._initial.default_mode),
                id="alt-preview",
                classes="alt-preview",
            )

            yield Label("Enter guarda · Esc cancela", classes="hint")
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Guardar", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#default-mode", RadioSet).focus()

    @on(RadioSet.Changed, "#default-mode")
    def _on_default_changed(self, event: RadioSet.Changed) -> None:
        mode = self._mode_from_radio_id(event.pressed.id, self._initial.default_mode)
        self.query_one("#alt-preview", Label).update(self._alt_preview_text(mode))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.dismiss(self._collect())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _collect(self) -> Config:
        radio_set = self.query_one("#default-mode", RadioSet)
        pressed = radio_set.pressed_button
        mode = self._mode_from_radio_id(
            pressed.id if pressed is not None else None,
            self._initial.default_mode,
        )
        return Config(default_mode=mode)

    @staticmethod
    def _mode_from_radio_id(radio_id: str | None, fallback: LaunchMode) -> LaunchMode:
        if radio_id and radio_id.startswith("default-"):
            candidate = radio_id.split("-", 1)[1]
            if candidate in VALID_MODES:
                return candidate
        return fallback

    @staticmethod
    def _alt_preview_text(default: LaunchMode) -> str:
        return f"Shift+Enter → {_MODE_LABELS[alternate_for(default)]}"


class ConfirmDeleteModal(ModalScreen[bool]):
    """Yes/no confirmation. Cancel-focused by default; ``y`` confirms.

    Dismisses with True (confirm) or False (cancel).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    DEFAULT_CSS = """
    ConfirmDeleteModal {
        align: center middle;
    }
    ConfirmDeleteModal > Vertical {
        background: $surface;
        border: thick $error;
        padding: 1 2;
        width: 80;
        height: auto;
    }
    ConfirmDeleteModal Label.title {
        text-style: bold;
        color: $error;
    }
    ConfirmDeleteModal Label.warning {
        color: $warning;
        text-style: bold;
        margin-top: 1;
    }
    ConfirmDeleteModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    ConfirmDeleteModal Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    ConfirmDeleteModal Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        title: str,
        details: list[str],
        *,
        warning: str | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.details = details
        self.warning = warning

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Vertical():
            yield Label(self.title_text, classes="title")
            for line in self.details:
                yield Static(line)
            if self.warning:
                yield Label(f"⚠️  {self.warning}", classes="warning")
            yield Label("`y` confirma · Enter/Esc cancela", classes="hint")
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Borrar", id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class MergeProjectModal(ModalScreen[Project | None]):
    """Pick a destination project to merge an orphan into.

    Lists candidate projects automatically detected (same repo root or same name).
    Dismisses with the chosen :class:`Project`, or ``None`` on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    MergeProjectModal {
        align: center middle;
    }
    MergeProjectModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    MergeProjectModal Label.title {
        text-style: bold;
    }
    MergeProjectModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    MergeProjectModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    MergeProjectModal Label.error {
        color: $error;
        margin-top: 1;
    }
    """

    def __init__(self, orphan: Project, candidates: list[Project]) -> None:
        super().__init__()
        self.orphan = orphan
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Reconciliar proyecto huérfano", classes="title")
            yield Static(f"Huérfano: {self.orphan.path}  ·  {self.orphan.session_count} sesión(es)")

            if self.candidates:
                yield Label("Candidatos detectados", classes="section")
                with RadioSet(id="merge-target"):
                    for idx, candidate in enumerate(self.candidates):
                        label = f"{candidate.name} — {candidate.path}"
                        yield RadioButton(label, value=(idx == 0), id=f"target-{idx}")
                yield Label("Enter confirma · Esc cancela", classes="hint")
            else:
                yield Label(
                    "No hay candidatos automáticos. Crea primero el proyecto destino con `a` "
                    "y vuelve a intentarlo.",
                    classes="hint",
                )

            yield Label("", id="merge-error", classes="error")

    def on_mount(self) -> None:
        if self.candidates:
            self.query_one("#merge-target", RadioSet).focus()

    def on_key(self, event: object) -> None:
        # Confirm with Enter when focused on the RadioSet.
        if not self.candidates:
            return
        key_name = getattr(event, "key", None)
        if key_name == "enter":
            self._submit_radio()

    def _submit_radio(self) -> None:
        if not self.candidates:
            self.dismiss(None)
            return
        radio_set = self.query_one("#merge-target", RadioSet)
        pressed = radio_set.pressed_button
        if pressed is None or pressed.id is None or not pressed.id.startswith("target-"):
            self._set_error("Selecciona un candidato.")
            return
        idx = int(pressed.id.split("-", 1)[1])
        self.dismiss(self.candidates[idx])

    def _set_error(self, msg: str) -> None:
        self.query_one("#merge-error", Label).update(msg)

    def action_cancel(self) -> None:
        self.dismiss(None)


_CLEANUP_PRESETS: tuple[tuple[str, int | None], ...] = (
    ("Más antiguas de 1 semana", 7),
    ("Más antiguas de 1 mes", 30),
    ("Más antiguas de 3 meses", 90),
    ("Más antiguas de 6 meses", 180),
    ("Más antiguas de 1 año", 365),
    ("Fecha personalizada", None),
)

_DEFAULT_PRESET_IDX = 1  # 1 mes


def _parse_iso_date(raw: str) -> float | None:
    """Parse ``YYYY-MM-DD`` to a UNIX timestamp at 00:00 UTC, or ``None``."""
    try:
        dt = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


class CleanupModal(ModalScreen[float | None]):
    """Bulk-delete sessions older than a threshold.

    Dismisses with the chosen UNIX timestamp on confirm (caller deletes every
    session with ``last_activity < threshold``), or ``None`` on cancel. The
    caller is responsible for skipping active sessions; this modal counts them
    for the preview.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancelar"),
    ]

    DEFAULT_CSS = """
    CleanupModal {
        align: center middle;
    }
    CleanupModal > Vertical {
        background: $surface;
        border: thick $error;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    CleanupModal Label.title {
        text-style: bold;
        color: $error;
    }
    CleanupModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    CleanupModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    CleanupModal Label.error {
        color: $error;
        margin-top: 1;
    }
    CleanupModal Static#preview {
        margin-top: 1;
        text-style: bold;
    }
    CleanupModal Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    CleanupModal Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        *,
        session_activities: list[float],
        active_count: int,
    ) -> None:
        """``session_activities`` are the ``last_activity`` mtimes of every session
        in the project; ``active_count`` is how many of those are reported as live
        (so the preview can show "N activa(s) se omiten")."""
        super().__init__()
        self._activities = session_activities
        self._active_total = active_count

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Vertical():
            yield Label("Limpieza masiva de sesiones", classes="title")
            yield Static(
                f"{len(self._activities)} sesión(es) en el proyecto"
                + (f" · {self._active_total} activa(s)" if self._active_total else "")
            )

            yield Label("Antigüedad mínima", classes="section")
            with RadioSet(id="cleanup-preset"):
                for idx, (label, _days) in enumerate(_CLEANUP_PRESETS):
                    yield RadioButton(label, value=(idx == _DEFAULT_PRESET_IDX), id=f"preset-{idx}")

            yield Label("Fecha personalizada (YYYY-MM-DD)", classes="section")
            yield Input(placeholder="2025-01-01", id="custom-date")

            yield Static("", id="preview")
            yield Label("", id="error", classes="error")
            yield Label("Esc cancela · sesiones activas se omiten siempre", classes="hint")
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Borrar", id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cleanup-preset", RadioSet).focus()
        self._update_preview()

    @on(RadioSet.Changed, "#cleanup-preset")
    def _on_preset_changed(self, event: RadioSet.Changed) -> None:
        self._update_preview()

    @on(Input.Changed, "#custom-date")
    def _on_date_changed(self, event: Input.Changed) -> None:
        # If the user starts typing a date, switch to the "custom" preset.
        if event.value.strip():
            target_idx = len(_CLEANUP_PRESETS) - 1
            target = self.query_one(f"#preset-{target_idx}", RadioButton)
            if not target.value:
                target.value = True
        self._update_preview()

    def _current_preset_idx(self) -> int:
        radio_set = self.query_one("#cleanup-preset", RadioSet)
        pressed = radio_set.pressed_button
        if pressed is None or pressed.id is None or not pressed.id.startswith("preset-"):
            return _DEFAULT_PRESET_IDX
        return int(pressed.id.split("-", 1)[1])

    def _compute_threshold(self) -> float | None:
        idx = self._current_preset_idx()
        _label, days = _CLEANUP_PRESETS[idx]
        if days is not None:
            return time.time() - days * 86400
        # Custom date branch
        raw = self.query_one("#custom-date", Input).value.strip()
        if not raw:
            return None
        return _parse_iso_date(raw)

    def _update_preview(self) -> None:
        threshold = self._compute_threshold()
        preview = self.query_one("#preview", Static)
        confirm_btn = self.query_one("#confirm", Button)
        error = self.query_one("#error", Label)
        if threshold is None:
            preview.update("Fecha inválida — usa YYYY-MM-DD o elige un preset.")
            confirm_btn.label = "Borrar"
            confirm_btn.disabled = True
            error.update("")
            return
        count_total = sum(1 for ts in self._activities if ts < threshold)
        # Active count is an upper bound on skipped (caller actually filters by id).
        skipped = min(count_total, self._active_total)
        to_delete = count_total - skipped
        when_str = datetime.fromtimestamp(threshold, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = f"Se borrarán {to_delete} sesión(es) anteriores a {when_str}"
        if skipped:
            msg += f" · {skipped} activa(s) se omiten"
        preview.update(msg)
        confirm_btn.label = f"Borrar {to_delete}" if to_delete else "Borrar"
        confirm_btn.disabled = to_delete == 0
        error.update("")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        threshold = self._compute_threshold()
        if threshold is None:
            return
        self.dismiss(threshold)

    def action_cancel(self) -> None:
        self.dismiss(None)


_FOLDER_UNASSIGN_SENTINEL = "\x00__unassign__"  # private marker; callers must not pass this


class AssignFolderModal(ModalScreen[str | None]):
    """Pick a folder to assign a project to.

    Dismisses with:
      - ``None`` → cancel (no change)
      - ``""``   → unassign (remove from any current folder)
      - ``"Trabajo"`` → assign to that folder (creating it if new)

    The user can type a brand-new name in the input or pick one of the existing
    folders from the list. ``Enter`` on the input creates+assigns; ``Enter`` on
    a list option assigns to that existing folder.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancelar"),
    ]

    DEFAULT_CSS = """
    AssignFolderModal {
        align: center middle;
    }
    AssignFolderModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 80;
        height: auto;
    }
    AssignFolderModal Label.title {
        text-style: bold;
    }
    AssignFolderModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    AssignFolderModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    AssignFolderModal OptionList#existing-folders {
        max-height: 10;
        border: round $accent;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        subtitle: str,
        existing_folders: list[str],
        current_folder: str | None,
    ) -> None:
        super().__init__()
        self.subtitle = subtitle
        self.existing_folders = existing_folders
        self.current_folder = current_folder

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        with Vertical():
            yield Label("Asignar proyecto a carpeta", classes="title")
            yield Static(self.subtitle)
            if self.current_folder:
                yield Static(f"Actualmente en: {self.current_folder}")

            yield Label("Crear carpeta nueva (acepta anidación con /)", classes="section")
            yield Input(placeholder="Trabajo · Trabajo/Cliente A", id="new-folder")

            yield Label("O elige existente", classes="section")
            options: list[Option] = []
            if self.current_folder is not None:
                options.append(Option("(quitar de la carpeta)", id="__unassign__"))
            # Sort folders by path so descendants follow their ancestor; indent leaves
            # to make hierarchy obvious.
            for name in sorted(self.existing_folders, key=str.casefold):
                depth = name.count("/")
                indent = "  " * depth
                leaf = name.rsplit("/", 1)[-1]
                label = f"{indent}📁 {leaf}" if depth else f"📁 {leaf}"
                options.append(Option(label, id=f"folder:{name}"))
            opt_list = OptionList(*options, id="existing-folders")
            opt_list.display = bool(options)
            yield opt_list

            yield Label("", id="folder-error", classes="error")
            yield Label("Enter en input crea · Enter en lista asigna · Esc cancela", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#new-folder", Input).focus()

    @on(Input.Submitted, "#new-folder")
    def _on_new_folder(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if not name:
            self._set_error("Indica un nombre o elige una existente")
            return
        self.dismiss(name)

    def on_option_list_option_selected(self, event: object) -> None:
        from textual.widgets import OptionList

        control = getattr(event, "control", None) or getattr(event, "option_list", None)
        if control is not None and getattr(control, "id", None) != "existing-folders":
            return
        _ = OptionList  # silence unused
        option = getattr(event, "option", None)
        option_id = getattr(option, "id", None) if option is not None else None
        if option_id == "__unassign__":
            self.dismiss("")
            return
        if isinstance(option_id, str) and option_id.startswith("folder:"):
            self.dismiss(option_id.split(":", 1)[1])

    def _set_error(self, msg: str) -> None:
        self.query_one("#folder-error", Label).update(msg)

    def action_cancel(self) -> None:
        self.dismiss(None)


_ = _FOLDER_UNASSIGN_SENTINEL  # kept for future internal use; not exported
