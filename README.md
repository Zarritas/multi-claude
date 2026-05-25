# multi-claude

TUI para navegar los proyectos y sesiones de Claude Code y reanudar (o crear) sesiones desde un punto central.

## Qué resuelve

Claude Code guarda cada sesión como un `.jsonl` bajo `~/.claude/projects/<encoded-path>/`. Cuando acumulas decenas de proyectos y cientos de sesiones, encontrar "aquella conversación de hace tres semanas sobre el refactor X" se vuelve incómodo: `claude --resume` te muestra solo las del cwd actual, y saltar entre proyectos implica `cd`s y memorizar UUIDs.

`multi-claude` es un dashboard en terminal que lista todos tus proyectos, muestra sus sesiones con metadatos legibles, y al pulsar Enter lanza `claude --resume <id>` en un panel/pestaña nueva del multiplexer o emulador de terminal.

## Stack

- Python 3.10+
- [Textual](https://textual.textualize.io/) para la TUI
- Standard library para todo lo demás (sin dependencias pesadas de parsing)

## Comportamiento

### Pantalla 1 — Proyectos

`DataTable` con una fila por proyecto detectado en `~/.claude/projects/`.

| Columna           | Origen                                                                 |
|-------------------|------------------------------------------------------------------------|
| Proyecto          | basename del cwd real                                                  |
| Path              | cwd real extraído del primer evento del jsonl (no por decodificación)  |
| Sesiones          | nº de archivos `.jsonl` en el directorio del proyecto                  |
| Última actividad  | mtime más reciente entre los `.jsonl` del proyecto                     |

- Orden por defecto: última actividad descendente.
- Proyectos huérfanos (cwd ya no existe en disco): aparecen en estilo apagado, no se pueden abrir.

Atajos:
- `Enter` — entrar a la pantalla de sesiones del proyecto.
- `r` — re-escanear `~/.claude/projects/`.
- `q` — salir.

### Pantalla 2 — Sesiones del proyecto

`DataTable` con una fila por `.jsonl`.

| Columna           | Origen                                                                                |
|-------------------|---------------------------------------------------------------------------------------|
| Primer prompt     | primer `type=user` con `role=user`, limpiando wrappers `<command-message>` / args     |
| Branch            | `gitBranch` del primer evento con cwd                                                 |
| Msgs              | nº de líneas del jsonl                                                                |
| Tamaño            | size en KB del jsonl                                                                  |
| Última actividad  | mtime del jsonl                                                                       |

- Orden por defecto: última actividad descendente.

Atajos:
- `Enter` — reanudar esta sesión con el **modo de lanzamiento predeterminado**.
- `Shift+Enter` — reanudar esta sesión con el **modo alternativo**.
- `n` — nueva sesión en este proyecto (modo predeterminado).
- `s` — abrir el modal de **Ajustes** para cambiar predeterminado/alternativo.
- `Esc` / `←` — volver a la pantalla de proyectos.
- `r` — re-escanear las sesiones del proyecto.
- `q` — salir.

## Cómo se lanza Claude

`launcher.launch_claude(cwd, session_id=None, *, mode="auto")` despacha según el modo elegido:

| Modo       | Estrategia                                                                                              |
|------------|---------------------------------------------------------------------------------------------------------|
| `auto`     | multiplexer split → ventana nueva del emulador → suspender la TUI                                       |
| `window`   | ventana nueva del emulador → suspender la TUI                                                           |
| `suspend`  | suspender la TUI siempre (`app.suspend()` + `subprocess.run`)                                           |

**Cadena `auto` (en orden):**

1. `$TMUX` → `tmux split-window -h -c <cwd> claude [--resume <id>]`.
2. `$ZELLIJ` → `zellij action new-pane --cwd <cwd> -- claude [--resume <id>]`.
3. `$TERMINATOR_UUID` → `terminator --new-tab --working-directory=<cwd> -x claude [...]`.
4. **Ventana nueva del emulador detectado** (ver tabla más abajo).
5. `app.suspend()` + `subprocess.run(["claude", ...], cwd=cwd)`.

**Emuladores soportados en modo `window`** (detectados vía env vars + binario en PATH):

| Emulador          | Comando lanzado                                                       |
|-------------------|-----------------------------------------------------------------------|
| kitty             | `kitty --directory <cwd> claude ...`                                  |
| WezTerm           | `wezterm start --cwd <cwd> -- claude ...`                             |
| Ghostty           | `ghostty --working-directory=<cwd> -e claude ...`                     |
| Alacritty         | `alacritty --working-directory <cwd> -e claude ...`                   |
| Konsole           | `konsole --workdir <cwd> -e claude ...`                               |
| GNOME Terminal    | `gnome-terminal --working-directory=<cwd> -- claude ...`              |
| foot              | `foot --working-directory=<cwd> claude ...`                           |
| Terminator        | `terminator --working-directory=<cwd> -x claude ...` (ventana nueva)  |
| x-terminal-emulator / xterm | `<term> -e sh -c "cd <cwd> && exec claude ..."`             |

Detección del emulador (en orden):

1. `$TERM_PROGRAM` (canónico, lo publican Ghostty, WezTerm…).
2. Env var específica del emulador (`$KITTY_PID`, `$GHOSTTY_RESOURCES_DIR`, `$ALACRITTY_LOG`, etc.).
3. Fallback genérico: `x-terminal-emulator` o `xterm` si están en PATH.

Si ninguno se detecta en modo `window`, la TUI se suspende como último recurso.

## Ajustes (`s`)

Modal en la TUI con dos selectores:

- **Enter (predeterminado)** — modo por defecto (recomendado: `auto`).
- **Shift+Enter (alternativo)** — modo del atajo alternativo (recomendado: `window`).

Solo se configura el **predeterminado**. El **alternativo** (Shift+Enter) se deriva automáticamente:

| Predeterminado | Alternativo (Shift+Enter) |
|----------------|---------------------------|
| `auto`         | `suspend`                 |
| `window`       | `suspend`                 |
| `suspend`      | `window`                  |

Persistido en `~/.config/multi-claude/config.json` (o `$XDG_CONFIG_HOME/multi-claude/config.json`):

```json
{
  "default_mode": "auto"
}
```

> Nota sobre `Shift+Enter`: la mayoría de los emuladores modernos lo transmiten distinto a `Enter`, pero algunos antiguos no — en ese caso `Shift+Enter` simplemente hará lo mismo que `Enter`. Si te ocurre, cambia el predeterminado en Ajustes para que ambas teclas hagan lo que quieres.

## Identidad de un proyecto

El nombre de la carpeta `~/.claude/projects/<encoded>/` es la ruta original con `/` reemplazado por `-`. Esta codificación es ambigua si el path original contenía guiones (`/foo-bar/baz` y `/foo/bar/baz` colisionan).

**Fuente de verdad**: el campo `cwd` del primer evento `type=user` del primer `.jsonl` del proyecto. Solo si no hay ningún jsonl parseable se cae a la heurística `-` → `/`.

`os.path.isdir(cwd)` decide si el proyecto está vivo o huérfano.

## Limitaciones conocidas (MVP)

- **Worktrees git**: cada worktree es un cwd distinto → un proyecto distinto en la TUI. No se agrupan bajo el repo raíz.
- **Proyecto movido de path**: si renombras una carpeta, las sesiones viejas y nuevas aparecen como dos proyectos. No se reconcilian.
- **Sin preview de mensajes**: la lista de sesiones muestra solo el primer prompt. Para leer la conversación tienes que reanudarla o abrir el jsonl a mano.
- **Sin búsqueda full-text**: no hay grep sobre el contenido de las sesiones. Filtras visualmente.

Todas son extensiones razonables para una v2.

## Instalación

### Requisitos previos

- **Python 3.10+** (la mayoría de distros modernas lo traen).
- **`claude`** (Claude Code CLI) en `PATH`. Sin él, `multi-claude` arranca pero no podrá reanudar sesiones — la propia TUI te lo dirá.
- *(Opcional)* **`tmux`**, **`zellij`** o **`terminator`** para que Claude se abra en un split/pestaña sin perder la TUI.
- *(Opcional)* Un emulador soportado (**kitty**, **WezTerm**, **Ghostty**, **Alacritty**, **Konsole**, **GNOME Terminal**, **foot**, **Terminator**, **xterm**…) — el modo `window` abre una ventana nueva en el emulador que estés usando. Sin nada de esto, la TUI se suspende y vuelve cuando cierras Claude.

### Paso 1 — Instalar un gestor de herramientas Python (si no tienes ninguno)

Cualquiera de los dos funciona; **uv** es el más rápido.

```bash
# uv (recomendado)
curl -LsSf https://astral.sh/uv/install.sh | sh

# o pipx
sudo apt install pipx && pipx ensurepath      # Debian/Ubuntu
brew install pipx && pipx ensurepath          # macOS
```

Cierra y abre la terminal para que `~/.local/bin` entre en `PATH`.

### Paso 2 — Instalar multi-claude

Una sola línea, sin clonar nada:

```bash
uv tool install git+https://github.com/Zarritas/multi-claude.git
# o
pipx install git+https://github.com/Zarritas/multi-claude.git
```

### Paso 3 — Lanzarlo

```bash
multi-claude
```

Deberías ver la lista de tus proyectos de Claude. Pulsa `Enter` para entrar en uno, `Enter` otra vez para reanudar una sesión.

### Actualizar a la última versión

```bash
uv tool upgrade multi-claude
# o
pipx upgrade multi-claude
```

### Desinstalar

```bash
uv tool uninstall multi-claude
# o
pipx uninstall multi-claude
```

### Instalación desde una copia local del repo

Si has clonado el repo y quieres instalar tu versión modificada:

```bash
git clone https://github.com/Zarritas/multi-claude.git
cd multi-claude
uv tool install .       # o: pipx install .
```

### Troubleshooting

- **`multi-claude: command not found`** tras instalar → `~/.local/bin` no está en tu `PATH`.
  - `uv` y `pipx` añaden automáticamente esa ruta a la config de tu shell, pero hace falta reiniciar la terminal. Si persiste, ejecuta `uv tool dir --bin` o `pipx environment --value PIPX_BIN_DIR` y añade esa ruta a tu `PATH`.
- **`claude no encontrado en PATH`** al pulsar Enter sobre una sesión → instala Claude Code CLI siguiendo su guía oficial.
- **Proyectos en gris (huérfanos)** → la carpeta original del proyecto ya no existe (moviste o borraste el directorio). Las sesiones siguen ahí pero no se pueden reanudar; bórralas con `d`.

## Desarrollo

```bash
git clone https://github.com/Zarritas/multi-claude.git
cd multi-claude
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

multi-claude        # arranca la TUI
pytest              # corre la suite (74 tests)
```

## Estructura del código

```
src/multi_claude/
  __main__.py        # entrypoint: arranca ClaudeBrowserApp
  app.py             # ClaudeBrowserApp(textual.App) — registra screens
  discovery.py       # scan_projects() → list[Project]
  session.py         # scan_sessions(project) → list[Session], parsers
  launcher.py        # launch_claude(cwd, session_id) con detección de multiplexer
  screens/
    projects.py      # ProjectsScreen — DataTable, bindings
    sessions.py      # SessionsScreen — DataTable, bindings
  styles.tcss        # estilos Textual
```
