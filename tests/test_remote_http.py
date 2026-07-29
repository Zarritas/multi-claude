"""Tests for the token store and the GitLab/GitHub drivers.

The drivers are exercised against a fake transport rather than the network: what matters is
that the right endpoints and payloads are produced, that a published session round-trips
byte-identically, and that failures turn into messages a user can act on.
"""

from __future__ import annotations

import base64
import gzip
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from multi_claude.remote import (
    REMOTE_TOKEN_ENV,
    RemoteError,
    RemoteSession,
    TokenStore,
    store_from_settings,
    token_path,
)
from multi_claude.remote_http import GitHubRemote, GitLabRemote, HttpRepoRemote
from tests.conftest import write_session

# --- token store --------------------------------------------------------------------


def _store(tmp_path: Path) -> TokenStore:
    """A store isolated from the real one, legacy fallback included.

    Both paths are pinned: without that the fallback would read the developer's actual
    ``~/.config/multi-claude/remote-token`` and the tests would depend on their machine.
    """
    return TokenStore(tmp_path / "remote-tokens.json", legacy=tmp_path / "remote-token")


def test_tokens_are_stored_readable_only_by_their_owner(tmp_path: Path) -> None:
    """They are credentials on disk; group/other must not be able to read them."""
    store = _store(tmp_path)
    store.set("glpat-secreto", "FactorLibre")

    assert store.get("FactorLibre") == "glpat-secreto"
    mode = stat.S_IMODE((tmp_path / "remote-tokens.json").stat().st_mode)
    assert mode == 0o600


def test_each_server_keeps_its_own_token(tmp_path: Path) -> None:
    """Two hosts do not share credentials, so one token per server."""
    store = _store(tmp_path)
    store.set("glpat-empresa", "FactorLibre")
    store.set("github_pat_x", "GitHub")

    assert store.get("FactorLibre") == "glpat-empresa"
    assert store.get("GitHub") == "github_pat_x"
    assert store.get("NoConfigurado") is None


def test_missing_token_reads_as_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get("cualquiera") is None
    assert store.has_token("cualquiera") is False


def test_a_corrupt_token_file_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "remote-tokens.json"
    path.write_text("{no es json", encoding="utf-8")
    assert TokenStore(path, legacy=tmp_path / "nada").get("x") is None


def test_the_old_single_token_file_still_works(tmp_path: Path) -> None:
    """An existing setup must keep publishing after tokens became per-server."""
    legacy = tmp_path / "remote-token"
    legacy.write_text("glpat-de-antes\n", encoding="utf-8")
    store = TokenStore(tmp_path / "remote-tokens.json", legacy=legacy)

    assert store.get("CualquierServidor") == "glpat-de-antes"
    assert store.get() == "glpat-de-antes"


def test_a_server_token_wins_over_the_legacy_one(tmp_path: Path) -> None:
    legacy = tmp_path / "remote-token"
    legacy.write_text("viejo\n", encoding="utf-8")
    store = TokenStore(tmp_path / "remote-tokens.json", legacy=legacy)
    store.set("nuevo", "FactorLibre")

    assert store.get("FactorLibre") == "nuevo"
    assert store.get("OtroServidor") == "viejo"


def test_env_var_overrides_every_stored_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So CI never has to write a secret to disk."""
    store = _store(tmp_path)
    store.set("el-de-disco", "FactorLibre")
    monkeypatch.setenv(REMOTE_TOKEN_ENV, "el-del-entorno")
    assert store.get("FactorLibre") == "el-del-entorno"


def test_a_token_can_be_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REMOTE_TOKEN_ENV, raising=False)
    store = _store(tmp_path)
    store.set("x", "FactorLibre")
    store.delete("FactorLibre")
    assert store.get("FactorLibre") is None
    store.delete("FactorLibre")  # idempotent


def test_tokens_live_beside_the_config_not_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert token_path() == tmp_path / "multi-claude" / "remote-tokens.json"
    assert token_path().name != "config.json"


# --- fake transport -----------------------------------------------------------------


class FakeApi:
    """Minimal in-memory stand-in for a provider's REST API.

    Records every call so tests can assert on endpoints and methods, and keeps written
    files so a publish/fetch round-trip can be checked end to end.
    """

    def __init__(self, *, provider: str) -> None:
        self.provider = provider
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_with: dict[str, int] = {}

    def install(self, remote: HttpRepoRemote, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(remote, "_request", self._request)
        monkeypatch.setattr(remote, "_get_json", lambda url: json.loads(self._request(url)))

    def _kind_of(self, path: str) -> str:
        """Whether ``path`` is a file, a directory, or absent — as the API would see it."""
        if path in self.files:
            return "file"
        if any(p.startswith(f"{path}/") for p in self.files):
            return "dir"
        return "missing"

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        self.calls.append((method, url))
        for fragment, code in self.fail_with.items():
            if fragment in url:
                raise RemoteError(f"{code}: forzado por el test")
        if method == "DELETE":
            self.files.pop(self._path_from_url(url), None)
            return b"{}"
        if method in ("POST", "PUT"):
            return self._write(url, body or {})
        return self._read(url, extra_headers or {})

    def _path_from_url(self, url: str) -> str:
        import urllib.parse

        if self.provider == "gitlab":
            if "/repository/files/" in url:
                tail = url.split("/repository/files/", 1)[1]
                return urllib.parse.unquote(tail.split("/raw")[0].split("?")[0])
            return ""
        tail = url.split("/contents/", 1)[1] if "/contents/" in url else ""
        return urllib.parse.unquote(tail.split("?")[0])

    def _write(self, url: str, body: dict[str, Any]) -> bytes:
        path = self._path_from_url(url)
        payload = base64.b64decode(body["content"])
        self.files[path] = payload
        return b"{}"

    def _read(self, url: str, headers: dict[str, str]) -> bytes:
        import urllib.parse

        if self.provider == "gitlab" and "/repository/tree" in url:
            query = urllib.parse.parse_qs(url.split("?", 1)[1])
            base = query["path"][0]
            recursive = query.get("recursive", ["false"])[0] == "true"
            page = int(query.get("page", ["1"])[0])
            entries = [
                {"type": "blob", "path": p}
                for p in sorted(self.files)
                if _under(p, base, recursive)
            ]
            return json.dumps(entries if page == 1 else []).encode()
        if self.provider == "gitlab" and url.endswith("sesiones"):
            return json.dumps({"path_with_namespace": "grupo/sesiones"}).encode()
        if self.provider == "github" and url.endswith("/repos/grupo/sesiones"):
            return json.dumps({"full_name": "grupo/sesiones"}).encode()

        path = self._path_from_url(url)
        kind = self._kind_of(path)
        if kind == "missing":
            raise RemoteError("404: no encontrado")
        if kind == "dir":
            if self.provider != "github":
                raise RemoteError("404: no encontrado")
            return json.dumps(self._github_dir(path)).encode()
        # GitHub uses the *same* URL for a file's metadata and its bytes, and picks by the
        # Accept header — so the fake has to honour it or the driver's two reads collide.
        if self.provider == "github" and "raw" not in headers.get("Accept", ""):
            return json.dumps({"sha": "deadbeef", "path": path}).encode()
        return self.files[path]

    def _github_dir(self, base: str) -> list[dict[str, str]]:
        names: dict[str, str] = {}
        for path in sorted(self.files):
            if not path.startswith(f"{base}/"):
                continue
            rest = path[len(base) + 1 :]
            head = rest.split("/", 1)[0]
            names[f"{base}/{head}"] = "file" if head == rest else "dir"
        return [{"path": p, "type": t, "sha": "deadbeef"} for p, t in names.items()]


def _under(path: str, base: str, recursive: bool) -> bool:
    if not path.startswith(f"{base}/"):
        return False
    return recursive or "/" not in path[len(base) + 1 :]


def _remote_and_api(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[HttpRepoRemote, FakeApi]:
    cls = GitLabRemote if provider == "gitlab" else GitHubRemote
    host = "https://git.example.com" if provider == "gitlab" else "https://api.github.com"
    remote = cls(host, "grupo/sesiones", "main", "un-token")
    api = FakeApi(provider=provider)
    api.install(remote, monkeypatch)
    return remote, api


# --- round trip over both providers -------------------------------------------------


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_publish_then_fetch_round_trips(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    jsonl = write_session(project, session_id="sid-1")
    subagents = project / "sid-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text('{"x":1}\n', encoding="utf-8")

    remote, api = _remote_and_api(provider, monkeypatch)
    remote.publish(
        RemoteSession(session_id="sid-1", published_at="2026-07-28T10:00:00+00:00"), project
    )

    assert "blobs/sid-1/session.jsonl.gz" in api.files
    assert gzip.decompress(api.files["blobs/sid-1/session.jsonl.gz"]) == jsonl.read_bytes()

    dest = tmp_path / "dest"
    dest.mkdir()
    result = remote.fetch("sid-1", dest)
    assert (dest / "sid-1.jsonl").read_bytes() == jsonl.read_bytes()
    assert (dest / "sid-1" / "subagents" / "agent-a.jsonl").read_text(encoding="utf-8") == (
        '{"x":1}\n'
    )
    assert len(result.written) == 2


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_the_manifest_is_written_after_the_blobs(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order is the only thing keeping a half-publish invisible rather than broken."""
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote, api = _remote_and_api(provider, monkeypatch)
    remote.publish(
        RemoteSession(session_id="sid-1", published_at="2026-07-28T10:00:00+00:00"), project
    )

    writes = [url for method, url in api.calls if method in ("POST", "PUT")]
    manifest_at = next(i for i, u in enumerate(writes) if "manifest" in u)
    blob_at = next(i for i, u in enumerate(writes) if "session.jsonl.gz" in u)
    assert blob_at < manifest_at


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_listing_returns_published_sessions(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote, _ = _remote_and_api(provider, monkeypatch)
    remote.publish(
        RemoteSession(
            session_id="sid-1",
            published_at="2026-07-28T10:00:00+00:00",
            published_by="carlos@example.com",
        ),
        project,
    )

    (listed,) = remote.list_sessions()
    assert listed.session_id == "sid-1"
    assert listed.published_by == "carlos@example.com"


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_an_empty_repo_lists_nothing_instead_of_failing(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh sessions repo has no manifest/ dir; that is "nothing yet", not an error."""
    remote, _ = _remote_and_api(provider, monkeypatch)
    assert remote.list_sessions() == ()


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_connection_test_reports_repo_and_count(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, _ = _remote_and_api(provider, monkeypatch)
    assert "grupo/sesiones" in remote.check_connection()


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_fetching_an_unknown_session_fails_cleanly(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, _ = _remote_and_api(provider, monkeypatch)
    with pytest.raises(RemoteError, match="no está en el remoto"):
        remote.fetch("sid-nope", tmp_path / "dest")


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_fetch_refuses_to_overwrite_a_local_session(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote, _ = _remote_and_api(provider, monkeypatch)
    with pytest.raises(RemoteError, match="ya existe en destino"):
        remote.fetch("sid-1", project)


# --- endpoints and auth -------------------------------------------------------------


def test_gitlab_url_encodes_the_project_path() -> None:
    """The project id must be the URL-encoded namespace, or every call 404s."""
    remote = GitLabRemote("https://git.example.com", "grupo/sub/sesiones", "main", "t")
    assert "projects/grupo%2Fsub%2Fsesiones" in remote._project_base()


def test_gitlab_sends_the_private_token_header() -> None:
    remote = GitLabRemote("https://git.example.com", "g/s", "main", "glpat-x")
    assert remote._headers()["PRIVATE-TOKEN"] == "glpat-x"


def test_github_sends_a_bearer_token_and_pins_the_api_version() -> None:
    remote = GitHubRemote("https://api.github.com", "g/s", "main", "ghp-x")
    headers = remote._headers()
    assert headers["Authorization"] == "Bearer ghp-x"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_a_trailing_slash_in_the_host_does_not_double_up() -> None:
    remote = GitLabRemote("https://git.example.com/", "g/s", "main", "t")
    assert "com//" not in remote._project_base()


def test_gitlab_retries_as_an_update_when_the_file_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitLab has no upsert: without the PUT retry, republishing would always fail."""
    remote = GitLabRemote("https://git.example.com", "grupo/sesiones", "main", "t")
    attempts: list[str] = []

    def fake_request(url: str, *, method: str = "GET", **kwargs: object) -> bytes:
        attempts.append(method)
        if method == "POST":
            raise RemoteError("400: A file with this name already exists")
        return b"{}"

    monkeypatch.setattr(remote, "_request", fake_request)
    remote._write_file("manifest/sid-1.json", b"{}")
    assert attempts == ["POST", "PUT"]


def test_a_non_400_error_on_write_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    remote = GitLabRemote("https://git.example.com", "grupo/sesiones", "main", "t")

    def fake_request(url: str, *, method: str = "GET", **kwargs: object) -> bytes:
        raise RemoteError("403: sin permisos")

    monkeypatch.setattr(remote, "_request", fake_request)
    with pytest.raises(RemoteError, match="403"):
        remote._write_file("manifest/sid-1.json", b"{}")


def test_calls_without_a_token_fail_before_hitting_the_network() -> None:
    remote = GitLabRemote("https://git.example.com", "g/s", "main", None)
    with pytest.raises(RemoteError, match="falta el token"):
        remote.list_sessions()


# --- factory ------------------------------------------------------------------------


def test_factory_builds_the_provider_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MULTI_CLAUDE_REMOTE_DIR", raising=False)
    gitlab = store_from_settings(
        "gitlab", "", host="https://git.example.com", repo="g/s", branch="trunk", token="t"
    )
    assert isinstance(gitlab, GitLabRemote)
    assert gitlab.branch == "trunk"
    assert isinstance(
        store_from_settings("github", "", host="https://api.github.com", repo="g/s", token="t"),
        GitHubRemote,
    )


def test_a_half_configured_provider_is_treated_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better no store than one that raises on every call."""
    monkeypatch.delenv("MULTI_CLAUDE_REMOTE_DIR", raising=False)
    assert store_from_settings("gitlab", "", host="https://git.example.com", repo="") is None
    assert store_from_settings("gitlab", "", host="", repo="g/s") is None


# --- unpublishing over the APIs -------------------------------------------------------


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_unpublish_deletes_the_manifest_and_the_blobs(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    (project / "sid-1" / "subagents").mkdir(parents=True)
    (project / "sid-1" / "subagents" / "agent-a.jsonl").write_text("{}\n", encoding="utf-8")

    remote, api = _remote_and_api(provider, monkeypatch)
    remote.publish(
        RemoteSession(session_id="sid-1", published_at="2026-07-28T10:00:00+00:00"), project
    )
    assert api.files

    remote.unpublish("sid-1")

    assert not [path for path in api.files if "sid-1" in path]
    assert remote.list_sessions() == ()


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_the_manifest_is_deleted_before_the_blobs(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse of publishing: an interrupted delete leaves invisible blobs, not a broken entry."""
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote, api = _remote_and_api(provider, monkeypatch)
    remote.publish(
        RemoteSession(session_id="sid-1", published_at="2026-07-28T10:00:00+00:00"), project
    )
    api.calls.clear()

    remote.unpublish("sid-1")

    deletes = [url for method, url in api.calls if method == "DELETE"]
    assert deletes, "no se envió ningún DELETE"
    assert "manifest" in deletes[0]


@pytest.mark.parametrize("provider", ["gitlab", "github"])
def test_unpublishing_something_absent_fails(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, _ = _remote_and_api(provider, monkeypatch)
    with pytest.raises(RemoteError, match="no está publicada aquí"):
        remote.unpublish("sid-nope")
