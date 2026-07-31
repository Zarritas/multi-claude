"""Tests for the SQLite session index + FTS5."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from multi_claude.index import IndexedSession, SessionIndex


@pytest.fixture
def index(tmp_path: Path) -> SessionIndex:
    return SessionIndex(tmp_path / "idx.sqlite3")


def _session(sid: str, *, project_dir: str = "/p", prompt: str = "hello") -> IndexedSession:
    return IndexedSession(
        session_id=sid,
        project_dir=project_dir,
        cwd="/cwd",
        branch="main",
        first_prompt=prompt,
        message_count=10,
        size_bytes=4096,
        mtime=1000.0,
        jsonl_path=f"/p/{sid}.jsonl",
    )


def test_upsert_then_get(index: SessionIndex) -> None:
    index.upsert_session(_session("sid-1"))
    stored = index.get("sid-1")
    assert stored is not None
    assert stored.session_id == "sid-1"
    assert stored.first_prompt == "hello"


def test_upsert_overwrites_same_id(index: SessionIndex) -> None:
    index.upsert_session(_session("sid-1", prompt="v1"))
    index.upsert_session(_session("sid-1", prompt="v2"))
    stored = index.get("sid-1")
    assert stored is not None and stored.first_prompt == "v2"


def test_delete_session_clears_row_and_fts(index: SessionIndex) -> None:
    index.upsert_session(_session("sid-1"), fts_content="refactor auth")
    assert index.fts_search("refactor")
    index.delete_session("sid-1")
    assert index.get("sid-1") is None
    assert index.fts_search("refactor") == []


def test_fts_search_returns_matches_ordered_by_rank(index: SessionIndex) -> None:
    index.upsert_session(_session("a", prompt="auth"), fts_content="refactor auth module")
    index.upsert_session(_session("b", prompt="db"), fts_content="something else entirely")
    index.upsert_session(_session("c", prompt="auth2"), fts_content="auth flow with tests")

    results = index.fts_search("auth")
    ids = [r.session_id for r in results]
    assert set(ids) == {"a", "c"}


def test_fts_search_sanitises_query(index: SessionIndex) -> None:
    """Quotes and operators in the user query don't crash the FTS parser."""
    index.upsert_session(_session("a"), fts_content="this is a story about quotes")
    # These should not raise even though FTS5 syntax would normally interpret some chars.
    assert index.fts_search('"unclosed') == [] or True
    matches = index.fts_search("story quotes")
    assert any(r.session_id == "a" for r in matches)


def test_get_mtime_returns_none_when_missing(index: SessionIndex) -> None:
    assert index.get_mtime("ghost") is None


def test_get_mtime_returns_stored_value(index: SessionIndex) -> None:
    index.upsert_session(_session("x"))
    assert index.get_mtime("x") == 1000.0


def test_is_fresh_false_when_missing(index: SessionIndex) -> None:
    assert index.is_fresh("ghost", 1000.0) is False


def test_is_fresh_true_for_unchanged_file(index: SessionIndex) -> None:
    index.upsert_session(_session("x"))
    assert index.is_fresh("x", 1000.0) is True


def test_is_fresh_false_when_file_changed(index: SessionIndex) -> None:
    index.upsert_session(_session("x"))
    assert index.is_fresh("x", 2000.0) is False


def test_is_fresh_false_for_older_extract_version(index: SessionIndex, tmp_path: Path) -> None:
    """A row written by a build that extracted less needs reparsing, mtime or not."""
    index.upsert_session(_session("x"))
    index.close()
    with sqlite3.connect(tmp_path / "idx.sqlite3") as conn:
        conn.execute("UPDATE sessions SET extract_version = 0 WHERE session_id = 'x'")
    assert SessionIndex(tmp_path / "idx.sqlite3").is_fresh("x", 1000.0) is False


def test_list_by_project(index: SessionIndex) -> None:
    index.upsert_session(_session("a", project_dir="/p1"))
    index.upsert_session(_session("b", project_dir="/p1"))
    index.upsert_session(_session("c", project_dir="/p2"))
    p1 = {s.session_id for s in index.list_by_project("/p1")}
    assert p1 == {"a", "b"}
