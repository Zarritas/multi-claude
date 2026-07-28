# Diseño técnico

Este documento extiende el README con detalles que no caben en una visión general.

## Decisiones cerradas

| Tema                          | Decisión                                                                 |
|-------------------------------|--------------------------------------------------------------------------|
| Stack                         | Python 3.10+ con Textual                                                 |
| Lanzamiento de `claude`       | Placement configurable: auto / split / tab / window / suspend, degradando en cadena |
| Flags de `claude`             | `claude_args` del usuario delante de las que gestiona la TUI; las reservadas se rechazan |
| Metadatos por sesión          | primer prompt, fecha, branch, tags, nº mensajes, tamaño                  |
| Búsqueda                      | índice SQLite + FTS5 como caché reconstruible, nunca fuente de verdad    |
| Huérfanos                     | visibles, estilo dim, acciones bloqueadas (salvo merge y borrado)        |
| Worktrees                     | agrupados por `git_common_dir`, solo cuando el cwd es la raíz del worktree |
| Proyecto movido de path       | sin reconciliación automática; merge manual del huérfano sobre el vivo   |
| Escrituras en disco de Claude | ninguna, salvo mover/borrar jsonl; el estado propio va a ficheros aparte  |

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

`launch_claude(cwd, session_id, *, mode, claude_args)` separa el **dónde** (placement) del **cómo**.
Cada modo degrada al siguiente eslabón cuando su destino no existe:

| Modo       | Cadena de despacho                                                        |
|------------|---------------------------------------------------------------------------|
| `auto`     | panel del multiplexer → pestaña → ventana nueva → suspend                 |
| `split`    | panel del multiplexer → pestaña → ventana nueva → suspend                 |
| `tab`      | pestaña de la ventana actual → ventana nueva → suspend                    |
| `window`   | ventana nueva del emulador → suspend                                      |
| `suspend`  | siempre `app.suspend()` + `subprocess.run([...], cwd=cwd)`                |

Devuelve un `LaunchOutcome(placement, target, fallback_reason)`. `fallback_reason` no es `None`
cuando el destino pedido no estaba disponible; las pantallas lo convierten en una notificación
`warning` para que la degradación nunca sea silenciosa. `preview_dispatch(mode)` recorre la misma
cadena **sin lanzar nada**, y es lo que alimenta la línea "Aquí y ahora" del modal de ajustes.

**Despacho de multiplexer** (tiene prioridad: tmux/zellij/terminator anidan dentro del emulador):

| Entorno                 | `split`                                                | `tab`                                    |
|-------------------------|--------------------------------------------------------|-------------------------------------------|
| `$TMUX` set             | `tmux split-window -h -c <cwd> claude [...]`           | `tmux new-window -c <cwd> claude [...]`   |
| `$ZELLIJ` set           | `zellij action new-pane --cwd <cwd> -- claude [...]`   | igual que `split`, con motivo declarado ¹ |
| `$TERMINATOR_UUID` set  | `terminator --new-tab --working-directory=<cwd> -x`    | idéntico (Terminator solo hace pestañas)  |

¹ `zellij action new-tab` solo acepta layout, no un comando, así que una petición de pestaña
aterriza en un panel y `fallback_reason` lo explica.

**Despacho de pestaña** (`Emulator.tab_argv`). `Emulator.tab_rpc` marca los clientes de control
remoto (`kitty @`, `wezterm cli`): salen inmediatamente y devuelven código ≠ 0 cuando la función
está deshabilitada, así que se ejecutan con `subprocess.run` y su fallo degrada a ventana. El resto
se lanzan detached como las ventanas.

| Emulador          | Pestaña                                                  |
|-------------------|-----------------------------------------------------------|
| kitty             | `kitty @ launch --type=tab --cwd <cwd> -- claude ...` (rpc) |
| WezTerm           | `wezterm cli spawn --cwd <cwd> -- claude ...` (rpc)        |
| GNOME Terminal    | `gnome-terminal --tab --working-directory=<cwd> -- ...`    |
| Konsole           | `konsole --new-tab --workdir <cwd> -e claude ...`          |
| Terminator        | `terminator --new-tab --working-directory=<cwd> -x ...`    |
| Windows Terminal  | `wt.exe -w 0 new-tab -d <cwd> -- claude ...`               |
| iTerm2            | `osascript` → `create tab with default profile`            |
| Ghostty, Alacritty, foot, Apple Terminal | `tab_argv=None` → degradan a ventana |

**Despacho de ventana** (modo `window`, o eslabón siguiente cuando no hay pestaña).

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
| GNOME Terminal    | `$GNOME_TERMINAL_SCREEN`                            | `gnome-terminal --window --working-directory=<cwd> -- claude ...` |
| foot              | `$FOOT_VERSION`                                     | `foot --working-directory=<cwd> claude ...`              |
| Terminator        | `$TERMINATOR_UUID`                                  | `terminator --working-directory=<cwd> -x claude ...`     |
| x-terminal-emulator / xterm | (fallback genérico)                       | `<term> -e sh -c "cd <cwd> && exec claude ..."`          |

La ventana se lanza con `subprocess.Popen(..., start_new_session=True, stdin/out/err=DEVNULL)` para desligarla de la TUI: el proceso hijo sobrevive si la TUI cae y no compite por el TTY.

**Errores:**

| Caso                  | Acción                                                                      |
|-----------------------|-----------------------------------------------------------------------------|
| `claude` no en PATH   | `LauncherError("claude no encontrado en PATH")` y `self.notify(...)`        |
| Env var set, binario ausente | se ignora esa opción y se cae al siguiente eslabón                   |
| Multiplexer presente que falla | `LauncherError` con su stderr (no se degrada: algo está roto)      |
| Sin emulador detectable | fallback a `suspend` con `fallback_reason`                                |
| Emulador detectado sin CLI (VS Code, Warp, Tabby, ConEmu) | fallback a `suspend` con el motivo        |

Prioridad dentro de `auto`: `tmux` > `zellij` > `terminator` > pestaña del emulador > ventana > `suspend`. tmux/zellij/Terminator anidan unos dentro de otros, así que respetar la jerarquía mantiene la sesión nueva lo más cerca posible de donde está el usuario.

## Configuración persistente

`~/.config/multi-claude/config.json` (o `$XDG_CONFIG_HOME/multi-claude/config.json`):

```json
{
  "default_mode": "auto",
  "claude_args": ["--dangerously-skip-permissions"]
}
```

- `default_mode` se invoca con `Enter` y desde el modal de Add Project.
- El modo de **Shift+Enter** se deriva mediante `config.alternate_for(default)`:

  | Default   | Shift+Enter |
  |-----------|-------------|
  | `auto`    | `suspend`   |
  | `split`   | `window`    |
  | `tab`     | `window`    |
  | `window`  | `suspend`   |
  | `suspend` | `window`    |

  Diseño: si el default ya evita suspender la TUI, el alternativo fuerza suspend o ventana aparte; si el default suspende, el alternativo abre ventana nueva. No hay configuración independiente del alternativo (regla, no preferencia).
- Modos válidos: `auto`, `split`, `tab`, `window`, `suspend`. Cualquier otro valor cae al default seguro; los ficheros con los tres modos antiguos siguen cargando sin migración.
- `claude_args` son flags extra que se anteponen a `--resume`/`-n` en cada lanzamiento. Se guardan como lista, pero un `config.json` editado a mano puede traer un string: `parse_claude_args` lo trocea con `shlex`. Las flags de `RESERVED_CLAUDE_FLAGS` (`--resume`, `-c`, `-n`, `-p`, `--bg`, `--from-pr`) se rechazan porque colisionan con la sesión que la TUI está reanudando.
- El modal `SettingsModal` (atajo `s`) edita `default_mode` y `claude_args`, dibuja un esquema ASCII del modo elegido, muestra qué haría ese modo en la terminal actual (`preview_dispatch`) y qué hará Shift+Enter. Devuelve `dataclasses.replace(initial, ...)` — nunca un `Config` nuevo — para no pisar el resto de preferencias.
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

## Lo que queda fuera

- Fork de sesión (`claude --resume <id> --fork-session`) como acción propia — de momento se puede
  conseguir metiendo `--fork-session` en `claude_args`, pero afectaría a todos los lanzamientos.
- Reconciliación automática de proyectos movidos vía remote URL del `.git` (hoy: merge manual con `m`).
- `claude_args` por proyecto: la configuración es global, no por proyecto ni por sesión.
- Pestañas en Ghostty, Alacritty, foot y Terminal.app: sus CLIs no lo permiten (ver la matriz de
  despacho más arriba). Si Ghostty acaba exponiendo `+new-tab`, es una entrada más en `EMULATORS`.
