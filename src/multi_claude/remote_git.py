"""A sessions repo reached with plain ``git`` over SSH.

The API drivers need a personal access token per person and per host. SSH needs nothing new:
a company that deploys keys already has the credential distributed, the key never leaves the
machine, and revoking access is something the git host already knows how to do. For many teams
that is the difference between "we can try this next week" and "someone has to mint and
distribute tokens first".

The tradeoff is a working copy. This keeps a bare-ish clone under the cache dir, fetches before
reading and pushes after writing, which buys something the REST drivers do not have:
**git resolves the race**. Two people publishing at the same moment produce a rejected push,
and a rebase-and-retry lands both, instead of one silently overwriting the other's manifest.

Layout on the remote is identical to every other backend (see :mod:`multi_claude.remote`), so a
session published over SSH is byte-identical to the same session published over the API, and a
repo can be read either way.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

from multi_claude.project_remotes import RemoteLink
from multi_claude.remote import (
    BLOB_ROOT,
    MAIN_BLOB,
    MANIFEST_ROOT,
    FetchResult,
    RemoteError,
    RemoteSession,
    blob_name_for,
    collect_session_files,
    is_compressed_blob,
    local_path_for,
    safe_session_id,
)

# Long enough for a clone of a repo full of transcripts on a slow link, short enough that a
# wrong host fails while the user is still watching.
_TIMEOUT = 120
_PUSH_ATTEMPTS = 3
_COMMIT_MESSAGE = "multi-claude: publish session"


def cache_root() -> Path:
    """Where working copies live. Cache, not config: they are rebuildable."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "multi-claude" / "repos"


class GitSshRemote:
    """Sessions in a git repo, reached over SSH with the user's own keys."""

    def __init__(self, link: RemoteLink, *, cache_dir: Path | None = None) -> None:
        self.link = link
        self.url = link.git_url()
        self.branch = link.branch or "main"
        root = cache_dir or cache_root()
        # One working copy per repo+branch, named after the URL so two links to the same repo
        # on different branches do not fight over one checkout.
        slug = "".join(c if c.isalnum() or c in "-_." else "-" for c in f"{self.url}-{self.branch}")
        self.work = root / slug

    def __str__(self) -> str:
        return f"{self.url} ({self.branch})"

    # --- git plumbing ---------------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        """Run git, turning any failure into a :class:`RemoteError` a user can act on."""
        env = {
            **os.environ,
            # Never prompt: a hung credential prompt in a TUI worker is invisible and looks
            # like a freeze. Failing with a message is strictly better.
            "GIT_TERMINAL_PROMPT": "0",
            # Pin the locale: git translates its errors, so on a Spanish system "repository
            # does not exist" arrives as "el repositorio no existe" and pattern-matching on it
            # silently stops working — leaving the user with raw git stderr.
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_SSH_COMMAND": os.environ.get(
                "GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
            ),
        }
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                check=False,
                timeout=_TIMEOUT,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RemoteError("git no está en el PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(f"git tardó demasiado contra {self.url}") from exc
        if result.returncode != 0:
            raise RemoteError(_git_message(result.stderr, self.url))
        return result.stdout

    def _ensure_clone(self) -> None:
        """Make sure the working copy exists and is on the right branch."""
        if (self.work / ".git").is_dir():
            return
        self.work.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.work, ignore_errors=True)
        try:
            self._git("clone", "--quiet", "--branch", self.branch, self.url, str(self.work))
        except RemoteError:
            # A brand-new sessions repo has no branch yet, so cloning it by name fails. Clone
            # whatever is there and create the branch locally; the first push publishes it.
            self._git("clone", "--quiet", self.url, str(self.work))
            self._git("checkout", "-B", self.branch, cwd=self.work)

    def _fetch(self) -> None:
        self._ensure_clone()
        self._git("fetch", "--quiet", "origin", cwd=self.work)
        # Discard anything local: the working copy is a cache, and the remote is the truth.
        # Failure means the branch is not on the remote yet, so there is nothing to reset to.
        with contextlib.suppress(RemoteError):
            self._git("reset", "--hard", f"origin/{self.branch}", cwd=self.work)

    # --- RemoteStore ----------------------------------------------------------------

    def check_connection(self) -> str:
        """Verify the URL and the key without cloning anything."""
        output = self._git("ls-remote", "--heads", self.url)
        branches = [line.split("refs/heads/")[-1] for line in output.splitlines() if line.strip()]
        if self.branch in branches:
            return f"OK · {self.url} · rama {self.branch}"
        if branches:
            return f"OK · {self.url} · la rama {self.branch} aún no existe (se creará)"
        return f"OK · {self.url} · repositorio vacío"

    def list_sessions(self) -> tuple[RemoteSession, ...]:
        self._fetch()
        manifest_dir = self.work / MANIFEST_ROOT
        if not manifest_dir.is_dir():
            return ()
        sessions: list[RemoteSession] = []
        for path in sorted(manifest_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # one bad manifest must not hide the rest
            try:
                sessions.append(RemoteSession.from_manifest(raw))
            except RemoteError:
                continue
        return tuple(sessions)

    def get_session(self, session_id: str) -> RemoteSession | None:
        self._fetch()
        path = self.work / MANIFEST_ROOT / f"{safe_session_id(session_id)}.json"
        try:
            return RemoteSession.from_manifest(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, RemoteError):
            return None

    def fetch(self, session_id: str, dest_dir: Path) -> FetchResult:
        safe_session_id(session_id)
        if (dest_dir / f"{session_id}.jsonl").exists():
            raise RemoteError(f"la sesión {session_id} ya existe en destino")
        self._fetch()
        blob_dir = self.work / BLOB_ROOT / session_id
        if not blob_dir.is_dir():
            raise RemoteError(f"la sesión {session_id} no está en el remoto")
        if not (blob_dir / MAIN_BLOB).is_file():
            raise RemoteError(f"la sesión {session_id} no tiene transcript en el remoto")

        written: list[Path] = []
        for blob in sorted(blob_dir.rglob("*")):
            if not blob.is_file():
                continue
            name = blob.relative_to(blob_dir).as_posix()
            target = local_path_for(dest_dir, session_id, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = blob.read_bytes()
            target.write_bytes(gzip.decompress(payload) if is_compressed_blob(name) else payload)
            written.append(target)
        return FetchResult(session_id=session_id, written=tuple(written))

    def publish(self, session: RemoteSession, project_dir: Path) -> None:
        files = collect_session_files(project_dir, session.session_id)
        if not files:
            raise RemoteError(f"la sesión {session.session_id} no tiene transcript en disco")
        self._fetch()

        blob_dir = self.work / BLOB_ROOT / session.session_id
        for path in files:
            name = blob_name_for(project_dir, session.session_id, path)
            target = blob_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = path.read_bytes()
            target.write_bytes(gzip.compress(payload) if is_compressed_blob(name) else payload)

        manifest = self.work / MANIFEST_ROOT / f"{session.session_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(session.to_manifest(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

        self._commit_and_push(session.session_id)

    def _commit_and_push(self, session_id: str) -> None:
        """Commit the staged session and push, rebasing if someone else got there first.

        This is what SSH buys over the REST drivers: a concurrent publish is a rejected push,
        and retrying on top of their commit keeps both sessions instead of overwriting one.
        """
        self._git("add", "--", MANIFEST_ROOT, BLOB_ROOT, cwd=self.work)
        status = self._git("status", "--porcelain", cwd=self.work)
        if not status.strip():
            return  # already published, byte for byte
        self._git("commit", "--quiet", "-m", f"{_COMMIT_MESSAGE} {session_id}", cwd=self.work)

        last: RemoteError | None = None
        for attempt in range(_PUSH_ATTEMPTS):
            try:
                self._git("push", "--quiet", "origin", f"HEAD:{self.branch}", cwd=self.work)
                return
            except RemoteError as exc:
                last = exc
                if attempt == _PUSH_ATTEMPTS - 1:
                    break
                try:
                    self._git(
                        "pull", "--rebase", "--quiet", "origin", self.branch, cwd=self.work
                    )
                except RemoteError:
                    break  # not a race: a real failure, report the push error
        detail = f": {last}" if last else ""
        raise RemoteError(f"no se pudo publicar en {self.url}{detail}")


def _git_message(stderr: str, url: str) -> str:
    """Turn git's stderr into one actionable line.

    Auth and host problems are the likely ones, and git's own wording ("Permission denied
    (publickey)") does not tell a user of this tool what to do about it.
    """
    text = " ".join(stderr.split())
    lowered = text.lower()
    if "permission denied" in lowered or "could not read from remote" in lowered:
        return f"la clave SSH no tiene acceso a {url} (comprueba tu clave y los permisos del repo)"
    if "host key verification failed" in lowered:
        return f"la clave del host no está aceptada para {url}"
    if "could not resolve hostname" in lowered:
        return f"no se pudo resolver el host de {url}"
    if (
        "repository not found" in lowered
        or "does not exist" in lowered
        or "does not appear to be a git repository" in lowered
    ):
        return f"{url} no existe o no es un repositorio"
    return text or f"git falló contra {url}"
