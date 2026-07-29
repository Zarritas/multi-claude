"""Modal screens: rename session, add project, confirm delete.

Each modal completes via ``self.dismiss(<result>)``. Callers use
``await self.app.push_screen(Modal(...), callback)`` and react in ``callback``.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static

from multi_claude.colors import PALETTE, ColorRule
from multi_claude.config import (
    VALID_MODES,
    ClaudeArgsError,
    Config,
    LaunchMode,
    alternate_for,
    parse_claude_args,
)
from multi_claude.discovery import Project
from multi_claude.launcher import PLACEMENT_LABELS, preview_dispatch
from multi_claude.project_remotes import RemoteLink, RemoteServer
from multi_claude.tags import parse_tag_list


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
    "auto": "Auto — panel del multiplexer > pestaña > ventana > suspend",
    "split": "Panel dividido del multiplexer (tmux/zellij)",
    "tab": "Pestaña nueva en la ventana actual",
    "window": "Ventana nueva del emulador",
    "suspend": "Suspender la TUI",
}

# Sketches of where the session lands, drawn from the point of view of the window
# multi-claude is running in. Kept to 46 columns so they fit the modal.
_MODE_SKETCHES: dict[LaunchMode, str] = {
    "auto": (
        "  panel  ▸  pestaña  ▸  ventana  ▸  aquí mismo\n"
        "  ╰── se queda en la primera que esté disponible\n"
        "\n"
        "  Con tmux/zellij: panel. Sin ellos: pestaña de\n"
        "  tu emulador, si sabe abrirlas."
    ),
    "split": (
        "  ┌───────────────┬───────────────┐\n"
        "  │ multi-claude  │ claude ▌      │\n"
        "  │               │               │\n"
        "  └───────────────┴───────────────┘\n"
        "  Una ventana, dos paneles lado a lado."
    ),
    "tab": (
        "  ┌ multi-claude │ claude ▌ ───────┐\n"
        "  │ claude ▌                       │\n"
        "  │                                │\n"
        "  └────────────────────────────────┘\n"
        "  La misma ventana, una pestaña más."
    ),
    "window": (
        "  ┌ multi-claude ──┐\n"
        "  │                │ ┌ claude ────────┐\n"
        "  └────────────────┘ │ ▌              │\n"
        "                     └────────────────┘\n"
        "  Dos ventanas independientes."
    ),
    "suspend": (
        "  ┌ esta misma terminal ───────────┐\n"
        "  │ claude ▌                       │\n"
        "  │                                │\n"
        "  └────────────────────────────────┘\n"
        "  La TUI se pausa y vuelve al salir de claude."
    ),
}


def _dispatch_hint(mode: LaunchMode) -> str:
    """One line describing what ``mode`` resolves to on this machine, right now."""
    outcome = preview_dispatch(mode)
    label = PLACEMENT_LABELS.get(outcome.placement, outcome.placement)
    if outcome.target == "inline":
        line = f"Aquí y ahora: {label.lower()}"
    else:
        line = f"Aquí y ahora: {label.lower()} vía {outcome.target}"
    if outcome.fallback_reason:
        line += f" — {outcome.fallback_reason}"
    return line


_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
# Both spellings bypass permissions; we normalise on the flag above so the
# checkbox and the free-text field can't end up disagreeing.
_SKIP_PERMISSIONS_EQUIVALENTS = (
    _SKIP_PERMISSIONS_FLAG,
    "--permission-mode=bypassPermissions",
)


def _split_skip_permissions(args: list[str]) -> tuple[bool, list[str]]:
    """Return (skip_enabled, remaining_args) by pulling bypass flags out of ``args``."""
    remaining: list[str] = []
    skip = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _SKIP_PERMISSIONS_EQUIVALENTS:
            skip = True
        elif arg == "--permission-mode" and i + 1 < len(args):
            if args[i + 1] == "bypassPermissions":
                skip = True
            else:
                remaining += [arg, args[i + 1]]
            i += 1
        else:
            remaining.append(arg)
        i += 1
    return skip, remaining


class SettingsModal(ModalScreen[Config | None]):
    """Edit the launch mode and the extra ``claude`` flags.

    Shift+Enter's mode is derived from the default (see alternate_for). The result
    is the incoming config with only these fields replaced, so unrelated prefs
    (sorts, preview, colour rules) survive a save.
    """

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
    SettingsModal TabbedContent {
        height: auto;
    }
    SettingsModal TabPane {
        height: auto;
        padding: 0 1;
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
    SettingsModal Label.error {
        color: $error;
    }
    SettingsModal Static.sketch {
        margin-top: 1;
        color: $text-muted;
        height: auto;
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
        self._skip_initial, self._extra_initial = _split_skip_permissions(config.claude_args)

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal
        from textual.widgets import Checkbox, TabbedContent, TabPane

        with Vertical():
            yield Label("Ajustes", classes="title")
            with TabbedContent(id="settings-tabs"):
                with TabPane("Lanzamiento", id="tab-launch"):
                    yield Label("Enter (predeterminado)", classes="section")
                    with RadioSet(id="default-mode"):
                        for mode in VALID_MODES:
                            yield RadioButton(
                                _MODE_LABELS[mode],
                                value=(mode == self._initial.default_mode),
                                id=f"default-{mode}",
                            )

                    yield Static(
                        _MODE_SKETCHES[self._initial.default_mode],
                        id="mode-sketch",
                        classes="sketch",
                        markup=False,
                    )
                    yield Label(
                        _dispatch_hint(self._initial.default_mode),
                        id="dispatch-hint",
                        classes="hint",
                    )
                    yield Label(
                        self._alt_preview_text(self._initial.default_mode),
                        id="alt-preview",
                        classes="alt-preview",
                    )

                    yield Label("Argumentos para `claude`", classes="section")
                    yield Checkbox(
                        f"Saltar permisos ({_SKIP_PERMISSIONS_FLAG})",
                        value=self._skip_initial,
                        id="skip-permissions",
                    )
                    yield Input(
                        value=" ".join(self._extra_initial),
                        placeholder="--model opus --effort high --add-dir ../shared",
                        id="claude-args",
                    )
                    yield Label("Se anteponen a --resume/-n en cada lanzamiento", classes="hint")
                    yield Label("", id="args-error", classes="error")

                with TabPane("Sesiones compartidas", id="tab-remote"):
                    yield Label("Servidores", classes="section")
                    yield Label(
                        _servers_summary(self._initial.remote_servers),
                        id="servers-summary",
                        classes="hint",
                    )
                    yield Label(
                        "Se configuran una vez y se eligen por nombre al enlazar un "
                        "repositorio a un proyecto (L).",
                        classes="hint",
                    )
                    yield Button("Servidores…", id="configure-servers", variant="default")

                    yield Label("Remoto global", classes="section")
                    yield Label(self._initial.remote_summary(), id="remote-summary", classes="hint")
                    yield Label(
                        "Solo para proyectos sin repos propios. Para enlazar uno: L.",
                        classes="hint",
                    )
                    yield Button("Configurar remoto…", id="configure-remote", variant="default")

                with TabPane("Colores", id="tab-colors"):
                    yield Label("Reglas automáticas", classes="section")
                    yield Label(
                        f"{len(self._initial.color_rules)} regla(s) definida(s)",
                        id="rules-summary",
                        classes="hint",
                    )
                    yield Label(
                        "En orden; gana la primera que casa. El color manual (c) manda.",
                        classes="hint",
                    )
                    yield Button("Editar reglas…", id="edit-rules", variant="default")

            yield Label("Enter guarda · Esc cancela", classes="hint")
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Guardar", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#default-mode", RadioSet).focus()

    @on(RadioSet.Changed, "#default-mode")
    def _on_default_changed(self, event: RadioSet.Changed) -> None:
        mode = self._mode_from_radio_id(event.pressed.id, self._initial.default_mode)
        self.query_one("#mode-sketch", Static).update(_MODE_SKETCHES[mode])
        self.query_one("#dispatch-hint", Label).update(_dispatch_hint(mode))
        self.query_one("#alt-preview", Label).update(self._alt_preview_text(mode))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self._try_dismiss()

    @on(Button.Pressed, "#configure-servers")
    def _configure_servers(self) -> None:
        """Manage the configured servers, nested on top of these settings."""
        self.app.push_screen(ServersModal(list(self._initial.remote_servers)), self._on_servers)

    def _on_servers(self, servers: list[RemoteServer] | None) -> None:
        if servers is None:
            return  # cancelled
        self._initial = replace(self._initial, remote_servers=servers)
        self.query_one("#servers-summary", Label).update(_servers_summary(servers))

    @on(Button.Pressed, "#configure-remote")
    def _configure_remote(self) -> None:
        """Open the remote settings on top, and fold its result into our own.

        Nested rather than inlined: the remote needs five fields plus a connection test,
        which would bury the launch settings this modal exists for.
        """
        modal = RepoLinkModal(
            self._initial.remote_link(),
            servers=list(self._initial.remote_servers),
            title="Remoto global (para proyectos sin enlaces propios)",
        )
        self.app.push_screen(modal, self._on_remote_configured)

    def _on_remote_configured(self, result: RemoteLink | None) -> None:
        if result is None:
            return  # cancelled: leave the remote settings untouched
        # ``_collect`` builds on ``_initial``, so updating it is what carries the remote
        # fields into the config this modal eventually returns.
        self._initial = self._initial.with_remote_link(result)
        self.query_one("#remote-summary", Label).update(result.summary())

    @on(Button.Pressed, "#edit-rules")
    def _edit_rules(self) -> None:
        """Edit the colour rules without leaving settings, and fold the result back in."""
        self.app.push_screen(
            ColorRulesEditorModal(list(self._initial.color_rules)), self._on_rules_edited
        )

    def _on_rules_edited(self, result: list[ColorRule] | None) -> None:
        if result is None:
            return
        self._initial = replace(self._initial, color_rules=result)
        self.query_one("#rules-summary", Label).update(f"{len(result)} regla(s) definida(s)")

    @on(Input.Submitted, "#claude-args")
    def _submit_args(self) -> None:
        self._try_dismiss()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _try_dismiss(self) -> None:
        """Dismiss with the new config, or stay open showing why the args are invalid."""
        try:
            result = self._collect()
        except ClaudeArgsError as exc:
            self.query_one("#args-error", Label).update(str(exc))
            self.query_one("#claude-args", Input).focus()
            return
        self.dismiss(result)

    def _collect(self) -> Config:
        from textual.widgets import Checkbox

        radio_set = self.query_one("#default-mode", RadioSet)
        pressed = radio_set.pressed_button
        mode = self._mode_from_radio_id(
            pressed.id if pressed is not None else None,
            self._initial.default_mode,
        )
        typed = parse_claude_args(self.query_one("#claude-args", Input).value)
        # The checkbox is the single source of truth for bypassing permissions:
        # drop any equivalent flag typed by hand, then re-add it if it's checked.
        _, extras = _split_skip_permissions(typed)
        if self.query_one("#skip-permissions", Checkbox).value:
            extras.append(_SKIP_PERMISSIONS_FLAG)
        return replace(self._initial, default_mode=mode, claude_args=extras)

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


def _servers_summary(servers: list[RemoteServer]) -> str:
    """One line naming the configured servers, for the settings tab."""
    if not servers:
        return "ninguno configurado"
    return f"{len(servers)}: " + ", ".join(s.name for s in servers)


_REMOTE_KIND_LABELS: dict[str, str] = {
    "none": "Desactivado",
    "directory": "Carpeta compartida (montaje, Syncthing…)",
    "gitlab": "GitLab (repo privado)",
    "github": "GitHub (repo privado)",
}


class ServerEditModal(ModalScreen["RemoteServer | None"]):
    """Define a server you can publish to: name, provider, host and token.

    Servers are configured once in Ajustes and then picked by name when linking a repo, so a
    host and a token get typed once instead of once per repository.

    The token never enters the returned value, because servers are stored in ``config.json``.
    It goes straight to :class:`~multi_claude.remote.TokenStore` keyed by server name, and the
    field shows whether one exists without ever rendering it.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+t", "test", "Probar"),
    ]

    DEFAULT_CSS = """
    ServerEditModal {
        align: center middle;
    }
    ServerEditModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 80;
        height: auto;
    }
    ServerEditModal Label.title { text-style: bold; }
    ServerEditModal Label.section { text-style: bold; color: $accent; }
    ServerEditModal Label.hint { color: $text-muted; }
    ServerEditModal Label.error { color: $error; }
    ServerEditModal Label.ok { color: $success; }
    ServerEditModal #server-ssh-fields, ServerEditModal #server-token-fields { height: auto; }
    ServerEditModal Horizontal { align: center middle; height: auto; margin-top: 1; }
    ServerEditModal Button { margin: 0 1; }
    """

    def __init__(self, server: RemoteServer, *, has_token: bool = False) -> None:
        super().__init__()
        self._initial = server
        self._has_token = has_token

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal, VerticalScroll

        with Vertical():
            yield Label("Servidor de sesiones", classes="title")
            yield Label("Ctrl+T prueba la conexión · Enter guarda · Esc cancela", classes="hint")

            # Fields in a scrollable body so the title, the keys and the buttons stay put on a
            # short terminal — the same shape the other long forms use.
            with VerticalScroll(id="server-body"):
                yield Label("Nombre", classes="section")
                yield Input(
                    value=self._initial.name, placeholder="FactorLibre GitLab", id="server-name"
                )

                yield Label("Proveedor", classes="section")
                with RadioSet(id="server-kind"):
                    yield RadioButton(
                        "GitLab", value=self._initial.kind != "github", id="server-kind-gitlab"
                    )
                    yield RadioButton(
                        "GitHub", value=self._initial.kind == "github", id="server-kind-github"
                    )

                yield Label("Autenticación", classes="section")
                with RadioSet(id="server-auth"):
                    yield RadioButton(
                        "Token de acceso (API)",
                        value=not self._initial.uses_ssh,
                        id="server-auth-token",
                    )
                    yield RadioButton(
                        "SSH (usa tus claves)",
                        value=self._initial.uses_ssh,
                        id="server-auth-ssh",
                    )

                yield Label("Servidor (vacío = gitlab.com / github.com)", classes="section")
                yield Input(
                    value=self._initial.host,
                    placeholder="https://git.tuempresa.com",
                    id="server-host",
                )

                with Vertical(id="server-ssh-fields"):
                    yield Label("Usuario SSH", classes="section")
                    yield Input(
                        value=self._initial.ssh_user, placeholder="git", id="server-ssh-user"
                    )
                    yield Label(
                        "No hace falta token: se usan las claves SSH que ya tengas.",
                        classes="hint",
                    )

                with Vertical(id="server-token-fields"):
                    yield Label("Token (lectura y escritura sobre los repos)", classes="section")
                    yield Input(
                        placeholder=(
                            "•••• guardado (escribe para reemplazarlo)"
                            if self._has_token
                            else "glpat-… / github_pat_…"
                        ),
                        password=True,
                        id="server-token",
                    )
                    yield Label(
                        "Se guarda aparte con permisos 0600, nunca en config.json",
                        classes="hint",
                    )

            yield Label("", id="server-status", classes="hint")
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Probar", id="test", variant="default")
                yield Button("Guardar", id="save", variant="primary")

    def on_mount(self) -> None:
        self._sync_auth_fields()
        self.query_one("#server-name", Input).focus()

    @on(RadioSet.Changed, "#server-auth")
    def _on_auth_changed(self, event: RadioSet.Changed) -> None:
        self._sync_auth_fields()

    def _sync_auth_fields(self) -> None:
        """Show only what the chosen authentication needs."""
        ssh = self._auth_from_radio() == "ssh"
        self.query_one("#server-ssh-fields").display = ssh
        self.query_one("#server-token-fields").display = not ssh

    def _auth_from_radio(self) -> str:
        pressed = self.query_one("#server-auth", RadioSet).pressed_button
        radio_id = pressed.id if pressed is not None else None
        return "ssh" if radio_id == "server-auth-ssh" else "token"

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self._try_save()

    @on(Button.Pressed, "#test")
    def _test(self) -> None:
        self.action_test()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._try_save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _try_save(self) -> None:
        server = self.collect()
        status = self.query_one("#server-status", Label)
        if not server.name:
            self._set_status(status, "Ponle un nombre al servidor", ok=False)
            self.query_one("#server-name", Input).focus()
            return
        token = self.typed_token()
        if token:
            from multi_claude.remote import TokenStore

            TokenStore().set(token, server.name)
        self.dismiss(server)

    def action_test(self) -> None:
        """Check the server as typed, before saving it."""
        from multi_claude.remote import RemoteError, TokenStore, store_from_link

        status = self.query_one("#server-status", Label)
        server = self.collect()
        if not server.is_configured:
            self._set_status(status, "Falta el nombre o la URL", ok=False)
            return
        if server.uses_ssh:
            self._test_ssh(server, status)
            return
        token = self.typed_token() or TokenStore().get(server.name)
        if not token:
            self._set_status(status, "Falta el token", ok=False)
            return
        # A server on its own has no repo to talk to, so the check needs one. Any name will do:
        # a 404 still proves the host answers and the token is accepted, while 401 proves it
        # is not — which is the useful distinction here.
        probe = RemoteLink(
            kind=server.kind, host=server.api_host, repo="multi-claude/_probe", branch="main"
        )
        store = store_from_link(probe, token=token)
        if store is None:
            self._set_status(status, "Configuración incompleta", ok=False)
            return
        check = getattr(store, "check_connection", None)
        if not callable(check):
            self._set_status(status, "Este proveedor no admite prueba de conexión", ok=False)
            return
        try:
            check()
            self._set_status(status, f"OK · {server.summary()} responde", ok=True)
        except RemoteError as exc:
            message = str(exc)
            if message.startswith("404"):
                self._set_status(
                    status, f"OK · {server.summary()} responde y acepta el token", ok=True
                )
            else:
                self._set_status(status, message, ok=False)

    def _test_ssh(self, server: RemoteServer, status: Label) -> None:
        """SSH needs no token, and ``ls-remote`` on the host proves the key works."""
        from multi_claude.remote import RemoteError
        from multi_claude.remote_git import GitSshRemote

        probe = RemoteLink(
            kind="ssh",
            host=server.ssh_host,
            ssh_user=server.ssh_user,
            repo="multi-claude/_probe",
            branch="main",
        )
        try:
            GitSshRemote(probe).check_connection()
            self._set_status(status, f"OK · {server.summary()} responde", ok=True)
        except RemoteError as exc:
            message = str(exc)
            if "no existe o no es un repositorio" in message:
                # The host answered and accepted the key; only the invented repo is missing.
                self._set_status(status, f"OK · {server.ssh_host} acepta tu clave SSH", ok=True)
            else:
                self._set_status(status, message, ok=False)

    @staticmethod
    def _set_status(label: Label, message: str, *, ok: bool) -> None:
        label.remove_class("error", "ok")
        label.add_class("ok" if ok else "error")
        label.update(message)

    def typed_token(self) -> str | None:
        """The token as typed, or None meaning "keep whatever is already stored"."""
        return self.query_one("#server-token", Input).value.strip() or None

    def collect(self) -> RemoteServer:
        pressed = self.query_one("#server-kind", RadioSet).pressed_button
        kind = (
            "github" if (pressed is not None and pressed.id == "server-kind-github") else "gitlab"
        )
        return RemoteServer(
            name=self.query_one("#server-name", Input).value.strip(),
            kind=kind,
            host=self.query_one("#server-host", Input).value.strip().rstrip("/"),
            auth=self._auth_from_radio(),
            ssh_user=self.query_one("#server-ssh-user", Input).value.strip() or "git",
        )


class ServersModal(ModalScreen["list[RemoteServer] | None"]):
    """Manage the configured servers. Returns the final list, or None on cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("a", "add", "Añadir"),
        Binding("e", "edit", "Editar"),
        Binding("delete", "remove", "Quitar"),
    ]

    DEFAULT_CSS = """
    ServersModal { align: center middle; }
    ServersModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 80;
        height: auto;
    }
    ServersModal Label.title { text-style: bold; }
    ServersModal Label.hint { color: $text-muted; }
    ServersModal Horizontal { align: center middle; height: auto; margin-top: 1; }
    ServersModal Button { margin: 0 1; }
    """

    def __init__(self, servers: list[RemoteServer]) -> None:
        super().__init__()
        self._servers = list(servers)

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Vertical():
            yield Label("Servidores de sesiones", classes="title")
            yield Label(
                "Configúralos aquí una vez y elígelos por nombre al enlazar un repositorio "
                "a un proyecto.",
                classes="hint",
            )
            yield Label("a añade · e edita · Supr quita · Enter guarda", classes="hint")
            yield Label("", id="servers-empty", classes="hint")
            with RadioSet(id="servers"):
                yield from self._buttons()
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Añadir…", id="add", variant="default")
                yield Button("Guardar", id="save", variant="primary")

    def _buttons(self) -> ComposeResult:
        for index, server in enumerate(self._servers):
            yield RadioButton(
                f"{server.name} — {server.summary()}", value=(index == 0), id=f"server-{index}"
            )

    def on_mount(self) -> None:
        self._sync_empty()

    def _sync_empty(self) -> None:
        self.query_one("#servers-empty", Label).update(
            "(ninguno configurado)" if not self._servers else ""
        )

    async def _rebuild(self) -> None:
        radio_set = self.query_one("#servers", RadioSet)
        await radio_set.remove_children()
        for index, server in enumerate(self._servers):
            await radio_set.mount(
                RadioButton(
                    f"{server.name} — {server.summary()}",
                    value=(index == 0),
                    id=f"server-{index}",
                )
            )
        self._sync_empty()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.dismiss(list(self._servers))

    @on(Button.Pressed, "#add")
    def _add_pressed(self) -> None:
        self.action_add()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_add(self) -> None:
        self.app.push_screen(ServerEditModal(RemoteServer(name="")), self._on_saved)

    def action_edit(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        from multi_claude.remote import TokenStore

        current = self._servers[index]
        self.app.push_screen(
            ServerEditModal(current, has_token=TokenStore().has_token(current.name)),
            self._on_saved,
        )

    def action_remove(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        del self._servers[index]
        self.run_worker(self._rebuild(), exclusive=False)

    def _on_saved(self, server: RemoteServer | None) -> None:
        if server is None or not server.name:
            return
        for index, current in enumerate(self._servers):
            if current.name == server.name:
                self._servers[index] = server
                break
        else:
            self._servers.append(server)
        self.run_worker(self._rebuild(), exclusive=False)

    def _selected_index(self) -> int | None:
        pressed = self.query_one("#servers", RadioSet).pressed_button
        radio_id = pressed.id if pressed is not None else None
        if not radio_id or not radio_id.startswith("server-"):
            return None
        index = int(radio_id.split("-", 1)[1])
        return index if 0 <= index < len(self._servers) else None


class RepoLinkModal(ModalScreen["RemoteLink | None"]):
    """Point a project at one sessions repo, on a configured server or a shared folder.

    The server list comes from Ajustes, so linking a repo asks only what is specific to it:
    which server, which repo, which branch, and what to call its tab.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    RepoLinkModal { align: center middle; }
    RepoLinkModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 84;
        height: auto;
    }
    RepoLinkModal Label.title { text-style: bold; }
    RepoLinkModal Label.section { text-style: bold; color: $accent; }
    RepoLinkModal Label.hint { color: $text-muted; }
    RepoLinkModal Label.error { color: $error; }
    RepoLinkModal Horizontal { align: center middle; height: auto; margin-top: 1; }
    RepoLinkModal Button { margin: 0 1; }
    RepoLinkModal #fields-folder, RepoLinkModal #fields-repo { height: auto; }
    """

    # Radio index of the "shared folder" option, which needs a path instead of a repo.
    FOLDER = "target-folder"

    def __init__(
        self, link: RemoteLink, *, servers: list[RemoteServer], title: str | None = None
    ) -> None:
        super().__init__()
        self._initial = link
        self._servers = servers
        self._title_text = title or "Repositorio de sesiones"

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal, VerticalScroll

        with Vertical():
            yield Label(self._title_text, classes="title")
            yield Label("Enter guarda · Esc cancela", classes="hint")
            if not self._servers:
                yield Label(
                    "No hay servidores configurados: añádelos en Ajustes para poder elegir "
                    "GitLab o GitHub.",
                    classes="hint",
                )

            with VerticalScroll(id="link-body"):
                yield Label("Dónde", classes="section")
                with RadioSet(id="link-target"):
                    for index, server in enumerate(self._servers):
                        yield RadioButton(
                            f"{server.name} — {server.summary()}",
                            value=(server.name == self._initial.server),
                            id=f"target-{index}",
                        )
                    yield RadioButton(
                        "Carpeta compartida",
                        value=(self._initial.kind == "directory" or not self._servers),
                        id=self.FOLDER,
                    )

                with Vertical(id="fields-folder"):
                    yield Label("Ruta de la carpeta", classes="section")
                    yield Input(
                        value=self._initial.path,
                        placeholder="/mnt/equipo/sesiones-claude",
                        id="link-path",
                    )

                with Vertical(id="fields-repo"):
                    yield Label("Repositorio", classes="section")
                    yield Input(
                        value=self._initial.repo,
                        placeholder="grupo/sesiones-cliente-x",
                        id="link-repo",
                    )
                    yield Label("Rama", classes="section")
                    yield Input(value=self._initial.branch, placeholder="main", id="link-branch")

                yield Label("Nombre de la pestaña (opcional)", classes="section")
                yield Input(
                    value=self._initial.label,
                    placeholder=self._initial.tab_label(),
                    id="link-label",
                )

            yield Label("", id="link-error", classes="error")
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Guardar", id="save", variant="primary")

    def on_mount(self) -> None:
        self._sync_visible_fields()
        self.query_one("#link-target", RadioSet).focus()
        self.call_after_refresh(self._scroll_to_top)

    def _scroll_to_top(self) -> None:
        self.query_one("#link-body").scroll_home(animate=False)

    @on(RadioSet.Changed, "#link-target")
    def _on_target_changed(self, event: RadioSet.Changed) -> None:
        self._sync_visible_fields()

    def _sync_visible_fields(self) -> None:
        folder = self._chosen_server() is None
        self.query_one("#fields-folder").display = folder
        self.query_one("#fields-repo").display = not folder

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self._try_save()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._try_save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _try_save(self) -> None:
        link = self.collect()
        server = self._chosen_server()
        missing = (
            "Falta la ruta de la carpeta"
            if server is None and not link.path
            else "Falta el repositorio"
            if server is not None and not link.repo
            else ""
        )
        if missing:
            self.query_one("#link-error", Label).update(missing)
            return
        self.dismiss(link)

    def _chosen_server(self) -> RemoteServer | None:
        """The selected server, or None when the folder option is chosen."""
        pressed = self.query_one("#link-target", RadioSet).pressed_button
        radio_id = pressed.id if pressed is not None else None
        if not radio_id or radio_id == self.FOLDER or not radio_id.startswith("target-"):
            return None
        index = int(radio_id.split("-", 1)[1])
        return self._servers[index] if 0 <= index < len(self._servers) else None

    def collect(self) -> RemoteLink:
        server = self._chosen_server()
        if server is None:
            return RemoteLink(
                kind="directory",
                path=self.query_one("#link-path", Input).value,
                label=self.query_one("#link-label", Input).value,
            ).normalised()
        return RemoteLink(
            kind=server.kind,
            host=server.api_host,
            server=server.name,
            repo=self.query_one("#link-repo", Input).value,
            branch=self.query_one("#link-branch", Input).value,
            label=self.query_one("#link-label", Input).value,
        ).normalised()


class ProjectRemotesModal(ModalScreen["list[RemoteLink] | None"]):
    """Manage which sessions repos a project publishes to.

    A project can be linked to several — one per client, one for product work — and each one
    becomes a tab in the sessions listing. Returns the final list, or None if cancelled.

    Links are stored against the project's git ``origin``, so linking one worktree links every
    worktree of that repo. The modal says so, because otherwise the shared effect looks like a
    bug the first time it happens.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("a", "add", "Añadir"),
        Binding("delete", "remove", "Quitar"),
    ]

    DEFAULT_CSS = """
    ProjectRemotesModal {
        align: center middle;
    }
    ProjectRemotesModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 86;
        height: auto;
    }
    ProjectRemotesModal Label.title {
        text-style: bold;
    }
    ProjectRemotesModal Label.hint {
        color: $text-muted;
    }
    ProjectRemotesModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    ProjectRemotesModal Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    ProjectRemotesModal Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        *,
        project_name: str,
        links: list[RemoteLink],
        inherited: bool = False,
        servers: list[RemoteServer] | None = None,
    ) -> None:
        super().__init__()
        self.project_name = project_name
        self._links = list(links)
        self.servers = list(servers or [])
        # ``inherited`` means what is listed came from the global setting, not from a link of
        # this project's own. Saving turns it into an explicit link, which is worth saying.
        self._inherited = inherited

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Vertical():
            yield Label(f"Repositorios de sesiones — {self.project_name}", classes="title")
            yield Label(
                "Cada repositorio es una pestaña. El enlace se guarda contra el origin "
                "del repo, así que vale para todos sus worktrees.",
                classes="hint",
            )
            if self._inherited:
                yield Label(
                    "Ahora usa el remoto global; al guardar tendrá enlaces propios.",
                    classes="hint",
                )

            yield Label("Enlazados", classes="section")
            yield Label("", id="links-empty", classes="hint")
            with RadioSet(id="links"):
                yield from self._link_buttons()

            yield Label("a añade · Supr quita el seleccionado · Enter guarda", classes="hint")
            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Añadir…", id="add", variant="default")
                yield Button("Quitar", id="remove", variant="default")
                yield Button("Guardar", id="save", variant="primary")

    def _link_buttons(self) -> ComposeResult:
        for index, link in enumerate(self._links):
            yield RadioButton(
                f"{link.tab_label()} — {link.summary()}",
                value=(index == 0),
                id=f"link-{index}",
            )

    def on_mount(self) -> None:
        self._sync_empty_label()

    def _sync_empty_label(self) -> None:
        label = self.query_one("#links-empty", Label)
        label.update("(ninguno)" if not self._links else "")

    async def _rebuild(self) -> None:
        radio_set = self.query_one("#links", RadioSet)
        await radio_set.remove_children()
        for index, link in enumerate(self._links):
            await radio_set.mount(
                RadioButton(
                    f"{link.tab_label()} — {link.summary()}",
                    value=(index == 0),
                    id=f"link-{index}",
                )
            )
        self._sync_empty_label()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.dismiss(list(self._links))

    @on(Button.Pressed, "#add")
    def _add_pressed(self) -> None:
        self.action_add()

    @on(Button.Pressed, "#remove")
    def _remove_pressed(self) -> None:
        self.action_remove()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_add(self) -> None:
        self.app.push_screen(
            RepoLinkModal(
                RemoteLink(),
                servers=self.servers,
                title=f"Añadir repositorio — {self.project_name}",
            ),
            self._on_added,
        )

    def _on_added(self, result: RemoteLink | None) -> None:
        if result is None or not result.is_configured:
            return
        # Re-adding the same target replaces it, so one repo can never own two tabs.
        for index, current in enumerate(self._links):
            if current.same_target(result):
                self._links[index] = result
                break
        else:
            self._links.append(result)
        self.run_worker(self._rebuild(), exclusive=False)

    def action_remove(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        del self._links[index]
        self.run_worker(self._rebuild(), exclusive=False)

    def _selected_index(self) -> int | None:
        pressed = self.query_one("#links", RadioSet).pressed_button
        radio_id = pressed.id if pressed is not None else None
        if not radio_id or not radio_id.startswith("link-"):
            return None
        index = int(radio_id.split("-", 1)[1])
        return index if 0 <= index < len(self._links) else None


class PublishModal(ModalScreen["RemoteLink | None"]):
    """Confirm publishing sessions, and choose which repo they go to.

    Was originally the delete-confirmation modal reused, which meant the accept button said
    "Borrar" in red — wrong verb, wrong colour, and alarming for an upload.

    Picking the destination here rather than beforehand means one dialogue answers both
    questions at once, and the file list stays visible while you choose. That list is the
    point: the transcript drags ``tool-results/`` along, so a session that once printed a
    ``.env`` would publish it.

    Dismisses with the chosen :class:`RemoteLink`, or None on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    PublishModal {
        align: center middle;
    }
    PublishModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 84;
        height: auto;
    }
    PublishModal Label.title {
        text-style: bold;
    }
    PublishModal Label.warning {
        color: $warning;
        text-style: bold;
    }
    PublishModal Label.keys {
        color: $text-muted;
    }
    PublishModal Label.hint {
        color: $text-muted;
    }
    PublishModal Label.section {
        text-style: bold;
        color: $accent;
    }
    PublishModal Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    PublishModal Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        *,
        session_count: int,
        files: list[str],
        destinations: list[RemoteLink],
        preselected: int = 0,
    ) -> None:
        super().__init__()
        self.session_count = session_count
        self.files = files
        self.destinations = destinations
        self.preselected = preselected if 0 <= preselected < len(destinations) else 0

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal, VerticalScroll

        with Vertical():
            yield Label(
                f"Publicar {self.session_count} sesión(es) · {len(self.files)} ficheros",
                classes="title",
            )
            yield Label(
                "⚠️  Se sube el transcript completo, incluidos los tool-results. "
                "Revisa que no haya secretos.",
                classes="warning",
            )
            yield Label("Enter publica · Esc cancela", classes="keys")

            # Everything that can grow lives in one scrollable body, so the title, the
            # warning and the buttons stay put however many repos or files there are.
            with VerticalScroll(id="publish-body"):
                if len(self.destinations) > 1:
                    yield Label("Repositorio de destino", classes="section")
                    with RadioSet(id="publish-destination"):
                        for index, link in enumerate(self.destinations):
                            yield RadioButton(
                                f"{link.tab_label()} — {link.summary()}",
                                value=(index == self.preselected),
                                id=f"dest-{index}",
                            )
                else:
                    target = self.destinations[0]
                    yield Label(
                        f"Destino: {target.tab_label()} — {target.summary()}", classes="hint"
                    )

                yield Label("Se suben estos ficheros", classes="section")
                for line in self.files:
                    yield Static(line)

            with Horizontal():
                yield Button("Cancelar", id="cancel", variant="default")
                yield Button("Publicar", id="publish", variant="primary")

    def on_mount(self) -> None:
        if len(self.destinations) > 1:
            self.query_one("#publish-destination", RadioSet).focus()
        else:
            self.query_one("#publish", Button).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#publish")
    def _publish(self) -> None:
        self.dismiss(self.chosen())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event: object) -> None:
        """Enter accepts from anywhere, including with the radio focused."""
        key = getattr(event, "key", None)
        if key == "enter":
            _stop_event(event)
            self.dismiss(self.chosen())

    def chosen(self) -> RemoteLink:
        """The destination the user selected, or the only one there is."""
        if len(self.destinations) == 1:
            return self.destinations[0]
        pressed = self.query_one("#publish-destination", RadioSet).pressed_button
        radio_id = pressed.id if pressed is not None else None
        if radio_id and radio_id.startswith("dest-"):
            index = int(radio_id.split("-", 1)[1])
            if 0 <= index < len(self.destinations):
                return self.destinations[index]
        return self.destinations[self.preselected]


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
            # Warning before the details, not after: the details can be a long file list, and
            # the one line that must not be missed is the reason to think twice.
            if self.warning:
                yield Label(f"⚠️  {self.warning}", classes="warning")
            yield Label("`y` confirma · Enter/Esc cancela", classes="hint")
            for line in self.details:
                yield Static(line)
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


class MoveSessionModal(ModalScreen["Project | None"]):
    """Pick a destination worktree (sibling or parent) to move session(s) into.

    Lists the other live members of the current repo's worktree group. Dismisses
    with the chosen :class:`Project`, or ``None`` on cancel. The caller guarantees
    ``candidates`` is non-empty.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    MoveSessionModal {
        align: center middle;
    }
    MoveSessionModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    MoveSessionModal Label.title {
        text-style: bold;
    }
    MoveSessionModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    MoveSessionModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    MoveSessionModal Label.error {
        color: $error;
        margin-top: 1;
    }
    """

    def __init__(self, session_count: int, candidates: list[Project]) -> None:
        super().__init__()
        self.session_count = session_count
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Mover sesión(es) a otro worktree", classes="title")
            yield Static(f"{self.session_count} sesión(es) seleccionada(s)")
            yield Label("Destino", classes="section")
            with RadioSet(id="move-target"):
                for idx, candidate in enumerate(self.candidates):
                    label = f"{candidate.name} — {candidate.path}"
                    yield RadioButton(label, value=(idx == 0), id=f"target-{idx}")
            yield Label("Enter confirma · Esc cancela", classes="hint")
            yield Label("", id="move-error", classes="error")

    def on_mount(self) -> None:
        self.query_one("#move-target", RadioSet).focus()

    def on_key(self, event: object) -> None:
        if getattr(event, "key", None) == "enter":
            self._submit()

    def _submit(self) -> None:
        radio_set = self.query_one("#move-target", RadioSet)
        pressed = radio_set.pressed_button
        if pressed is None or pressed.id is None or not pressed.id.startswith("target-"):
            self.query_one("#move-error", Label).update("Selecciona un destino.")
            return
        idx = int(pressed.id.split("-", 1)[1])
        self.dismiss(self.candidates[idx])

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilePathModal(ModalScreen["Path | None"]):
    """Prompt for a filesystem path, prefilled with a default the user can edit.

    Shell-like autocomplete (mirroring :class:`AddProjectModal`) surfaces matching
    directories and files below the input:
      - ``Tab``  → extend the input to the longest common prefix of candidates.
      - ``↓``    → move focus into the suggestion list; ``Enter`` picks one.
      - ``Enter`` on the input → submit and resolve the path.

    Files are filtered to ``suffixes`` (default ``(".zip",)``); directories are
    always shown so you can keep descending. ``mode="open"`` requires the final
    path to be an existing file (import an archive); ``mode="save"`` only forbids
    a directory (choose where to write an export). Returns the resolved
    :class:`Path` on submit, ``None`` on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "focus_suggestions", "Elegir sugerencia", priority=True),
    ]

    DEFAULT_CSS = """
    FilePathModal {
        align: center middle;
    }
    FilePathModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    FilePathModal Label.title {
        text-style: bold;
    }
    FilePathModal Label.error {
        color: $error;
        margin-top: 1;
    }
    FilePathModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    FilePathModal OptionList#path-suggestions {
        max-height: 12;
        border: round $accent;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        mode: str = "open",
        default: str = "",
        placeholder: str = "/ruta/al/archivo.zip",
        suffixes: tuple[str, ...] = (".zip",),
    ) -> None:
        super().__init__()
        self._title = title
        self._mode = mode
        self._default = default
        self._placeholder = placeholder
        self._suffixes = suffixes

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList

        with Vertical():
            yield Label(self._title, classes="title")
            yield Input(value=self._default, placeholder=self._placeholder, id="path-input")
            suggestions = OptionList(id="path-suggestions")
            suggestions.display = False
            yield suggestions
            yield Label("", id="path-error", classes="error")
            yield Label("Enter confirma · Tab completa · ↓ elige · Esc cancela", classes="hint")

    def on_mount(self) -> None:
        input_w = self.query_one("#path-input", Input)
        input_w.focus()
        input_w.cursor_position = len(input_w.value)
        self._refresh_suggestions(input_w.value)

    # -- typing + suggestions ------------------------------------------------ #

    @on(Input.Changed, "#path-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_suggestions(event.value)

    def _refresh_suggestions(self, prefix: str) -> None:
        from textual.widgets import OptionList

        from multi_claude.path_complete import list_suggestions

        suggestions = list_suggestions(prefix, include_files=True, suffixes=self._suffixes)
        opt_list = self.query_one("#path-suggestions", OptionList)
        opt_list.clear_options()
        if not suggestions:
            opt_list.display = False
            return
        opt_list.display = True
        for path in suggestions:
            opt_list.add_option(str(path) + ("/" if path.is_dir() else ""))

    # -- keys ---------------------------------------------------------------- #

    def on_key(self, event: object) -> None:
        key = getattr(event, "key", None)
        if key == "tab":
            self._tab_complete()
            _stop_event(event)
            return
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
            opt_list = self.query_one("#path-suggestions", OptionList)
        except Exception:
            return False
        return bool(opt_list.has_focus)

    def _suggestions_at_top(self) -> bool:
        from textual.widgets import OptionList

        try:
            opt_list = self.query_one("#path-suggestions", OptionList)
        except Exception:
            return False
        return opt_list.highlighted in (None, 0)

    def _focus_input(self) -> None:
        input_w = self.query_one("#path-input", Input)
        input_w.focus()
        input_w.cursor_position = len(input_w.value)

    def action_focus_suggestions(self) -> None:
        """Move focus into the suggestion list (priority binding so Input doesn't eat ↓)."""
        from textual.widgets import OptionList

        input_w = self.query_one("#path-input", Input)
        opt_list = self.query_one("#path-suggestions", OptionList)
        if not input_w.has_focus:
            return
        if not opt_list.display or opt_list.option_count == 0:
            return
        opt_list.focus()
        opt_list.highlighted = 0

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "focus_suggestions":
            try:
                input_w = self.query_one("#path-input", Input)
            except Exception:
                return False
            if not input_w.has_focus:
                return False
            from textual.widgets import OptionList

            try:
                opt_list = self.query_one("#path-suggestions", OptionList)
            except Exception:
                return False
            if not opt_list.display or opt_list.option_count == 0:
                return False
        return True

    def _tab_complete(self) -> None:
        from multi_claude.path_complete import common_prefix_completion

        input_w = self.query_one("#path-input", Input)
        completion = common_prefix_completion(
            input_w.value, include_files=True, suffixes=self._suffixes
        )
        if completion is None or completion == input_w.value:
            return
        input_w.value = completion
        input_w.cursor_position = len(completion)
        self._refresh_suggestions(completion)

    # -- option picked ------------------------------------------------------- #

    def _handle_suggestion_selected(self, prompt: str) -> None:
        if not prompt:
            return
        # The label already carries a trailing "/" for directories, so picking one
        # lists its contents on the next refresh; a file becomes the final value.
        input_w = self.query_one("#path-input", Input)
        input_w.value = prompt
        input_w.cursor_position = len(prompt)
        input_w.focus()
        self._refresh_suggestions(prompt)

    def on_option_list_option_selected(self, event: object) -> None:
        control = getattr(event, "control", None) or getattr(event, "option_list", None)
        if control is not None and getattr(control, "id", None) != "path-suggestions":
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
        try:
            resolved = Path(raw).expanduser().resolve(strict=False)
        except OSError as exc:
            self._set_error(f"Ruta inválida: {exc}")
            return
        if self._mode == "open":
            if not resolved.exists():
                self._set_error(f"No existe: {resolved}")
                return
            if not resolved.is_file():
                self._set_error(f"No es un archivo: {resolved}")
                return
        elif resolved.is_dir():
            self._set_error("Es un directorio; indica un nombre de archivo")
            return
        self.dismiss(resolved)

    def _set_error(self, msg: str) -> None:
        self.query_one("#path-error", Label).update(msg)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ImportTargetModal(ModalScreen["Project | None"]):
    """Pick the destination project for an imported session archive.

    Lists every live (non-orphan) project; the imported sessions land in the chosen
    project's encoded dir and resume under its cwd. Dismisses with the chosen
    :class:`Project`, or ``None`` on cancel. The caller guarantees ``candidates`` is
    non-empty.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    ImportTargetModal {
        align: center middle;
    }
    ImportTargetModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    ImportTargetModal Label.title {
        text-style: bold;
    }
    ImportTargetModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    ImportTargetModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    ImportTargetModal Label.error {
        color: $error;
        margin-top: 1;
    }
    """

    def __init__(self, summary: list[str], candidates: list[Project]) -> None:
        super().__init__()
        self.summary = summary
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Importar sesiones — elegir proyecto destino", classes="title")
            for line in self.summary:
                yield Static(line)
            yield Label("Destino", classes="section")
            with RadioSet(id="import-target"):
                for idx, candidate in enumerate(self.candidates):
                    label = f"{candidate.name} — {candidate.path}"
                    yield RadioButton(label, value=(idx == 0), id=f"target-{idx}")
            yield Label("Enter confirma · Esc cancela", classes="hint")
            yield Label("", id="import-error", classes="error")

    def on_mount(self) -> None:
        self.query_one("#import-target", RadioSet).focus()

    def on_key(self, event: object) -> None:
        if getattr(event, "key", None) == "enter":
            self._submit()

    def _submit(self) -> None:
        radio_set = self.query_one("#import-target", RadioSet)
        pressed = radio_set.pressed_button
        if pressed is None or pressed.id is None or not pressed.id.startswith("target-"):
            self.query_one("#import-error", Label).update("Selecciona un destino.")
            return
        idx = int(pressed.id.split("-", 1)[1])
        self.dismiss(self.candidates[idx])

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


class TagEditorModal(ModalScreen[list[str] | None]):
    """Edit the tag list of a session.

    Dismisses with:
      - ``None``       → cancel (no change)
      - ``[]``         → clear every tag from the session
      - ``["a", "b"]`` → replace the session's tags with this list (normalised)

    The input accepts both comma- and whitespace-separated tags. ``Tab`` extends
    the current trailing token to the longest common prefix among known tags
    that start with it; pressing it again with no extension picks the next
    candidate so the user can cycle. Clicking on a known-tag chip toggles it
    in the input.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancelar"),
        Binding("ctrl+s", "save", "Guardar", show=False),
    ]

    DEFAULT_CSS = """
    TagEditorModal {
        align: center middle;
    }
    TagEditorModal > Vertical {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 90;
        height: auto;
    }
    TagEditorModal Label.title {
        text-style: bold;
    }
    TagEditorModal Label.section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    TagEditorModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    TagEditorModal OptionList#known-tags {
        max-height: 10;
        border: round $accent;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        subtitle: str,
        current_tags: tuple[str, ...] | list[str],
        known_tags: list[str],
    ) -> None:
        super().__init__()
        self.subtitle = subtitle
        self.current_tags = list(current_tags)
        self.known_tags = sorted({t for t in known_tags if t})

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        with Vertical():
            yield Label("Etiquetas de la sesión", classes="title")
            yield Static(self.subtitle)
            yield Label("Etiquetas (espacio o coma para separar)", classes="section")
            yield Input(
                value=" ".join(self.current_tags),
                placeholder="bug urgente cliente-acme",
                id="tags-input",
            )
            if self.known_tags:
                yield Label("Conocidas (Enter para añadir / quitar)", classes="section")
                opt_list = OptionList(
                    *[Option(f"# {t}", id=f"tag:{t}") for t in self.known_tags],
                    id="known-tags",
                )
                yield opt_list
            yield Label(
                "Enter en input guarda · vacío borra todas · Esc cancela",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.query_one("#tags-input", Input).focus()

    @on(Input.Submitted, "#tags-input")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(parse_tag_list(event.value))

    def on_key(self, event: object) -> None:
        key = getattr(event, "key", None)
        if key == "tab":
            self._tab_complete()
            _stop_event(event)

    def _tab_complete(self) -> None:
        if not self.known_tags:
            return
        input_w = self.query_one("#tags-input", Input)
        text = input_w.value
        # Find the last token (after the last comma or whitespace).
        boundary = max(text.rfind(" "), text.rfind(","), text.rfind("\t"))
        prefix = text[boundary + 1 :].lower()
        if not prefix:
            return
        matches = [t for t in self.known_tags if t.startswith(prefix)]
        if not matches:
            return
        # Cycle if Tab is pressed while we already match exactly one of them.
        candidate = matches[0]
        if prefix in self.known_tags:
            try:
                idx = matches.index(prefix)
            except ValueError:
                idx = -1
            candidate = matches[(idx + 1) % len(matches)]
        new_value = text[: boundary + 1] + candidate
        input_w.value = new_value
        input_w.cursor_position = len(new_value)

    def on_option_list_option_selected(self, event: object) -> None:
        control = getattr(event, "control", None) or getattr(event, "option_list", None)
        if control is not None and getattr(control, "id", None) != "known-tags":
            return
        option = getattr(event, "option", None)
        option_id = getattr(option, "id", None) if option is not None else None
        if not isinstance(option_id, str) or not option_id.startswith("tag:"):
            return
        tag = option_id.split(":", 1)[1]
        input_w = self.query_one("#tags-input", Input)
        current = parse_tag_list(input_w.value)
        if tag in current:
            current.remove(tag)
        else:
            current.append(tag)
        input_w.value = " ".join(current)
        input_w.cursor_position = len(input_w.value)
        input_w.focus()

    def action_save(self) -> None:
        self.dismiss(parse_tag_list(self.query_one("#tags-input", Input).value))

    def action_cancel(self) -> None:
        self.dismiss(None)
