# multi-claude

TUI para navegar los proyectos y sesiones de Claude Code y reanudar (o crear) sesiones desde un punto central.

## Qué resuelve

Claude Code guarda cada sesión como un `.jsonl` bajo `~/.claude/projects/<encoded-path>/`. Cuando acumulas decenas de proyectos y cientos de sesiones, encontrar "aquella conversación de hace tres semanas sobre el refactor X" se vuelve incómodo: `claude --resume` te muestra solo las del cwd actual, y saltar entre proyectos implica `cd`s y memorizar UUIDs.

`multi-claude` es un dashboard en terminal que lista todos tus proyectos, muestra sus sesiones con metadatos legibles, y al pulsar Enter lanza `claude --resume <id>` en un panel/pestaña nueva del multiplexer o emulador de terminal.

## Qué trae

- **Búsqueda full-text** (`?`) sobre el contenido de todas las sesiones, con índice FTS5 de SQLite — encuentra "aquella conversación sobre el refactor X" por lo que se dijo dentro.
- **Preview** (`p`) de los últimos turnos de una sesión sin reanudarla.
- **Worktrees agrupados** por defecto: los worktrees de un mismo repo colapsan en una fila, con pantalla propia para entrar a cada uno.
- **Carpetas de usuario** (`f`) para organizar proyectos en un árbol propio.
- **Filtro incremental** (`/`) con `branch:`, `path:`, `id:`, `tag:` y texto libre fuzzy.
- **Etiquetas** (`t`) y **colores** por sesión (`c`), con reglas automáticas por branch, antigüedad o actividad (`C`).
- **Nombres persistentes** (`e`) para sesiones y proyectos, incluido el `/rename` de Claude como fallback.
- **Mover, exportar e importar** sesiones entre worktrees o hacia un `.zip` compartible (`m`, `x`, `i`).
- **Sesiones compartidas** (`L`, `u`): enlaza cada proyecto a uno o varios repositorios de sesiones (GitLab, GitHub o una carpeta), que aparecen como pestañas; publica con `u` y reanuda la sesión de un compañero con `Enter`, sin exportar ni importar nada.
- **Sin duplicados**: si una sesión ya está abierta en otra terminal, la trae al frente en vez de abrir una segunda.
- **Borrado y limpieza** (`d`, `D`) que arrastran todos los artefactos en disco, no solo el jsonl.

## Stack

- Python 3.10+
- [Textual](https://textual.textualize.io/) para la TUI
- [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) para el matching fuzzy del filtro
- SQLite (`sqlite3` de la stdlib, con FTS5) para el índice y la búsqueda global
- Standard library para todo lo demás (sin dependencias pesadas de parsing)

## Comportamiento

### Pantalla 1 — Proyectos

`DataTable` con una fila por proyecto detectado en `~/.claude/projects/`.

| Columna           | Origen                                                                 |
|-------------------|------------------------------------------------------------------------|
| Proyecto          | basename del cwd real                                                  |
| Path              | cwd real extraído del primer evento del jsonl (no por decodificación)  |
| Sesiones          | nº de archivos `.jsonl` en el directorio del proyecto                  |
| Última            | mtime más reciente entre los `.jsonl` del proyecto                     |

- Orden por defecto: última actividad descendente.
- Proyectos huérfanos (cwd ya no existe en disco): aparecen en estilo apagado, no se pueden abrir.
- Los **worktrees git del mismo repo se agrupan** en una sola fila (ver [Worktrees](#worktrees-g)).
- Los proyectos asignados a una **carpeta** se muestran dentro de ella (ver [Carpetas](#carpetas-f)).

Atajos:
- `Enter` — entrar a la pantalla de sesiones del proyecto (o a la de worktrees / la carpeta, según la fila).
- `a` — añadir un proyecto a mano indicando su path.
- `e` — renombrar el proyecto (alias local, no toca el disco).
- `f` — asignar el proyecto a una **carpeta** (o quitarlo de ella).
- `g` — alternar entre **worktrees agrupados** y expandidos.
- `i` — **importar** un `.zip` de sesiones exportado por otra persona; tras validar el archivo, eliges en qué proyecto existente aterrizan.
- `L` — enlazar el proyecto a uno o varios **repositorios de sesiones** compartidas.
- `m` — **merge de un proyecto huérfano** sobre otro proyecto vivo: mueve sus sesiones y le traspasa el alias. Solo disponible sobre filas huérfanas.
- `d` — borrar el proyecto (cascada sobre todas sus sesiones y artefactos en disco).
- `C` — editar las **reglas de color** (ver [Colores](#colores-c-y-c)).
- `/` — filtrar la lista (ver [Filtro](#filtro-)).
- `?` — **búsqueda full-text global** sobre el contenido de todas las sesiones (ver [Búsqueda global](#búsqueda-global-full-text-)).
- `1`…`4` — ordenar por nombre / path / nº de sesiones / última actividad.
- `Shift+S` — invertir la dirección del orden.
- `s` — abrir el modal de **Ajustes**.
- `r` — re-escanear `~/.claude/projects/`.
- `Esc` — limpiar el filtro.
- `Ctrl+Q` — salir.

### Pantalla 2 — Sesiones del proyecto

`DataTable` con una fila por `.jsonl`.

| Columna           | Origen                                                                                |
|-------------------|---------------------------------------------------------------------------------------|
| Prompt            | primer `type=user` con `role=user`, limpiando wrappers `<command-message>` / args      |
| Branch            | `gitBranch` del primer evento con cwd                                                 |
| Tags              | etiquetas asignadas a mano (ver [Etiquetas](#etiquetas-t))                             |
| Msgs              | nº de líneas del jsonl                                                                |
| Tamaño            | size en KB del jsonl                                                                  |
| Última            | mtime del jsonl                                                                       |

- Orden por defecto: última actividad descendente.
- Si la sesión tiene un nombre puesto con `e` (o con el `/rename` de Claude), ese nombre sustituye al primer prompt en la columna **Prompt**.

Atajos:
- `Enter` — reanudar esta sesión con el **modo de lanzamiento predeterminado** (por defecto `auto`: panel del multiplexer, o pestaña de la ventana actual si no hay).
- `Shift+Enter` — reanudar esta sesión con el **modo alternativo**, derivado del predeterminado (ver [Ajustes](#ajustes-s)).

> **Sesiones ya abiertas**: si la sesión ya está corriendo en otra terminal (registrada como viva en `~/.claude/sessions/`), `Enter`/`Shift+Enter` **no abren un duplicado** — multi-claude intenta traer al frente la terminal existente (tmux → X11/XWayland vía `xdotool`/`wmctrl` → GNOME Wayland vía la extensión [Window Calls](https://github.com/ickyicky/window-calls) → macOS vía System Events). Si ninguna estrategia aplica en tu entorno (p.ej. GNOME Wayland sin esa extensión), se bloquea el lanzamiento con un aviso en lugar de abrir una segunda terminal sobre el mismo jsonl.
- `n` — nueva sesión en este proyecto (modo predeterminado).
- `Espacio` — marcar/desmarcar la sesión actual (multi-selección).
- `p` — mostrar/ocultar el **panel de preview** (ver [Preview](#preview-p)).
- `e` — renombrar la sesión (nombre persistente propio de multi-claude).
- `t` — editar las **etiquetas** de la sesión.
- `c` — asignar un **color** manual a la sesión; `C` — editar las **reglas de color**.
- `y` — copiar el **id** de la sesión al portapapeles.
- `m` — **mover** la(s) sesión(es) seleccionada(s) a otro worktree del mismo repo (el checkout principal o un worktree hermano). Si no hay nada marcado, mueve la fila actual.
- `x` — **exportar** la(s) sesión(es) seleccionada(s) a un único `.zip` compartible (para enviárselo a un compañero). Si no hay nada marcado, exporta la fila actual.
- `u` — **publicar** la(s) sesión(es) en el repositorio de la pestaña activa, sin zip de por medio (ver [Sesiones compartidas](#sesiones-compartidas-l-y-u)). Pide confirmación mostrando qué ficheros se suben.
- `L` — gestionar los **repositorios de sesiones** enlazados a este proyecto; cada uno es una pestaña del listado, y `Enter` sobre una fila compartida la trae y la reanuda.
- `d` — borrar la(s) sesión(es) seleccionada(s) y todos sus artefactos en disco.
- `D` — **limpieza** por antigüedad: eliges un umbral y borra de golpe las sesiones más viejas (las sesiones vivas quedan protegidas).
- `/` — filtrar la lista (ver [Filtro](#filtro-)).
- `1`…`6` — ordenar por prompt / branch / tags / msgs / tamaño / última actividad.
- `Shift+S` — invertir la dirección del orden.
- `s` — abrir el modal de **Ajustes**: modo de lanzamiento (con vista previa) y flags extra para `claude`.
- `Esc` / `←` — limpiar el filtro, o volver a la pantalla de proyectos.
- `r` — re-escanear las sesiones del proyecto.
- `Ctrl+Q` — salir.

### Worktrees (`g`)

Varios worktrees de un mismo repo son cwds distintos y por tanto proyectos distintos en `~/.claude/projects/`. multi-claude los **agrupa por defecto** (`group_worktrees: true`): los proyectos que comparten `git_common_dir` colapsan en una única fila con el nº de sesiones y la última actividad agregados del grupo.

- `g` alterna entre agrupados y expandidos, y la preferencia se persiste.
- `Enter` sobre una fila de grupo abre la **pantalla de worktrees**, que lista los miembros individuales; desde ahí `Enter` entra a las sesiones de ese worktree concreto y `e` lo renombra.

### Carpetas (`f`)

Además de la agrupación automática por repo, puedes organizar los proyectos en carpetas jerárquicas creadas por ti (`Trabajo`, `Trabajo/Cliente A`…). Un proyecto pertenece como máximo a una carpeta.

- `f` sobre un proyecto lo asigna a una carpeta (o lo saca de ella).
- `Enter` sobre una carpeta abre la **pantalla de carpeta**, con sus subcarpetas y sus proyectos. Dentro: `n` crea una subcarpeta, `e` renombra, `f` quita un proyecto de la carpeta, `i` importa un `.zip`, `d` borra la carpeta.

El árbol se guarda en `project-folders.json` (ver [Ficheros de estado](#ficheros-de-estado)).

### Búsqueda global full-text (`?`)

`?` desde la pantalla de proyectos abre una pantalla de búsqueda que consulta una tabla **FTS5 de SQLite** construida sobre la concatenación de los prompts del usuario y el texto del asistente de cada sesión. Escribes y los resultados se refrescan en un worker en background (hasta 200 filas), con columnas Sesión / Proyecto / Branch / Última.

`Enter` sobre un resultado te lleva a la pantalla de sesiones del proyecto que lo contiene.

El tokenizer es `unicode61 remove_diacritics 2`, así que `refactor` encuentra `refactorización` y los acentos son indiferentes. El índice es una **caché, no la fuente de verdad**: vive en `$XDG_DATA_HOME/multi-claude/index.sqlite3` (por defecto `~/.local/share/...`) y si se corrompe se reconstruye en el siguiente escaneo.

### Preview (`p`)

`p` en la pantalla de sesiones abre un panel lateral de solo lectura que renderiza los **últimos turnos** de la sesión bajo el cursor (hasta 12 turnos, leyendo las últimas 60 líneas del jsonl, con el texto recortado a 800 caracteres por mensaje). Sirve para reconocer una conversación sin reanudarla. La visibilidad se persiste en `preview_visible`.

### Filtro (`/`)

`/` abre un input de filtrado incremental sobre la tabla actual. La sintaxis admite restricciones `clave:valor` mezcladas con texto libre:

| Clave     | Efecto                                                              |
|-----------|---------------------------------------------------------------------|
| `branch:` | subcadena sobre la branch                                           |
| `path:`   | subcadena sobre el path del proyecto                                |
| `id:`     | subcadena sobre el id de la sesión                                  |
| `tag:`    | lista separada por comas; **todas** las etiquetas deben coincidir   |

Todo lo que no sea `clave:valor` se trata como texto libre y se puntúa con `rapidfuzz.fuzz.partial_ratio` (umbral 70), así que tolera erratas. Ejemplo: `branch:main tag:bug,urgente refacto`.

> Ojo: `/` filtra las filas que ya están en pantalla. Para buscar **dentro del contenido** de las conversaciones, usa `?`.

### Etiquetas (`t`)

Etiquetas planas y múltiples por sesión, para cortar la lista con `tag:`. Se normalizan a minúsculas, los espacios internos pasan a `-` y los caracteres reservados (`,` y `:`) se descartan para que nunca choquen con la sintaxis del filtro. Se guardan en `session-tags.json`.

### Colores (`c` y `C`)

Dos capas, y la manual gana:

1. **Color manual** (`c`) — eliges de una paleta de 9 colores y queda fijado a esa sesión en `session-colors.json`.
2. **Reglas** (`C`) — patrones evaluados en orden; gana la primera que casa. Se guardan en `config.json`.

Condiciones soportadas en una regla (un `when` por regla):

| Condición             | Significado                                                        |
|-----------------------|--------------------------------------------------------------------|
| `branch=main`         | branch exacta (case-insensitive)                                   |
| `branch~=feature/*`   | glob sobre la branch                                               |
| `prompt~=^/`          | regex sobre el prompt / nombre mostrado                            |
| `active=true`         | la sesión está viva según `~/.claude/sessions`                     |
| `age<1h`, `age<2d`    | la última actividad es más reciente que el umbral (`s`/`m`/`h`/`d`/`w`) |

### Sesiones compartidas (`L` y `u`)

Publicar una sesión en un sitio común y que un compañero la reanude con `Enter`, sin el
viaje de ida y vuelta de `x` (exportar) → enviar el zip → `i` (importar).

**Enlazar repositorios a un proyecto.** Cada proyecto de Claude puede publicar a **uno o
varios** repositorios de sesiones, y cada uno aparece como **pestaña** en el listado de
sesiones. Se gestionan con **`L`**, tanto en la pantalla de proyectos como dentro del proyecto.

El enlace se guarda contra el **`origin` del repo**, no contra la ruta, así que:

- Todos los **worktrees** de un repo comparten enlace: enlazas uno y quedan enlazados todos.
- `git@host:grupo/repo.git` y `https://host/grupo/repo.git` son la misma clave.
- Un proyecto sin `origin` se enlaza por su ruta absoluta (funciona, pero solo en esa máquina).

**Remoto global.** En **Ajustes (`s`) → pestaña «Sesiones compartidas»** se configura un remoto
que sirve de *fallback* para los proyectos sin enlaces propios. Los enlaces del proyecto ganan
por completo: un proyecto enlazado al repo de un cliente no publica además al global.

El diálogo de configuración pide proveedor, servidor, repositorio, rama, token y nombre de la
pestaña, y tiene **`Ctrl+T` para probar la conexión** antes de guardar. El remoto global se puede
**desactivar** eligiendo «Desactivado»; los enlaces de un proyecto se quitan con su botón
**Quitar**.

**Servidores.** En Ajustes defines los servidores una vez (nombre, proveedor, URL y
autenticación) y luego, al enlazar un repositorio a un proyecto, los eliges por nombre: solo
tienes que indicar el repo y la rama. Corregir una URL o rotar un token arregla de golpe todos
los repositorios que apuntan a ese servidor.

| Autenticación | Qué necesita | Cuándo |
|---------------|--------------|--------|
| **Token de acceso** | un token por servidor | Vía API REST. Un token por persona y por host |
| **SSH** | nada nuevo | Usa las claves que ya tienes. Sin tokens que repartir, y **git resuelve las publicaciones simultáneas** en vez de que la última pise a la anterior |

> **El usuario SSH es siempre `git`**, no tu usuario de GitHub/GitLab. En
> `git@github.com:Zarritas/multi-claude.git`, `Zarritas` es parte del *repositorio*. Solo hay que
> cambiarlo en instalaciones self-hosted que usen otro usuario.
>
> **Si tu servidor usa un puerto SSH distinto del 22, ponlo.** Míralo en la URL SSH de cualquier
> repo suyo: en `ssh://git@git.tuempresa.com:2211/grupo/repo.git` el puerto es `2211`. Es
> frecuente en GitLab self-hosted, y no se puede deducir de la URL web (esa contesta por 443
> igualmente).
>
> `Ctrl+T` sobre un servidor SSH ejecuta `ssh -T` y te dice como quién te autentica
> (`autenticado en git.tuempresa.com:2211 como jesus.lorenzo`), sin necesitar ningún
> repositorio.

| Destino | Qué necesita |
|---------|--------------|
| Carpeta compartida | una ruta (montaje de red, Syncthing…). Los permisos son los del sistema de ficheros |
| GitLab / GitHub | un servidor configurado + `grupo/repo` + rama |

Con GitLab o GitHub los permisos, el SSO y la auditoría son los del propio repositorio: quien
pueda leerlo puede leer las sesiones, y cada publicación es un commit con su autor. Es la razón
principal para preferirlos a una carpeta.

Los **tokens nunca se guardan en `config.json`** (ese fichero se comparte y se pega en issues):
van a `remote-tokens.json`, junto a la config, con permisos `0600` y uno por servidor.
`$MULTI_CLAUDE_REMOTE_TOKEN` los sobreescribe, para que CI no tenga que escribir un secreto en
disco. Con autenticación SSH no hay token que guardar.

Con SSH se mantiene una copia de trabajo del repo bajo `~/.cache/multi-claude/repos/`, que es
caché reconstruible: se puede borrar sin perder nada.

Los campos también se pueden editar a mano en `config.json` (`remote_kind`, `remote_host`,
`remote_repo`, `remote_branch`, `remote_path`), y `MULTI_CLAUDE_REMOTE_DIR=/ruta` fuerza una
carpeta por encima de todo lo demás — útil para probar sin tocar tu configuración real.

**Qué hace cada tecla:**

- `L` — gestiona los repositorios de sesiones enlazados a este proyecto (añadir, quitar).
- **Pestañas** — `Locales` muestra tus sesiones; cada `☁ nombre` es una vista del repositorio:
  **todo lo publicado en él**, con su autor y un indicador del estado de tu copia local. La barra
  se oculta si no hay nada enlazado.

  | Marca | Significado |
  |-------|-------------|
  | `☁` | Publicada, no la tienes en local. `Enter` la trae y la reanuda |
  | `✓` | Descargada y al día con lo publicado |
  | `↻` | Descargada, pero alguien la continuó después: **hay versión más reciente** |
  | `↑` | Descargada y la has continuado tú: **tienes turnos sin publicar** |

  El estado se calcula comparando el tamaño de tu `.jsonl` con el que registra el manifest.
  Como el transcript solo crece, cualquier diferencia es contenido real.

- **En la pestaña `Locales`** también se ve qué sesiones están compartidas, con el mismo
  vocabulario visto desde el otro lado:

  | Marca | Significado |
  |-------|-------------|
  | (sin marca) | Solo tuya, no está en ningún repositorio |
  | `✓` | Publicada y al día. Si la subió otra persona, se indica: `· de ana` |
  | `↻` | El repositorio tiene una versión más reciente que tu copia |
  | `↑` | Tienes turnos que no has publicado |

  Se consulta a los repositorios enlazados en segundo plano, así que la lista aparece al
  instante y las marcas se pintan al llegar. Si un repositorio no responde, te quedas sin
  esa marca, no sin listado.
- `u` — publica la fila actual (o todas las marcadas). El diálogo pide confirmación y, si hay
  **varios repositorios enlazados, te deja elegir a cuál** (parte del de la pestaña en la que
  estés). Muestra además **la lista de ficheros exactos** que salen de la máquina: el
  transcript incluye los `tool-results/`, así que una sesión que imprimió un `.env` lo
  publicaría.
- `Enter` sobre una compartida — si no la tienes (`☁`), la descarga en el directorio de este
  proyecto preservando su uuid y la reanuda; si ya la tienes, reanuda tu copia local. Avisa antes
  de lanzar cuando se grabó sobre otro commit, o cuando tu copia está por detrás de la publicada
  (traer los turnos nuevos de una sesión ya descargada todavía no está implementado).
- `d` sobre una compartida — **despublicarla**: la quita del repositorio para todos. Tu copia
  local no se toca, y el diálogo lo dice explícitamente. En la pestaña `Locales`, `d` sigue
  borrando la sesión de tu disco.

Sobre una fila compartida las acciones locales (renombrar, etiquetar, borrar, mover) están
ocultas: todavía no hay jsonl que tocar.

**Qué viaja y qué no.** Sube el `<uuid>.jsonl`, los `subagents/` (en una sesión con fan-out
son la mayor parte del trabajo) y los `tool-results/`. **No** sube el `memory/` del
proyecto — esa es tu auto-memoria personal — ni nada llamado `session-env`.

El código no viaja: tu compañero necesita el repo. El aviso de commit divergente es lo que
lo hace visible en vez de sorprendente.

Diseño completo y fases pendientes en [docs/REMOTE-SESSIONS.md](docs/REMOTE-SESSIONS.md).

## Cómo se lanza Claude

`launcher.launch_claude(cwd, session_id=None, *, mode="auto", claude_args=None)` decide **dónde**
aterriza la sesión. Cada modo degrada al siguiente cuando su destino no está disponible:

| Modo       | Cadena de despacho                                                        |
|------------|---------------------------------------------------------------------------|
| `auto`     | panel del multiplexer → pestaña → ventana nueva → suspender la TUI        |
| `split`    | panel del multiplexer → pestaña → ventana nueva → suspender               |
| `tab`      | pestaña en la ventana actual → ventana nueva → suspender                  |
| `window`   | ventana nueva del emulador → suspender                                    |
| `suspend`  | suspender la TUI siempre (`app.suspend()` + `subprocess.run`)             |

Cuando hay degradación, la TUI lo notifica con el motivo (`kitty` sin control remoto, emulador sin
pestañas por CLI, etc.) en vez de hacerlo en silencio.

**Multiplexers** (tienen prioridad porque anidan dentro del emulador):

| Entorno                | Panel (`split`)                              | Pestaña (`tab`)                    |
|------------------------|-----------------------------------------------|-------------------------------------|
| `$TMUX`                | `tmux split-window -h -c <cwd> claude ...`    | `tmux new-window -c <cwd> claude ...` |
| `$ZELLIJ`              | `zellij action new-pane --cwd <cwd> -- ...`   | panel (zellij no admite comando en pestaña) |
| `$TERMINATOR_UUID`     | `terminator --new-tab ...`                    | `terminator --new-tab ...`          |

**Emuladores** (detectados vía `$TERM_PROGRAM`, env vars y binario en PATH):

| Emulador          | Ventana nueva                                              | Pestaña en la ventana actual                          |
|-------------------|-------------------------------------------------------------|--------------------------------------------------------|
| kitty             | `kitty --directory <cwd> claude ...`                        | `kitty @ launch --type=tab --cwd <cwd> -- claude ...` ¹ |
| WezTerm           | `wezterm start --cwd <cwd> -- claude ...`                   | `wezterm cli spawn --cwd <cwd> -- claude ...`          |
| GNOME Terminal    | `gnome-terminal --window --working-directory=<cwd> -- ...`  | `gnome-terminal --tab --working-directory=<cwd> -- ...` |
| Konsole           | `konsole --workdir <cwd> -e claude ...`                     | `konsole --new-tab --workdir <cwd> -e claude ...`      |
| Terminator        | `terminator --working-directory=<cwd> -x claude ...`        | `terminator --new-tab ...`                             |
| Windows Terminal  | `wt.exe -w -1 new-tab -d <cwd> -- claude ...`               | `wt.exe -w 0 new-tab -d <cwd> -- claude ...`           |
| iTerm2 (macOS)    | `osascript` → `create window with default profile`          | `osascript` → `create tab with default profile`        |
| Ghostty           | `ghostty --working-directory=<cwd> -e claude ...`           | — (su CLI no expone `+new-tab`)                        |
| Alacritty         | `alacritty --working-directory <cwd> -e claude ...`         | — (no tiene pestañas)                                  |
| foot              | `foot --working-directory=<cwd> claude ...`                 | — (no tiene pestañas)                                  |
| Apple Terminal    | `osascript` → `do script "cd <cwd> && exec claude ..."`     | — (requeriría sintetizar ⌘T con System Events)         |
| x-terminal-emulator / xterm | `<term> -e sh -c "cd <cwd> && exec claude ..."`   | —                                                      |

¹ Requiere `allow_remote_control` en `kitty.conf`. Si falla, se abre ventana nueva y se avisa.

Detección del emulador (en orden):

1. `$TERM_PROGRAM` (canónico, lo publican Ghostty, WezTerm…).
2. Env var específica del emulador (`$KITTY_PID`, `$GHOSTTY_RESOURCES_DIR`, `$ALACRITTY_LOG`, `$WT_SESSION`, etc.).
3. Fallback genérico: `x-terminal-emulator` o `xterm` si están en PATH (POSIX).

Emuladores detectables pero no controlables desde la CLI (VS Code, Warp, Tabby, ConEmu) caen a
ejecución inline con el motivo indicado. Si no se detecta nada, la TUI se suspende como último recurso.

### Argumentos extra para `claude`

`claude_args` (configurable en Ajustes) se antepone a las flags que gestiona la TUI:

```
claude <tus flags> --resume <id> -n <nombre>
```

Sirve para `--dangerously-skip-permissions`, `--model`, `--effort`, `--add-dir`, `--ide`… Las flags
que multi-claude necesita controlar (`--resume`, `-c`, `-n`, `-p`, `--bg`, `--from-pr`) se rechazan
con un error en el modal en vez de colisionar con la sesión que se está reanudando.

## Ajustes (`s`)

El modal está dividido en pestañas: **Lanzamiento** (modo de Enter y argumentos de `claude`), **Sesiones compartidas** (remoto global) y **Colores** (reglas automáticas).

Modal en la TUI con:

- **Enter (predeterminado)** — dónde se abre la sesión (`auto`, `split`, `tab`, `window`, `suspend`).
  Debajo se dibuja un esquema del modo seleccionado y una línea *"Aquí y ahora: …"* que resuelve en
  seco lo que haría ese modo en tu terminal concreta.
- **Saltar permisos** — checkbox para `--dangerously-skip-permissions`.
- **Argumentos para `claude`** — resto de flags extra, en formato línea de comandos.

Solo se configura el **predeterminado**. El **alternativo** (Shift+Enter) se deriva automáticamente:

| Predeterminado | Alternativo (Shift+Enter) |
|----------------|---------------------------|
| `auto`         | `suspend`                 |
| `split`        | `window`                  |
| `tab`          | `window`                  |
| `window`       | `suspend`                 |
| `suspend`      | `window`                  |

Persistido en:
- **Linux/macOS**: `~/.config/multi-claude/config.json` (o `$XDG_CONFIG_HOME/multi-claude/config.json` si está definido).
- **Windows**: `%APPDATA%\multi-claude\config.json` (típicamente `C:\Users\<user>\AppData\Roaming\multi-claude\config.json`).

El fichero guarda, además del modo, el estado de la UI que se recuerda entre arranques:

```json
{
  "default_mode": "auto",
  "claude_args": ["--dangerously-skip-permissions"],
  "projects_sort": { "key": "last_activity", "descending": true },
  "sessions_sort": { "key": "last_activity", "descending": true },
  "preview_visible": true,
  "group_worktrees": true,
  "color_rules": []
}
```

Un `config.json` ausente, corrupto o con claves inválidas cae silenciosamente a estos valores por defecto — nunca es un error fatal.

> Nota sobre `Shift+Enter`: la mayoría de los emuladores modernos lo transmiten distinto a `Enter`, pero algunos antiguos no — en ese caso `Shift+Enter` simplemente hará lo mismo que `Enter`. Si te ocurre, cambia el predeterminado en Ajustes para que ambas teclas hagan lo que quieres.

## Ficheros de estado

Todo lo que multi-claude guarda por su cuenta (nunca escribe dentro de los jsonl de Claude). Las rutas respetan `$XDG_CONFIG_HOME` / `$XDG_DATA_HOME` si están definidas, con `%APPDATA%` en Windows para la config:

| Fichero                                          | Contenido                                            |
|--------------------------------------------------|------------------------------------------------------|
| `~/.config/multi-claude/config.json`             | preferencias (modo, orden, preview, agrupación, reglas de color) |
| `~/.config/multi-claude/names.json`              | nombres de sesión persistentes (`e`)                 |
| `~/.config/multi-claude/session-tags.json`       | etiquetas por sesión (`t`)                           |
| `~/.config/multi-claude/session-colors.json`     | colores manuales por sesión (`c`)                    |
| `~/.config/multi-claude/project-folders.json`    | árbol de carpetas y asignación de proyectos (`f`)    |
| `~/.local/share/multi-claude/index.sqlite3`      | índice SQLite + tabla FTS5 (caché reconstruible)     |

Borrar cualquiera de ellos es seguro: se pierde ese estado, no las sesiones.

## Identidad de un proyecto

El nombre de la carpeta `~/.claude/projects/<encoded>/` es la ruta original con `/` reemplazado por `-`. Esta codificación es ambigua si el path original contenía guiones (`/foo-bar/baz` y `/foo/bar/baz` colisionan).

**Fuente de verdad**: el campo `cwd` del primer evento `type=user` del primer `.jsonl` del proyecto. Solo si no hay ningún jsonl parseable se cae a la heurística `-` → `/`.

`os.path.isdir(cwd)` decide si el proyecto está vivo o huérfano.

## Limitaciones conocidas

- **La búsqueda global solo ve lo indexado**: el índice FTS se puebla en `scan_sessions`, es decir al **entrar** a la pantalla de sesiones de un proyecto. Un proyecto que nunca has abierto en la TUI no aparece en los resultados de `?`. Si `?` te devuelve menos de lo esperado, entra una vez en los proyectos que te falten.
- **Payload FTS acotado por sesión**: se indexan como máximo las primeras 2.000 líneas del jsonl y 64 KB de texto (`FTS_REINDEX_SCAN_LINES` / `FTS_CONTENT_MAX_CHARS` en `session.py`). En sesiones muy largas, el final de la conversación no es buscable.
- **Proyecto movido de path**: si renombras la carpeta de un proyecto, las sesiones viejas y nuevas siguen siendo dos entradas distintas en `~/.claude/projects/`. No se reconcilian solas — la vieja queda como huérfana y la unes a mano con `m` (merge).
- **No todos los emuladores saben abrir pestañas desde la CLI**: Ghostty (su CLI no expone `+new-tab`), Alacritty, foot y Terminal.app solo pueden abrir ventanas, así que en modo `tab` la sesión acaba en una ventana nueva y la TUI te lo dice. En kitty y WezTerm la pestaña exige tener el control remoto activado (`allow_remote_control` en `kitty.conf`); si está apagado, mismo fallback.
- **zellij no puede lanzar un comando en una pestaña nueva**: `zellij action new-tab` solo acepta un layout, no un comando, así que el modo `tab` dentro de zellij abre un panel.
- **Ordenar por tags no se persiste**: `3` ordena la tabla de sesiones por etiquetas en la sesión actual de la TUI, pero `tags` no está en `VALID_SESSION_SORT` (`config.py`), así que al reabrir vuelve al orden por última actividad.
- **Republicar una sesión compartida sobrescribe la versión del remoto**: si dos personas reanudan la misma sesión compartida y ambas publican, la segunda publicación pisa la primera en el remoto (cada una conserva la suya en local). El fork explícito que lo resuelve está planificado, no implementado — ver [docs/REMOTE-SESSIONS.md](docs/REMOTE-SESSIONS.md).
- **Publicar en GitLab/GitHub hace un commit por fichero**: una sesión con subagentes son varios commits en el repo de sesiones, no uno. El manifest siempre va el último, así que una publicación interrumpida queda invisible en vez de a medias, pero el historial del repo es más ruidoso de lo necesario.
- **Las sesiones compartidas no entran en la búsqueda global (`?`)** hasta que las traes: el índice FTS solo indexa lo que hay en disco.

## Instalación

### Requisitos previos

- **Linux** (Ubuntu/Debian/Fedora/Arch testados), **macOS** o **Windows 10/11**.
- **Python 3.10+** (la mayoría de distros modernas lo traen; en macOS `brew install python@3.13`; en Windows usa el instalador oficial o `winget install Python.Python.3.13`).
- **`claude`** (Claude Code CLI) en `PATH`. Sin él, `multi-claude` arranca pero no podrá reanudar sesiones — la propia TUI te lo dirá.
- *(Opcional, Linux/macOS)* **`tmux`** o **`zellij`** (o **`terminator`** sólo en Linux) para que Claude se abra en un panel sin perder la TUI. Sin multiplexer, la mayoría de emuladores abren pestaña en la misma ventana (ver [Cómo se lanza Claude](#cómo-se-lanza-claude)).
- *(Opcional)* Un emulador soportado:
  - **Linux**: kitty, WezTerm, Ghostty, Alacritty, Konsole, GNOME Terminal, foot, Terminator, xterm.
  - **macOS**: **iTerm2** (pestañas y ventanas) o **Terminal.app** (solo ventanas); ambos vía AppleScript con `osascript`, que viene de serie en macOS. kitty, WezTerm, Ghostty y Alacritty también funcionan si los usas.
  - **Windows**: **Windows Terminal** (`wt.exe`: pestaña de la ventana actual en modo `auto`/`tab`, ventana aparte en modo `window`).

  Sin nada de esto, la TUI se suspende y vuelve cuando cierras Claude.

### Paso 1 — Instalar un gestor de herramientas Python (si no tienes ninguno)

Cualquiera de los dos funciona; **uv** es el más rápido y el único que cubre las tres plataformas con el mismo binario.

**Linux / macOS:**

```bash
# uv (recomendado)
curl -LsSf https://astral.sh/uv/install.sh | sh

# o pipx
sudo apt install pipx && pipx ensurepath      # Debian/Ubuntu
brew install pipx && pipx ensurepath          # macOS
```

Cierra y abre la terminal para que `~/.local/bin` entre en `PATH`.

**Windows (PowerShell):**

```powershell
# uv (recomendado)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# o vía winget
winget install --id=astral-sh.uv -e
```

Cierra y abre PowerShell (o reinicia Windows Terminal) para que `%USERPROFILE%\.local\bin` entre en `PATH`.

### Paso 2 — Instalar multi-claude

Una sola línea, sin clonar nada — funciona idéntico en Linux, macOS y Windows:

```bash
uv tool install git+https://github.com/Zarritas/multi-claude.git
# o (Linux/macOS):
pipx install git+https://github.com/Zarritas/multi-claude.git
```

### Paso 3 — Lanzarlo

```bash
multi-claude
```

Deberías ver la lista de tus proyectos de Claude. Pulsa `Enter` para entrar en uno, `Enter` otra vez para reanudar una sesión.

> **macOS**: si es la primera vez que multi-claude lanza una sesión en una ventana nueva de iTerm2 / Terminal.app, macOS te pedirá permiso para que `osascript` controle esas apps (System Settings → Privacy & Security → Automation). Acepta una vez y queda persistido.
>
> **Windows**: en modo `auto` o `tab` las sesiones se abren en una pestaña de la ventana actual de Windows Terminal (`wt.exe -w 0`); en modo `window`, en una ventana aparte (`wt.exe -w -1`). Si no estás en Windows Terminal (p.ej. `cmd.exe` o ConEmu), la TUI se suspende y `claude` corre inline.

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
uv tool install .                       # snapshot del estado actual
# o, para que los cambios futuros del repo se reflejen sin reinstalar:
uv tool install --editable .
```

### Troubleshooting

- **`multi-claude: command not found`** tras instalar (Linux/macOS) → `~/.local/bin` no está en tu `PATH`.
  - `uv` y `pipx` añaden automáticamente esa ruta a la config de tu shell, pero hace falta reiniciar la terminal. Si persiste, ejecuta `uv tool dir --bin` o `pipx environment --value PIPX_BIN_DIR` y añade esa ruta a tu `PATH`.
- **`multi-claude` no se reconoce como comando** (Windows) → reinicia Windows Terminal/PowerShell tras instalar. Si persiste, comprueba que `%USERPROFILE%\.local\bin` (o el directorio que muestre `uv tool dir --bin`) está en tu `PATH` de usuario.
- **`claude no encontrado en PATH`** al pulsar Enter sobre una sesión → instala Claude Code CLI siguiendo su guía oficial.
- **macOS pide permiso de Automation** la primera vez que lanzas una sesión → es el prompt nativo de `osascript` para controlar iTerm2 / Terminal.app. Acepta y no volverá a aparecer.
- **Proyectos en gris (huérfanos)** → la carpeta original del proyecto ya no existe (moviste o borraste el directorio). Las sesiones siguen ahí pero no se pueden reanudar; bórralas con `d`.

## Desarrollo

```bash
git clone https://github.com/Zarritas/multi-claude.git
cd multi-claude
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

multi-claude        # arranca la TUI
pytest              # corre la suite (282 tests)
```

## Estructura del código

```
src/multi_claude/
  __main__.py        # entrypoint: arranca ClaudeBrowserApp
  app.py             # ClaudeBrowserApp(textual.App) — registra screens y stores
  app_protocol.py    # Protocol que las screens usan para hablar con la app
  discovery.py       # scan_projects() → list[Project], WorktreeGroup, ProjectFolder
  session.py         # scan_sessions(project) → list[Session], parsers, payload FTS
  index.py           # SessionIndex — SQLite + tabla FTS5 (caché reconstruible)
  launcher.py        # launch_claude(): panel/pestaña/ventana/inline según emulador y multiplexer
  focus.py           # traer al frente la terminal de una sesión ya viva
  deletion.py        # borrado de sesiones/proyectos y sus artefactos en disco
  transfer.py        # export/import de sesiones en .zip
  filtering.py       # parseo de las queries de `/` + matching fuzzy
  config.py          # Config persistida en config.json
  names.py           # NamesStore — nombres de sesión
  project_names.py   # alias de proyectos
  project_folders.py # árbol de carpetas de usuario
  tags.py            # TagsStore — etiquetas por sesión
  colors.py          # colores manuales + ColorRule
  formatting.py      # formateo de tiempos/tamaños para las tablas
  path_complete.py   # autocompletado de paths en los modales
  clipboard.py       # copiar al portapapeles (`y`)
  modals.py          # modales: ajustes, rename, tags, colores, import/export…
  screens/
    projects.py      # ProjectsScreen — DataTable, bindings
    sessions.py      # SessionsScreen — DataTable, bindings, preview
    worktrees.py     # WorktreesScreen — miembros de un grupo de worktrees
    folder.py        # FolderScreen — subcarpetas + proyectos de una carpeta
    search.py        # SearchScreen — búsqueda FTS5 global
  widgets/
    preview.py       # SessionPreview — últimos turnos del jsonl
  styles.tcss        # estilos Textual
```
