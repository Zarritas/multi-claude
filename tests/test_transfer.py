"""Tests for session export/import (multi_claude.transfer)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from multi_claude.names import NamesStore
from multi_claude.session import Session
from multi_claude.tags import TagsStore
from multi_claude.transfer import (
    FORMAT,
    VERSION,
    ArchiveError,
    export_sessions,
    import_archive,
    read_manifest,
    safe_filename,
)
from tests.conftest import write_session


def _session(
    jsonl: Path, *, sid: str, name: str | None = None, tags: tuple[str, ...] = ()
) -> Session:
    stat = jsonl.stat()
    return Session(
        id=sid,
        path=jsonl,
        first_prompt="hola",
        branch="main",
        cwd="/old/cwd",
        message_count=3,
        size_bytes=stat.st_size,
        last_activity=stat.st_mtime,
        display_name=name,
        tags=tags,
    )


def test_export_then_read_manifest_round_trips(tmp_path: Path) -> None:
    project = tmp_path / "project"
    j1 = write_session(project, session_id="sid-1", first_prompt="primero")
    j2 = write_session(project, session_id="sid-2", first_prompt="segundo")
    (project / "sid-1").mkdir()
    (project / "sid-1" / "agent.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "out" / "export.zip"

    count = export_sessions(
        [
            _session(j1, sid="sid-1", name="Mi sesión", tags=("bug", "urgente")),
            _session(j2, sid="sid-2"),
        ],
        project,
        dest,
    )

    assert count == 2
    assert dest.exists()
    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "sessions/sid-1.jsonl" in names
    assert "sessions/sid-1/agent.json" in names
    assert "sessions/sid-2.jsonl" in names

    sessions = read_manifest(dest)
    by_id = {s.session_id: s for s in sessions}
    assert set(by_id) == {"sid-1", "sid-2"}
    assert by_id["sid-1"].display_name == "Mi sesión"
    assert by_id["sid-1"].tags == ("bug", "urgente")


def test_export_skips_missing_files_and_writes_nothing_when_empty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    ghost = Session(
        id="ghost",
        path=project / "ghost.jsonl",
        first_prompt="x",
        branch=None,
        cwd=None,
        message_count=0,
        size_bytes=0,
        last_activity=0.0,
        display_name=None,
    )
    dest = tmp_path / "export.zip"

    assert export_sessions([ghost], project, dest) == 0
    assert not dest.exists()


def test_import_round_trip_restores_files_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    j1 = write_session(source, session_id="sid-1", first_prompt="primero")
    (source / "sid-1").mkdir()
    (source / "sid-1" / "agent.json").write_text("payload", encoding="utf-8")
    archive = tmp_path / "export.zip"
    export_sessions([_session(j1, sid="sid-1", name="Etiqueta", tags=("rev",))], source, archive)

    dest = tmp_path / "dest"
    names = NamesStore(tmp_path / "names.json")
    tags = TagsStore(tmp_path / "tags.json")

    result = import_archive(archive, dest, names_store=names, tags_store=tags)

    assert result.imported == ("sid-1",)
    assert result.skipped_existing == ()
    assert (dest / "sid-1.jsonl").exists()
    assert (dest / "sid-1" / "agent.json").read_text(encoding="utf-8") == "payload"
    assert names.get("sid-1") == "Etiqueta"
    assert tags.get("sid-1") == ("rev",)


def test_import_skips_existing_session(tmp_path: Path) -> None:
    source = tmp_path / "source"
    j1 = write_session(source, session_id="sid-dup", first_prompt="archive version")
    archive = tmp_path / "export.zip"
    export_sessions([_session(j1, sid="sid-dup")], source, archive)

    dest = tmp_path / "dest"
    write_session(dest, session_id="sid-dup", first_prompt="local version")

    result = import_archive(
        archive,
        dest,
        names_store=NamesStore(tmp_path / "n.json"),
        tags_store=TagsStore(tmp_path / "t.json"),
    )

    assert result.imported == ()
    assert result.skipped_existing == ("sid-dup",)
    # Local copy untouched.
    assert "local version" in (dest / "sid-dup.jsonl").read_text(encoding="utf-8")


def _write_zip(path: Path, manifest: object, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        if manifest is not None:
            zf.writestr("manifest.json", json.dumps(manifest))
        for name, content in members.items():
            zf.writestr(name, content)


def test_read_manifest_rejects_non_zip(tmp_path: Path) -> None:
    bogus = tmp_path / "x.zip"
    bogus.write_text("not a zip", encoding="utf-8")
    with pytest.raises(ArchiveError):
        read_manifest(bogus)


def test_read_manifest_rejects_missing_manifest(tmp_path: Path) -> None:
    path = tmp_path / "x.zip"
    _write_zip(path, None, {"sessions/sid.jsonl": "{}"})
    with pytest.raises(ArchiveError):
        read_manifest(path)


def test_read_manifest_rejects_wrong_format_and_version(tmp_path: Path) -> None:
    bad_format = tmp_path / "a.zip"
    _write_zip(bad_format, {"format": "something-else", "version": VERSION, "sessions": []}, {})
    with pytest.raises(ArchiveError):
        read_manifest(bad_format)

    bad_version = tmp_path / "b.zip"
    _write_zip(
        bad_version,
        {"format": FORMAT, "version": 999, "sessions": [{"id": "x"}]},
        {},
    )
    with pytest.raises(ArchiveError):
        read_manifest(bad_version)


def test_read_manifest_rejects_empty_session_list(tmp_path: Path) -> None:
    path = tmp_path / "x.zip"
    _write_zip(path, {"format": FORMAT, "version": VERSION, "sessions": []}, {})
    with pytest.raises(ArchiveError):
        read_manifest(path)


def test_import_guards_against_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "sessions": [{"id": "evil", "tags": []}],
    }
    _write_zip(
        archive,
        manifest,
        {
            "sessions/evil.jsonl": "{}",
            "sessions/evil/../../escaped.txt": "pwned",
        },
    )
    dest = tmp_path / "dest"
    with pytest.raises(ArchiveError):
        import_archive(
            archive,
            dest,
            names_store=NamesStore(tmp_path / "n.json"),
            tags_store=TagsStore(tmp_path / "t.json"),
        )
    assert not (tmp_path / "escaped.txt").exists()


def test_safe_filename_sanitises() -> None:
    assert safe_filename("refactor: el módulo/x") == "refactor-el-módulo-x"
    assert safe_filename("") == "session"
    assert safe_filename("///") == "session"
