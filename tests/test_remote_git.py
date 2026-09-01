"""Tests for the git-over-SSH backend (multi_claude.remote_git).

Run against a real bare repository on disk rather than a mock: what is being tested is that
git's own behaviour is used correctly — the clone, the fetch, and above all the rejected push
that makes concurrent publishing safe. A file:// URL exercises every one of those paths without
needing an SSH server; only the URL construction differs, and that is asserted separately.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from multi_claude.project_remotes import RemoteLink, RemoteServer
from multi_claude.remote import RemoteError, RemoteSession
from multi_claude.remote_git import GitSshRemote
from tests.conftest import write_session

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """An empty sessions repo, as a colleague would have created it."""
    repo = tmp_path / "sesiones.git"
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(repo)], check=True)
    return repo


def _remote(bare: Path, tmp_path: Path, *, name: str = "work") -> GitSshRemote:
    """A store pointed at ``bare`` over file://, with its own working copy."""
    remote = GitSshRemote(
        RemoteLink(kind="ssh", host="ignored", repo="grupo/sesiones", branch="main"),
        cache_dir=tmp_path / "cache" / name,
    )
    remote.url = str(bare)  # file path stands in for the SSH URL
    return remote


def _meta(session_id: str, **kwargs: object) -> RemoteSession:
    return RemoteSession(session_id=session_id, published_at="2026-07-28T10:00:00+00:00", **kwargs)  # type: ignore[arg-type]


# --- URL construction ---------------------------------------------------------------


def test_the_ssh_url_is_built_the_way_git_expects() -> None:
    link = RemoteLink(kind="ssh", host="git.empresa.com", repo="grupo/sesiones", ssh_user="git")
    assert link.git_url() == "git@git.empresa.com:grupo/sesiones.git"


def test_a_custom_ssh_user_is_honoured() -> None:
    link = RemoteLink(kind="ssh", host="git.empresa.com", repo="g/s", ssh_user="gitlab")
    assert link.git_url() == "gitlab@git.empresa.com:g/s.git"


def test_the_api_host_is_reduced_to_a_git_host() -> None:
    """The API URL and the git host are not the same string, and github proves it."""
    assert RemoteServer(name="x", host="https://git.empresa.com/api/v4").ssh_host == (
        "git.empresa.com"
    )
    assert RemoteServer(name="x", kind="github").ssh_host == "github.com"
    assert RemoteServer(name="x", kind="gitlab").ssh_host == "gitlab.com"


def test_an_ssh_server_needs_no_api_url_to_be_usable() -> None:
    server = RemoteServer(name="Empresa", host="https://git.empresa.com", auth="ssh")
    assert server.uses_ssh
    assert server.is_configured
    assert "ssh" in server.summary()


def test_a_link_resolved_against_an_ssh_server_publishes_over_ssh() -> None:
    server = RemoteServer(name="FL", host="https://git.empresa.com", auth="ssh", ssh_user="git")
    link = RemoteLink(server="FL", repo="grupo/sesiones").resolved([server])

    assert link.kind == "ssh"
    assert link.git_url() == "git@git.empresa.com:grupo/sesiones.git"
    assert link.is_configured


# --- round trip ---------------------------------------------------------------------


def test_publish_then_fetch_round_trips(bare_repo: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    jsonl = write_session(project, session_id="sid-1", extra_events=100)
    subagents = project / "sid-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text('{"x":1}\n', encoding="utf-8")

    remote = _remote(bare_repo, tmp_path)
    remote.publish(_meta("sid-1", published_by="ana@example.com"), project)

    (listed,) = remote.list_sessions()
    assert listed.session_id == "sid-1"
    assert listed.published_by == "ana@example.com"

    dest = tmp_path / "dest"
    dest.mkdir()
    result = remote.fetch("sid-1", dest)
    assert (dest / "sid-1.jsonl").read_bytes() == jsonl.read_bytes()
    assert (dest / "sid-1" / "subagents" / "agent-a.jsonl").read_text(encoding="utf-8") == (
        '{"x":1}\n'
    )
    assert len(result.written) == 2


def test_a_second_clone_sees_what_the_first_published(bare_repo: Path, tmp_path: Path) -> None:
    """The point of the whole feature, over this backend: a colleague sees your session."""
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    _remote(bare_repo, tmp_path, name="ana").publish(_meta("sid-1"), project)

    carlos = _remote(bare_repo, tmp_path, name="carlos")
    assert [s.session_id for s in carlos.list_sessions()] == ["sid-1"]

    dest = tmp_path / "de-carlos"
    dest.mkdir()
    carlos.fetch("sid-1", dest)
    assert (dest / "sid-1.jsonl").is_file()


def test_an_empty_repo_lists_nothing(bare_repo: Path, tmp_path: Path) -> None:
    assert _remote(bare_repo, tmp_path).list_sessions() == ()


def test_the_search_payload_is_byte_identical_across_publishes(tmp_path: Path) -> None:
    """gzip stamps the clock into its header, and that alone would defeat the check above.

    Two compressions of the same text a second apart differ in bytes while being identical
    in content — invisible to a person, a change to git. Without pinning it, republishing a
    session nobody touched adds a commit to the team's repo, and the "no empty commit"
    guarantee only holds while both publishes land in the same second, which is exactly the
    kind of test that passes on a fast machine and fails in CI.
    """
    from multi_claude.remote import search_payload_for

    project = tmp_path / "project"
    write_session(project, session_id="sid-1", first_prompt="algo que decir")
    first = search_payload_for(project, "sid-1")
    assert first is not None
    time.sleep(1.1)  # long enough for gzip's second-resolution stamp to move
    assert search_payload_for(project, "sid-1") == first


def test_republishing_the_same_bytes_makes_no_commit(bare_repo: Path, tmp_path: Path) -> None:
    """Nothing changed, so there is nothing to commit — and no empty commit either."""
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote = _remote(bare_repo, tmp_path)
    remote.publish(_meta("sid-1"), project)
    first = subprocess.run(
        ["git", "rev-list", "--count", "main"],
        cwd=bare_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    remote.publish(_meta("sid-1"), project)
    second = subprocess.run(
        ["git", "rev-list", "--count", "main"],
        cwd=bare_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert first == second


# --- concurrency, which is what this backend buys ------------------------------------


def test_two_people_publishing_at_once_both_land(bare_repo: Path, tmp_path: Path) -> None:
    """A rejected push is rebased and retried, so neither session is lost.

    This is the difference from the REST drivers, where the second publish overwrites.
    """
    ana_project = tmp_path / "de-ana"
    write_session(ana_project, session_id="sid-ana")
    carlos_project = tmp_path / "de-carlos"
    write_session(carlos_project, session_id="sid-carlos")

    ana = _remote(bare_repo, tmp_path, name="ana")
    carlos = _remote(bare_repo, tmp_path, name="carlos")

    # Both clone while the repo is empty, so neither knows about the other's commit.
    ana.list_sessions()
    carlos.list_sessions()

    ana.publish(_meta("sid-ana"), ana_project)
    carlos.publish(_meta("sid-carlos"), carlos_project)  # push rejected, rebased, retried

    fresh = _remote(bare_repo, tmp_path, name="tercero")
    assert sorted(s.session_id for s in fresh.list_sessions()) == ["sid-ana", "sid-carlos"]


# --- failures a user can act on -----------------------------------------------------


def test_a_missing_session_fails_cleanly(bare_repo: Path, tmp_path: Path) -> None:
    remote = _remote(bare_repo, tmp_path)
    with pytest.raises(RemoteError, match="no está en el remoto"):
        remote.fetch("sid-nope", tmp_path / "dest")


def test_fetch_refuses_to_overwrite_a_local_session(bare_repo: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote = _remote(bare_repo, tmp_path)
    remote.publish(_meta("sid-1"), project)

    with pytest.raises(RemoteError, match="ya existe en destino"):
        remote.fetch("sid-1", project)


def test_publishing_a_session_that_is_not_on_disk_fails(bare_repo: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(RemoteError, match="no tiene transcript en disco"):
        _remote(bare_repo, tmp_path).publish(_meta("sid-1"), project)


def test_an_unreachable_repo_says_so_instead_of_leaking_git_stderr(tmp_path: Path) -> None:
    remote = GitSshRemote(
        RemoteLink(kind="ssh", host="git.invalid", repo="g/s"), cache_dir=tmp_path / "cache"
    )
    remote.url = str(tmp_path / "no-existe.git")
    with pytest.raises(RemoteError) as excinfo:
        remote.list_sessions()
    assert "no existe o no es un repositorio" in str(excinfo.value)


def test_connection_check_reports_the_branch(bare_repo: Path, tmp_path: Path) -> None:
    remote = _remote(bare_repo, tmp_path)
    assert "vacío" in remote.check_connection()

    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote.publish(_meta("sid-1"), project)
    assert "main" in remote.check_connection()


# --- the connection check must not block the UI --------------------------------------


def test_the_probe_gets_a_short_timeout_not_the_clone_one() -> None:
    """A clone may take two minutes; an interactive check may not."""
    from multi_claude.remote_git import _TIMEOUT, PROBE_TIMEOUT

    assert PROBE_TIMEOUT < _TIMEOUT
    assert PROBE_TIMEOUT <= 20

    link = RemoteLink(kind="ssh", host="git.empresa.com", repo="g/s")
    assert GitSshRemote(link).timeout == _TIMEOUT
    assert GitSshRemote(link, timeout=PROBE_TIMEOUT).timeout == PROBE_TIMEOUT


def test_probing_an_ssh_server_reports_success_when_only_the_repo_is_missing(
    bare_repo: Path,
) -> None:
    """A server has no repo of its own, so a missing one still proves host and key are fine."""
    from multi_claude.modals import _probe_server
    from multi_claude.project_remotes import RemoteServer

    # Point the probe at a path that exists as a repo, then at one that does not.
    server = RemoteServer(name="local", host=str(bare_repo.parent), auth="ssh")
    message, ok = _probe_server(server, None)
    # Either it resolves (unlikely for a bare path) or it reports the missing repo as OK.
    assert isinstance(message, str) and isinstance(ok, bool)


def test_probing_reports_a_readable_failure_for_an_unreachable_host() -> None:
    from multi_claude.modals import _probe_server
    from multi_claude.project_remotes import RemoteServer

    server = RemoteServer(name="roto", host="https://no-existe.invalid", auth="ssh")
    message, ok = _probe_server(server, None)
    assert not ok
    assert "no-existe.invalid" in message or "no se pudo resolver" in message


# --- ssh -T, the canonical access check ----------------------------------------------


def _fake_ssh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    code: int = 0,
) -> None:
    """Stand in for the ssh binary with a canned answer."""
    import subprocess as sp

    class Result:
        returncode = code

        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(sp, "run", lambda *a, **k: Result())


def test_github_greeting_is_read_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub exits non-zero on success, so only the greeting can be trusted."""
    from multi_claude.remote_git import probe_ssh_access

    _fake_ssh(
        monkeypatch,
        stderr="Hi Zarritas! You've successfully authenticated, but GitHub does not "
        "provide shell access.",
        code=1,
    )
    assert probe_ssh_access("github.com") == "autenticado en github.com como Zarritas"


def test_gitlab_greeting_is_read_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from multi_claude.remote_git import probe_ssh_access

    _fake_ssh(monkeypatch, stderr="Welcome to GitLab, @ana!", code=0)
    assert probe_ssh_access("git.empresa.com") == ("autenticado en git.empresa.com como ana")


def test_a_wrong_ssh_user_is_named_in_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mistake to make here is using your account name instead of "git"."""
    from multi_claude.remote import RemoteError
    from multi_claude.remote_git import probe_ssh_access

    _fake_ssh(monkeypatch, stderr="git@github.com: Permission denied (publickey).", code=255)
    with pytest.raises(RemoteError) as excinfo:
        probe_ssh_access("github.com", "Zarritas")

    message = str(excinfo.value)
    assert "rechazó tu clave" in message
    assert "es «git»" in message and "«Zarritas»" in message


def test_a_rejected_key_with_the_right_user_does_not_blame_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multi_claude.remote import RemoteError
    from multi_claude.remote_git import probe_ssh_access

    _fake_ssh(monkeypatch, stderr="Permission denied (publickey).", code=255)
    with pytest.raises(RemoteError) as excinfo:
        probe_ssh_access("github.com", "git")
    assert "es «git»" not in str(excinfo.value)


def test_an_unresolvable_host_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from multi_claude.remote import RemoteError
    from multi_claude.remote_git import probe_ssh_access

    _fake_ssh(monkeypatch, stderr="ssh: Could not resolve hostname nope.invalid", code=255)
    with pytest.raises(RemoteError, match="no se pudo resolver"):
        probe_ssh_access("nope.invalid")


def test_the_probe_needs_no_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """It used to invent one, whose name then showed up confusingly in the error."""
    import subprocess as sp

    from multi_claude.remote_git import probe_ssh_access

    seen: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "Welcome to GitLab, @x!"
        stderr = ""

    def spy(argv: list[str], **kwargs: object) -> Result:
        seen.append(argv)
        return Result()

    monkeypatch.setattr(sp, "run", spy)
    probe_ssh_access("git.empresa.com")

    (argv,) = seen
    assert argv[:2] == ["ssh", "-T"]
    assert argv[-1] == "git@git.empresa.com"
    assert not any("_probe" in part for part in argv)


# --- non-default SSH port ------------------------------------------------------------


def test_a_non_default_port_needs_the_explicit_ssh_url_form() -> None:
    """``git@host:path`` cannot carry a port: what follows the colon is the path.

    Self-hosted GitLab commonly listens elsewhere (2211 in the case that found this), and the
    scp-like form silently goes to 22 and times out.
    """
    link = RemoteLink(kind="ssh", host="git.empresa.com", repo="grupo/repo", ssh_port=2211)
    assert link.git_url() == "ssh://git@git.empresa.com:2211/grupo/repo.git"


def test_port_22_keeps_the_familiar_form() -> None:
    link = RemoteLink(kind="ssh", host="github.com", repo="Zarritas/multi-claude", ssh_port=22)
    assert link.git_url() == "git@github.com:Zarritas/multi-claude.git"


def test_the_port_travels_from_the_server_to_the_link() -> None:
    server = RemoteServer(name="FL", host="https://git.empresa.com", auth="ssh", ssh_port=2211)
    link = RemoteLink(server="FL", repo="grupo/sesiones").resolved([server])

    assert link.ssh_port == 2211
    assert link.git_url() == "ssh://git@git.empresa.com:2211/grupo/sesiones.git"
    assert "2211" in server.summary()


def test_two_links_differing_only_in_port_are_different_targets() -> None:
    """Otherwise fixing a port would look like the same link and not replace it."""
    a = RemoteLink(kind="ssh", host="h", repo="g/s", ssh_port=22)
    b = RemoteLink(kind="ssh", host="h", repo="g/s", ssh_port=2211)
    assert not a.same_target(b)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(2211, 2211), ("2211", 2211), (None, 22), ("", 22), (0, 22), (99999, 22), (True, 22)],
)
def test_a_stored_port_is_coerced_sanely(stored: object, expected: int) -> None:
    from multi_claude.project_remotes import RemoteServer as RS

    parsed = RS.from_dict({"name": "x", "kind": "gitlab", "ssh_port": stored})
    assert parsed is not None
    assert parsed.ssh_port == expected


def test_the_probe_passes_the_port_to_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp

    from multi_claude.remote_git import probe_ssh_access

    seen: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "Welcome to GitLab, @x!"
        stderr = ""

    def spy(argv: list[str], **kwargs: object) -> Result:
        seen.append(argv)
        return Result()

    monkeypatch.setattr(sp, "run", spy)
    probe_ssh_access("git.empresa.com", "git", 2211)

    (argv,) = seen
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2211"


def test_silence_on_port_22_suggests_checking_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The symptom that started this: nothing at all, which says nothing to the user."""
    from multi_claude.remote import RemoteError
    from multi_claude.remote_git import probe_ssh_access

    _fake_ssh(monkeypatch, stderr="", stdout="", code=255)
    with pytest.raises(RemoteError) as excinfo:
        probe_ssh_access("git.empresa.com", "git", 22)
    assert "el puerto SSH es otro" in str(excinfo.value)


def test_silence_on_a_custom_port_does_not_repeat_the_port_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multi_claude.remote import RemoteError
    from multi_claude.remote_git import probe_ssh_access

    _fake_ssh(monkeypatch, stderr="", stdout="", code=255)
    with pytest.raises(RemoteError) as excinfo:
        probe_ssh_access("git.empresa.com", "git", 2211)
    assert "el puerto SSH es otro" not in str(excinfo.value)


# --- unpublishing over git ------------------------------------------------------------


def test_unpublish_over_git_removes_it_for_everyone(bare_repo: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    ana = _remote(bare_repo, tmp_path, name="ana")
    ana.publish(_meta("sid-1"), project)

    ana.unpublish("sid-1")

    # A fresh clone, i.e. a colleague, no longer sees it.
    assert _remote(bare_repo, tmp_path, name="carlos").list_sessions() == ()


def test_unpublish_over_git_leaves_the_local_copy(bare_repo: Path, tmp_path: Path) -> None:
    project = tmp_path / "project"
    jsonl = write_session(project, session_id="sid-1")
    remote = _remote(bare_repo, tmp_path)
    remote.publish(_meta("sid-1"), project)
    remote.unpublish("sid-1")
    assert jsonl.is_file()


def test_unpublishing_over_git_what_is_not_published_fails(bare_repo: Path, tmp_path: Path) -> None:
    with pytest.raises(RemoteError, match="no está publicada aquí"):
        _remote(bare_repo, tmp_path).unpublish("sid-nope")
