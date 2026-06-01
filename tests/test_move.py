"""Tests for moving a single session between projects (deletion.move_session)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multi_claude.deletion import (
    SessionActiveError,
    SessionCollisionError,
    move_session,
)
from multi_claude.index import IndexedSession, SessionIndex
from tests.conftest import write_session


@pytest.fixture
def fresh_index(tmp_path: Path) -> SessionIndex:
    return SessionIndex(tmp_path / "index.sqlite3")


@pytest.fixture
def isolated_active_dir(tmp_path: Path) -> Path:
    return tmp_path / "no-active"


def _move_kwargs(*, active: Path, index: SessionIndex) -> dict[str, object]:
    return {"active_sessions_dir": active, "index": index}


def test_move_session_relocates_jsonl_and_subdir(
    tmp_path: Path, isolated_active_dir: Path, fresh_index: SessionIndex
) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    write_session(source, session_id="sid-1", cwd=str(source), first_prompt="hola")
    (source / "sid-1").mkdir()
    (source / "sid-1" / "data.txt").write_text("payload", encoding="utf-8")

    move_session(
        "sid-1", source, dest, **_move_kwargs(active=isolated_active_dir, index=fresh_index)
    )

    assert not (source / "sid-1.jsonl").exists()
    assert not (source / "sid-1").exists()
    assert (dest / "sid-1.jsonl").exists()
    assert (dest / "sid-1" / "data.txt").read_text(encoding="utf-8") == "payload"


def test_move_session_updates_index_row(
    tmp_path: Path, isolated_active_dir: Path, fresh_index: SessionIndex
) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    jsonl = write_session(source, session_id="sid-x", cwd=str(source))
    fresh_index.upsert_session(
        IndexedSession(
            session_id="sid-x",
            project_dir=str(source),
            cwd=str(source),
            branch="main",
            first_prompt="hola",
            message_count=3,
            size_bytes=jsonl.stat().st_size,
            mtime=jsonl.stat().st_mtime,
            jsonl_path=str(jsonl),
        )
    )

    move_session(
        "sid-x", source, dest, **_move_kwargs(active=isolated_active_dir, index=fresh_index)
    )

    row = fresh_index.get("sid-x")
    assert row is not None
    assert row.project_dir == str(dest)
    assert row.jsonl_path == str(dest / "sid-x.jsonl")
    # Unrelated metadata is preserved.
    assert row.branch == "main"
    assert row.first_prompt == "hola"


def test_move_session_missing_source_is_noop(
    tmp_path: Path, isolated_active_dir: Path, fresh_index: SessionIndex
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    dest = tmp_path / "dest"
    move_session(
        "ghost", source, dest, **_move_kwargs(active=isolated_active_dir, index=fresh_index)
    )
    assert not (dest / "ghost.jsonl").exists()


def test_move_session_blocks_on_collision(
    tmp_path: Path, isolated_active_dir: Path, fresh_index: SessionIndex
) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    write_session(source, session_id="sid-dup", first_prompt="source version")
    write_session(dest, session_id="sid-dup", first_prompt="dest version")

    with pytest.raises(SessionCollisionError) as excinfo:
        move_session(
            "sid-dup", source, dest, **_move_kwargs(active=isolated_active_dir, index=fresh_index)
        )

    assert excinfo.value.session_id == "sid-dup"
    # Both files stay put; no silent overwrite.
    assert "source version" in (source / "sid-dup.jsonl").read_text(encoding="utf-8")
    assert "dest version" in (dest / "sid-dup.jsonl").read_text(encoding="utf-8")


def test_move_session_blocks_on_active(tmp_path: Path, fresh_index: SessionIndex) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    active = tmp_path / "active"
    active.mkdir()
    (active / "host.json").write_text(json.dumps({"sessionId": "sid-live"}), encoding="utf-8")
    write_session(source, session_id="sid-live", cwd=str(source))

    with pytest.raises(SessionActiveError) as excinfo:
        move_session("sid-live", source, dest, active_sessions_dir=active, index=fresh_index)

    assert excinfo.value.active_ids == {"sid-live"}
    assert (source / "sid-live.jsonl").exists()
    assert not (dest / "sid-live.jsonl").exists()


def test_move_session_force_bypasses_active_guard(
    tmp_path: Path, fresh_index: SessionIndex
) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    active = tmp_path / "active"
    active.mkdir()
    (active / "host.json").write_text(json.dumps({"sessionId": "sid-live"}), encoding="utf-8")
    write_session(source, session_id="sid-live", cwd=str(source))

    move_session(
        "sid-live", source, dest, active_sessions_dir=active, index=fresh_index, force=True
    )

    assert not (source / "sid-live.jsonl").exists()
    assert (dest / "sid-live.jsonl").exists()
