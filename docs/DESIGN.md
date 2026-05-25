# Diseño técnico

Este documento extiende el README con detalles que no caben en una visión general.

## Decisiones cerradas

| Tema                          | Decisión                                                                 |
|-------------------------------|--------------------------------------------------------------------------|
| Stack                         | Python 3.10+ con Textual                                                 |
| Lanzamiento de `claude`       | Modo configurable: auto (mux→window→suspend) / window / suspend           |
| Metadatos por sesión          | primer prompt, fecha, branch, nº mensajes, tamaño                        |
| Scope MVP                     | navegación + reanudar + nueva sesión (sin preview, búsqueda, borrar)     |
| Huérfanos                     | visibles, estilo dim, acciones bloqueadas                                |
| Acciones extra (editor, etc.) | fuera del MVP                                                            |
| Worktrees / proyecto movido   | no agrupación; tratamiento como proyectos independientes                 |

## Fuente de verdad del cwd

El nombre de carpeta `~/.claude/projects/<encoded>/` codifica el path original con `/` → `-`. La decodificación inversa es ambigua: `/foo-bar/baz` y `/foo/bar/baz` colisionan.

Por eso resolvemos el cwd real leyendo el campo `cwd` del primer evento del primer `.jsonl` del proyecto. Solo si ningún jsonl es parseable (proyecto recién creado, corrupto…) caemos al heurístico ingenuo.

Pseudo-código:
```python
def resolve_real_cwd(project_dir):
    for jsonl in sorted(project_dir.glob("*.jsonl")):
        for line in read_first_n_lines(jsonl, 50):
            event = json.loads(line)
            if cwd := event.get("cwd"):
                return Path(cwd)
    return decode_path_fallback(project_dir.name)
```

## Parsing barato

Para el listado **no** parseamos el jsonl entero. Solo necesitamos:
- Primer evento con `cwd` y `gitBranch`.
- Primer evento `type=user` con `message.role=user` (suele estar entre los primeros 20-30 eventos).
- mtime del archivo (stat).
- nº de líneas (lectura streaming).

El parser pesado se reserva para v2 (preview de mensajes).

## Primer prompt legible

Una sesión arrancada con un slash-command tiene como primer user message:
```
<command-message>refine-task</command-message>
<command-name>/refine-task</command-name>
<command-args>https://git.factorlibre.com/odoo-16/fl-v16/-/issues/8758</command-args>
```

`strip_command_wrappers` debe convertirlo en algo como `/refine-task https://git.factorlibre.com/...`.

Si el primer user message es texto plano (no comando), se muestra recortado a ~80 chars con `…`.

Si la sesión tenía `--name`, ese display name gana al primer prompt en la columna.

## Launcher: modos y matriz

`launch_claude(cwd, session_id, *, mode)` acepta tres modos:

| Modo       | Cadena de despacho                                                                           |
|------------|----------------------------------------------------------------------------------------------|
| `auto`     | tmux split → zellij split → terminator tab → ventana nueva del emulador → suspend            |
| `window`   | ventana nueva del emulador → suspend                                                         |
| `suspend`  | siempre `app.suspend()` + `subprocess.run([...], cwd=cwd)`                                   |

**Despacho de multiplexer** (modo `auto`):

| Entorno                 | Acción                                                                            |
|-------------------------|-----------------------------------------------------------------------------------|
| `$TMUX` set             | `tmux split-window -h -c <cwd> claude [--resume <id>]`                            |
| `$ZELLIJ` set           | `zellij action new-pane --cwd <cwd> -- claude [--resume <id>]`                    |
| `$TERMINATOR_UUID` set  | `terminator --new-tab --working-directory=<cwd> -x claude [--resume <id>]`        |

**Despacho de ventana** (modo `window`, o `auto` cuando no hay multiplexer).

Detección en este orden:

1. `$TERM_PROGRAM` (mapa `ghostty` → ghostty, `wezterm` → wezterm). Canónico y case-insensitive.
2. Env var específica del emulador.
3. Fallback genérico `x-terminal-emulator` / `xterm`.

| Emulador          | Señal de detección                                  | Comando lanzado                                          |
|-------------------|-----------------------------------------------------|----------------------------------------------------------|
| kitty             | `$KITTY_PID`                                        | `kitty --directory <cwd> claude ...`                     |
| WezTerm           | `$TERM_PROGRAM=WezTerm` o `$WEZTERM_EXECUTABLE`     | `wezterm start --cwd <cwd> -- claude ...`                |
| Ghostty           | `$TERM_PROGRAM=ghostty` o `$GHOSTTY_RESOURCES_DIR`  | `ghostty --working-directory=<cwd> -e claude ...`        |
| Alacritty         | `$ALACRITTY_WINDOW_ID` / `$ALACRITTY_LOG`           | `alacritty --working-directory <cwd> -e claude ...`      |
| Konsole           | `$KONSOLE_VERSION`                                  | `konsole --workdir <cwd> -e claude ...`                  |
| GNOME Terminal    | `$GNOME_TERMINAL_SCREEN`                            | `gnome-terminal --working-directory=<cwd> -- claude ...` |
| foot              | `$FOOT_VERSION`                                     | `foot --working-directory=<cwd> claude ...`              |
| Terminator        | `$TERMINATOR_UUID`                                  | `terminator --working-directory=<cwd> -x claude ...`     |
| x-terminal-emulator / xterm | (fallback genérico)                       | `<term> -e sh -c "cd <cwd> && exec claude ..."`          |

La ventana se lanza con `subprocess.Popen(..., start_new_session=True, stdin/out/err=DEVNULL)` para desligarla de la TUI: el proceso hijo sobrevive si la TUI cae y no compite por el TTY.

**Errores:**

| Caso                  | Acción                                                                      |
|-----------------------|-----------------------------------------------------------------------------|
| `claude` no en PATH   | `LauncherError("claude no encontrado en PATH")` y `self.notify(...)`        |
| Env var set, binario ausente | se ignora esa opción y se cae al siguiente eslabón                   |
| `mode="window"` sin emulador detectable | fallback a `suspend`                                      |

Prioridad dentro de `auto`: `tmux` > `zellij` > `terminator-tab` > `window` > `suspend`. tmux/zellij/Terminator anidan unos dentro de otros, así que respetar la jerarquía mantiene el nuevo pane lo más cerca posible del pane actual del usuario.

## Configuración persistente

`~/.config/multi-claude/config.json` (o `$XDG_CONFIG_HOME/multi-claude/config.json`):

```json
{
  "default_mode": "auto"
}
```

- `default_mode` se invoca con `Enter` y desde el modal de Add Project.
- El modo de **Shift+Enter** se deriva mediante `config.alternate_for(default)`:

  | Default   | Shift+Enter |
  |-----------|-------------|
  | `auto`    | `suspend`   |
  | `window`  | `suspend`   |
  | `suspend` | `window`    |

  Diseño: si el default ya evita suspender la TUI, el alternativo fuerza suspend; si el default suspende, el alternativo abre ventana nueva. No hay configuración independiente del alternativo (regla, no preferencia).
- Modos válidos: `auto`, `window`, `suspend`. Cualquier otro valor cae al default seguro.
- El modal `SettingsModal` (atajo `s`) edita `default_mode` y muestra en vivo cuál será el alternativo. Persiste vía `app.update_prefs()`.
- Claves legacy (`alternate_mode`) en `config.json` se ignoran al cargar — forward-compat sin romper instalaciones existentes.

## Layout Textual

```
ClaudeBrowserApp
├── ProjectsScreen (initial)
│   ├── Header
│   ├── DataTable
│   └── Footer
└── SessionsScreen (pushed on Enter)
    ├── Header (con nombre + path real del proyecto)
    ├── DataTable
    └── Footer
```

`Footer` muestra los bindings activos automáticamente.

## Plan de implementación sugerido

1. `discovery.py` + tests con un fixture pequeño en `tests/fixtures/`.
2. `session.py` + tests, incluido `strip_command_wrappers` (puro, fácil).
3. `launcher.py` con detect + build argv, mockeando `subprocess`.
4. `ProjectsScreen` con scan real, sin acciones (solo render).
5. `SessionsScreen` con scan real, sin acciones.
6. Wiring de Enter / n / launcher.
7. Manejo de huérfanos (estilos + bloqueo de actions).
8. Pruebas end-to-end manuales con `uv run multi-claude`.

## Lo que queda fuera (v2+)

- Preview de la sesión seleccionada (panel a la derecha con últimos mensajes).
- Búsqueda full-text sobre el contenido de los jsonl.
- Borrar / renombrar sesiones.
- Fork de sesión (`claude --resume <id> --fork-session`).
- Filtrado por branch o por fecha.
- Agrupación de worktrees bajo su repo raíz.
- Reconciliación de proyectos movidos vía remote URL del `.git`.
