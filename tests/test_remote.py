"""Tests for the shared-session remote (multi_claude.remote)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from multi_claude.remote import (
    FORMAT,
    REMOTE_DIR_ENV,
    VERSION,
    DirectoryRemote,
    RemoteError,
    RemoteSession,
    collect_session_files,
    local_path_for,
    session_to_remote,
    store_from_settings,
)
from multi_claude.session import Session
from tests.conftest import write_session


def _session(
    jsonl: Path, *, sid: str, name: str | None = None, tags: tuple[str, ...] = ()
) -> Session:
    stat = jsonl.stat()
    return Session(
        id=sid,
        path=jsonl,
        first_prompt="hola",
        branch="fl-v16-9269",
        cwd="/home/quien-la-grabo/WS/repo",
        message_count=3,
        size_bytes=stat.st_size,
        last_activity=stat.st_mtime,
        display_name=name,
        tags=tags,
    )


def _publish(remote: DirectoryRemote, project: Path, sid: str, **kwargs: object) -> RemoteSession:
    jsonl = project / f"{sid}.jsonl"
    meta = session_to_remote(_session(jsonl, sid=sid), **kwargs)  # type: ignore[arg-type]
    remote.publish(meta, project)
    return meta


# --- round trip ---------------------------------------------------------------------


def test_publish_then_fetch_round_trips_every_artifact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    jsonl = write_session(project, session_id="sid-1", first_prompt="primero")
    subagents = project / "sid-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
    (subagents / "agent-a.meta.json").write_text('{"agentType":"Explore"}', encoding="utf-8")
    results = project / "sid-1" / "tool-results"
    results.mkdir()
    (results / "toolu_01.txt").write_text("salida muy larga", encoding="utf-8")

    remote = DirectoryRemote(tmp_path / "remote")
    _publish(remote, project, "sid-1", published_by="quien@example.com")

    dest = tmp_path / "otro-proyecto"
    dest.mkdir()
    result = remote.fetch("sid-1", dest)

    assert result.session_id == "sid-1"
    assert (dest / "sid-1.jsonl").read_bytes() == jsonl.read_bytes()
    assert (dest / "sid-1" / "subagents" / "agent-a.jsonl").read_text(encoding="utf-8") == (
        '{"type":"assistant"}\n'
    )
    assert (dest / "sid-1" / "subagents" / "agent-a.meta.json").read_text(encoding="utf-8") == (
        '{"agentType":"Explore"}'
    )
    assert (dest / "sid-1" / "tool-results" / "toolu_01.txt").read_text(encoding="utf-8") == (
        "salida muy larga"
    )
    assert len(result.written) == 4


def test_manifest_metadata_survives_the_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote = DirectoryRemote(tmp_path / "remote")
    jsonl = project / "sid-1.jsonl"
    meta = session_to_remote(
        _session(jsonl, sid="sid-1", name="Mi sesión", tags=("bug", "urgente")),
        published_by="quien@example.com",
        git_remote="git@git.example.com:group/repo.git",
        git_head="abc1234",
        forked_from="sid-0",
        published_at="2026-07-28T10:00:00+00:00",
    )
    remote.publish(meta, project)

    (listed,) = remote.list_sessions()
    assert listed == meta
    assert listed.display_name == "Mi sesión"
    assert listed.tags == ("bug", "urgente")
    assert listed.git_head == "abc1234"
    assert listed.forked_from == "sid-0"
    assert listed.published_by == "quien@example.com"


def test_transcript_is_compressed_on_the_remote(tmp_path: Path) -> None:
    project = tmp_path / "project"
    jsonl = write_session(project, session_id="sid-1", extra_events=400)
    remote = DirectoryRemote(tmp_path / "remote")
    _publish(remote, project, "sid-1")

    blob = tmp_path / "remote" / "blobs" / "sid-1" / "session.jsonl.gz"
    assert blob.stat().st_size < jsonl.stat().st_size
    assert gzip.decompress(blob.read_bytes()) == jsonl.read_bytes()


# --- what must never travel ---------------------------------------------------------


def test_session_env_is_never_published(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    env = project / "sid-1" / "session-env"
    env.mkdir(parents=True)
    (env / "secrets.json").write_text('{"TOKEN":"no-subir"}', encoding="utf-8")

    assert not any("session-env" in p.parts for p in collect_session_files(project, "sid-1"))

    remote = DirectoryRemote(tmp_path / "remote")
    _publish(remote, project, "sid-1")
    published = [p.as_posix() for p in (tmp_path / "remote").rglob("*") if p.is_file()]
    assert not any("session-env" in name for name in published)
    assert not any(
        "no-subir" in p.read_text(encoding="utf-8", errors="replace")
        for p in (tmp_path / "remote").rglob("*")
        if p.is_file() and p.suffix == ".json"
    )


def test_project_memory_dir_is_never_published(tmp_path: Path) -> None:
    """``memory/`` is the employee's own auto-memory and sits outside any session."""
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    memory = project / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("notas personales", encoding="utf-8")

    remote = DirectoryRemote(tmp_path / "remote")
    _publish(remote, project, "sid-1")

    published = [p for p in (tmp_path / "remote").rglob("*") if p.is_file()]
    assert not any("memory" in p.parts for p in published)
    assert not any(b"notas personales" in p.read_bytes() for p in published)


# --- refusals ----------------------------------------------------------------------


def test_fetch_refuses_to_overwrite_an_existing_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote = DirectoryRemote(tmp_path / "remote")
    _publish(remote, project, "sid-1")

    with pytest.raises(RemoteError, match="ya existe en destino"):
        remote.fetch("sid-1", project)


def test_fetch_of_unknown_session_fails_cleanly(tmp_path: Path) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    with pytest.raises(RemoteError, match="no está en el remoto"):
        remote.fetch("sid-nope", tmp_path / "dest")


def test_publish_without_a_transcript_on_disk_fails(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    remote = DirectoryRemote(tmp_path / "remote")
    meta = RemoteSession(session_id="sid-1", published_at="2026-07-28T10:00:00+00:00")
    with pytest.raises(RemoteError, match="no tiene transcript en disco"):
        remote.publish(meta, project)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 65, "..", "sid 1"])
def test_ids_that_could_escape_the_store_are_refused(tmp_path: Path, bad: str) -> None:
    remote = DirectoryRemote(tmp_path / "remote")
    with pytest.raises(RemoteError, match="id de sesión no válido"):
        remote.fetch(bad, tmp_path / "dest")


def test_blob_name_cannot_escape_the_session_tree(tmp_path: Path) -> None:
    """A crafted blob name must not let a fetch write outside the session's own dir."""
    with pytest.raises(RemoteError, match="ruta sospechosa"):
        local_path_for(tmp_path, "sid-1", "../../../etc/passwd")


# --- tolerating a broken remote ----------------------------------------------------


def test_a_corrupt_manifest_does_not_hide_the_healthy_ones(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-good")
    remote = DirectoryRemote(tmp_path / "remote")
    _publish(remote, project, "sid-good")
    (tmp_path / "remote" / "manifest" / "sid-bad.json").write_text("{no es json", encoding="utf-8")

    listed = remote.list_sessions()
    assert [s.session_id for s in listed] == ["sid-good"]


def test_an_unknown_manifest_version_is_refused_with_a_readable_error() -> None:
    with pytest.raises(RemoteError, match="versión de manifest no soportada"):
        RemoteSession.from_manifest({"format": FORMAT, "version": VERSION + 1, "id": "sid-1"})


def test_a_foreign_manifest_is_refused() -> None:
    with pytest.raises(RemoteError, match="no parece un manifest"):
        RemoteSession.from_manifest({"format": "otra-cosa", "version": VERSION, "id": "sid-1"})


def test_unknown_manifest_keys_are_ignored_for_forward_compatibility() -> None:
    parsed = RemoteSession.from_manifest(
        {
            "format": FORMAT,
            "version": VERSION,
            "id": "sid-1",
            "published_at": "2026-07-28T10:00:00+00:00",
            "algo_del_futuro": {"x": 1},
        }
    )
    assert parsed.session_id == "sid-1"


def test_listing_an_empty_remote_returns_nothing(tmp_path: Path) -> None:
    assert DirectoryRemote(tmp_path / "remote").list_sessions() == ()


def test_get_session_returns_none_for_a_missing_manifest(tmp_path: Path) -> None:
    assert DirectoryRemote(tmp_path / "remote").get_session("sid-1") is None


def test_a_failed_upload_leaves_no_manifest_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is written last, so a partial upload is invisible, not broken."""
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote = DirectoryRemote(tmp_path / "remote")

    import multi_claude.remote as remote_mod

    def boom(target: Path, payload: bytes) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(remote_mod, "_atomic_write", boom)
    with pytest.raises(OSError):
        _publish(remote, project, "sid-1")

    assert not (tmp_path / "remote" / "manifest" / "sid-1.json").exists()
    assert remote.list_sessions() == ()


def test_manifest_on_disk_declares_format_and_version(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_session(project, session_id="sid-1")
    remote = DirectoryRemote(tmp_path / "remote")
    _publish(remote, project, "sid-1")

    raw = json.loads((tmp_path / "remote" / "manifest" / "sid-1.json").read_text(encoding="utf-8"))
    assert raw["format"] == FORMAT
    assert raw["version"] == VERSION
    assert raw["id"] == "sid-1"


# --- picking a backend from settings ------------------------------------------------


def test_no_remote_configured_means_sharing_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    assert store_from_settings("none", "") is None
    assert store_from_settings("directory", "") is None


def test_a_configured_directory_becomes_a_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REMOTE_DIR_ENV, raising=False)
    store = store_from_settings("directory", "/srv/sesiones")
    assert isinstance(store, DirectoryRemote)
    assert store.root == Path("/srv/sesiones")


def test_the_env_var_overrides_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """So trying a remote is a one-liner, and a test never writes shared state."""
    monkeypatch.setenv(REMOTE_DIR_ENV, "/tmp/otro-remoto")
    store = store_from_settings("directory", "/srv/sesiones")
    assert isinstance(store, DirectoryRemote)
    assert store.root == Path("/tmp/otro-remoto")


def test_the_env_var_works_even_with_sharing_off_in_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REMOTE_DIR_ENV, "/tmp/otro-remoto")
    assert isinstance(store_from_settings("none", ""), DirectoryRemote)


def test_a_store_names_its_destination(tmp_path: Path) -> None:
    """The publish confirmation shows this, so it has to be the path, not a repr."""
    assert str(DirectoryRemote(tmp_path / "remoto")) == str(tmp_path / "remoto")
