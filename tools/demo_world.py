"""The synthetic machine that the README screenshots and the demo GIF are recorded on.

Shared so both show the same scenario: three projects, a couple of sessions running, a
sessions repository two colleagues publish to. Everything is invented on purpose
(`/tmp/multi-claude-demo`, `ana@example.com`) — the docs must not leak real paths, client
names or colleagues' addresses.

The environment variables have to be set before ``multi_claude`` is imported, which is why
the imports live inside :func:`build` instead of at module level. Call it first, then
import whatever you need.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path("/tmp/multi-claude-demo")


@dataclass(frozen=True)
class DemoWorld:
    """Where the fake machine lives, plus the live-session registry to patch in."""

    root: Path
    projects: Path
    api: Path
    remote: Path
    live: dict[str, Any]


def build() -> DemoWorld:
    """Create the world from scratch and return its handles. Wipes any previous run."""
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    WORLD = ROOT
    PROJECTS = WORLD / "projects"
    PROJECTS.mkdir(parents=True)

    os.environ["XDG_CONFIG_HOME"] = str(WORLD / "config")
    os.environ["XDG_DATA_HOME"] = str(WORLD / "data")

    from multi_claude import discovery as discovery_module
    from multi_claude.focus import LiveSession
    from multi_claude.index import default_index, reset_default_index_for_tests
    from multi_claude.remote import REMOTE_DIR_ENV, DirectoryRemote, RemoteSession
    from multi_claude.session import scan_sessions

    discovery_module.CLAUDE_PROJECTS_DIR = PROJECTS
    reset_default_index_for_tests()

    NOW = time.time()
    HOUR = 3600
    DAY = 86400

    def _turn(role: str, text: str, sid: str) -> dict:
        return {
            "type": role,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
            "sessionId": sid,
        }

    def session(
        project_dir: Path,
        sid: str,
        cwd: str,
        *,
        branch: str,
        prompt: str,
        name: str | None = None,
        msgs: int = 40,
        age: float = HOUR,
        tail: tuple[tuple[str, str], ...] = (),
    ) -> None:
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{sid}.jsonl"
        events: list[dict] = [
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
                "cwd": cwd,
                "gitBranch": branch,
                "sessionId": sid,
            }
        ]
        if name:
            events.append({"type": "ai-title", "aiTitle": name, "sessionId": sid})
        events += [{"type": "assistant", "seq": i, "sessionId": sid} for i in range(msgs)]
        # Real text turns at the end, which is what the preview panel renders.
        events += [_turn(role, text, sid) for role, text in tail]
        with path.open("w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        stamp = NOW - age
        os.utime(path, (stamp, stamp))

    # --- a believable machine ---------------------------------------------------

    API = WORLD / "proyectos" / "tienda-api"
    WEB = WORLD / "proyectos" / "tienda-web"
    INFRA = WORLD / "proyectos" / "infra"
    for repo in (API, WEB, INFRA):
        repo.mkdir(parents=True)

    session(
        PROJECTS / "-tienda-api",
        "a1b2c3d4-1111-4111-8111-000000000001",
        str(API),
        branch="fix/checkout-502",
        prompt="el checkout devuelve 502 cuando el carrito tiene más de 40 líneas",
        name="El checkout devuelve 502 con carritos grandes",
        msgs=312,
        age=8 * 60,
        tail=(
            (
                "user",
                "sigue cayéndose con el carrito de 47 líneas, pero solo detrás del proxy",
            ),
            (
                "assistant",
                "Entonces no es la aplicación: gunicorn responde en 1,8 s y el 502 lo escribe "
                "nginx a los 60 s exactos. El `proxy_read_timeout` del vhost está en el valor "
                "por defecto y el listado con 47 líneas lo cruza. Subo el timeout del bloque "
                "`location /checkout` y añado un test que serialice ese carrito.",
            ),
            ("user", "hazlo, pero deja el timeout en 120 y no en 300"),
        ),
    )
    session(
        PROJECTS / "-tienda-api",
        "a1b2c3d4-1111-4111-8111-000000000002",
        str(API),
        branch="main",
        prompt="migrar el envío de facturas a la cola de trabajos",
        name="Migrar el envío de facturas a la cola",
        msgs=178,
        age=2 * HOUR,
    )
    session(
        PROJECTS / "-tienda-api",
        "a1b2c3d4-1111-4111-8111-000000000003",
        str(API),
        branch="fix/checkout-502",
        prompt="añadir índice a pedido_linea, que el listado tarda 9s",
        name="Índice para el listado de líneas de pedido",
        msgs=64,
        age=3 * DAY,
    )
    session(
        PROJECTS / "-tienda-web",
        "b2c3d4e5-2222-4222-8222-000000000001",
        str(WEB),
        branch="feat/dark-mode",
        prompt="el tema oscuro deja ilegibles los botones secundarios",
        name="Tema oscuro: contraste de botones secundarios",
        msgs=95,
        age=40 * 60,
    )
    session(
        PROJECTS / "-tienda-web",
        "b2c3d4e5-2222-4222-8222-000000000002",
        str(WEB),
        branch="main",
        prompt="por qué el bundle pasó de 400KB a 1.2MB en la última release",
        name="El bundle creció 3x en una release",
        msgs=221,
        age=5 * DAY,
    )
    session(
        PROJECTS / "-infra",
        "c3d4e5f6-3333-4333-8333-000000000001",
        str(INFRA),
        branch="main",
        prompt="nginx corta las respuestas largas del exportador a los 60s",
        name="nginx corta el exportador a los 60s",
        msgs=140,
        age=6 * HOUR,
    )

    app_tags = WORLD / "config" / "multi-claude"
    app_tags.mkdir(parents=True, exist_ok=True)
    (app_tags / "session-tags.json").write_text(
        json.dumps(
            {
                "a1b2c3d4-1111-4111-8111-000000000001": ["bug", "urgente"],
                "a1b2c3d4-1111-4111-8111-000000000003": ["rendimiento"],
                "c3d4e5f6-3333-4333-8333-000000000001": ["infra"],
            }
        ),
        encoding="utf-8",
    )

    # --- a sessions repository the team shares ---------------------------------

    REMOTE = WORLD / "sessions-repo"
    # Deliberately NOT via MULTI_CLAUDE_REMOTE_DIR: that global override wins over a
    # project's own links and carries no label, so the tab would read "sessions-repo".
    os.environ.pop(REMOTE_DIR_ENV, None)
    store = DirectoryRemote(REMOTE)
    for sid, who, name, branch, msgs, tags in (
        (
            "d4e5f607-4444-4444-8444-000000000001",
            "ana@example.com",
            "El pool de conexiones se agota en el pico de las 9",
            "fix/db-pool",
            260,
            ("infra",),
        ),
        (
            "d4e5f607-4444-4444-8444-000000000002",
            "carlos@example.com",
            "Migración de precios: redondeo a 2 decimales",
            "feat/precios",
            141,
            ("datos",),
        ),
        (
            "d4e5f607-4444-4444-8444-000000000003",
            "ana@example.com",
            "nginx: proxy_read_timeout para el exportador",
            "main",
            88,
            ("infra", "nginx"),
        ),
    ):
        src = WORLD / f"otro-{sid[-1]}"
        session(src, sid, "/home/otra-persona/tienda-api", branch=branch, prompt=name, msgs=msgs)
        store.publish(
            RemoteSession(
                session_id=sid,
                published_at="2026-08-01T09:12:00+00:00",
                published_by=who,
                branch=branch,
                display_name=name,
                first_prompt=name,
                tags=tags,
                message_count=msgs,
                size_bytes=msgs * 3200,
            ),
            src,
        )

    # Link the api project to that repository, keyed by its origin (no git here → path).
    CONFIG_DIR = WORLD / "config" / "multi-claude"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # The preview panel halves the table's width, so it is off for the listing shots and
    # turned on with `p` for its own one.
    (CONFIG_DIR / "config.json").write_text(
        json.dumps({"default_mode": "auto", "preview_visible": False}), encoding="utf-8"
    )

    links = CONFIG_DIR / "project-remotes.json"
    links.write_text(
        json.dumps(
            {
                str(API): [
                    {
                        "kind": "directory",
                        "path": str(REMOTE),
                        "label": "sesiones-tienda",
                        "branch": "main",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    for entry in sorted(PROJECTS.iterdir()):
        if entry.is_dir():
            scan_sessions(entry, index=default_index())
    LIVE = {
        "a1b2c3d4-1111-4111-8111-000000000001": LiveSession(
            session_id="a1b2c3d4-1111-4111-8111-000000000001", pid=4242, status="busy"
        ),
        "a1b2c3d4-1111-4111-8111-000000000002": LiveSession(
            session_id="a1b2c3d4-1111-4111-8111-000000000002", pid=4243, status="waiting"
        ),
        "b2c3d4e5-2222-4222-8222-000000000001": LiveSession(
            session_id="b2c3d4e5-2222-4222-8222-000000000001", pid=4245, status="waiting"
        ),
        "c3d4e5f6-3333-4333-8333-000000000001": LiveSession(
            session_id="c3d4e5f6-3333-4333-8333-000000000001", pid=4244, status="busy"
        ),
    }

    return DemoWorld(root=WORLD, projects=PROJECTS, api=API, remote=REMOTE, live=LIVE)
