"""`file:` — which conversation edited a given file.

Three layers, tested where each can be wrong: the extractor (what counts as touching a
file), the index (the SQL that answers it), and the filter (the same question in Python,
over rows already on screen). The last two ask the same thing two ways, so there is a test
holding them against each other — a divergence there shows up as the listing and the global
search disagreeing about the same session, which is the kind of bug nobody reports.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multi_claude.filtering import file_matches, parse_query
from multi_claude.index import EXTRACT_VERSION, IndexedSession, SessionIndex, basename
from multi_claude.screens.search import _split_file_term
from multi_claude.session import extract_indexables, extract_touched_files, scan_sessions

from .conftest import write_session


def index_at(tmp_path: Path) -> SessionIndex:
    return SessionIndex(tmp_path / "index.sqlite3")


def indexed(session_id: str, *, mtime: float = 1.0, jsonl: str = "/x/s.jsonl") -> IndexedSession:
    return IndexedSession(
        session_id=session_id,
        project_dir="/proj",
        cwd="/repo",
        branch="main",
        first_prompt="hola",
        message_count=3,
        size_bytes=10,
        mtime=mtime,
        jsonl_path=jsonl,
    )


# --- the extractor ---------------------------------------------------------------------


def test_an_edit_is_a_touch(projects_root: Path) -> None:
    jsonl = write_session(projects_root / "p", edited_files=("/repo/src/app.py",))
    assert extract_touched_files(jsonl) == ("/repo/src/app.py",)


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_every_editing_tool_counts(projects_root: Path, tool: str) -> None:
    jsonl = write_session(projects_root / "p", edited_files=("/repo/a.py",), edit_tool=tool)
    assert extract_touched_files(jsonl) == ("/repo/a.py",)


def test_a_notebook_edit_carries_its_path_under_another_key(projects_root: Path) -> None:
    jsonl = write_session(
        projects_root / "p", edited_files=("/repo/nb.ipynb",), edit_tool="NotebookEdit"
    )
    assert extract_touched_files(jsonl) == ("/repo/nb.ipynb",)


def test_reading_a_file_is_not_touching_it(projects_root: Path) -> None:
    """Otherwise a session that greps around would claim to have edited half the tree."""
    jsonl = write_session(projects_root / "p", edited_files=("/repo/a.py",), edit_tool="Read")
    assert extract_touched_files(jsonl) == ()


def test_a_bash_command_is_not_mined_for_paths(projects_root: Path) -> None:
    """`sed -i` edits a file, but recovering that from a command line means parsing shell."""
    jsonl = write_session(
        projects_root / "p", edited_files=("sed -i s/a/b/ /repo/a.py",), edit_tool="Bash"
    )
    assert extract_touched_files(jsonl) == ()


def test_the_same_file_twice_is_one_entry(projects_root: Path) -> None:
    jsonl = write_session(projects_root / "p", edited_files=("/repo/a.py", "/repo/a.py"))
    assert extract_touched_files(jsonl) == ("/repo/a.py",)


def test_order_is_first_touch_first(projects_root: Path) -> None:
    jsonl = write_session(projects_root / "p", edited_files=("/repo/b.py", "/repo/a.py"))
    assert extract_touched_files(jsonl) == ("/repo/b.py", "/repo/a.py")


def test_a_malformed_line_does_not_lose_the_rest(projects_root: Path) -> None:
    jsonl = write_session(projects_root / "p", edited_files=("/repo/a.py",))
    with jsonl.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/z"}}
                        ],
                    },
                }
            )
            + "\n"
        )
    assert extract_touched_files(jsonl) == ("/repo/a.py", "/repo/z")


def test_text_and_files_come_out_of_one_pass(projects_root: Path) -> None:
    """They are two questions about the same megabytes; reading twice would double the scan."""
    jsonl = write_session(
        projects_root / "p", first_prompt="arregla el parser", edited_files=("/repo/parser.py",)
    )
    both = extract_indexables(jsonl)
    assert "arregla el parser" in both.fts_content
    assert both.touched_files == ("/repo/parser.py",)


def test_the_fts_cap_does_not_stop_the_file_scan(projects_root: Path, monkeypatch) -> None:
    """A long conversation must not go half-mapped: paths cost bytes, and the question
    is about the whole session."""
    monkeypatch.setattr("multi_claude.session.FTS_CONTENT_MAX_CHARS", 20)
    jsonl = write_session(
        projects_root / "p", first_prompt="x" * 200, edited_files=("/repo/late.py",)
    )
    both = extract_indexables(jsonl)
    assert len(both.fts_content) <= 20
    assert both.touched_files == ("/repo/late.py",)


def test_the_number_of_files_is_capped(projects_root: Path, monkeypatch) -> None:
    monkeypatch.setattr("multi_claude.session.TOUCHED_FILES_MAX", 3)
    jsonl = write_session(
        projects_root / "p", edited_files=tuple(f"/repo/f{i}.py" for i in range(10))
    )
    assert len(extract_touched_files(jsonl)) == 3


# --- the index -------------------------------------------------------------------------


def test_the_index_answers_by_basename(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/src/app.py",))
    assert [s.session_id for s in idx.sessions_touching("app.py")] == ["a"]


def test_a_fragment_of_the_name_is_enough(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/src/launcher.py",))
    assert [s.session_id for s in idx.sessions_touching("launch")] == ["a"]


def test_a_term_with_a_slash_is_matched_against_the_path(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/src/app.py",))
    idx.upsert_session(indexed("b"), touched_files=("/other/lib/app.py",))
    assert [s.session_id for s in idx.sessions_touching("src/app.py")] == ["a"]


def test_the_match_is_case_insensitive(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/README.md",))
    assert [s.session_id for s in idx.sessions_touching("readme")] == ["a"]


def test_an_underscore_is_not_a_wildcard(tmp_path: Path) -> None:
    """LIKE reads `_` as "any character": unescaped, this would match test-x.py too."""
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/test_x.py",))
    idx.upsert_session(indexed("b"), touched_files=("/repo/test-x.py",))
    assert [s.session_id for s in idx.sessions_touching("test_x.py")] == ["a"]


def test_a_percent_is_not_a_wildcard(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/a.py",))
    assert idx.sessions_touching("%") == []


def test_results_come_most_recent_first(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("old", mtime=1.0), touched_files=("/repo/a.py",))
    idx.upsert_session(indexed("new", mtime=99.0), touched_files=("/repo/a.py",))
    assert [s.session_id for s in idx.sessions_touching("a.py")] == ["new", "old"]


def test_one_session_touching_a_file_twice_is_one_row(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/a.py", "/repo/sub/a.py"))
    assert [s.session_id for s in idx.sessions_touching("a.py")] == ["a"]


def test_a_reparse_replaces_the_file_list(tmp_path: Path) -> None:
    """Merging would accumulate paths no version of the conversation touches."""
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/gone.py",))
    idx.upsert_session(indexed("a"), touched_files=("/repo/now.py",))
    assert idx.sessions_touching("gone.py") == []
    assert [s.session_id for s in idx.sessions_touching("now.py")] == ["a"]


def test_upserting_without_files_leaves_the_list_alone(tmp_path: Path) -> None:
    """None means "not extracted this time", which is not the same as "edited nothing"."""
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/a.py",))
    idx.upsert_session(indexed("a"))
    assert [s.session_id for s in idx.sessions_touching("a.py")] == ["a"]


def test_files_for_sessions_maps_ids_to_paths(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/a.py",))
    idx.upsert_session(indexed("b"), touched_files=())
    assert idx.files_for_sessions(["a", "b"]) == {"a": ("/repo/a.py",)}


def test_deleting_a_session_takes_its_files(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/a.py",))
    idx.delete_session("a")
    assert idx.sessions_touching("a.py") == []


def test_purging_a_missing_jsonl_takes_its_files(tmp_path: Path) -> None:
    """Or a deleted session keeps answering `file:` with a row nobody can open."""
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a", jsonl=str(tmp_path / "gone.jsonl")), touched_files=("/r/a.py",))
    assert idx.purge_missing() == 1
    assert idx.sessions_touching("a.py") == []


def test_an_empty_term_asks_nothing(tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=("/repo/a.py",))
    assert idx.sessions_touching("   ") == []


# --- the filter ------------------------------------------------------------------------


def test_file_is_a_known_key() -> None:
    assert parse_query("file:app.py").constraints == {"file": "app.py"}


def test_the_filter_matches_by_basename() -> None:
    assert file_matches("app.py", ("/repo/src/app.py",))


def test_the_filter_matches_a_path_when_the_term_has_a_slash() -> None:
    assert file_matches("src/app.py", ("/repo/src/app.py",))
    assert not file_matches("src/app.py", ("/repo/lib/app.py",))


def test_the_filter_ignores_case() -> None:
    assert file_matches("APP", ("/repo/app.py",))


def test_the_filter_says_no_when_there_is_nothing_to_match() -> None:
    assert not file_matches("app.py", ())


def test_a_windows_path_still_has_a_basename() -> None:
    """A colleague's published session can carry them onto a Linux box."""
    assert basename("C:\\src\\app.py") == "app.py"
    assert file_matches("app.py", ("C:\\src\\app.py",))


@pytest.mark.parametrize(
    "term,path",
    [
        ("app.py", "/repo/src/app.py"),
        ("app", "/repo/src/app.py"),
        ("src/app.py", "/repo/src/app.py"),
        ("nope.py", "/repo/src/app.py"),
        ("test_x.py", "/repo/test-x.py"),
    ],
)
def test_the_filter_and_the_index_agree(tmp_path: Path, term: str, path: str) -> None:
    """The listing (Python) and the global search (SQL) must answer the same question."""
    idx = index_at(tmp_path)
    idx.upsert_session(indexed("a"), touched_files=(path,))
    in_sql = bool(idx.sessions_touching(term))
    in_python = file_matches(term, (path,))
    assert in_sql == in_python, f"{term!r} vs {path!r}: sql={in_sql} python={in_python}"


# --- the global search's parsing -------------------------------------------------------


def test_a_query_with_no_file_term_is_untouched() -> None:
    assert _split_file_term("refactor del parser") == (None, "refactor del parser")


def test_the_file_term_is_lifted_out_of_the_query() -> None:
    assert _split_file_term("file:index.py refactor") == ("index.py", "refactor")


def test_other_keys_stay_in_the_text() -> None:
    """They are not filter keys here — FTS gets the query verbatim, as it always did."""
    assert _split_file_term("branch:main file:a.py") == ("a.py", "branch:main")


def test_a_bare_file_prefix_is_not_a_term() -> None:
    """`file:` with nothing after it is someone mid-typing, not a question."""
    assert _split_file_term("file:") == (None, "file:")


def test_the_last_file_term_wins() -> None:
    """Which is what an input re-searched on every keystroke tends to produce."""
    assert _split_file_term("file:a.py file:b.py") == ("b.py", "")


# --- end to end, through a real scan ---------------------------------------------------


def test_scanning_a_project_records_what_it_edited(projects_root: Path, tmp_path: Path) -> None:
    idx = index_at(tmp_path)
    project = projects_root / "-repo"
    write_session(project, session_id="s1", edited_files=("/repo/src/index.py",))
    scan_sessions(project, index=idx)
    assert [s.session_id for s in idx.sessions_touching("index.py")] == ["s1"]


def test_a_row_from_an_older_build_is_reparsed(projects_root: Path, tmp_path: Path) -> None:
    """EXTRACT_VERSION is the whole point: without the bump, `file:` would quietly miss
    every session indexed before it existed."""
    idx = index_at(tmp_path)
    project = projects_root / "-repo"
    jsonl = write_session(project, session_id="s1", edited_files=("/repo/src/index.py",))
    mtime = jsonl.stat().st_mtime
    # A row as an older build left it: same mtime, no files, stale extract_version.
    idx.upsert_session(
        IndexedSession(
            session_id="s1",
            project_dir=str(project),
            cwd="/repo",
            branch="main",
            first_prompt="hola",
            message_count=1,
            size_bytes=1,
            mtime=mtime,
            jsonl_path=str(jsonl),
        )
    )
    # Reaching into the connection on purpose: there is no public way to write a row that
    # claims an older build extracted it, which is the state under test.
    with idx._connection() as conn:
        conn.execute("UPDATE sessions SET extract_version = 2 WHERE session_id = 's1'")
    assert not idx.is_fresh("s1", mtime)
    scan_sessions(project, index=idx)
    assert idx.is_fresh("s1", mtime)
    assert [s.session_id for s in idx.sessions_touching("index.py")] == ["s1"]


def test_the_extract_version_is_ahead_of_the_one_that_lacked_files() -> None:
    assert EXTRACT_VERSION >= 3
