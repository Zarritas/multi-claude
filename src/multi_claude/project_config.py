"""The sessions repos a project declares **in its own repository**, for the whole team.

The per-project links in :mod:`multi_claude.project_remotes` live in each person's config,
which means the shared archive had single-player onboarding: the second colleague to arrive
had to be told which repo to link, find `L`, pick the provider and not mistype it. A repo
that declares its own sessions repo removes that step entirely — clone, run multi-claude,
and the team's tab is already there.

**This file is untrusted input.** It is versioned, so anyone who can push to the project can
change it, and what it configures is *where sessions get published*. One rule keeps that from
being a way to exfiltrate transcripts:

    The repo says **which repository**. You say **which server**, and hold the credential.

So an entry may only name a ``server`` that already exists in the reader's own config, and is
refused if it tries to carry its own ``host``, ``kind`` or ``path``. A file naming a server
you have not configured resolves to nothing rather than to somewhere unexpected — the same
inert-and-visible outcome :meth:`RemoteLink.resolved` already gives a link whose server was
deleted. Local folders (``kind: directory``) are refused outright: a path is specific to one
machine, so versioning it means nothing, and honouring one would turn a file in a repository
into an arbitrary write on every reader's disk.

Nothing here is a substitute for the publish confirmation: pressing ``u`` still shows what
goes up and where. This decides which tabs exist, not what leaves the machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from multi_claude.project_remotes import RemoteLink

#: Read from the root of the project's working tree, next to its own config.
PROJECT_CONFIG_NAME = ".multi-claude.json"

#: A repo declaring more than a handful of sessions repos is a mistake or an attempt to
#: bury the real one in a wall of tabs. The listing's tab bar has to stay readable.
MAX_DECLARED_LINKS = 8

#: The file is read on every project open, so a malformed or hostile one must not be able
#: to cost more than a stat. Any real declaration is a few hundred bytes.
MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProjectConfig:
    """What a project's own repository declares, plus why anything was refused.

    ``problems`` is not decoration: a declaration that is quietly dropped looks exactly like
    a repo that declares nothing, and the person who has to fix it is usually not the person
    reading the screen. They are shown in the link manager (`L`), where the fix happens.
    """

    links: tuple[RemoteLink, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.links and not self.problems


def config_path(cwd: Path) -> Path:
    return Path(cwd) / PROJECT_CONFIG_NAME


def read_project_config(cwd: Path | str | None) -> ProjectConfig:
    """Read ``.multi-claude.json`` from a project's working tree.

    Never raises and never partially fails: an unreadable, oversized or malformed file is a
    project that declares nothing, with a problem recorded saying so.
    """
    if not cwd:
        return ProjectConfig()
    path = config_path(Path(cwd))
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            return ProjectConfig(problems=(f"{PROJECT_CONFIG_NAME} es demasiado grande",))
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ProjectConfig()
    except OSError:
        return ProjectConfig(problems=(f"no se pudo leer {PROJECT_CONFIG_NAME}",))
    except json.JSONDecodeError as exc:
        return ProjectConfig(problems=(f"{PROJECT_CONFIG_NAME} no es JSON válido: {exc.msg}",))
    return parse_project_config(raw)


def parse_project_config(raw: object) -> ProjectConfig:
    """Validate a decoded ``.multi-claude.json``. Pure: no disk, no config, no network."""
    if not isinstance(raw, dict):
        return ProjectConfig(problems=(f"{PROJECT_CONFIG_NAME} debe ser un objeto JSON",))
    declared = raw.get("sessions_repos")
    if declared is None:
        return ProjectConfig()
    if not isinstance(declared, list):
        return ProjectConfig(problems=("`sessions_repos` debe ser una lista",))

    links: list[RemoteLink] = []
    problems: list[str] = []
    for index, entry in enumerate(declared[:MAX_DECLARED_LINKS]):
        link, problem = _parse_entry(entry, index)
        if problem:
            problems.append(problem)
        elif link is not None:
            # Same rule as the local store: one repo cannot become two tabs.
            if any(link.same_target(existing) for existing in links):
                problems.append(f"entrada {index + 1}: repetida, se ignora")
                continue
            links.append(link)
    if len(declared) > MAX_DECLARED_LINKS:
        problems.append(
            f"solo se leen los primeros {MAX_DECLARED_LINKS} de `sessions_repos` "
            f"({len(declared)} declarados)"
        )
    return ProjectConfig(links=tuple(links), problems=tuple(problems))


def _parse_entry(entry: object, index: int) -> tuple[RemoteLink | None, str]:
    """One declaration into a link, or a reason it was refused."""
    where = f"entrada {index + 1}"
    if not isinstance(entry, dict):
        return None, f"{where}: debe ser un objeto"

    # The rule the whole file rests on. Carrying a host would let a repository choose the
    # server your sessions are published to; naming one you configured cannot.
    for forbidden, why in (
        ("host", "el host lo pone tu configuración, no el repo"),
        ("kind", "el tipo lo pone el servidor que nombres"),
        ("path", "una carpeta local no se puede versionar"),
    ):
        if entry.get(forbidden) not in (None, ""):
            return None, f"{where}: `{forbidden}` no está permitido aquí — {why}"

    server = entry.get("server")
    if not isinstance(server, str) or not server.strip():
        return None, f"{where}: falta `server` (el nombre de un servidor de tu configuración)"
    repo = entry.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return None, f"{where}: falta `repo`"

    branch = entry.get("branch")
    label = entry.get("label")
    return (
        RemoteLink(
            server=server.strip(),
            repo=repo.strip().strip("/"),
            branch=branch.strip() if isinstance(branch, str) and branch.strip() else "main",
            label=label.strip() if isinstance(label, str) else "",
        ).normalised(),
        "",
    )


class ProjectConfigReader:
    """Reads and caches ``.multi-claude.json`` per working tree, keyed by mtime.

    The links are asked for on every open of a project's listing and on every rescan, so
    this must cost a ``stat`` and not a parse. Keyed by mtime rather than cached forever
    because the file is versioned: a ``git pull`` that changes it has to take effect without
    restarting the TUI.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, int, ProjectConfig]] = {}

    def read(self, cwd: Path | str | None) -> ProjectConfig:
        if not cwd:
            return ProjectConfig()
        path = config_path(Path(cwd))
        try:
            stat = path.stat()
        except OSError:
            self._cache.pop(str(path), None)
            return ProjectConfig()
        cached = self._cache.get(str(path))
        if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        config = read_project_config(cwd)
        self._cache[str(path)] = (stat.st_mtime, stat.st_size, config)
        return config

    def clear(self) -> None:
        self._cache.clear()
