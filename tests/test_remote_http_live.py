"""The GitLab/GitHub drivers against a real HTTP server on localhost.

``test_remote_http`` replaces ``_request``, which is fine for asserting endpoints and
payloads but never exercises the transport itself: urllib, the auth headers actually going
over the wire, base64 round-tripping, or an HTTPError becoming a readable message. This
closes that gap with a server that emulates just enough of each provider — including the
two behaviours the drivers exist to work around: GitLab refusing to create an existing file
(400) and GitHub refusing to overwrite without a sha (422).
"""

from __future__ import annotations

import base64
import gzip
import json
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from multi_claude.remote import RemoteError, RemoteSession
from multi_claude.remote_http import GitHubRemote, GitLabRemote, HttpRepoRemote
from tests.conftest import write_session

GOOD_TOKEN = "token-bueno"


class _Forge:
    """In-memory state shared by the request handler."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def kind(self, path: str) -> str:
        if path in self.files:
            return "file"
        if any(p.startswith(f"{path}/") for p in self.files):
            return "dir"
        return "missing"


def _make_handler(forge: _Forge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _send(self, code: int, payload: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code: int, obj: object) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _read_body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            loaded = json.loads(self.rfile.read(length))
            return loaded if isinstance(loaded, dict) else {}

        @property
        def _gitlab(self) -> bool:
            return self.path.startswith("/api/v4/")

        def _authorised(self) -> bool:
            if self._gitlab:
                return self.headers.get("PRIVATE-TOKEN") == GOOD_TOKEN
            return self.headers.get("Authorization") == f"Bearer {GOOD_TOKEN}"

        def do_GET(self) -> None:
            forge.calls.append(("GET", self.path))
            if not self._authorised():
                self._json(401, {"message": "401 Unauthorized"})
                return
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)

            if self._gitlab:
                if "/repository/tree" in parsed.path:
                    base = query.get("path", [""])[0]
                    recursive = query.get("recursive", ["false"])[0] == "true"
                    page = int(query.get("page", ["1"])[0])
                    entries = [
                        {"type": "blob", "path": p}
                        for p in sorted(forge.files)
                        if p.startswith(f"{base}/") and (recursive or "/" not in p[len(base) + 1 :])
                    ]
                    self._json(200, entries if page == 1 else [])
                    return
                if "/repository/files/" in parsed.path:
                    tail = parsed.path.split("/repository/files/", 1)[1]
                    path = urllib.parse.unquote(tail.removesuffix("/raw"))
                    if path not in forge.files:
                        self._json(404, {"message": "404 File Not Found"})
                        return
                    self._send(200, forge.files[path], "text/plain")
                    return
                self._json(200, {"path_with_namespace": "grupo/sesiones"})
                return

            if "/contents/" in parsed.path:
                path = urllib.parse.unquote(parsed.path.split("/contents/", 1)[1])
                kind = forge.kind(path)
                if kind == "missing":
                    self._json(404, {"message": "Not Found"})
                    return
                if kind == "dir":
                    names: dict[str, str] = {}
                    for candidate in sorted(forge.files):
                        if not candidate.startswith(f"{path}/"):
                            continue
                        rest = candidate[len(path) + 1 :]
                        head = rest.split("/", 1)[0]
                        names[f"{path}/{head}"] = "file" if head == rest else "dir"
                    self._json(
                        200,
                        [{"path": p, "type": t, "sha": "sha1"} for p, t in names.items()],
                    )
                    return
                if "raw" in (self.headers.get("Accept") or ""):
                    self._send(200, forge.files[path], "application/octet-stream")
                    return
                self._json(200, {"path": path, "sha": "sha1", "type": "file"})
                return
            self._json(200, {"full_name": "grupo/sesiones"})

        def do_POST(self) -> None:
            forge.calls.append(("POST", self.path))
            if not self._authorised():
                self._json(401, {"message": "401 Unauthorized"})
                return
            body = self._read_body()
            path = urllib.parse.unquote(self.path.split("/repository/files/", 1)[1].split("?")[0])
            if path in forge.files:
                self._json(400, {"message": "A file with this name already exists"})
                return
            forge.files[path] = base64.b64decode(str(body["content"]))
            self._json(201, {"file_path": path})

        def do_PUT(self) -> None:
            forge.calls.append(("PUT", self.path))
            if not self._authorised():
                self._json(401, {"message": "401 Unauthorized"})
                return
            body = self._read_body()
            if self._gitlab:
                path = urllib.parse.unquote(
                    self.path.split("/repository/files/", 1)[1].split("?")[0]
                )
            else:
                path = urllib.parse.unquote(self.path.split("/contents/", 1)[1].split("?")[0])
                if path in forge.files and "sha" not in body:
                    self._json(422, {"message": "sha wasn't supplied"})
                    return
            forge.files[path] = base64.b64decode(str(body["content"]))
            self._json(200, {"content": {"path": path}})

    return Handler


@pytest.fixture
def forge() -> Iterator[tuple[_Forge, str]]:
    """A live server on an ephemeral port, serving both providers' APIs."""
    state = _Forge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _remote(provider: str, base: str, token: str = GOOD_TOKEN) -> HttpRepoRemote:
    if provider == "gitlab":
        return GitLabRemote(f"{base}/api/v4", "grupo/sesiones", "main", token)
    return GitHubRemote(base, "grupo/sesiones", "main", token)


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_publish_and_fetch_over_real_http(
    provider: str, forge: tuple[_Forge, str], tmp_path: Path
) -> None:
    state, base = forge
    project = tmp_path / "project"
    jsonl = write_session(project, session_id="sid-1", extra_events=200)
    subagents = project / "sid-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (subagents / "agent-a.meta.json").write_text('{"agentType":"Explore"}', encoding="utf-8")

    remote = _remote(provider, base)
    remote.publish(
        RemoteSession(
            session_id="sid-1",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="ana@example.com",
        ),
        project,
    )

    # Transported as gzip, and small enough to prove it was actually compressed.
    blob = state.files["blobs/sid-1/session.jsonl.gz"]
    assert gzip.decompress(blob) == jsonl.read_bytes()
    assert len(blob) < jsonl.stat().st_size

    (listed,) = remote.list_sessions()
    assert listed.published_by == "ana@example.com"

    dest = tmp_path / "dest"
    dest.mkdir()
    remote.fetch("sid-1", dest)
    assert (dest / "sid-1.jsonl").read_bytes() == jsonl.read_bytes()
    assert (dest / "sid-1" / "subagents" / "agent-a.jsonl").read_text(encoding="utf-8") == (
        '{"x":1}\n'
    )
    # The uncompressed sibling travels as-is and still arrives intact.
    assert (dest / "sid-1" / "subagents" / "agent-a.meta.json").read_text(encoding="utf-8") == (
        '{"agentType":"Explore"}'
    )


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_republishing_over_real_http_works(
    provider: str, forge: tuple[_Forge, str], tmp_path: Path
) -> None:
    """The provider quirks are why this test exists: 400 on GitLab, 422 on GitHub."""
    _, base = forge
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote = _remote(provider, base)
    meta = RemoteSession(session_id="sid-1", published_at="2026-07-28T10:00:00+00:00")

    remote.publish(meta, project)
    remote.publish(meta, project)  # would fail without the retry / the sha

    assert len(remote.list_sessions()) == 1


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_a_bad_token_says_so_over_real_http(provider: str, forge: tuple[_Forge, str]) -> None:
    _, base = forge
    remote = _remote(provider, base, token="token-malo")
    with pytest.raises(RemoteError, match="token inválido o sin permisos"):
        remote.list_sessions()


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_connection_check_over_real_http(provider: str, forge: tuple[_Forge, str]) -> None:
    _, base = forge
    assert "grupo/sesiones" in _remote(provider, base).check_connection()


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_an_unreachable_host_fails_with_a_readable_error(provider: str) -> None:
    """A closed port must not surface as a raw urllib traceback."""
    remote = _remote(provider, "http://127.0.0.1:1")
    with pytest.raises(RemoteError, match="no se pudo conectar"):
        remote.check_connection()
