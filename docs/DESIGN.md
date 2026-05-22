# Diseño técnico

Este documento extiende el README con detalles que no caben en una visión general.

## Decisiones cerradas

| Tema                          | Decisión                                                                 |
|-------------------------------|--------------------------------------------------------------------------|
| Stack                         | Python 3.10+ con Textual                                                 |
| Lanzamiento de `claude`       | Auto-detect tmux → zellij → suspend                                      |
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

## Launcher: matriz de casos

| Entorno              | Acción                                                           |
|----------------------|------------------------------------------------------------------|
| `$TMUX` set          | `tmux split-window -h -c <cwd> claude [--resume <id>]`           |
| `$ZELLIJ` set        | `zellij action new-pane --cwd <cwd> -- claude [--resume <id>]`   |
| Ninguno              | `app.suspend()` + `subprocess.run([...], cwd=cwd)`               |
| `claude` no en PATH  | `LauncherError("claude no encontrado en PATH")` y notify         |

Edge case: `$TMUX` está set pero el binario `tmux` no existe (raro pero posible) → fallback a suspend.

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
