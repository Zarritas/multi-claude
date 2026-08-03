"""Tests for the shared filter parser + matcher."""

from __future__ import annotations

from multi_claude.filtering import FilterQuery, matches_fuzzy, parse_query
from multi_claude.remote import RemoteSession
from multi_claude.screens.search import _project_label
from multi_claude.screens.sessions import _remote_matches


def test_parse_empty_query() -> None:
    q = parse_query("")
    assert q.is_empty
    assert q.free_text == ""
    assert q.constraints == {}


def test_parse_plain_free_text() -> None:
    q = parse_query("refactor")
    assert q.free_text == "refactor"
    assert q.constraints == {}


def test_parse_branch_constraint() -> None:
    q = parse_query("branch:main feature")
    assert q.free_text == "feature"
    assert q.constraints == {"branch": "main"}


def test_parse_unknown_key_falls_back_to_free_text() -> None:
    """An unrecognised ``key:value`` token stays as free text — no silent ignore."""
    q = parse_query("color:red foo")
    assert q.free_text == "color:red foo"


def test_parse_id_and_path_constraints() -> None:
    q = parse_query("id:abc1234 path:gextia stuff")
    assert q.free_text == "stuff"
    assert q.constraints == {"id": "abc1234", "path": "gextia"}


def test_parse_tag_constraint() -> None:
    q = parse_query("tag:bug review")
    assert q.free_text == "review"
    assert q.constraints == {"tag": "bug"}


def test_parse_tag_constraint_with_comma_list() -> None:
    """Comma-separated lists are preserved verbatim — caller AND-matches each part."""
    q = parse_query("tag:bug,urgent")
    assert q.free_text == ""
    assert q.constraints == {"tag": "bug,urgent"}


def test_parse_author_constraint() -> None:
    q = parse_query("author:ana nginx")
    assert q.free_text == "nginx"
    assert q.constraints == {"author": "ana"}


def test_parse_author_is_lowercased_like_the_other_keys() -> None:
    """Emails come in mixed case; the matcher compares lowercased on both sides."""
    assert parse_query("Author:Ana@Example.com").constraints == {"author": "ana@example.com"}


def test_parse_author_without_a_value_is_free_text() -> None:
    assert parse_query("author:").free_text == "author:"


def test_matches_fuzzy_substring_wins() -> None:
    assert matches_fuzzy("refactor auth module", "refactor")


def test_matches_fuzzy_handles_typo() -> None:
    """A small typo still scores >= threshold via partial_ratio."""
    assert matches_fuzzy("refactor auth module", "refacto")


def test_matches_fuzzy_rejects_unrelated() -> None:
    assert not matches_fuzzy("database performance work", "snorkel")


def test_matches_fuzzy_empty_query_matches_anything() -> None:
    assert matches_fuzzy("whatever", "")


def test_filter_query_is_empty_with_only_constraints() -> None:
    q = FilterQuery(constraints={"branch": "main"})
    assert q.is_empty is False


# --- how a search hit names its project ----------------------------------------------


def _indexed(cwd: str | None, project_dir: str = "/home/me/.claude/projects/-home-me-tienda-api"):
    from multi_claude.index import IndexedSession

    return IndexedSession(
        session_id="s1",
        project_dir=project_dir,
        cwd=cwd,
        branch="main",
        first_prompt="hola",
        message_count=1,
        size_bytes=1,
        mtime=0.0,
        jsonl_path="/x.jsonl",
    )


def test_project_label_uses_the_real_directory_name() -> None:
    """Not the encoded dir: `-home-me-tienda-api` is unreadable in a column."""
    assert _project_label(_indexed("/home/me/tienda-api")) == "tienda-api"


def test_project_label_falls_back_to_the_encoded_dir_without_a_cwd() -> None:
    assert _project_label(_indexed(None)) == "-home-me-tienda-api"


def test_project_label_ignores_a_trailing_slash() -> None:
    assert _project_label(_indexed("/home/me/tienda-api/")) == "tienda-api"


# --- matching remote rows ------------------------------------------------------------


def _published(author: str | None = "ana@example.com", **kwargs: object) -> RemoteSession:
    defaults: dict[str, object] = {
        "session_id": "ses-1",
        "published_at": "2026-08-01T10:00:00+00:00",
        "published_by": author,
        "branch": "fix/nginx",
        "display_name": "el deploy falla",
        "first_prompt": "por qué",
        "tags": ("infra",),
    }
    defaults.update(kwargs)
    return RemoteSession(**defaults)  # type: ignore[arg-type]


def test_author_matches_a_publisher_by_local_part() -> None:
    assert _remote_matches(_published(), parse_query("author:ana"))


def test_author_matches_the_full_email() -> None:
    assert _remote_matches(_published(), parse_query("author:ana@example.com"))


def test_author_rejects_someone_else() -> None:
    assert not _remote_matches(_published(), parse_query("author:carlos"))


def test_author_rejects_a_session_with_no_publisher() -> None:
    assert not _remote_matches(_published(author=None), parse_query("author:ana"))


def test_author_is_case_insensitive_on_both_sides() -> None:
    assert _remote_matches(_published(author="Ana@Example.COM"), parse_query("author:ANA"))


def test_author_combines_with_other_constraints_as_and() -> None:
    session = _published()
    assert _remote_matches(session, parse_query("author:ana branch:fix"))
    assert not _remote_matches(session, parse_query("author:ana branch:main"))


def test_author_combines_with_free_text() -> None:
    session = _published()
    assert _remote_matches(session, parse_query("author:ana deploy"))
    assert not _remote_matches(session, parse_query("author:ana snorkel"))
