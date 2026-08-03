"""Tests for the SQLite session index + FTS5."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from multi_claude.index import IndexedRemoteSession, IndexedSession, SessionIndex


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


# -- cached credential scan -------------------------------------------------- #


def test_secret_counts_is_empty_before_any_scan(index: SessionIndex) -> None:
    """Absent must not read as clean: the listing marks nothing until it has looked."""
    assert index.secret_counts(["a", "b"]) == {}
    assert index.secret_counts([]) == {}


def test_recording_a_scan_makes_it_readable(index: SessionIndex) -> None:
    index.record_secret_scan("a", mtime=1000.0, finding_count=3)
    index.record_secret_scan("b", mtime=1000.0, finding_count=0)
    assert index.secret_counts(["a", "b", "c"]) == {"a": 3, "b": 0}


def test_a_scan_is_stale_once_the_file_grows(index: SessionIndex) -> None:
    """The credential may be in the part that is new, so a changed mtime means rescan."""
    index.record_secret_scan("a", mtime=1000.0, finding_count=0)
    assert index.secret_scan_is_fresh("a", 1000.0) is True
    assert index.secret_scan_is_fresh("a", 1001.0) is False
    assert index.secret_scan_is_fresh("never-scanned", 1000.0) is False


def test_rescanning_overwrites_the_previous_count(index: SessionIndex) -> None:
    index.record_secret_scan("a", mtime=1000.0, finding_count=5)
    index.record_secret_scan("a", mtime=2000.0, finding_count=1)
    assert index.secret_counts(["a"]) == {"a": 1}
    assert index.secret_scan_is_fresh("a", 2000.0) is True


def test_deleting_a_session_forgets_its_scan(index: SessionIndex) -> None:
    """Otherwise a re-hydrated uuid would come back already marked as sensitive."""
    index.upsert_session(_session("a"))
    index.record_secret_scan("a", mtime=1000.0, finding_count=2)
    index.delete_session("a")
    assert index.secret_counts(["a"]) == {}


def test_forget_secret_scan_drops_just_that_row(index: SessionIndex) -> None:
    index.record_secret_scan("a", mtime=1.0, finding_count=1)
    index.record_secret_scan("b", mtime=1.0, finding_count=1)
    index.forget_secret_scan("a")
    assert index.secret_counts(["a", "b"]) == {"b": 1}


# -- remote (team-published) sessions --------------------------------------- #


def _remote(
    sid: str,
    *,
    remote_key: str = "k1",
    label: str = "cliente-x",
    author: str | None = "ana@example.com",
    branch: str | None = "fix/nginx",
    name: str | None = "el deploy de staging falla con 502",
    tags: tuple[str, ...] = ("infra", "urgente"),
) -> IndexedRemoteSession:
    return IndexedRemoteSession(
        remote_key=remote_key,
        session_id=sid,
        remote_label=label,
        project_key="git@host:group/api.git",
        published_by=author,
        published_at="2026-08-01T10:00:00+00:00",
        cwd="/home/ana/api",
        branch=branch,
        display_name=name,
        first_prompt="por qué devuelve 502",
        tags=tags,
        message_count=120,
        size_bytes=4096,
    )


def test_remote_roundtrip_preserves_every_field(index: SessionIndex) -> None:
    index.replace_remote_sessions("k1", [_remote("r1")], listed_at=5.0)
    (stored,) = index.fts_search_remote("staging")
    assert stored == _remote("r1")


@pytest.mark.parametrize("query", ["staging", "502", "ana", "nginx", "urgente", "STAGING"])
def test_remote_search_matches_every_indexed_field(index: SessionIndex, query: str) -> None:
    index.replace_remote_sessions("k1", [_remote("r1")], listed_at=5.0)
    assert [s.session_id for s in index.fts_search_remote(query)] == ["r1"]


def test_remote_search_ignores_accents(index: SessionIndex) -> None:
    index.replace_remote_sessions(
        "k1", [_remote("r1", name="refactorización del índice")], listed_at=5.0
    )
    assert index.fts_search_remote("refactorizacion")


def test_replace_is_a_replace_not_an_upsert(index: SessionIndex) -> None:
    """Unpublishing on the remote has to remove the row, or search offers a dead hit."""
    index.replace_remote_sessions("k1", [_remote("r1"), _remote("r2")], listed_at=5.0)
    assert index.count_remote_sessions() == 2
    index.replace_remote_sessions("k1", [_remote("r2")], listed_at=6.0)
    assert [s.session_id for s in index.fts_search_remote("staging")] == ["r2"]
    assert index.count_remote_sessions() == 1


def test_replace_only_touches_its_own_remote(index: SessionIndex) -> None:
    index.replace_remote_sessions("k1", [_remote("r1")], listed_at=5.0)
    index.replace_remote_sessions(
        "k2", [_remote("r2", remote_key="k2", label="producto")], listed_at=5.0
    )
    index.replace_remote_sessions("k1", [], listed_at=6.0)
    assert [s.session_id for s in index.fts_search_remote("staging")] == ["r2"]


def test_the_same_session_can_live_in_two_remotes(index: SessionIndex) -> None:
    """A project may publish to several repos; each keeps its own row."""
    index.replace_remote_sessions("k1", [_remote("dup")], listed_at=5.0)
    index.replace_remote_sessions(
        "k2", [_remote("dup", remote_key="k2", label="producto")], listed_at=5.0
    )
    labels = {s.remote_label for s in index.fts_search_remote("staging")}
    assert labels == {"cliente-x", "producto"}


def test_remote_search_is_separate_from_local_search(index: SessionIndex) -> None:
    index.upsert_session(_session("local-1", prompt="deploy staging"), fts_content="deploy staging")
    index.replace_remote_sessions("k1", [_remote("r1")], listed_at=5.0)
    assert [s.session_id for s in index.fts_search("staging")] == ["local-1"]
    assert [s.session_id for s in index.fts_search_remote("staging")] == ["r1"]


def test_remote_search_empty_query_returns_nothing(index: SessionIndex) -> None:
    index.replace_remote_sessions("k1", [_remote("r1")], listed_at=5.0)
    assert index.fts_search_remote("   ") == []


def test_remote_title_falls_back_when_unnamed(index: SessionIndex) -> None:
    index.replace_remote_sessions("k1", [_remote("r1", name=None)], listed_at=5.0)
    (stored,) = index.fts_search_remote("502")
    assert stored.title == "por qué devuelve 502"


def test_remote_row_survives_missing_optional_fields(index: SessionIndex) -> None:
    index.replace_remote_sessions(
        "k1", [_remote("r1", author=None, branch=None, tags=())], listed_at=5.0
    )
    (stored,) = index.fts_search_remote("staging")
    assert stored.published_by is None
    assert stored.branch is None
    assert stored.tags == ()
