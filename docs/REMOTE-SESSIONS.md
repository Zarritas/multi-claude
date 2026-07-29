# Sesiones compartidas (v1)

Plan técnico para que un empleado reanude la sesión de un compañero pulsando `Enter`, sin
export/import manual y preservando el uuid original. Extiende [DESIGN.md](DESIGN.md).

## Problema

Hoy compartir una sesión es `x` (exportar a zip) → mandar el fichero → `i` (importar). Funciona,
pero es manual y no escala a un equipo. El objetivo es que las sesiones que un compañero decida
publicar aparezcan en el listado y se reanuden como si fueran locales.

El requisito que **no** hay que resolver: cada empleado usa su propia cuenta de Claude. El servidor
guarda transcripts, no credenciales.

## Por qué no basta con montar el directorio en red

La alternativa obvia —montar `~/.claude/projects/` desde un servidor— no resuelve el problema:

- **El nombre de carpeta lo deriva Claude Code del cwd local** (`encode_cwd`, ver DESIGN.md
  "Fuente de verdad del cwd"). Dos empleados con `$HOME` distinto escriben en carpetas distintas
  del mismo montaje, así que `claude --resume` nunca ve las sesiones del otro. Se comparte el disco
  sin compartir nada.
- **Si los paths coinciden, es peor**: dos procesos hacen `append` al mismo `.jsonl`. En local es
  atómico para líneas pequeñas; sobre NFS/SSHFS no está garantizado y el fichero se corrompe a
  nivel de bytes, no se bifurca. Ningún merge por `uuid` lo arregla.
- SQLite sobre filesystem de red corrompe, y ahí vive el índice de `index.py`.

El ritmo de escritura **no** es el problema (medido: mediana 4,8 eventos/min, pico 28). La
objeción es de identidad, no de rendimiento.

Conclusión: local-first, con multi-claude como **traductor de identidad** entre el uuid del
compañero y la carpeta de proyecto local.

## Decisiones cerradas

| Tema | Decisión |
|------|----------|
| Modelo | Local-first; el remoto es un almacén, nunca el directorio de trabajo |
| uuid | Se preserva tal cual (uuid v4, no colisionan entre empleados) |
| `cwd` embebido | No se reescribe: es histórico y Claude lo ignora (validado, ver Fase 0) |
| Backends | Carpeta (`DirectoryRemote`), API REST de GitLab/GitHub (`remote_http.py`), git por SSH (`remote_git.py`) |
| Servidores | Definidos una vez en Ajustes; los enlaces los referencian **por nombre**, no copian su host |
| Autenticación | Token por servidor, o SSH con las claves del usuario |
| Alcance | **Por proyecto**: cada proyecto se enlaza a uno o varios repos de sesiones, cada uno una pestaña. El remoto global solo es fallback |
| Clave del enlace | El `origin` normalizado del repo, así que todos sus worktrees comparten enlace |
| Credenciales | Fichero `remote-token` con permisos `0600`, o `$MULTI_CLAUDE_REMOTE_TOKEN`; **nunca** en `config.json` |
| Compresión | gzip de la stdlib (medido ~3,7:1 sobre una sesión real de 4,6 MB) |
| Manifests | Uno por sesión, nunca un manifest global (evita conflictos de escritura) |
| Concurrencia | Fork explícito, no merge (ver "Concurrencia") |
| Cifrado | Ninguno en v1: el control de acceso lo dan los permisos del directorio (y, cuando exista, el repo privado) |

### Excepción a "ninguna escritura en disco de Claude"

DESIGN.md fija que no escribimos en `~/.claude/` salvo mover o borrar jsonl. Hidratar una sesión
remota **es** una escritura nueva en el directorio del proyecto. Se acepta como extensión de esa
regla, con el precedente de `import_archive` (`transfer.py`), que ya escribe ahí. La regla real que
se mantiene es la de fondo: no tocamos ficheros que Claude esté usando, y todo lo que escribimos es
un jsonl completo con su subdirectorio, nunca una modificación parcial.

## Qué compone una sesión

Una sesión no es un fichero, son cuatro rutas, y solo tres viajan:

| Ruta | ¿Viaja? | Por qué |
|------|---------|---------|
| `<uuid>.jsonl` | Sí | El transcript |
| `<uuid>/subagents/*.jsonl` + `.meta.json` | Sí | En sesiones con fan-out es la mayor parte del trabajo |
| `<uuid>/tool-results/*.txt` | Sí | Outputs grandes volcados fuera del jsonl; sin ellos la conversación tiene agujeros |
| `<proyecto>/memory/` | **No** | Auto-memoria personal del empleado (preferencias, notas) |
| `session-env` | **No** | Puede contener secretos de máquina; Claude lo recrea |

`transfer.py` ya copia `<uuid>/` de forma recursiva, así que las tres primeras salen gratis, y ya
excluye las dos últimas por construcción.

Lo que **no** viaja y no tiene solución: el código. Ver "Divergencia de código".

## Layout en el remoto

```
manifest/<uuid>.json
blobs/<uuid>/session.jsonl.gz
blobs/<uuid>/subagents/agent-*.jsonl.gz
blobs/<uuid>/subagents/agent-*.meta.json
blobs/<uuid>/tool-results/*.txt.gz
```

`manifest/<uuid>.json`:

```json
{
  "format": "multi-claude/remote-session",
  "version": 1,
  "id": "<uuid>",
  "published_at": "<iso8601>",
  "published_by": "<email>",
  "cwd": "/home/quien-la-grabo/WS/repo",
  "branch": "fl-v16-9269",
  "git_remote": "git@git.factorlibre.com:odoo-16/fl-v16.git",
  "git_head": "abc1234",
  "display_name": "...",
  "tags": ["..."],
  "first_prompt": "...",
  "message_count": 412,
  "size_bytes": 1234567,
  "forked_from": null
}
```

`git_remote` + `git_head` son la base del aviso de divergencia y, más adelante, de la
reconciliación de proyectos por remote URL que DESIGN.md deja fuera de alcance.

## Arquitectura

Un módulo nuevo:

```
src/multi_claude/project_remotes.py
    RemoteLink              qué es un remoto: kind/path/host/repo/branch/label
    ProjectRemotesStore     proyecto -> [RemoteLink, ...], indexado por origin normalizado
    normalize_git_remote()  ssh y https del mismo repo dan la misma clave

src/multi_claude/remote.py
    RemoteStore(Protocol)   list_sessions() / fetch() / publish()
    DirectoryRemote         driver de carpeta
    RemoteSession           dataclass de metadatos de una sesión publicada
    TokenStore              token de API, fichero aparte con permisos 0600
    store_from_link()       factoría a partir de un RemoteLink

src/multi_claude/remote_http.py
    HttpRepoRemote          maquinaria común: listar / leer / escribir un fichero del repo
    GitLabRemote            endpoints y auth de GitLab (gitlab.com y self-hosted)
    GitHubRemote            endpoints y auth de GitHub
```

El layout en el remoto, el gzip y el orden «manifest al final» viven en `remote.py`, así que una
sesión publicada en una carpeta y otra publicada en GitLab son idénticas byte a byte. Un backend
nuevo solo aporta cuatro operaciones: listar un directorio, leer un fichero, escribir un fichero
y decir quién es el repo.

`DirectoryRemote` no es solo un doble de test: da un segundo backend real (carpeta compartida,
Syncthing, NFS de solo-lectura) sin código extra, y permite los tests sin red.

### Drivers de GitLab y GitHub

Vía API REST, sin clonar el repo. Ambos proveedores se reducen a las mismas cuatro operaciones,
con endpoints distintos:

| Operación | GitLab | GitHub |
|-----------|--------|--------|
| Listar | `GET /projects/:id/repository/tree` (`recursive=true`) | `GET /repos/:o/:r/contents/:path` (no recursivo: se recorre a mano) |
| Leer | `GET .../repository/files/:path/raw` | `GET .../contents/:path` con `Accept: vnd.github.raw` |
| Escribir | `POST .../repository/files/:path`, y `PUT` si ya existe | `PUT .../contents/:path`, con `sha` si ya existe |
| Identidad | `GET /projects/:id` → `path_with_namespace` | `GET /repos/:o/:r` → `full_name` |
| Auth | cabecera `PRIVATE-TOKEN` | `Authorization: Bearer` + versión de API fijada |

Dos asimetrías que el código absorbe: **GitLab no tiene upsert** (crear un fichero que ya existe
es un 400, así que se reintenta como `PUT` — sin eso, republicar fallaría siempre), y **GitHub
exige el `sha` del blob** para sobrescribir, lo que obliga a un `GET` previo.

**Un commit por fichero, no uno por sesión.** El plan original quería usar el endpoint de commits
multi-`action` de GitLab para que publicar fuese atómico. Se descartó: GitHub no tiene equivalente
directo, y mantener dos caminos distintos por proveedor duplicaba la parte más delicada. Con
escritura fichero a fichero y el manifest al final, la invariante que importa —una publicación a
medias es invisible, no rota— se conserva en ambos. El coste es un historial más ruidoso en el
repo de sesiones.

Tampoco hay listado incremental por commit sha: cada `R` relista los manifests. Ver
"Desviaciones del plan".

### Cambios en módulos existentes

**`index.py`** — sin cambios. La idea original era cachear el listado remoto en tres columnas
(`origin`, `remote_author`, `remote_updated`) vía `_ensure_columns()`. No se hizo: ver
"Desviaciones del plan". Consecuencia asumida: las sesiones compartidas **no** entran en el
FTS global (`?`) hasta que se hidratan.

**`screens/sessions.py`** — una barra de pestañas y dos bindings:

| Elemento | Acción |
|----------|--------|
| Pestaña `Locales` | Las sesiones de este proyecto en disco |
| Pestaña `☁ nombre` | **Todo** lo publicado en ese repo, con el estado de la copia local |
| `u` | Publicar al repo de la pestaña activa (ver "Publicar con varias pestañas") |
| `L` | Gestionar los repos enlazados a este proyecto |

Las pestañas iniciales se construyen en `compose`, no tras montar: hacerlo desde un worker dejaba
la pantalla brevemente incompleta y volvía inestables los tests que navegan a ella.

**`screens/sessions.py`, acción `Enter`** — si la fila es remota: hidratar en `project_dir` y luego
`launch_claude(cwd, session_id=...)`, cuya firma ya sirve sin cambios.

**`config.py`** — `remote_kind` (`none` | `directory` | `gitlab` | `github`), `remote_path`,
`remote_host`, `remote_repo` y `remote_branch`. `none` por defecto: hay que activarlo a mano.
`remote_api_host()` cae al host por defecto del proveedor, para que en gitlab.com o github.com no
haya que teclear URL. `$MULTI_CLAUDE_REMOTE_DIR` gana sobre todo, para probar sin tocar estado
compartido. El token **no** está aquí: ver `TokenStore`.

**`modals.py`** — `RemoteSettingsModal` con proveedor, servidor, repo, rama y token, más una
prueba de conexión (`Ctrl+T`) que valida los campos *antes* de guardarlos. Se abre desde el modal
de ajustes existente (`s` → «Configurar remoto…»), anidado en vez de en línea porque cinco campos
más una prueba enterrarían los ajustes de lanzamiento que ese modal existe para editar. Devuelve
el config con solo los campos `remote_*` reemplazados, igual que `SettingsModal` hace con el resto
de preferencias.

**`app.py` / `app_protocol.py`** — el store vive en `app.remote` (`RemoteStore | None`) y se
reconstruye en `update_prefs`, así que cambiar de remoto no exige reiniciar.

## Flujos

**Publicar** (`u`): reunir artefactos → gzip → subir los blobs → escribir el manifest al final.
Antes de confirmar se muestra el listado de ficheros que se van a subir (ver "Riesgos"). Corre en
un worker, así que la TUI no se congela con una sesión de varios MB.

**Descubrir** (`R`): lista los manifests remotos que no están en local, mezclados en la tabla y
marcados con su autor. El flujo "un compañero me pega un uuid por Slack" ya funciona sin código
nuevo: `y` copia el uuid al portapapeles y el filtro soporta `id:`.

**Reanudar** (`Enter` sobre remota): descargar → descomprimir en `project_dir` con el uuid intacto →
comparar `git_remote`/`git_head` del manifest con el estado local → si divergen, avisar antes de
lanzar → `launch_claude`.

## Servidores y autenticación

Un servidor (`RemoteServer`) es proveedor + URL + autenticación; un enlace (`RemoteLink`) es
repo + rama + nombre de pestaña **sobre** un servidor. Están separados porque cambian a ritmos
distintos: una empresa tiene uno o dos servidores y un repo por cliente, así que teclear el host
y pegar un token en cada repo era trabajo repetido y dejaba la misma credencial en varios sitios.

Los enlaces referencian el servidor **por nombre**, así que corregir una URL o rotar un token
arregla todos los repos que apuntan a él. Un enlace que nombra un servidor inexistente resuelve a
`kind="none"`: inerte y visiblemente inerte, mejor que publicar en otro sitio sin avisar.

### Comprobar acceso SSH: `ssh -T`, no un repo inventado

La primera versión probaba un servidor pidiéndole un repositorio inexistente y trataba el 404
como éxito. Funciona, pero el mensaje de error acababa mostrando la URL fabricada
(`…:multi-claude/_probe.git`), que no es de nadie y confunde: un usuario razonablemente asumió
que la herramienta estaba buscando el repo equivocado.

`ssh -T` es lo que GitHub y GitLab esperan para esto: no necesita repositorio y ambos responden
con el nombre de la cuenta, que es lo que uno quiere confirmar de verdad. Dos detalles:

- **GitHub sale con código ≠ 0 al autenticar correctamente** («does not provide shell access»),
  así que el código de salida no dice nada y solo sirve el saludo.
- El error de clave rechazada **nombra la equivocación probable**: el usuario SSH es siempre
  `git`, y confundirlo con la cuenta (`Zarritas@github.com`) es el fallo natural, porque en
  `git@github.com:Zarritas/repo.git` la cuenta aparece en la parte del repositorio.

Verificado contra github.com real: `git@github.com` responde
`autenticado en github.com como Zarritas`, y `Zarritas@github.com` responde
`github.com rechazó tu clave SSH — en github.com el usuario SSH es «git», no «Zarritas»`.

### Nada que toque la red corre en el hilo de la UI

Invariante, y aprendida por las malas: la prueba de conexión del editor de servidor era
síncrona, así que pulsar «Probar» con SSH congelaba la aplicación hasta el timeout — hasta dos
minutos con la terminal muerta y sin ninguna indicación de que estuviera trabajando.

Todas las operaciones remotas van en un `@work(thread=True)` y devuelven por
`call_from_thread`: publicar, listar, hidratar, el índice de publicados y la prueba de conexión.
El callback comprueba `is_mounted` antes de escribir en la pantalla, porque el modal puede
haberse cerrado mientras la llamada estaba en vuelo.

La prueba usa además un timeout propio y corto (`PROBE_TIMEOUT`, 15 s) en lugar del de clonado
(120 s): es interactiva, y fallar rápido vale más que acertar tarde.

### SSH frente a token

| | Token (API REST) | SSH (`remote_git.py`) |
|---|---|---|
| Credencial | Una por persona y por host, hay que crearla y repartirla | Las claves que ya están desplegadas |
| Transporte | `urllib` contra la API | El binario `git` contra un clon en `~/.cache/` |
| Concurrencia | La segunda publicación **pisa** la primera | Push rechazado → rebase → reintento: **ambas sobreviven** |
| Coste | Ninguno en disco | Una copia de trabajo por repo y rama |

Esa fila de concurrencia es la razón técnica para preferir SSH: es el único backend donde dos
personas publicando a la vez no se pierden trabajo, porque git ya sabe resolver eso. El layout en
el remoto es idéntico en los tres, así que una sesión publicada por SSH y la misma publicada por
API son iguales byte a byte, y un repo se puede leer de las dos formas.

Dos detalles del driver de git que no son obvios:

- **`LC_ALL=C` es obligatorio.** git traduce sus errores, así que en un sistema en español
  «repository does not exist» llega como «el repositorio no existe» y cualquier interpretación
  del stderr deja de funcionar en silencio — dejando al usuario el mensaje crudo de git. Lo
  descubrió un test al ejecutarse en una máquina con git en español.
- **`GIT_TERMINAL_PROMPT=0` y `BatchMode=yes`.** Una petición de credenciales colgada dentro de
  un worker de la TUI es invisible y parece que la aplicación se ha congelado; fallar con un
  mensaje es estrictamente mejor.

## Estado de la copia local

La pestaña de un repo lista **todo** lo publicado, no solo lo que no tienes. Ocultar lo que ya
está en disco parecía evitar duplicados, pero rompía lo primero que uno quiere después de
publicar: ver su sesión ahí como confirmación de que la subida funcionó.

Cada fila lleva entonces un indicador de cómo está tu copia respecto a la publicada:

| Marca | Estado | Cómo se decide |
|-------|--------|----------------|
| `☁` | `absent` | No hay `.jsonl` local con ese uuid |
| `✓` | `current` | El tamaño local coincide con `size_bytes` del manifest |
| `↻` | `stale` | El manifest declara más bytes: alguien la continuó y republicó |
| `↑` | `ahead` | El local tiene más bytes: has seguido trabajando sin publicar |

Comparar tamaños basta porque el jsonl es append-only (verificado en la Fase 0: 0 uuids
duplicados, nunca se reescribe), así que cualquier diferencia es contenido real y no una
reescritura. El coste es un `stat` por fila.

`Enter` sobre una fila ya descargada reanuda la copia local en lugar de intentar traerla: el
`fetch` se niega a sobrescribir, y de todos modos es la misma sesión. Sobre una `↻` avisa
primero, porque traer los turnos que faltan es el merge que sigue pendiente.

### La misma información en la pestaña local

Saber si una sesión propia está compartida exige preguntar a los remotos, así que se construye
un índice `session_id -> (manifest, remoto)` en un worker al entrar en el proyecto y tras cada
publicación. La lista local se pinta sin esperarlo y se repinta cuando llega; si un remoto falla,
el coste es una marca ausente, no un listado roto.

Las marcas son las mismas (`✓ ↻ ↑`) para que signifiquen lo mismo en los dos lados, y se añade
la autoría cuando quien publicó no eres tú — que es lo que distingue «mía y compartida» de
«traída de un compañero».

### Efecto secundario: hidratar no debe dejar el proyecto huérfano

El jsonl de un compañero lleva **su** `$HOME`, que aquí no existe. Al ser el fichero más
reciente, ganaba el desempate de `resolve_real_cwd` y el proyecto pasaba a resolverse a una ruta
inexistente: marcado como huérfano y por tanto no abrible — justo la sesión que acababas de
traer. Ahora la resolución prefiere, por este orden, el candidato cuyo nombre codificado coincide
con el directorio, luego **uno que exista en disco**, y solo después el más reciente.

## Concurrencia

En v1 **no hay merge**. El plan es que si dos empleados continúan la misma sesión, la segunda
publicación cree una variante con uuid nuevo y `forked_from: <uuid-original>`, visible en el
listado como bifurcación. **Todavía no está implementado**: hoy la segunda publicación sobrescribe
(ver "Desviaciones del plan"). El campo `forked_from` ya viaja en el manifest.

Esto es viable porque Claude Code ya soporta `--resume <id> --fork-session` de forma nativa
(DESIGN.md lo listaba como pendiente): el fork no hay que fabricarlo reescribiendo `sessionId`,
basta con lanzar con esa flag y publicar el uuid resultante.

Decisión consciente: un merge real (unión de eventos por `uuid`, que el DAG `parentUuid` permite —
verificado: 0 uuids duplicados en las sesiones reales) queda para v2, y solo si el fork explícito
demuestra ser insuficiente en uso real.

## Divergencia de código

El transcript viaja; el repo no. Si el compañero grabó la sesión sobre `abc1234` y tú estás en
`def5678`, la conversación describe ficheros que ya no son esos.

v1 lo mitiga, no lo resuelve: al hidratar se compara `git_remote`/`git_head` y se avisa antes de
lanzar. Un prefacio inyectado en el contexto ("esto se grabó sobre `abc1234`, estás en `def5678`")
es v1.1. Es una limitación inherente que se documenta, no se esconde.

## Fases

| Fase | Qué | Estado |
|------|-----|--------|
| 0 | Spike bloqueante: `--resume` de un jsonl ajeno con `cwd` inexistente | **validado** |
| 1 | `remote.py` + `DirectoryRemote` + tests | **hecho** |
| 2 | `GitLabRemote` + `GitHubRemote` + credenciales + UI de configuración | **hecho** |
| 3 | Publicar (`u`) sobre selección múltiple | **hecho** |
| 4 | Listado remoto (`R`) | **hecho** (sin columnas de índice, ver abajo) |
| 5 | Hidratar + `Enter` + aviso de divergencia | **hecho** |
| 6 | Fork en re-publicación (`--fork-session`) | pendiente |

### Desviaciones del plan, y por qué

**El orden se invirtió: la UI antes que los drivers de API.** Con `DirectoryRemote` el MVP se
pudo probar apuntando a una carpeta, sin token ni repo ni servidor. Empezar por GitLab habría
dejado la feature inservible hasta tener infraestructura.

**Un commit por fichero en vez de un commit atómico por sesión.** Ver "Drivers de GitLab y
GitHub": mantener dos caminos distintos por proveedor duplicaba la parte más delicada, y la
invariante que de verdad protege (publicación a medias = invisible) se conserva igual.

**Sin listado incremental por commit sha.** Cada `R` relista los manifests. Es una llamada por
manifest en los proveedores de API, así que con muchas sesiones publicadas convendrá cachear; con
las decenas que tiene un equipo pequeño, no compensa la invalidación.

**Sin columnas en el índice SQLite.** La Fase 4 preveía `origin`/`remote_author`/
`remote_updated` para cachear el listado remoto. No se han añadido: la lista se pide al
store cada vez que se pulsa `R` y vive en memoria. Con manifests de unos cientos de bytes
sobre un directorio, cachear era optimizar algo que no dolía, a cambio de una migración de
esquema y un estado más que invalidar. Cuando el backend sea una API con latencia, esa
caché vuelve a tener sentido.

**Republicar sobrescribe, no bifurca.** La Fase 6 sigue pendiente, así que hoy
`publish` sobre un uuid ya presente pisa los blobs y el manifest. En el caso lineal (uno
hidrata, continúa y republica) no se pierde nada, porque su jsonl contiene la historia del
otro más su continuación. Pero si dos personas continúan la misma sesión en paralelo, la
segunda publicación pisa a la primera en el remoto. Es el riesgo conocido del MVP y está
declarado como limitación en el README.

## Enlaces por proyecto

Un solo remoto global no sobrevive al trabajo real: las sesiones sobre el código de un cliente no
deben acabar en el mismo repo que las de otro, y la razón de preferir un repo privado a una carpeta
es justamente que sus permisos ya expresan eso.

Así que cada proyecto se enlaza a **uno o varios** repos de sesiones (`L`), y cada enlace es una
**pestaña** en el listado. Resolución, primero que gana:

1. `$MULTI_CLAUDE_REMOTE_DIR` — override total a una carpeta, para pruebas.
2. Los enlaces propios del proyecto.
3. El remoto global de `config.json`.

Los propios ganan **por completo** sobre el global, no se suman: un proyecto enlazado al repo de un
cliente no debe publicar además al repo por defecto.

### Por qué la clave es el `origin` y no la ruta

`project_remote_key` usa el `origin` normalizado (`normalize_git_remote`), con la ruta absoluta como
fallback. Dos consecuencias, ambas buscadas:

- **Todos los worktrees de un repo comparten enlace.** `repo`, `repo/.claude/worktrees/x` y un
  checkout hermano tienen el mismo `origin`, que es como ya los trata la agrupación de worktrees.
- **`git@host:g/r.git` y `https://host/g/r.git` son la misma clave**, porque son el mismo
  repositorio y nadie debería tener que enlazarlo dos veces.

Un proyecto sin `origin` se indexa por ruta: sigue sirviendo en una máquina, pero no viaja entre
checkouts.

### Elegir destino al publicar

`u` abre un diálogo propio (`PublishModal`) que hace las dos preguntas a la vez: a qué repo va y
si confirmas. Con varios repos enlazados muestra un selector, preseleccionando el de la pestaña
activa; con uno solo, lo declara y no hay nada que elegir.

La primera versión reutilizaba el modal de borrado, lo que dejaba un botón «Borrar» en rojo al
publicar — verbo y color equivocados para una subida— y obligaba a abrir la pestaña del destino
antes de pulsar `u`. Preguntarlo en el propio diálogo es menos pasos y mantiene la lista de
ficheros a la vista mientras eliges, que es justamente lo que hay que revisar.

## Cómo probarlo

**Con una carpeta**, sin configurar nada:

```bash
mkdir -p /tmp/remoto-sesiones
MULTI_CLAUDE_REMOTE_DIR=/tmp/remoto-sesiones uv run multi-claude
```

**Con GitLab o GitHub**: crea un repo privado vacío para las sesiones, y en la TUI pulsa `s` →
«Configurar remoto…». Elige proveedor, rellena servidor (vacío para gitlab.com/github.com),
`grupo/repo`, rama y token, y pulsa `Ctrl+T` para verificar antes de guardar. El token necesita
permiso de lectura y escritura sobre ese repo (`api` en GitLab, `contents:write` en GitHub).

1. Entra en un proyecto, sitúate en una sesión y pulsa `u`. Confirma con `y` en el modal,
   que lista los ficheros exactos que se van a subir.
2. Pulsa `R`: no verás nada nuevo, porque una sesión que ya tienes en local no se ofrece
   duplicada como compartida.
3. Para simular a un compañero, publica desde otra máquina (o copia un `<uuid>.jsonl` a
   otro directorio de proyecto y publícalo desde ahí). Al pulsar `R` aparecerá al final de
   la lista con `☁` y su autor, y `Enter` la trae y la reanuda.

El remoto queda inspeccionable a mano, que es parte de la gracia de este backend:

```bash
find /tmp/remoto-sesiones -type f | head
python3 -c "import json;print(json.load(open('/tmp/remoto-sesiones/manifest/<uuid>.json')))"
```

~6 días. El trabajo pesado ya existe: `transfer.py` resuelve el empaquetado y la extracción blindada
contra path traversal, `launcher.py` lanza, `index.py` indexa, `filtering.py` filtra.

### Fase 0: resultado del spike

`transfer.py` documentaba que el `cwd` embebido es histórico y no necesita reescritura, pero eso
estaba verificado solo para sesiones movidas **entre proyectos de la misma máquina**, donde el cwd
de origen existe. El caso nuevo es un `$HOME` que no existe en la máquina destino.

Montaje del spike: `CLAUDE_CONFIG_DIR` apuntando a un config dir aislado, un cwd de prueba nuevo, y
el jsonl de una sesión real de 13 mensajes con sus 33 eventos `cwd` reescritos a
`/home/carlos/proyectos/gextia-dev` (inexistente en la máquina). Lanzado con
`claude --resume <uuid> -p "..."` desde el cwd de prueba.

**Resultado: funciona.** Claude cargó el historial completo y resumió correctamente el contenido
real de la conversación. Queda validado que:

- El uuid se preserva; no hace falta reescribir `sessionId`.
- El `cwd` embebido apuntando a un `$HOME` inexistente **no** impide reanudar.
- `CLAUDE_CONFIG_DIR` aísla el spike sin tocar el `~/.claude` real (útil también para los tests).

**Hallazgo adicional**: al continuar, Claude **añade** eventos al mismo fichero con el `cwd` local,
dejando el jsonl **híbrido** — 33 eventos con el cwd del compañero y 5 con el propio. Esto podría
haber roto la identidad del proyecto, porque `resolve_real_cwd` lee el `cwd` de las sesiones para
decidir a qué directorio real corresponde el proyecto.

No lo rompe: `resolve_real_cwd` ya prefiere el candidato cuyo `encode_cwd` coincide con el nombre
del directorio, precisamente para el caso análogo de sesiones movidas entre cwds. Queda un caso
residual: **un proyecto cuyas sesiones sean todas importadas** no tiene ningún candidato que
coincida y caería al fallback, resolviéndose al cwd del compañero y apareciendo como huérfano. La
Fase 5 debe cubrirlo pasando el cwd de destino conocido (el usuario lo eligió al hidratar) en lugar
de dejar que se infiera.

## Tests

En `tests/test_remote.py`, con `tests/test_transfer.py` como modelo:

- Round-trip contra `DirectoryRemote`: publicar → listar → hidratar → bytes idénticos.
- `memory/` y `session-env` verificadamente ausentes del payload.
- Fork al re-publicar contenido divergente bajo el mismo uuid.
- Manifest corrupto o de versión desconocida → error legible, nunca un traceback.
- `GitLabRemote` con respuestas grabadas; sin red en CI.

## Riesgos

| Riesgo | Mitigación |
|--------|------------|
| La Fase 0 falla | Reescribir `cwd`/`sessionId` al hidratar; sube la Fase 5, no rompe el diseño |
| **Secretos en `tool-results/`** | Un `Bash` que imprimió un `.env` acaba en un `.txt` que se publica sin mirar. Mínimo imprescindible: confirmación con el listado de ficheros antes de subir. Escáner de patrones en v1.1 — no lanzar al equipo sin al menos el aviso |
| Divergencia de código | Aviso al hidratar (ver arriba) |
| Payload grande en la API | gzip; medido, una sesión de 4,6 MB sube como 1,26 MB |
| El remoto como única copia | Si sustituye al histórico local (que Claude purga a los 30 días según `cleanupPeriodDays`), pasa a ser infra crítica y necesita backup |

## Fuera de alcance en v1

- Merge de ramas concurrentes (unión de eventos por `uuid`).
- Destilado o resumen automático de sesiones.
- Colector automático desde las máquinas AF (hoy: `tar` + `scp` manual).
- Cifrado extremo a extremo.
- Presencia y leases ("Ana está en esta sesión ahora mismo").
- Hook de publicación automática al cerrar sesión (`SessionEnd`) — v1.1, deliberadamente manual
  primero para que publicar sea un acto consciente.
