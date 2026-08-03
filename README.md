# multi-claude

El archivo compartido de las sesiones de Claude Code de un equipo: navega los cientos de conversaciones acumuladas de todos tus proyectos —y las de tus compañeros— y reanuda cualquiera desde un punto central.

![Recorrido por la TUI: la lista de proyectos, las sesiones de uno de ellos con su estado en vivo, la pestaña del repositorio que comparte el equipo filtrada por autor, y la búsqueda global encontrando a la vez sesiones propias y de un compañero](docs/img/demo.gif)

## Qué resuelve

Claude Code guarda cada sesión como un `.jsonl` bajo `~/.claude/projects/<encoded-path>/`. Cuando acumulas decenas de proyectos y cientos de sesiones, encontrar "aquella conversación de hace tres semanas sobre el refactor X" se vuelve incómodo: `claude --resume` te muestra solo las del cwd actual, y saltar entre proyectos implica `cd`s y memorizar UUIDs.

Y hay una segunda mitad del problema: esas conversaciones son **de una persona y de una máquina**. El compañero que ya peleó con ese despliegue tiene la sesión en su disco, y lo único que puedes hacer es preguntarle por Slack y que te la resuma.

`multi-claude` es un dashboard en terminal para las dos cosas. Lista todos tus proyectos con sus sesiones y te deja organizarlas como un archivo que se consulta meses después —carpetas, etiquetas, colores, worktrees agrupados, búsqueda por lo que se dijo dentro—, enlaza cada proyecto a uno o varios repositorios de sesiones que el equipo comparte, y al pulsar Enter lanza `claude --resume <id>` en un panel/pestaña nueva del multiplexer o emulador de terminal: sea tu sesión o la de otra persona.

### Frente al `agent view` de Claude Code

Claude Code trae desde la 2.1.139 su propio `claude agents`: un panel de las sesiones **en marcha**, agrupadas por estado, con un `/resume` para las históricas del repo (2.1.212+). Para saber qué está pasando ahora mismo en esta máquina, eso es mejor que cualquier herramienta de terceros — es quien produce el dato.

multi-claude no compite ahí: lee el mismo registro local de sesiones vivas que alimenta a agent view (ver [Estado en vivo](#estado-en-vivo)) y muestra su estado en la tabla, para no obligarte a mirar en dos sitios. Lo que añade encima es lo que agent view no hace:

- **el histórico como archivo organizable** — carpetas propias, etiquetas, colores por reglas, nombres persistentes, worktrees agrupados por repo, mover sesiones de un worktree a otro;
- **búsqueda full-text por el contenido** de las conversaciones, no por su nombre;
- **y el equipo** — publicar una sesión en un repositorio común y reanudar la de otra persona conservando su uuid.

## Qué trae

- **Sesiones compartidas** (`L`, `u`): enlaza cada proyecto a uno o varios repositorios de sesiones (GitLab, GitHub o una carpeta), que aparecen como pestañas; publica con `u` y reanuda la sesión de un compañero con `Enter`, sin exportar ni importar nada (ver [Sesiones compartidas](#sesiones-compartidas-l-y-u)).
- **Escáner de secretos** antes de publicar: revisa lo que va a subir y, si encuentra algo con pinta de credencial, el diálogo cambia de forma para que publicarlo sea un acto deliberado. Las sesiones sospechosas van marcadas con `⚠` en el listado, y `multi-claude --audit-secrets` revisa el histórico completo (ver [Escáner de secretos](#escáner-de-secretos-al-publicar)).
- **Búsqueda full-text** (`?`) sobre el contenido de todas las sesiones, con índice FTS5 de SQLite — encuentra "aquella conversación sobre el refactor X" por lo que se dijo dentro, y en la misma lista **las del equipo** que ya has visto publicadas, marcadas con quién las publicó (ver [Búsqueda global](#búsqueda-global-full-text-)).
- **Servidor MCP** (`multi-claude-mcp`) sobre ese mismo índice: Claude busca en sus propias sesiones pasadas en vez de volver a deducir lo que ya resolvió (ver [Servidor MCP](#servidor-mcp-multi-claude-mcp)).
- **Worktrees agrupados** por defecto: los worktrees de un mismo repo colapsan en una fila, con pantalla propia para entrar a cada uno.
- **Carpetas de usuario** (`f`) para organizar proyectos en un árbol propio.
- **Etiquetas** (`t`) y **colores** por sesión (`c`), con reglas automáticas por branch, antigüedad o actividad (`C`).
- **Nombres persistentes** (`e`) para sesiones y proyectos, con el `/rename` de Claude y el título que Claude genera solo como fallbacks (ver [Nombres](#nombres-e)).
- **Filtro incremental** (`/`) con `branch:`, `path:`, `id:`, `tag:`, `author:` y texto libre fuzzy.
- **Preview** (`p`) de los últimos turnos de una sesión sin reanudarla.
- **Mover, exportar e importar** sesiones entre worktrees o hacia un `.zip` compartible (`m`, `x`, `i`).
- **Estado en vivo** de cada sesión, leído del registro de Claude Code: con varias corriendo a la vez, ves en la misma tabla cuál trabaja y cuál te está esperando (ver [Estado en vivo](#estado-en-vivo)).
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

![Pantalla de proyectos: una fila por proyecto con su path real, número de sesiones y última actividad](docs/img/01-proyectos.png)

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

![Listado de sesiones de un proyecto: nombre generado por Claude, estado en vivo de cada sesión (trabajando / te espera), branch, etiquetas y una pestaña con el repositorio de sesiones del equipo](docs/img/02-sesiones.png)

| Columna           | Origen                                                                                |
|-------------------|---------------------------------------------------------------------------------------|
| Prompt            | nombre de la sesión si tiene, y si no el primer `type=user` con `role=user`, limpiando wrappers `<command-message>` / args |
| Estado            | qué está haciendo ahora mismo, según `~/.claude/sessions/` (ver [Estado en vivo](#estado-en-vivo)) |
| Branch            | `gitBranch` del primer evento con cwd                                                 |
| Tags              | etiquetas asignadas a mano (ver [Etiquetas](#etiquetas-t))                             |
| Msgs              | nº de líneas del jsonl                                                                |
| Tamaño            | size en KB del jsonl                                                                  |
| Última            | mtime del jsonl                                                                       |

- Orden por defecto: última actividad descendente.
- El **nombre** que sustituye al primer prompt en la columna **Prompt** sale del primero que haya de estos tres, por orden: el que pusiste con `e`, el `/rename` de Claude, o el título que Claude genera por su cuenta (ver [Nombres](#nombres-e)).

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
- `L` — gestionar los **repositorios de sesiones** enlazados a este proyecto (añadir, editar, quitar). Cada uno aparece como pestaña del listado.
- `Ctrl+→` / `Ctrl+←` — **cambiar de pestaña** entre el listado local y cada repositorio enlazado, dando la vuelta al llegar al final. Sin repositorios enlazados no hacen nada (la barra de pestañas está oculta).
- `d` — borrar la(s) sesión(es) seleccionada(s) y todos sus artefactos en disco. Sobre una fila **compartida** no borra nada tuyo: la **despublica** del repositorio.
- `D` — **limpieza** por antigüedad: eliges un umbral y borra de golpe las sesiones más viejas (las sesiones vivas quedan protegidas).
- `/` — filtrar la lista (ver [Filtro](#filtro-)).
- `1`…`7` — ordenar por prompt / estado / branch / tags / msgs / tamaño / última actividad.
- `Shift+S` — invertir la dirección del orden.
- `s` — abrir el modal de **Ajustes**: modo de lanzamiento (con vista previa) y flags extra para `claude`.
- `Esc` / `←` — limpiar el filtro, o volver a la pantalla de proyectos.
- `r` — re-escanear las sesiones del proyecto.
- `Ctrl+Q` — salir.

### Estado en vivo

La columna **Estado** dice qué está haciendo cada sesión ahora mismo, con **dos fuentes** y dos cadencias, porque tienen costes muy distintos:

| Fuente | Cada | Qué aporta |
|--------|------|------------|
| el registro por PID (`~/.claude/sessions/<pid>.json`) | **2 s** | las sesiones interactivas, con la latencia buena. Son unos pocos ficheros json pequeños |
| `claude agents --json` | **15 s** | la vía **soportada**: añade las sesiones de *background* (las que despachas desde `agent view`, que no están en el registro por PID) y trae los estados con vocabulario documentado |

Lo segundo va en un tick lento a propósito: el comando arranca un proceso node y tarda **~350 ms** (medido, cinco ejecuciones en caliente). A la cadencia de dos segundos serían 350 ms de subproceso cada dos segundos para siempre; lo que aporta vale unos segundos de retardo, no eso. Cuando las dos fuentes conocen una sesión, el `pid` lo da el registro (es lo que hace falta para traer una terminal al frente) y el estado lo da `claude agents` (su vocabulario sí está documentado).

| Celda           | Significado                                                     |
|-----------------|-----------------------------------------------------------------|
| `○ te espera`   | parada esperando que le contestes (`waiting`, `needs input`)     |
| `● trabajando`  | ocupada (`busy`, `working`)                                     |
| `· libre`       | viva y lista para el siguiente prompt (`idle`)                   |
| `✓ terminada`   | la tarea acabó bien (`completed`)                                |
| `✗ falló`       | acabó con error (`failed`)                                       |
| `■ detenida`    | la paraste a mano (`stopped`)                                    |
| `● abierta`     | viva, con un estado que multi-claude no conoce                   |
| `—`             | no está corriendo                                                |

La columna no pretende sustituir al [`agent view`](#frente-al-agent-view-de-claude-code) de Claude Code, que para eso es más completo: está aquí para que, mientras buscas en el archivo, no tengas que abrir otra vista para saber si la sesión sobre la que estás ya la tienes corriendo. `2` ordena por estado y pone arriba lo que te espera.

Dos cosas que conviene saber:

- **El vocabulario puede crecer, y un valor nuevo no se interpreta.** Los estados de `claude agents` están documentados; los del registro por PID (`busy`, `waiting`) no. Cualquier otro valor se muestra como `● abierta` en lugar de inventarle un significado.
- **Solo ve sesiones de esta máquina**: ambas fuentes son locales, así que las filas de las pestañas de [sesiones compartidas](#sesiones-compartidas-l-y-u) siempre muestran `—`. Si `claude` no está en el PATH, o es una versión sin `agents`, la columna sigue funcionando con el registro por PID: se pierden las de background, no la columna.

Un registro que sobrevive a su proceso (una terminal que murió mal) no cuenta como vivo: la entrada solo vale si su PID sigue existiendo y —en Linux— si el `procStart` anotado coincide con `/proc/<pid>/stat`, para que un PID reutilizado no se haga pasar por la sesión.

### Nombres (`e`)

En la columna **Prompt**, el primer prompt es el último recurso. Si la sesión tiene nombre, se muestra el nombre, y hay tres fuentes con esta precedencia:

1. **El tuyo** (`e`), guardado en `names.json`. Lo que eliges a mano manda siempre.
2. **El `/rename` de Claude**, que queda escrito dentro del jsonl. Si hay varios, gana el último.
3. **El título que Claude genera solo** (eventos `ai-title` del jsonl), que se actualiza a medida que avanza la conversación. Gana el último.

Es decir, que la mayoría de sesiones llegan con un título legible sin que hagas nada; `e` solo hace falta cuando no te gusta el que hay.

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

`?` desde la pantalla de proyectos abre una pantalla de búsqueda que consulta una tabla **FTS5 de SQLite** construida sobre la concatenación de los prompts del usuario y el texto del asistente de cada sesión. Escribes y los resultados se refrescan en un worker en background (hasta 200 filas por origen), con columnas Sesión / Dónde / Proyecto / Branch / Última.

![Búsqueda global: la query «nginx» devuelve dos sesiones propias marcadas «local» y una publicada por Ana marcada con el icono de nube, y el subtítulo cuenta cuántas hay de cada origen](docs/img/05-busqueda.png)

En la misma lista aparecen **dos orígenes**, y la columna `Dónde` los distingue porque no se buscan igual:

| `Dónde`     | Qué es                          | Sobre qué busca                                                        |
|-------------|---------------------------------|------------------------------------------------------------------------|
| `local`     | tus sesiones en disco            | el **contenido** de la conversación                                     |
| `☁ ana`     | una sesión publicada por Ana     | solo los **metadatos** de su manifest: nombre, primer prompt, tags, branch y autor |

La asimetría es del formato, no una limitación provisional: un manifest no lleva la transcripción —esa se queda en la máquina de quien la grabó—, así que de una sesión ajena se puede encontrar *de qué va*, no una frase dicha en el turno 400. Ver [Sesiones compartidas](#sesiones-compartidas-l-y-u).

`Enter` sobre un resultado tuyo te lleva a la pantalla de sesiones del proyecto que lo contiene. Sobre uno del equipo, además **abre la pestaña del repositorio que lo tiene**, con la fila ya en pantalla: desde ahí `Enter` otra vez lo descarga y lo reanuda.

Los resultados se ordenan por relevancia dentro de cada origen, los tuyos primero: los `rank` de dos tablas FTS distintas no son comparables, así que se concatenan en vez de entremezclarse fingiendo un orden común.

**Nada de esto toca la red.** Las filas del equipo son las que dejó cacheadas la última visita a la pestaña de cada repositorio: la pantalla de búsqueda no habla con ningún remoto. Por eso una sesión publicada por un compañero *después* de tu última visita a esa pestaña no aparece hasta que la abras de nuevo.

El tokenizer es `unicode61 remove_diacritics 2`, así que `refactor` encuentra `refactorización` y los acentos son indiferentes. El índice es una **caché, no la fuente de verdad**: vive en `$XDG_DATA_HOME/multi-claude/index.sqlite3` (por defecto `~/.local/share/...`) y si se corrompe se reconstruye en el siguiente escaneo. Re-listar un remoto **reemplaza** sus filas en lugar de acumularlas, de modo que lo que alguien despublica deja de ser un resultado.

### Preview (`p`)

`p` en la pantalla de sesiones abre un panel lateral de solo lectura que renderiza los **últimos turnos** de la sesión bajo el cursor (hasta 12 turnos, leyendo las últimas 60 líneas del jsonl, con el texto recortado a 800 caracteres por mensaje). Sirve para reconocer una conversación sin reanudarla. La visibilidad se persiste en `preview_visible`.

![El panel de preview junto a la tabla, mostrando los últimos turnos de usuario y de Claude de la sesión seleccionada](docs/img/03-preview.png)

### Filtro (`/`)

`/` abre un input de filtrado incremental sobre la tabla actual. La sintaxis admite restricciones `clave:valor` mezcladas con texto libre:

| Clave     | Efecto                                                              |
|-----------|---------------------------------------------------------------------|
| `branch:` | subcadena sobre la branch                                           |
| `path:`   | subcadena sobre el path del proyecto                                |
| `id:`     | subcadena sobre el id de la sesión                                  |
| `tag:`    | lista separada por comas; **todas** las etiquetas deben coincidir   |
| `author:` | subcadena sobre quién publicó la sesión (`author:ana`, o el correo completo) |

Todo lo que no sea `clave:valor` se trata como texto libre y se puntúa con `rapidfuzz.fuzz.partial_ratio` (umbral 70), así que tolera erratas. Ejemplo: `branch:main tag:bug,urgente refacto`.

`author:` responde en las dos pestañas, pero cada una a una pregunta distinta:

- en la pestaña de un **repositorio compartido**, cada fila tiene publicador, así que `author:ana` es "de lo que hay publicado aquí, lo de Ana";
- en la pestaña **local**, es "de las sesiones que tengo en disco, cuáles vinieron de otra persona" — el caso de una que hidrataste de un compañero. El autor sale del índice de publicadas, que se carga en segundo plano, así que igual que la marca `✓` tarda un instante en aparecer.

> Ojo: `/` filtra las filas que ya están en pantalla. Para buscar **dentro del contenido** de las conversaciones, usa `?` — donde el autor también funciona como texto libre, porque el nombre de quien publicó entra en el índice de las sesiones del equipo.

Una clave que la tabla en pantalla no puede responder **no deja pasar nada**, en vez de ignorarse: `author:`, `tag:`, `id:` y `branch:` son propiedades de una sesión, así que en la lista de **proyectos** filtran a cero. Devolver todos los proyectos se leería como "ninguno tiene ese autor" cuando lo que ocurre es que la pregunta no aplica a ese nivel.

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

Publicar una sesión en un repositorio común y que un compañero la reanude con `Enter`, sin el
viaje de ida y vuelta de `x` (exportar) → enviar el zip → `i` (importar). La sesión conserva su
uuid, así que es literalmente la misma conversación, no una copia.

Es la razón principal por la que este proyecto existe: el trabajo con Claude deja un rastro que
hoy muere en el disco de quien lo hizo. Un repositorio de sesiones convierte ese rastro en algo
que el equipo consulta —quién ya se peleó con este despliegue, cómo se resolvió aquel bug— con las
mismas herramientas y los mismos permisos con los que ya comparte el código.

![La pestaña de un repositorio de sesiones compartido, con tres sesiones publicadas por Ana y Carlos, cada una precedida de quién la publicó](docs/img/04-equipo.png)

Está **desactivado por defecto**: hay que configurarlo.

#### Puesta en marcha en un equipo

1. **Un repositorio vacío para las sesiones**, en vuestro GitLab o GitHub. Privado, y con acceso
   solo para quien deba leer esas conversaciones — sus permisos *son* los permisos de las
   sesiones. Uno por cliente o por área es lo razonable; no hace falta inicializarlo.

2. **Cada persona configura el servidor una vez**: `s` → pestaña «Sesiones compartidas» →
   «Servidores…» → «Añadir». Nombre libre, proveedor, URL, y token o SSH (ver abajo).
   **`Ctrl+T` comprueba el acceso antes de guardar**; no sigas sin un OK.

3. **Cada persona enlaza el proyecto al repositorio**: abre el proyecto, `L` → «Añadir», elige el
   servidor por nombre e indica `grupo/repo-de-sesiones` y la rama. Aparece una pestaña nueva con
   el nombre del repositorio.

4. **Publicar**: sitúate en una sesión y pulsa `u`. El diálogo lista los ficheros que van a
   subir y avisa si encuentra algo con pinta de credencial (ver
   [Escáner de secretos](#escáner-de-secretos-al-publicar)). Revísalo y confirma. En la
   pestaña `Locales` la sesión queda marcada con `✓`.

5. **Traer la de otro**: en la pestaña del repositorio aparecen las sesiones de los demás con `☁`.
   `Enter` la descarga y la reanuda como si fuera tuya.

El enlace se guarda contra el **`origin` del repo de trabajo**, no contra la ruta, así que cada
persona lo configura en su máquina pero *acierta el mismo destino* aunque tenga el proyecto en
otra carpeta. Y todos los **worktrees** de un repo comparten enlace: enlazas uno y quedan
enlazados todos.

#### Servidores y autenticación

Un servidor se define una vez (nombre, proveedor, URL, autenticación) y luego se elige **por
nombre** al enlazar cada repositorio: solo hay que indicar repo y rama. Corregir una URL o rotar
un token arregla de golpe todos los repositorios que apuntan a ese servidor.

| Autenticación | Qué necesita | Qué implica |
|---------------|--------------|-------------|
| **SSH** *(recomendada)* | nada nuevo | Usa las claves que ya tenéis. Sin tokens que crear ni repartir, y **git resuelve las publicaciones simultáneas**: si dos personas publican a la vez, ambas sesiones sobreviven |
| **Token de acceso** | un token por persona y por servidor | Vía API REST. Más simple de arrancar, pero si dos publican la misma sesión a la vez, la segunda pisa a la primera |

> **El usuario SSH es siempre `git`**, no tu usuario de GitHub/GitLab. En
> `git@github.com:Zarritas/multi-claude.git`, `Zarritas` es parte del *repositorio*. Cámbialo solo
> en instalaciones self-hosted que usen otro.
>
> **Si tu servidor usa un puerto SSH distinto del 22, ponlo.** Míralo en la URL SSH de cualquier
> repo suyo: en `ssh://git@git.tuempresa.com:2211/grupo/repo.git` el puerto es `2211`. Es
> frecuente en GitLab self-hosted, y no se puede deducir de la URL web — esa contesta por 443
> igualmente. Si el puerto está mal, la prueba de conexión no recibe *nada*: por eso el aviso te
> sugiere revisarlo.
>
> `Ctrl+T` sobre un servidor SSH ejecuta `ssh -T` y te dice como quién te autentica
> (`autenticado en git.tuempresa.com:2211 como jesus.lorenzo`), sin necesitar ningún repositorio.

También se puede publicar a una **carpeta compartida** (montaje de red, Syncthing) en lugar de un
repositorio: ahí los permisos son los del sistema de ficheros, y no hay control de acceso por
cliente ni autoría. Va bien para probar; para un equipo, un repositorio privado es mejor.

Los **tokens nunca se guardan en `config.json`** (ese fichero se comparte y se pega en issues):
van a `remote-tokens.json`, con permisos `0600` y uno por servidor.
`$MULTI_CLAUDE_REMOTE_TOKEN` los sobreescribe, para que CI no tenga que escribir un secreto en
disco. Con SSH no hay token que guardar.

Con SSH se mantiene una copia de trabajo del repo en `~/.cache/multi-claude/repos/`. Es caché
reconstruible: se puede borrar sin perder nada.

#### Remoto global

En Ajustes se puede configurar además un remoto **global**, que sirve de respaldo para los
proyectos sin enlaces propios. Los enlaces del proyecto ganan por completo: un proyecto enlazado
al repositorio de un cliente **no** publica además al global. Se desactiva eligiendo
«Desactivado» en su diálogo.

#### Pestañas y marcas

`Locales` muestra tus sesiones; cada `☁ nombre` es una vista del repositorio: **todo lo publicado
en él**, con su autor y el estado de tu copia. La barra se oculta si no hay nada enlazado.

En la pestaña de un repositorio:

| Marca | Significado |
|-------|-------------|
| `☁` | Publicada, no la tienes en local. `Enter` la trae y la reanuda |
| `✓` | Descargada y al día con lo publicado |
| `↻` | Descargada, pero alguien la continuó después: **hay versión más reciente** |
| `↑` | Descargada y la has continuado tú: **tienes turnos sin publicar** |

En `Locales`, el mismo vocabulario visto desde el otro lado:

| Marca | Significado |
|-------|-------------|
| (sin marca) | Solo tuya, no está en ningún repositorio |
| `✓` | Publicada y al día. Si la subió otra persona, se indica: `· de ana` |
| `↻` | El repositorio tiene una versión más reciente que tu copia |
| `↑` | Tienes turnos que no has publicado |

El estado se calcula comparando el tamaño de tu `.jsonl` con el que registra el manifest: como el
transcript solo crece, cualquier diferencia es contenido real. Se consulta a los repositorios en
segundo plano, así que la lista aparece al instante y las marcas se pintan al llegar; si un
repositorio no responde, te quedas sin esa marca, no sin listado.

#### Teclas

- `L` — gestiona los repositorios enlazados a este proyecto (añadir, editar, quitar). Está en la
  pantalla de proyectos y dentro del proyecto.
- `u` — publica la fila actual, o todas las marcadas con `Espacio`. El diálogo pide confirmación
  y, si hay varios repositorios enlazados, **te deja elegir a cuál** (parte del de la pestaña en
  la que estés). Muestra **la lista exacta de ficheros** que salen de la máquina.
- `Enter` sobre una compartida — si no la tienes (`☁`), la descarga preservando su uuid y la
  reanuda; si ya la tienes, reanuda tu copia local. Avisa antes de lanzar si se grabó sobre otro
  commit, o si tu copia está por detrás de la publicada.
- `d` sobre una compartida — **despublicarla**: la quita del repositorio para todos. Tu copia
  local no se toca, y el diálogo lo dice. En `Locales`, `d` sigue borrando la sesión de tu disco.

Sobre una fila compartida las acciones locales (renombrar, etiquetar, mover) están ocultas:
todavía no hay jsonl que tocar.

#### Qué viaja y qué no

Sube el `<uuid>.jsonl`, los `subagents/` (en una sesión con fan-out son la mayor parte del
trabajo) y los `tool-results/`. **No** sube el `memory/` del proyecto —esa es tu auto-memoria
personal— ni nada llamado `session-env`.

#### Antes de usarlo en serio con un equipo

- **Revisa la lista de ficheros al publicar.** El transcript arrastra los `tool-results/`, así que
  una sesión que en su día imprimió un `.env` o un log con credenciales **lo publicaría**. No hay
  escáner de secretos todavía: el diálogo te muestra qué sube, y esa revisión es manual.
- **El código no viaja.** Tu compañero necesita el repositorio de trabajo, y si está en otro
  commit la conversación describe ficheros que ya no son esos. Al reanudar se avisa de la
  divergencia, que es lo que lo hace visible en vez de sorprendente.
- **Con token, republicar pisa.** Si dos personas continúan la misma sesión y ambas publican, con
  API la segunda sobrescribe a la primera en el remoto (cada una conserva la suya en local). Con
  **SSH no**: git rechaza el push y se reintenta encima. Es la razón principal para preferir SSH
  en un equipo.
- **Una sesión traída y luego continuada por el otro no se puede actualizar.** Se ve el aviso
  `↻`, pero traer los turnos nuevos exige un merge que aún no está implementado.

Diseño completo y fases pendientes en [docs/REMOTE-SESSIONS.md](docs/REMOTE-SESSIONS.md).

### Escáner de secretos al publicar

Un transcript arrastra todo lo que la conversación tocó: el `Bash` que imprimió un `.env`, el `cat` de una clave privada, el token que pegaste en un prompt. Publicar eso en un repositorio que lee todo el equipo es el fallo con más probabilidad de que la feature acabe prohibida en una organización, así que antes de abrir el diálogo se revisa lo que va a subir.

Se reconocen claves privadas PEM, tokens con prefijo de proveedor (GitHub, GitLab, Anthropic, OpenAI, Slack, Google, Stripe, AWS), JWT, credenciales dentro de una URL, cabeceras `Authorization`, y asignaciones cuyo **nombre** suena a credencial *y* cuyo **valor** parece serlo.

Cuando hay hallazgos, el diálogo no solo lo dice: **cambia de forma**.

- El aviso pasa a rojo y encabeza el diálogo, con la lista de hallazgos: fichero, línea, qué regla y un fragmento **recortado**.
- El foco arranca en **Cancelar**, y el botón de publicar dice «Publicar de todas formas».
- **`Enter` deja de publicar.** El fallo del que esto protege es pulsar Enter en automático, así que con hallazgos en pantalla Enter pulsa el botón enfocado (Cancelar) y hay que ir al otro a propósito.

Tres decisiones que conviene conocer:

- **Nunca se imprime el valor encontrado.** Un escáner que escribe el secreto en un diálogo —y de ahí a una captura, a un scrollback o a un informe de error— lo ha filtrado por segunda vez. Solo salen los primeros caracteres, los dos últimos y la longitud.
- **Un hallazgo avisa, no veta.** Sobre texto libre de conversación los falsos positivos son inevitables, y un escáner que impide publicar enseña a la gente a rodearlo. La fricción es deliberada; la decisión sigue siendo de la persona.
- **Un mismo valor repetido es un hallazgo, no cien.** Una clave que imprimió un comando ejecutado setenta veces se lista una vez, con el número de apariciones — si no, entierra todo lo demás.

#### Revisar todo el histórico, no solo lo que publicas

Que una clave no llegue al repositorio del equipo es la mitad del problema. La otra es que **ya está en claro en tu disco**, se publique o no, y eso se arregla rotándola:

```bash
multi-claude --audit-secrets              # todo el histórico
multi-claude --audit-secrets --project ~/work/api
multi-claude --audit-secrets --verbose    # con un fragmento recortado de cada hallazgo
```

Imprime, por sesión afectada, su id, su título, su proyecto y qué reglas saltaron y dónde. **Sale con código 1 si encuentra algo**, así que sirve en un hook o en un `cron`. Sin `--verbose` no muestra ni los fragmentos enmascarados, para que la salida se pueda pegar en un ticket; el **título también va redactado**, porque el título es el primer prompt y ahí es donde acaba un token pegado.

De paso deja el resultado en el índice, que es lo que alimenta la marca del listado:

#### La marca `⚠` en el listado

Una sesión con posibles credenciales lleva `⚠` delante del nombre en la pantalla de sesiones, antes de la marca de compartida: la pregunta que responde —¿esto debería salir de la máquina?— viene antes que «¿ya ha salido?».

El escaneo va en segundo plano y se cachea en el índice contra el `mtime` del jsonl, así que la primera visita a un proyecto grande lo calcula y las siguientes son gratis. Una sesión que ha crecido desde su escaneo se vuelve a mirar, porque la credencial puede estar en la parte nueva. Y **una sesión sin escanear no lleva marca ni deja de llevarla**: la ausencia de `⚠` significa «escaneada y limpia» solo después de que el escaneo haya corrido.

Las reglas están calibradas contra 60 MB de transcripts reales: la primera versión daba 714 avisos (nombres como `input_tokens`, `tokenize` o los `\tPassword:` de un `grep -n`), y esa versión no sirve de nada porque se ignora. Exigiendo que el nombre no lleve sufijo de letras y que el valor tenga aspecto de credencial, el mismo material da 10 hallazgos únicos en 7 de 33 sesiones. Si tocas las reglas, vuelve a medir: `tests/test_secret_scan.py` cubre tanto lo que debe detectar como lo que **no** debe.


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
| `$TERMINATOR_UUID`     | `remotinator vsplit -x "cd <cwd> && exec claude ..."` ¹ | `terminator --new-tab ...` |

¹ `remotinator` viene con Terminator y habla con su API DBus: `vsplit` parte el terminal de
`$TERMINATOR_UUID` en dos columnas, como `tmux split-window -h`. Solo puede heredar el directorio
del terminal que parte, así que el comando lleva su propio `cd`. Si `remotinator` no está en el PATH
o el DBus de Terminator está apagado (`terminator -u`), la sesión cae a pestaña y la TUI lo avisa.

**Emuladores** (detectados vía `$TERM_PROGRAM`, env vars y binario en PATH):

| Emulador          | Ventana nueva                                              | Pestaña en la ventana actual                          |
|-------------------|-------------------------------------------------------------|--------------------------------------------------------|
| kitty             | `kitty --directory <cwd> claude ...`                        | `kitty @ launch --type=tab --cwd <cwd> -- claude ...` ² |
| WezTerm           | `wezterm start --cwd <cwd> -- claude ...`                   | `wezterm cli spawn --cwd <cwd> -- claude ...`          |
| GNOME Terminal    | `gnome-terminal --window --working-directory=<cwd> -- ...`  | `gnome-terminal --tab --working-directory=<cwd> -- ...` |
| Konsole           | `konsole --workdir <cwd> -e claude ...`                     | `konsole --new-tab --workdir <cwd> -e claude ...`      |
| Terminator        | `terminator --working-directory=<cwd> -x claude ...`        | `terminator --new-tab ...`                             |
| Windows Terminal  | `wt.exe -w -1 new-tab -d <cwd> -- claude ...`               | `wt.exe -w 0 new-tab -d <cwd> -- claude ...`           |
| iTerm2 (macOS)    | `osascript` → `create window with default profile`          | `osascript` → `create tab with default profile`        |
| Ghostty           | `ghostty +new-window --working-directory=<cwd> -e claude ...` ³ | — (no tiene IPC de pestañas; ver ⁴)                |
| Alacritty         | `alacritty --working-directory <cwd> -e claude ...`         | — (no tiene pestañas)                                  |
| foot              | `foot --working-directory=<cwd> claude ...`                 | — (no tiene pestañas)                                  |
| Apple Terminal    | `osascript` → `do script "cd <cwd> && exec claude ..."`     | — (requeriría sintetizar ⌘T con System Events)         |
| x-terminal-emulator / xterm | `<term> -e sh -c "cd <cwd> && exec claude ..."`   | —                                                      |

² Requiere `allow_remote_control` en `kitty.conf`. Si falla, se abre ventana nueva y se avisa.

³ `+new-window` le pide la ventana a la instancia que ya está corriendo (D-Bus, solo GTK/Linux) en
vez de arrancar un segundo proceso. Sale con código ≠ 0 si no la alcanza —o si tu Ghostty es
anterior a la acción—, y entonces se cae a `ghostty --working-directory=<cwd> -e claude ...`. En
macOS Ghostty no acepta lanzar el emulador desde su propia CLI ni implementa IPC, así que ahí se usa
`open -na Ghostty.app --args --working-directory=<cwd> -e claude ...`.

⁴ Ghostty solo expone dos acciones IPC, `new_window` y `toggle_quick_terminal`; no hay `+new-tab` ni
`+new-split` y upstream cerró la petición como *not planned*
([#12136](https://github.com/ghostty-org/ghostty/issues/12136)). Sus acciones D-Bus por ventana
(`win.new-tab`, `win.split-right`) no aceptan directorio ni comando, así que no pueden llevar un
`claude --resume`. Para paneles y pestañas dentro de Ghostty, usa tmux o zellij.

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

El modal está dividido en pestañas:

| Pestaña | Qué configura |
|---------|---------------|
| **Lanzamiento** | dónde se abre la sesión con Enter, y argumentos extra para `claude` |
| **Sesiones compartidas** | los **servidores** (nombre, proveedor, URL, token o SSH) y el **remoto global** |
| **Colores** | las reglas automáticas de color |

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
  "color_rules": [],
  "remote_servers": [
    { "name": "FactorLibre", "kind": "gitlab", "host": "https://git.factorlibre.com",
      "auth": "ssh", "ssh_user": "git", "ssh_port": 2211 }
  ],
  "remote_kind": "none",
  "remote_server": "",
  "remote_repo": "",
  "remote_branch": "main",
  "remote_path": ""
}
```

Los `remote_*` sueltos son el **remoto global**; `remote_servers` es el catálogo de servidores.
Los enlaces por proyecto viven aparte, en `project-remotes.json`, y los tokens en
`remote-tokens.json` — nunca aquí.

Un `config.json` ausente, corrupto o con claves inválidas cae silenciosamente a estos valores por defecto — nunca es un error fatal.

> Nota sobre `Shift+Enter`: la mayoría de los emuladores modernos lo transmiten distinto a `Enter`, pero algunos antiguos no — en ese caso `Shift+Enter` simplemente hará lo mismo que `Enter`. Si te ocurre, cambia el predeterminado en Ajustes para que ambas teclas hagan lo que quieres.

## Servidor MCP (`multi-claude-mcp`)

El índice FTS5 que alimenta la búsqueda global (`?`) no tiene por qué consultarse solo a mano. `multi-claude-mcp` lo pone detrás de un servidor MCP, de modo que **Claude puede buscar en su propio trabajo pasado**: en vez de volver a deducir cómo se resolvió el puerto SSH de GitLab, encuentra la conversación donde se resolvió — de cualquier proyecto, incluido uno que nunca ha tenido en contexto.

Registrarlo una vez:

```bash
# para todos tus proyectos
claude mcp add multi-claude --scope user -- multi-claude-mcp

# o solo para el proyecto actual
claude mcp add multi-claude -- multi-claude-mcp
```

Y a partir de ahí, dentro de cualquier sesión: *"¿habíamos peleado ya con la autenticación SSH de este repo?"*.

> Si vienes de una versión anterior, `multi-claude-mcp` es un comando nuevo y no aparece hasta que reinstales (`uv tool upgrade multi-claude`, o `uv pip install -e .` en un checkout). Mientras, el módulo funciona igual invocado a mano: `claude mcp add multi-claude -- python -m multi_claude.mcp`.

### Herramientas que expone

| Herramienta       | Qué hace                                                                       |
|-------------------|--------------------------------------------------------------------------------|
| `search_sessions` | búsqueda full-text sobre el **contenido** de todas tus sesiones indexadas; opcionalmente acotada a un `project_path` |
| `search_team_sessions` | las sesiones que publicó el equipo, por los **metadatos** de su manifest (nombre, primer prompt, tags, branch, autor) — nunca por la transcripción, que no sale de la máquina que la grabó |
| `get_session`     | metadatos de una sesión y sus últimos N turnos, para leerla sin reanudarla       |
| `list_projects`   | los proyectos con historial en esta máquina, con su path real y nº de sesiones   |
| `refresh_index`   | puebla el índice; solo hace falta si `search_sessions` no encuentra algo que sí está en disco |

### Decisiones

- **Solo lectura.** Ninguna herramienta mueve, borra, publica ni renombra nada. Lo único que escribe es la caché del índice de multi-claude.
- **Sin el SDK de MCP.** El transporte stdio del protocolo es JSON-RPC 2.0 delimitado por líneas, así que son `json` y la stdlib: unos cientos de líneas en `mcp.py` en lugar del árbol de dependencias del SDK, coherente con el resto del proyecto. Se negocia versión de protocolo (se devuelve la del cliente si la conocemos, y la nuestra si no) y se distingue error de protocolo (`-32602` y compañía) de fallo de ejecución (`isError` dentro del resultado), como manda la spec.
- **Texto, no JSON.** Las herramientas devuelven texto legible en vez de `structuredContent`: el consumidor es un modelo, y duplicar el payload en JSON serializado solo gastaría tokens.
- **El índice se puebla solo la primera vez.** Como se escribe al *entrar* a un proyecto en la TUI, una instalación recién hecha no tendría nada que buscar; la primera búsqueda sobre un índice vacío escanea todo y lo dice en la respuesta (medido: 1,2 s para 34 sesiones, 65 MB de jsonl). Las siguientes son de milisegundos.
- **Una sesión borrada no se ofrece.** El índice es una caché que nunca se purga, así que sobrevive a las sesiones que describe; los resultados cuyo jsonl ya no está en disco se descartan antes de responder, en vez de devolver un id que `get_session` no podría abrir.

> **Privacidad**: esto le da al modelo acceso de lectura al contenido de tus conversaciones anteriores, que es justo el objetivo — pero una sesión que en su día imprimió un `.env` o un token lo tiene dentro de su jsonl, y por tanto en el índice. Es el mismo material que ya está en tu disco, no sale de la máquina, pero conviene saber que entra en contexto.

Para las sesiones del equipo, `search_team_sessions` es deliberadamente una herramienta aparte y no un flag de la otra: lo que devuelve no es comparable —metadatos frente a contenido, y una sesión que hay que descargar antes de poder leer—, y mezclarlas invitaría al modelo a dar por buscada una conversación que nadie ha indexado.

## Ficheros de estado

Todo lo que multi-claude guarda por su cuenta (nunca escribe dentro de los jsonl de Claude). Las rutas respetan `$XDG_CONFIG_HOME` / `$XDG_DATA_HOME` si están definidas, con `%APPDATA%` en Windows para la config:

| Fichero                                          | Contenido                                            |
|--------------------------------------------------|------------------------------------------------------|
| `~/.config/multi-claude/config.json`             | preferencias (modo, orden, preview, agrupación, reglas de color) |
| `~/.config/multi-claude/names.json`              | nombres de sesión persistentes (`e`)                 |
| `~/.config/multi-claude/session-tags.json`       | etiquetas por sesión (`t`)                           |
| `~/.config/multi-claude/session-colors.json`     | colores manuales por sesión (`c`)                    |
| `~/.config/multi-claude/project-folders.json`    | árbol de carpetas y asignación de proyectos (`f`)    |
| `~/.config/multi-claude/project-remotes.json`    | repositorios de sesiones enlazados a cada proyecto (`L`), indexados por el `origin` del repo |
| `~/.config/multi-claude/remote-tokens.json`      | un token por servidor, con permisos `0600`           |
| `~/.local/share/multi-claude/index.sqlite3`      | índice SQLite + tablas FTS5 de tus sesiones y del último listado de cada repositorio compartido (caché reconstruible) |
| `~/.cache/multi-claude/repos/`                   | copias de trabajo de los repositorios de sesiones por SSH (caché reconstruible) |

Borrar cualquiera de ellos es seguro: se pierde ese estado, no las sesiones. `remote-tokens.json`
es el único que contiene un secreto, y por eso se crea con permisos de solo-propietario.

## Identidad de un proyecto

El nombre de la carpeta `~/.claude/projects/<encoded>/` es la ruta original con `/` reemplazado por `-`. Esta codificación es ambigua si el path original contenía guiones (`/foo-bar/baz` y `/foo/bar/baz` colisionan).

**Fuente de verdad**: el campo `cwd` del primer evento `type=user` del primer `.jsonl` del proyecto. Solo si no hay ningún jsonl parseable se cae a la heurística `-` → `/`.

`os.path.isdir(cwd)` decide si el proyecto está vivo o huérfano.

## Limitaciones conocidas

- **El índice se puebla en segundo plano al arrancar**, no al entrar a cada proyecto: la primera vez tras actualizar cuesta un momento (0,8 s para 35 sesiones donde se midió) y desde entonces son unos `stat`. Mientras ese primer barrido corre, `?` puede devolver menos de lo que hay.
- **Payload FTS acotado por sesión**: como máximo las primeras 20.000 líneas del jsonl y 512 KB de texto (`FTS_REINDEX_SCAN_LINES` / `FTS_CONTENT_MAX_CHARS` en `session.py`). Cubre de sobra las sesiones medidas —la más larga tenía 7.555 líneas—, pero una conversación extraordinariamente larga seguiría cortándose por el final. Solo entra el texto de usuario y asistente: las llamadas a herramientas y su salida nunca se indexan, así que no se pueden buscar.
- **Proyecto movido de path**: si renombras la carpeta de un proyecto, las sesiones viejas y nuevas siguen siendo dos entradas distintas en `~/.claude/projects/`. No se reconcilian solas — la vieja queda como huérfana y la unes a mano con `m` (merge).
- **No todos los emuladores saben abrir pestañas desde la CLI**: Ghostty (sus únicas acciones IPC son `new_window` y `toggle_quick_terminal`; upstream cerró la petición de pestañas por CLI como *not planned*), Alacritty, foot y Terminal.app solo pueden abrir ventanas, así que en modo `tab` la sesión acaba en una ventana nueva y la TUI te lo dice. Si quieres paneles o pestañas dentro de Ghostty, mete tmux o zellij por debajo. En kitty y WezTerm la pestaña exige tener el control remoto activado (`allow_remote_control` en `kitty.conf`); si está apagado, mismo fallback.
- **zellij no puede lanzar un comando en una pestaña nueva**: `zellij action new-tab` solo acepta un layout, no un comando, así que el modo `tab` dentro de zellij abre un panel.
- **El estado en vivo es de esta máquina, y un valor nuevo no se interpreta**: ver [Estado en vivo](#estado-en-vivo). Un estado que no esté en `_STATUS_CELLS` (`screens/sessions.py`) se muestra como `● abierta`.
- **Las sesiones de background tardan hasta 15 s en aparecer**: solo las conoce `claude agents --json`, que se consulta en un tick lento porque cuesta ~350 ms. Sin `claude` en el PATH no aparecen en absoluto.
- **Con token, republicar una sesión compartida sobrescribe la versión del remoto**: si dos personas continúan la misma sesión y ambas publican vía API, la segunda pisa a la primera en el remoto (cada una conserva la suya en local). **Con autenticación SSH no ocurre**: git rechaza el segundo push y se reintenta encima, así que ambas sobreviven. El fork explícito que lo resolvería también en API está planificado, no implementado — ver [docs/REMOTE-SESSIONS.md](docs/REMOTE-SESSIONS.md).
- **Una sesión ya descargada no se puede actualizar**: si un compañero la continúa después de que la traigas, la fila lo indica con `↻` pero no hay forma de incorporar esos turnos. Requiere el merge por `uuid` que sigue pendiente.
- **El escáner de secretos es heurístico, no una garantía**: reconoce formatos conocidos (claves privadas, tokens con prefijo de proveedor, credenciales en URLs) y asignaciones cuyo nombre y cuyo valor parecen una credencial, pero una contraseña dictada en prosa o un formato propio se le escapan. Es una red de seguridad, no una autorización — ver [Escáner de secretos](#escáner-de-secretos-al-publicar). Los binarios y los ficheros de más de 8 MB no se revisan, y el diálogo lo dice.
- **Publicar en GitLab/GitHub hace un commit por fichero**: una sesión con subagentes son varios commits en el repo de sesiones, no uno. El manifest siempre va el último, así que una publicación interrumpida queda invisible en vez de a medias, pero el historial del repo es más ruidoso de lo necesario.
- **De las sesiones compartidas solo se buscan sus metadatos**, no su contenido: el manifest no lleva la transcripción, así que `?` y `search_team_sessions` encuentran una sesión ajena por nombre, primer prompt, tags, branch o autor, pero no por algo dicho dentro. Para buscar en su contenido hay que traerla primero.
- **Las filas del equipo son de la última visita a cada pestaña**: se cachean cuando abres la pestaña de un repositorio, no en segundo plano, así que lo publicado después no aparece en `?` hasta que vuelvas a abrirla. Es deliberado — la pantalla de búsqueda no debe hacer llamadas de red.
- **La primera búsqueda del servidor MCP sobre un índice vacío escanea todo el histórico**: 1,2 s para 34 sesiones (65 MB de jsonl) en la máquina donde se midió, pero crece con el histórico y el trabajo es leer y parsear ficheros. Si tu cliente la cortase por timeout, ejecuta `multi-claude` una vez (o llama a `refresh_index`) y repite: a partir de ahí las consultas son de milisegundos.

## Instalación

### Requisitos previos

- **Linux** (Ubuntu/Debian/Fedora/Arch testados), **macOS** o **Windows 10/11**.
- **Python 3.10+** (la mayoría de distros modernas lo traen; en macOS `brew install python@3.13`; en Windows usa el instalador oficial o `winget install Python.Python.3.13`).
- **`claude`** (Claude Code CLI) en `PATH`. Sin él, `multi-claude` arranca pero no podrá reanudar sesiones — la propia TUI te lo dirá.
- *(Opcional, Linux/macOS)* **`tmux`** o **`zellij`** (o **`terminator`** con su `remotinator`, sólo en Linux) para que Claude se abra en un panel sin perder la TUI. Sin multiplexer, la mayoría de emuladores abren pestaña en la misma ventana (ver [Cómo se lanza Claude](#cómo-se-lanza-claude)).
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

Sin argumentos abre la TUI; hay dos cosas que tienen más sentido como informe de una sola vez:

```bash
multi-claude --audit-secrets    # revisa el histórico buscando credenciales (sale 1 si hay)
multi-claude --help
```

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
  index.py           # SessionIndex — SQLite + FTS5 de tus sesiones y de las del equipo
  transcript.py      # lectura de turnos de un jsonl, sin Textual (preview y MCP)
  mcp.py             # servidor MCP sobre el índice: JSON-RPC 2.0 por stdio, sin SDK
  secret_scan.py     # busca credenciales en lo que se va a publicar; enmascara y redacta
  audit.py           # barrido de todo el histórico (--audit-secrets) e informe
  launcher.py        # launch_claude(): panel/pestaña/ventana/inline según emulador y multiplexer
  focus.py           # traer al frente la terminal de una sesión ya viva
  deletion.py        # borrado de sesiones/proyectos y sus artefactos en disco
  transfer.py        # export/import de sesiones en .zip
  project_remotes.py # RemoteServer, RemoteLink y qué repos tiene enlazado cada proyecto
  remote.py          # RemoteStore (protocolo), DirectoryRemote, TokenStore, manifests
  remote_http.py     # GitLabRemote / GitHubRemote sobre sus API REST
  remote_git.py      # GitSshRemote — git por SSH, y comprobación de acceso con ssh -T
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
    search.py        # SearchScreen — búsqueda FTS5 global, tus sesiones y las del equipo
  widgets/
    preview.py       # SessionPreview — últimos turnos del jsonl
  styles.tcss        # estilos Textual
```
