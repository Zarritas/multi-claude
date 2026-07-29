"""Which sessions repo each project publishes to.

A single global remote does not survive contact with real work: sessions about one client's
code should not land in the same repo as another's, and the whole reason to prefer a private
repo over a shared folder is that its permissions already express that. So a project can be
**linked** to its own sessions repo, and the global setting is only the fallback.

Links are keyed by the project's **normalised git remote URL**, not its path. Two consequences,
both wanted:

- Every worktree of a repo shares one link. ``repo``, ``repo/.claude/worktrees/x`` and a
  sibling checkout all have the same ``origin``, so linking one links them all — which is how
  the worktree grouping in :mod:`multi_claude.discovery` already treats them.
- ``git@host:group/repo.git`` and ``https://host/group/repo.git`` are the same key, because
  they are the same repository and nobody should have to link it twice.

Projects with no git remote fall back to being keyed by absolute path: still useful on one
machine, just not shared between checkouts.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

# Default API hosts per provider, so only self-hosted GitLab needs a URL typed in.
DEFAULT_REMOTE_HOSTS: dict[str, str] = {
    "gitlab": "https://gitlab.com",
    "github": "https://api.github.com",
}

_SCHEME_RE = re.compile(r"\A[a-zA-Z][a-zA-Z0-9+.-]*://")

_LINK_KINDS = ("directory", "gitlab", "github")


@dataclass(frozen=True)
class RemoteServer:
    """A configured host you can publish to: provider, URL and (elsewhere) its token.

    Split out from :class:`RemoteLink` because the two change at different rates. A company
    has one or two servers and a repo per client, so typing the host and pasting a token for
    every repo was busywork — and it meant the same credential lived in several places.

    Links refer to a server **by name** rather than copying its host, so correcting a URL or
    rotating a token fixes every repo pointing at it at once.
    """

    name: str
    kind: str = "gitlab"
    host: str = ""

    @property
    def api_host(self) -> str:
        return self.host or DEFAULT_REMOTE_HOSTS.get(self.kind, "")

    @property
    def is_configured(self) -> bool:
        return bool(self.name and self.kind in ("gitlab", "github") and self.api_host)

    def summary(self) -> str:
        host = self.api_host.replace("https://", "").replace("http://", "").rstrip("/")
        return f"{self.kind} · {host}"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "kind": self.kind, "host": self.host}

    @classmethod
    def from_dict(cls, raw: object) -> RemoteServer | None:
        if not isinstance(raw, dict):
            return None
        name, kind = raw.get("name"), raw.get("kind")
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(kind, str) or kind not in ("gitlab", "github"):
            return None
        return cls(name=name.strip(), kind=kind, host=_as_str(raw.get("host")).rstrip("/"))


@dataclass(frozen=True)
class RemoteLink:
    """Where sessions go: a backend plus whatever that backend needs to be reached.

    One value object shared by the global config, the settings modal and the per-project
    store, so "a remote" means the same thing everywhere and only has to be validated once.

    ``label`` is what the tab is called. It defaults to something derived from the repo or
    folder, because a project can be linked to several remotes and they need telling apart
    at a glance — "cliente-x" and "producto", not two identical cloud icons.
    """

    kind: str = "none"
    path: str = ""
    host: str = ""
    repo: str = ""
    branch: str = "main"
    label: str = ""
    # Name of a configured RemoteServer. When set, it supplies kind and host, and the link
    # only carries what is specific to the repo.
    server: str = ""

    def tab_label(self) -> str:
        """Short name for the tab: the explicit label, or one derived from the target."""
        if self.label:
            return self.label
        if self.kind == "directory":
            return Path(self.path).name or "carpeta"
        if self.repo:
            return self.repo.rstrip("/").rsplit("/", 1)[-1]
        return self.kind or "remoto"

    def resolved(self, servers: Sequence[RemoteServer]) -> RemoteLink:
        """This link with ``kind`` and ``host`` filled in from the server it names.

        A link naming a server that no longer exists resolves to ``kind="none"``: better
        inert, and visibly so, than silently publishing somewhere else.
        """
        if not self.server:
            return self
        for server in servers:
            if server.name == self.server:
                return replace(self, kind=server.kind, host=server.api_host)
        return replace(self, kind="none")

    def same_target(self, other: RemoteLink) -> bool:
        """Whether two links point at the same place, ignoring the label.

        Used to keep a project from being linked twice to one repo, which would show the
        same sessions under two tabs.
        """
        a, b = self.normalised(), other.normalised()
        return (a.kind, a.path, a.api_host, a.repo, a.branch) == (
            b.kind,
            b.path,
            b.api_host,
            b.repo,
            b.branch,
        )

    @property
    def is_configured(self) -> bool:
        """Whether this describes a usable remote, rather than "off" or half-filled."""
        if self.kind == "directory":
            return bool(self.path)
        if self.kind in ("gitlab", "github"):
            return bool(self.repo and self.api_host)
        return False

    @property
    def api_host(self) -> str:
        """The API base URL: the configured one, or the provider's default."""
        return self.host or DEFAULT_REMOTE_HOSTS.get(self.kind, "")

    def summary(self) -> str:
        """One-line description for the settings and project screens."""
        if self.kind == "none":
            return "desactivado"
        if self.kind == "directory":
            return f"carpeta · {self.path or '(sin ruta)'}"
        host = self.api_host.replace("https://", "").replace("http://", "").rstrip("/")
        return f"{self.kind} · {host}/{self.repo or '(sin repo)'}"

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": self.path,
            "host": self.host,
            "repo": self.repo,
            "branch": self.branch,
            "label": self.label,
            "server": self.server,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RemoteLink | None:
        """Parse a stored link. Returns None for anything unusable, never raises."""
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        server = _as_str(raw.get("server"))
        # A link that names a server does not carry its own kind: that comes from the server
        # when the link is resolved, so an empty kind here is expected rather than broken.
        if not server and (not isinstance(kind, str) or kind not in _LINK_KINDS):
            return None
        return cls(
            kind=kind if isinstance(kind, str) else "",
            path=_as_str(raw.get("path")),
            host=_as_str(raw.get("host")),
            repo=_as_str(raw.get("repo")),
            branch=_as_str(raw.get("branch")) or "main",
            label=_as_str(raw.get("label")),
            server=_as_str(raw.get("server")),
        )

    def normalised(self) -> RemoteLink:
        """Trim the shapes that would otherwise produce doubled-up URLs."""
        return replace(
            self,
            path=self.path.strip(),
            host=self.host.strip().rstrip("/"),
            repo=self.repo.strip().strip("/"),
            branch=self.branch.strip() or "main",
            label=self.label.strip(),
            server=self.server.strip(),
        )


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def normalize_git_remote(url: str | None) -> str | None:
    """Reduce a git remote URL to a stable ``host/path`` key, or None.

    ``git@host:group/repo.git``, ``https://host/group/repo.git`` and
    ``ssh://git@host:22/group/repo`` all collapse to ``host/group/repo``: the same repository
    reached three ways must not become three different links.
    """
    if not url:
        return None
    text = url.strip()
    if not text:
        return None
    if _SCHEME_RE.match(text):
        without_scheme = text.split("://", 1)[1]
    elif "@" in text and ":" in text.split("@", 1)[1]:
        # scp-like syntax: git@host:group/repo.git
        host_part, path_part = text.split("@", 1)[1].split(":", 1)
        without_scheme = f"{host_part}/{path_part}"
    else:
        without_scheme = text
    without_creds = without_scheme.split("@", 1)[-1]
    if "/" not in without_creds:
        return None
    host, _, path = without_creds.partition("/")
    host = host.split(":", 1)[0].lower()  # drop any port
    path = path.strip("/").removesuffix(".git")
    if not host or not path:
        return None
    return f"{host}/{path}"


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "multi-claude" / "project-remotes.json"


class ProjectRemotesStore:
    """File-backed map of ``project key -> [RemoteLink, ...]``.

    A list, not a single link: a project can publish to several sessions repos at once —
    one per client, one for product work — and each becomes a tab in the sessions listing.
    Order is preserved, because it is the order of the tabs.

    Tolerant of a missing or corrupt file (treated as empty) and written atomically, like the
    other stores. Keys come from :func:`normalize_git_remote` when the project is in a repo
    with an ``origin``, and from its absolute path otherwise.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._data: dict[str, list[RemoteLink]] | None = None

    def _load(self) -> dict[str, list[RemoteLink]]:
        if self._data is not None:
            return self._data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            raw = {}
        data: dict[str, list[RemoteLink]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(key, str):
                    continue
                # A bare object is the single-link shape this file used to have; accepting it
                # means an existing file keeps working instead of silently losing its links.
                entries = value if isinstance(value, list) else [value]
                links = [link for link in (RemoteLink.from_dict(e) for e in entries) if link]
                if links:
                    data[key] = links
        self._data = data
        return data

    def reload(self) -> None:
        self._data = None
        self._load()

    def get(self, key: str | None) -> tuple[RemoteLink, ...]:
        """Every remote linked to ``key``, in tab order. Empty when there is no link."""
        if not key:
            return ()
        return tuple(self._load().get(key, ()))

    def add(self, key: str, link: RemoteLink) -> tuple[RemoteLink, ...]:
        """Link ``key`` to one more remote, replacing any link to the same target.

        Re-adding a target updates it in place rather than appending a duplicate, so the
        same repo can never appear under two tabs.
        """
        clean = link.normalised()
        data = self._load()
        existing = list(data.get(key, ()))
        for index, current in enumerate(existing):
            if current.same_target(clean):
                existing[index] = clean
                break
        else:
            existing.append(clean)
        data[key] = existing
        self._write(data)
        return tuple(existing)

    def remove(self, key: str, link: RemoteLink) -> tuple[RemoteLink, ...]:
        """Unlink one remote from ``key``. Removing the last one drops the key entirely."""
        data = self._load()
        remaining = [c for c in data.get(key, ()) if not c.same_target(link)]
        if remaining:
            data[key] = remaining
        else:
            data.pop(key, None)
        self._write(data)
        return tuple(remaining)

    def set_all(self, key: str, links: list[RemoteLink]) -> None:
        data = self._load()
        if links:
            data[key] = [link.normalised() for link in links]
        else:
            data.pop(key, None)
        self._write(data)

    def all(self) -> dict[str, tuple[RemoteLink, ...]]:
        return {key: tuple(links) for key, links in self._load().items()}

    def _write(self, data: dict[str, list[RemoteLink]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(
            prefix=".project-remotes.", suffix=".tmp", dir=str(self.path.parent)
        )
        tmp = Path(tmp_str)
        try:
            payload = {key: [link.to_dict() for link in links] for key, links in data.items()}
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
