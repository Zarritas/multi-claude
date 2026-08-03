# Diseño técnico

Este documento extiende el README con detalles que no caben en una visión general.

Las sesiones compartidas entre máquinas y compañeros tienen su propio plan en
[REMOTE-SESSIONS.md](REMOTE-SESSIONS.md).

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
| Alcance frente a `agent view` | el "ahora mismo" es de `claude agents`; lo nuestro es el archivo histórico, su organización y el equipo |
| Servidor MCP                  | JSON-RPC 2.0 por stdio con la stdlib, sin el SDK; solo lectura; salida en texto, no `structuredContent` |
| Sesiones del equipo en `?`    | se cachea el listado de cada remoto al visitar su pestaña; la búsqueda nunca toca la red |
| Contenido de una sesión ajena | payload de búsqueda en un blob aparte (`search/<uuid>.txt.gz`), no en el manifest: listar lee todos los manifests |
| Republicar sobre otra versión | se bloquea antes de escribir (fast-forward por `published_at`); reemplazar es explícito, y bifurcar es `--fork-session` del propio Claude |
| Escáner de secretos           | avisa, no veta; nunca imprime el valor; calibrado contra transcripts reales, no contra un corpus sintético |
| Barrido del histórico         | informe por CLI (`--audit-secrets`), no una pantalla: la acción útil —rotar la credencial— ocurre fuera, y así se puede colgar de un hook |
| Marca `⚠` del listado         | escaneo en worker cacheado contra el `mtime`; sin escanear ≠ limpia, y el índice guarda el número de hallazgos, nunca un valor |
| Estado en vivo                | dos fuentes: registro por PID cada 2 s (rápido) + `claude agents --json` cada 15 s (soportado, ~350 ms), fusionadas |
| Índice                        | se puebla en segundo plano al arrancar, no al entrar a un proyecto; y se purga de filas cuyo jsonl ya no está |

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
| `$TERMINATOR_UUID` set  | `remotinator vsplit -x "cd <cwd> && exec claude [...]"` ² | `terminator --new-tab --working-directory=<cwd> -x` |

¹ `zellij action new-tab` solo acepta layout, no un comando, así que una petición de pestaña
aterriza en un panel y `fallback_reason` lo explica.

² `remotinator` (incluido en Terminator) habla con su API DBus. `vsplit` → `split_axis(vertical=False)`
→ `HPaned`, es decir dos columnas, el equivalente a `tmux split-window -h`. La API solo hereda el cwd
del terminal que parte (`terminal.get_cwd()`), de ahí el `cd` dentro del comando; el objetivo sale de
`$TERMINATOR_UUID`, la misma señal que usa `detect_multiplexer()`. Los fallos llegan por dos vías:
código ≠ 0 si el bus no responde, y `ERROR: ...` por stdout **con** código 0 si responde y rechaza la
petición — `_try_terminator_pane` comprueba las dos y degrada a pestaña con motivo.

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
| Ghostty           | `$TERM_PROGRAM=ghostty` o `$GHOSTTY_RESOURCES_DIR`  | `ghostty +new-window --working-directory=<cwd> -e claude ...` ³ |
| Alacritty         | `$ALACRITTY_WINDOW_ID` / `$ALACRITTY_LOG`           | `alacritty --working-directory <cwd> -e claude ...`      |
| Konsole           | `$KONSOLE_VERSION`                                  | `konsole --workdir <cwd> -e claude ...`                  |
| GNOME Terminal    | `$GNOME_TERMINAL_SCREEN`                            | `gnome-terminal --window --working-directory=<cwd> -- claude ...` |
| foot              | `$FOOT_VERSION`                                     | `foot --working-directory=<cwd> claude ...`              |
| Terminator        | `$TERMINATOR_UUID`                                  | `terminator --working-directory=<cwd> -x claude ...`     |
| x-terminal-emulator / xterm | (fallback genérico)                       | `<term> -e sh -c "cd <cwd> && exec claude ..."`          |

³ `Emulator.window_rpc_argv` es la vía "pídele la ventana a la instancia que ya corre", que se
intenta **antes** de `argv` y se comprueba por código de salida igual que `tab_rpc`. En Ghostty es
`+new-window`, que va por D-Bus y solo existe en el apprt GTK (el de macOS devuelve `false` para
todas las acciones IPC). Si no alcanza la instancia —bus caído, o un Ghostty anterior a la acción—
se cae a `argv`, que arranca un proceso nuevo; el placement es el mismo, así que ese fallback
interno no genera `fallback_reason`. En macOS `argv` es
`open -na Ghostty.app --args --working-directory=<cwd> -e claude ...`, porque la CLI de Ghostty se
niega a lanzar el emulador ahí ("only actions are supported"); por lo mismo el `binary` a buscar en
PATH pasa a ser `open`, y una instalación solo-`.app` sigue detectándose.

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

## Buscar las sesiones del equipo sin tocar la red

La búsqueda global tiene dos orígenes con propiedades distintas, y el diseño consiste sobre
todo en no disimularlo.

**Dónde se puebla.** `_load_remote_worker` ya está en un thread y ya tiene el listado que
pidió al remoto, así que ahí mismo se escribe en `remote_sessions` con
`replace_remote_sessions`. La alternativa —que la pantalla de búsqueda consultara los
remotos— haría que teclear en un input disparara llamadas de red por pulsación. El precio
es que lo publicado después de tu última visita a esa pestaña no aparece hasta que vuelvas,
y eso se documenta en lugar de ocultarse.

**Replace, no upsert.** El listado *es* la verdad del remoto en ese instante. Con un upsert,
una sesión que alguien despublicó seguiría siendo un resultado que nadie puede traer.

**La clave es la identidad del enlace**, `RemoteLink.identity_key()`, no su etiqueta:
renombrar una pestaña no debe convertirla en un segundo remoto con las filas duplicadas. La
misma función define `same_target`, así que "el mismo remoto" significa una sola cosa en
todo el código.

**Qué se puede buscar.** Ya es el contenido, no solo los metadatos, pero el texto **no** vive
en el manifest: listar una pestaña lee todos los manifests, así que media MB de texto por
sesión convertiría abrir una pestaña en una descarga de decenas de MB. Va en un blob aparte
(`search/<uuid>.txt.gz`) que se descarga bajo demanda y pesa unas 36 veces menos que el
transcript. El texto es el mismo payload que indexa la búsqueda local —prompts y
respuestas, sin salida de herramientas—, lo que de paso mantiene fuera de lo que se sube el
lugar donde es más probable que se cuele una credencial.

**Dos tablas, dos búsquedas.** Los `rank` de dos tablas FTS5 no son comparables, así que los
resultados se concatenan (los tuyos primero) en vez de entremezclarse con un orden inventado.
La columna `Dónde` es lo que mantiene visible de qué origen es cada fila.

## El servidor MCP no usa el SDK

`mcp.py` habla el protocolo a mano. El transporte stdio de MCP es JSON-RPC 2.0 con un mensaje
por línea y sin newlines embebidos, y los métodos que hace falta atender para exponer
herramientas son cuatro: `initialize`, `notifications/initialized` (que no se responde),
`tools/list` y `tools/call`. Eso cabe en un módulo con `json` y la stdlib; el SDK oficial
traería pydantic, anyio y compañía a un proyecto cuyo stack son Textual, rapidfuzz y la
librería estándar.

Dos consecuencias de la spec que condicionan el código:

- **Nada que no sea un mensaje MCP puede ir a stdout.** Todo diagnóstico va a stderr, y por eso
  el bucle de `serve()` captura cualquier excepción y responde un error JSON-RPC en vez de
  dejar que un traceback contamine el canal.
- **La negociación de versión no es un rechazo.** Si el cliente pide una versión que conocemos
  se le devuelve esa; si no, se devuelve la nuestra y es el cliente quien decide desconectar.

Las herramientas devuelven texto legible, no `structuredContent`: la spec pide que quien
devuelve contenido estructurado incluya *además* el JSON serializado en un bloque de texto, y
duplicar el payload solo gastaría tokens de quien lo consume, que es un modelo.

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
  El plan de [sesiones compartidas](REMOTE-SESSIONS.md) lo necesita para bifurcar una sesión
  publicada por otra persona, así que es ahí donde acabará convirtiéndose en acción propia.
- Reconciliación automática de proyectos movidos vía remote URL del `.git` (hoy: merge manual con `m`).
- `claude_args` por proyecto: la configuración es global, no por proyecto ni por sesión.
- Pestañas en Ghostty, Alacritty, foot y Terminal.app: sus CLIs no lo permiten (ver la matriz de
  despacho más arriba). En Ghostty no es cuestión de tiempo: sus únicas acciones IPC son
  `new_window` y `toggle_quick_terminal`, sus acciones D-Bus por ventana (`win.new-tab`,
  `win.split-right`) no aceptan cwd ni comando —así que no pueden llevar un `claude --resume`— y
  upstream cerró la petición de pestañas por CLI como *not planned*
  ([#12136](https://github.com/ghostty-org/ghostty/issues/12136)). Sintetizar `ctrl+shift+t` con
  `ydotool`/`osascript` y teclear el comando queda descartado por frágil. Si algún día aparece una
  acción IPC de pestaña o split, es un `tab_argv` más en `EMULATORS`. Mientras tanto, paneles dentro
  de Ghostty = tmux/zellij.
