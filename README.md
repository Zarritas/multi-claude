# multi-claude

TUI para navegar los proyectos y sesiones de Claude Code y reanudar (o crear) sesiones desde un punto central.

## Qué resuelve

Claude Code guarda cada sesión como un `.jsonl` bajo `~/.claude/projects/<encoded-path>/`. Cuando acumulas decenas de proyectos y cientos de sesiones, encontrar "aquella conversación de hace tres semanas sobre el refactor X" se vuelve incómodo: `claude --resume` te muestra solo las del cwd actual, y saltar entre proyectos implica `cd`s y memorizar UUIDs.

`multi-claude` es un dashboard en terminal que lista todos tus proyectos, muestra sus sesiones con metadatos legibles, y al pulsar Enter lanza `claude --resume <id>` en un panel nuevo del multiplexer.

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
- `Enter` — reanudar esta sesión (`claude --resume <id>` con cwd del proyecto).
- `n` — nueva sesión en este proyecto (`claude` con cwd del proyecto, sin `--resume`).
- `Esc` / `←` — volver a la pantalla de proyectos.
- `r` — re-escanear las sesiones del proyecto.
- `q` — salir.

## Cómo se lanza Claude

`launcher.launch_claude(cwd, session_id=None)` resuelve el entorno y elige modo:

1. **`$TMUX` está definido** → `tmux split-window -h -c <cwd> claude [--resume <id>]`. La TUI sigue viva en su pane.
2. **`$ZELLIJ` está definido** → `zellij action new-pane --cwd <cwd> -- claude [--resume <id>]`.
3. **Ninguno** → `app.suspend()` y `subprocess.run(["claude", ...], cwd=cwd)`. Al salir de Claude vuelves a la TUI.

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

Lo más rápido — instalación global aislada desde GitHub, queda en PATH como `mc`:

```bash
pipx install git+https://github.com/Zarritas/multi-claude.git
# o, si prefieres uv:
uv tool install git+https://github.com/Zarritas/multi-claude.git
```

Y ya:

```bash
mc
```

Para actualizar a la última versión:

```bash
pipx upgrade multi-claude     # o: uv tool upgrade multi-claude
```

Para desinstalar:

```bash
pipx uninstall multi-claude   # o: uv tool uninstall multi-claude
```

### Requisitos

- Python 3.10+
- `claude` (Claude Code CLI) en `PATH`
- Opcional: `tmux` o `zellij` para abrir Claude en un split sin perder la TUI

### Instalación desde una copia local

Si has clonado el repo y quieres instalar tu versión:

```bash
pipx install .
# o
uv tool install .
```

## Desarrollo

```bash
git clone https://github.com/Zarritas/multi-claude.git
cd multi-claude
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

mc                  # arranca la TUI
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
