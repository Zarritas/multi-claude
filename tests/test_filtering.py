"""Tests for the shared filter parser + matcher."""

from __future__ import annotations

import pytest

from multi_claude.filtering import (
    FilterQuery,
    matches_fuzzy,
    parse_query,
    secrets_verdict,
    secrets_wanted,
)
from multi_claude.focus import LiveSession
from multi_claude.remote import RemoteSession
from multi_claude.screens.search import _project_label
from multi_claude.screens.sessions import _remote_matches, _status_presentation


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


# --- secrets: -------------------------------------------------------------------------


def test_parse_secrets_constraint() -> None:
    q = parse_query("secrets:yes nginx")
    assert q.free_text == "nginx"
    assert q.constraints == {"secrets": "yes"}


@pytest.mark.parametrize("spelling", ["yes", "si", "sí", "true", "1", "YES"])
def test_secrets_yes_spellings(spelling: str) -> None:
    assert secrets_wanted(spelling) == "yes"


@pytest.mark.parametrize("spelling", ["no", "false", "0", "clean", "limpias"])
def test_secrets_no_spellings(spelling: str) -> None:
    assert secrets_wanted(spelling) == "no"


@pytest.mark.parametrize("spelling", ["unknown", "desconocido", "?"])
def test_secrets_unknown_spellings(spelling: str) -> None:
    assert secrets_wanted(spelling) == "unknown"


def test_an_unrecognised_secrets_value_is_not_silently_ignored() -> None:
    """Returning None makes the caller filter to nothing, which is the honest answer."""
    assert secrets_wanted("puede") is None
    assert secrets_wanted("") is None


def test_never_scanned_is_its_own_answer_not_a_no() -> None:
    """Collapsing unknown into no would have the filter claim something it cannot know."""
    assert secrets_verdict(None) == "unknown"
    assert secrets_verdict(0) == "no"
    assert secrets_verdict(1) == "yes"
    assert secrets_verdict(7) == "yes"


def test_secrets_is_not_answerable_on_a_remote_tab() -> None:
    """A manifest says nothing about credentials, so no row can answer — not even the
    ones already fetched, because half a list answering is worse than none."""
    assert not _remote_matches(_published(), parse_query("secrets:yes"))
    assert not _remote_matches(_published(), parse_query("secrets:no"))


def test_file_is_not_answerable_on_a_remote_tab() -> None:
    """Same reason as secrets: a manifest does not carry the files its session edited."""
    assert not _remote_matches(_published(), parse_query("file:index.py"))


# --- the Estado column's two vocabularies --------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        # From the per-PID registry (undocumented).
        ("busy", "● trabajando"),
        ("waiting", "○ te espera"),
        # From `claude agents --json` (documented).
        ("working", "● trabajando"),
        ("needs input", "○ te espera"),
        ("needs_input", "○ te espera"),
        ("idle", "· libre"),
        ("completed", "✓ terminada"),
        ("failed", "✗ falló"),
        ("stopped", "■ detenida"),
        # Case is not a contract either.
        ("BUSY", "● trabajando"),
    ],
)
def test_every_known_state_gets_its_own_label(state: str, expected: str) -> None:
    label, _style, _rank = _status_presentation(LiveSession("s", pid=1, status=state))
    assert label == expected


def test_an_unknown_state_says_open_without_guessing() -> None:
    """The vocabulary can grow; inventing a meaning for a new value would be worse."""
    label, _, rank = _status_presentation(LiveSession("s", pid=1, status="teleporting"))
    assert label == "● abierta"
    assert rank == 1


def test_not_running_and_running_without_a_state_are_different() -> None:
    assert _status_presentation(None)[0] == "—"
    assert _status_presentation(LiveSession("s", pid=1, status=None))[0] == "● abierta"


def test_what_waits_on_you_sorts_above_what_works() -> None:
    """Descending by status has to put the sessions that need you first."""
    waiting = _status_presentation(LiveSession("s", pid=1, status="needs input"))[2]
    working = _status_presentation(LiveSession("s", pid=1, status="working"))[2]
    finished = _status_presentation(LiveSession("s", pid=1, status="completed"))[2]
    not_live = _status_presentation(None)[2]
    assert waiting > working > finished > not_live


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
