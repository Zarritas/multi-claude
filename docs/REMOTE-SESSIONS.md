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
| Backend v1 | Directorio compartido (`DirectoryRemote`). GitLab vía API REST: pendiente |
| Credenciales | Variable de entorno o fichero `600`; **nunca** en `config.json` |
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
src/multi_claude/remote.py
    RemoteStore(Protocol)   list_sessions() / fetch() / publish()
    DirectoryRemote         driver de carpeta (implementado)
    GitLabRemote            driver de API REST (pendiente)
    RemoteSession           dataclass de metadatos
    store_from_settings()   factoría; $MULTI_CLAUDE_REMOTE_DIR gana sobre el config
```

`DirectoryRemote` no es solo un doble de test: da un segundo backend real (carpeta compartida,
Syncthing, NFS de solo-lectura) sin código extra, y permite los tests sin red.

### Driver de GitLab

Vía API REST, sin clonar el repo:

| Operación | Endpoint |
|-----------|----------|
| Listar | `GET /projects/:id/repository/tree?path=manifest&ref=main` |
| Leer | `GET /projects/:id/repository/files/:path/raw?ref=main` |
| Escribir | `POST /projects/:id/repository/commits` con varias `actions` |

El endpoint de commits acepta múltiples ficheros por llamada, así que **publicar una sesión es un
commit atómico** con el jsonl, sus subagentes, sus tool-results y su manifest. Autoría y auditoría
salen del propio git.

Listado incremental: se guarda el último commit sha visto; si no cambió, no se re-lista. Los
manifests leídos se cachean en el índice local.

### Cambios en módulos existentes

**`index.py`** — sin cambios. La idea original era cachear el listado remoto en tres columnas
(`origin`, `remote_author`, `remote_updated`) vía `_ensure_columns()`. No se hizo: ver
"Desviaciones del plan". Consecuencia asumida: las sesiones compartidas **no** entran en el
FTS global (`?`) hasta que se hidratan.

**`screens/sessions.py`** — dos bindings nuevos (`u` y `R` están libres):

| Tecla | Acción |
|-------|--------|
| `u` | Publicar la sesión bajo el cursor, o todas las marcadas con `space` |
| `R` | Alternar la visibilidad de las sesiones remotas en el listado |

**`screens/sessions.py`, acción `Enter`** — si la fila es remota: hidratar en `project_dir` y luego
`launch_claude(cwd, session_id=...)`, cuya firma ya sirve sin cambios.

**`config.py`** — `remote_kind` (`none` | `directory`) y `remote_path`. `none` por defecto: hay
que activarlo a mano. `$MULTI_CLAUDE_REMOTE_DIR` gana sobre el fichero, para poder probar sin
tocar estado compartido. Aún no son editables desde el modal de ajustes (`s`). Cuando exista
backend de API, su token **no** irá aquí.

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
| 2 | `GitLabRemote` + credenciales | pendiente |
| 3 | Publicar (`u`) sobre selección múltiple | **hecho** |
| 4 | Listado remoto (`R`) | **hecho** (sin columnas de índice, ver abajo) |
| 5 | Hidratar + `Enter` + aviso de divergencia | **hecho** |
| 6 | Fork en re-publicación (`--fork-session`) | pendiente |

### Desviaciones del plan, y por qué

**El orden se invirtió: la UI antes que `GitLabRemote`.** Con `DirectoryRemote` el MVP se
puede probar apuntando a una carpeta, sin token ni repo ni servidor. Empezar por GitLab
habría dejado la feature inservible hasta tener infraestructura.

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

## Cómo probarlo

```bash
mkdir -p /tmp/remoto-sesiones
MULTI_CLAUDE_REMOTE_DIR=/tmp/remoto-sesiones uv run multi-claude
```

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
