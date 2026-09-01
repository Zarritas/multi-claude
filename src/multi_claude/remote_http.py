"""GitLab- and GitHub-backed session stores, over their REST APIs.

Sessions live as files in a private repo, which buys the thing that is expensive to build
and easy to get wrong: **access control and an audit trail you already trust**. Whoever can
read the repo can read the sessions, the provider's SSO decides that, and every publish is
a commit with an author.

Both providers reduce to the same four primitives — list a directory, read a file, write a
file (creating or updating), and identify the repo — so :class:`HttpRepoRemote` holds the
shared behaviour and each subclass supplies endpoints and auth. The on-remote layout, the
gzip and the manifest-last ordering all come from :mod:`multi_claude.remote`, so a session
published to a directory and one published to GitLab are byte-identical.

No new dependency: ``urllib`` from the stdlib, in keeping with the rest of the project.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from multi_claude.remote import (
    BLOB_ROOT,
    MAIN_BLOB,
    MANIFEST_ROOT,
    FetchResult,
    RemoteError,
    RemoteSession,
    blob_name_for,
    collect_session_files,
    decode_search_payload,
    is_compressed_blob,
    local_path_for,
    safe_session_id,
    search_blob_name,
    search_payload_for,
    with_search_size,
)

_TIMEOUT = 15
_PER_PAGE = 100
_COMMIT_MESSAGE = "multi-claude: publish session"
_DELETE_MESSAGE = "multi-claude: unpublish session"


class HttpRepoRemote:
    """A git-hosting provider used as a session store, over its REST API."""

    def __init__(self, host: str, repo: str, branch: str, token: str | None) -> None:
        self.host = host.rstrip("/")
        self.repo = repo.strip("/")
        self.branch = branch or "main"
        self._token = token

    def __str__(self) -> str:
        return f"{self.host}/{self.repo} ({self.branch})"

    # --- provider hooks -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _list_dir(self, dir_path: str, *, recursive: bool) -> list[str]:
        """Return repo-relative paths of the files under ``dir_path``."""
        raise NotImplementedError

    def _read_file(self, file_path: str) -> bytes:
        raise NotImplementedError

    def _write_file(self, file_path: str, payload: bytes) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable identity of the repo, used by the connection test."""
        raise NotImplementedError

    def _delete_file(self, file_path: str) -> None:
        raise NotImplementedError

    # --- shared HTTP ----------------------------------------------------------------

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        """Perform one API call, translating every failure into :class:`RemoteError`.

        A 404 is *not* special-cased here: callers decide whether a missing path means
        "empty" (no sessions published yet) or a real error.
        """
        if not self._token:
            raise RemoteError("falta el token de acceso (configúralo en Ajustes)")
        headers = {**self._headers(), **(extra_headers or {})}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise RemoteError(_http_message(exc)) from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"no se pudo conectar con {self.host}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RemoteError(f"tiempo de espera agotado contra {self.host}") from exc

    def _get_json(self, url: str) -> object:
        raw = self._request(url)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RemoteError(f"respuesta no-JSON de {self.host}") from exc

    # --- RemoteStore ----------------------------------------------------------------

    def check_connection(self) -> str:
        """Verify host, repo and token in one call. Returns a line to show the user."""
        identity = self.describe()
        published = len(self._safe_list(MANIFEST_ROOT, recursive=False))
        return f"OK · {identity} · {published} sesión(es) publicada(s)"

    def _safe_list(self, dir_path: str, *, recursive: bool) -> list[str]:
        """List ``dir_path``, treating "not found" as empty rather than as a failure.

        A brand-new sessions repo has no ``manifest/`` dir until the first publish, and
        that must read as "nothing shared yet", not as a broken remote.
        """
        try:
            return self._list_dir(dir_path, recursive=recursive)
        except RemoteError as exc:
            if "404" in str(exc):
                return []
            raise

    def list_sessions(self) -> tuple[RemoteSession, ...]:
        sessions: list[RemoteSession] = []
        for path in sorted(self._safe_list(MANIFEST_ROOT, recursive=False)):
            if not path.endswith(".json"):
                continue
            try:
                raw = json.loads(self._read_file(path))
            except (RemoteError, json.JSONDecodeError):
                continue  # one unreadable manifest must not hide the rest
            try:
                sessions.append(RemoteSession.from_manifest(raw))
            except RemoteError:
                continue
        return tuple(sessions)

    def get_session(self, session_id: str) -> RemoteSession | None:
        path = f"{MANIFEST_ROOT}/{safe_session_id(session_id)}.json"
        try:
            raw = json.loads(self._read_file(path))
            return RemoteSession.from_manifest(raw)
        except (RemoteError, json.JSONDecodeError):
            return None

    def fetch(self, session_id: str, dest_dir: Path) -> FetchResult:
        safe_session_id(session_id)
        if (dest_dir / f"{session_id}.jsonl").exists():
            raise RemoteError(f"la sesión {session_id} ya existe en destino")
        prefix = f"{BLOB_ROOT}/{session_id}"
        blobs = self._safe_list(prefix, recursive=True)
        if not blobs:
            raise RemoteError(f"la sesión {session_id} no está en el remoto")
        if f"{prefix}/{MAIN_BLOB}" not in blobs:
            raise RemoteError(f"la sesión {session_id} no tiene transcript en el remoto")

        written: list[Path] = []
        for path in sorted(blobs):
            name = path[len(prefix) + 1 :]
            target = local_path_for(dest_dir, session_id, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = self._read_file(path)
            target.write_bytes(gzip.decompress(payload) if is_compressed_blob(name) else payload)
            written.append(target)
        return FetchResult(session_id=session_id, written=tuple(written))

    def publish(self, session: RemoteSession, project_dir: Path) -> None:
        files = collect_session_files(project_dir, session.session_id)
        if not files:
            raise RemoteError(f"la sesión {session.session_id} no tiene transcript en disco")
        for path in files:
            name = blob_name_for(project_dir, session.session_id, path)
            payload = path.read_bytes()
            self._write_file(
                f"{BLOB_ROOT}/{session.session_id}/{name}",
                # mtime=0 keeps it byte-identical across publishes; see remote.py.
                gzip.compress(payload, mtime=0) if is_compressed_blob(name) else payload,
            )
        # The search payload: one small blob that lets colleagues search this session's
        # content without downloading it.
        search = search_payload_for(project_dir, session.session_id)
        if search is not None:
            self._write_file(search_blob_name(session.session_id), search)
        # Manifest last: a publish interrupted halfway leaves unreferenced blobs, which
        # are invisible to listing, rather than a manifest pointing at a partial session.
        self._write_file(
            f"{MANIFEST_ROOT}/{session.session_id}.json",
            json.dumps(
                with_search_size(session, search).to_manifest(), indent=2, ensure_ascii=False
            ).encode("utf-8"),
        )

    def fetch_search_text(self, session_id: str) -> str | None:
        try:
            payload = self._read_file(search_blob_name(session_id))
        except RemoteError:
            return None  # published by an older build, or simply too big to have one
        return decode_search_payload(payload)

    def unpublish(self, session_id: str) -> None:
        """Delete the manifest first, then every blob.

        The reverse order of publishing, for the same reason: the manifest is what makes the
        session visible, so an interrupted delete leaves invisible blobs rather than a manifest
        with no payload behind it.
        """
        safe_session_id(session_id)
        prefix = f"{BLOB_ROOT}/{session_id}"
        blobs = self._safe_list(prefix, recursive=True)
        manifest = f"{MANIFEST_ROOT}/{session_id}.json"
        if not blobs and manifest not in self._safe_list(MANIFEST_ROOT, recursive=False):
            raise RemoteError(f"la sesión {session_id} no está publicada aquí")
        with contextlib.suppress(RemoteError):
            self._delete_file(manifest)
        # The search blob too, or it lingers as an orphan nothing references.
        with contextlib.suppress(RemoteError):
            self._delete_file(search_blob_name(session_id))
        for path in blobs:
            with contextlib.suppress(RemoteError):
                self._delete_file(path)


class GitLabRemote(HttpRepoRemote):
    """Sessions in a GitLab repo. Works against gitlab.com and self-hosted alike."""

    def _project_base(self) -> str:
        return f"{self.host}/api/v4/projects/{urllib.parse.quote(self.repo, safe='')}"

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token or ""}

    def describe(self) -> str:
        data = self._get_json(self._project_base())
        if isinstance(data, dict) and isinstance(data.get("path_with_namespace"), str):
            return str(data["path_with_namespace"])
        return self.repo

    def _list_dir(self, dir_path: str, *, recursive: bool) -> list[str]:
        paths: list[str] = []
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "path": dir_path,
                    "ref": self.branch,
                    "per_page": _PER_PAGE,
                    "page": page,
                    "recursive": "true" if recursive else "false",
                }
            )
            entries = self._get_json(f"{self._project_base()}/repository/tree?{query}")
            if not isinstance(entries, list) or not entries:
                break
            for entry in entries:
                if isinstance(entry, dict) and entry.get("type") == "blob":
                    path = entry.get("path")
                    if isinstance(path, str):
                        paths.append(path)
            if len(entries) < _PER_PAGE:
                break
            page += 1
        return paths

    def _file_url(self, file_path: str) -> str:
        quoted = urllib.parse.quote(file_path, safe="")
        return f"{self._project_base()}/repository/files/{quoted}"

    def _read_file(self, file_path: str) -> bytes:
        return self._request(f"{self._file_url(file_path)}/raw?ref={self.branch}")

    def _delete_file(self, file_path: str) -> None:
        query = urllib.parse.urlencode({"branch": self.branch, "commit_message": _DELETE_MESSAGE})
        self._request(f"{self._file_url(file_path)}?{query}", method="DELETE")

    def _write_file(self, file_path: str, payload: bytes) -> None:
        body: dict[str, object] = {
            "branch": self.branch,
            "content": base64.b64encode(payload).decode("ascii"),
            "encoding": "base64",
            "commit_message": _COMMIT_MESSAGE,
        }
        try:
            self._request(self._file_url(file_path), method="POST", body=body)
        except RemoteError as exc:
            # GitLab has no upsert: creating an existing file is a 400. Retrying as an
            # update is what makes republishing work at all.
            if "400" not in str(exc):
                raise
            self._request(self._file_url(file_path), method="PUT", body=body)


class GitHubRemote(HttpRepoRemote):
    """Sessions in a GitHub repo, via the contents API."""

    def _repo_base(self) -> str:
        return f"{self.host}/repos/{self.repo}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token or ''}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def describe(self) -> str:
        data = self._get_json(self._repo_base())
        if isinstance(data, dict) and isinstance(data.get("full_name"), str):
            return str(data["full_name"])
        return self.repo

    def _contents_url(self, path: str) -> str:
        return f"{self._repo_base()}/contents/{urllib.parse.quote(path)}"

    def _list_dir(self, dir_path: str, *, recursive: bool) -> list[str]:
        """Walk the contents API. It is not recursive, so subdirs are visited by hand."""
        entries = self._get_json(f"{self._contents_url(dir_path)}?ref={self.branch}")
        if not isinstance(entries, list):
            raise RemoteError(f"{dir_path} no es un directorio en el remoto")
        paths: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path, kind = entry.get("path"), entry.get("type")
            if not isinstance(path, str):
                continue
            if kind == "file":
                paths.append(path)
            elif kind == "dir" and recursive:
                paths.extend(self._list_dir(path, recursive=True))
        return paths

    def _read_file(self, file_path: str) -> bytes:
        return self._request(
            f"{self._contents_url(file_path)}?ref={self.branch}",
            extra_headers={"Accept": "application/vnd.github.raw"},
        )

    def _file_sha(self, file_path: str) -> str | None:
        """The blob sha of an existing file, or None. Required to update in place."""
        try:
            data = self._get_json(f"{self._contents_url(file_path)}?ref={self.branch}")
        except RemoteError as exc:
            if "404" in str(exc):
                return None
            raise
        if isinstance(data, dict) and isinstance(data.get("sha"), str):
            return str(data["sha"])
        return None

    def _delete_file(self, file_path: str) -> None:
        existing = self._file_sha(file_path)
        if existing is None:
            return  # already gone
        self._request(
            self._contents_url(file_path),
            method="DELETE",
            body={"message": _DELETE_MESSAGE, "sha": existing, "branch": self.branch},
        )

    def _write_file(self, file_path: str, payload: bytes) -> None:
        body: dict[str, object] = {
            "message": _COMMIT_MESSAGE,
            "content": base64.b64encode(payload).decode("ascii"),
            "branch": self.branch,
        }
        existing = self._file_sha(file_path)
        if existing is not None:
            body["sha"] = existing  # omitting it on an existing file is a 422
        self._request(self._contents_url(file_path), method="PUT", body=body)


def _http_message(exc: urllib.error.HTTPError) -> str:
    """Turn an HTTP failure into something a user can act on.

    Auth and permission problems are by far the most likely, and "401" alone does not
    tell anyone to go check their token.
    """
    if exc.code in (401, 403):
        return f"{exc.code}: token inválido o sin permisos sobre el repo"
    if exc.code == 404:
        return "404: no encontrado (¿repo, rama o ruta correctos?)"
    detail = ""
    try:
        body = json.loads(exc.read())
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "")
    except (json.JSONDecodeError, OSError, ValueError):
        detail = ""
    return f"{exc.code}: {detail or exc.reason}"
