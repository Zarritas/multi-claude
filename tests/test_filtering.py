"""Tests for the shared filter parser + matcher."""

from __future__ import annotations

from multi_claude.filtering import FilterQuery, matches_fuzzy, parse_query


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
