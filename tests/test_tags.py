"""Tests for multi_claude.tags."""

from __future__ import annotations

import json
from pathlib import Path

from multi_claude.tags import TagsStore, default_path, normalize_tag, parse_tag_list


def test_default_path_uses_xdg(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert default_path() == Path("/tmp/xdg/multi-claude/session-tags.json")


def test_default_path_falls_back_to_home(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
    assert default_path() == Path("/home/test/.config/multi-claude/session-tags.json")


def test_normalize_tag_lowercases_and_trims() -> None:
    assert normalize_tag("  BUG  ") == "bug"


def test_normalize_tag_collapses_whitespace_to_hyphen() -> None:
    assert normalize_tag("cliente acme prio") == "cliente-acme-prio"


def test_normalize_tag_strips_reserved_chars() -> None:
    assert normalize_tag("bug,urgent") == "bugurgent"
    assert normalize_tag("foo:bar") == "foobar"


def test_normalize_tag_rejects_empty() -> None:
    assert normalize_tag("") is None
    assert normalize_tag("   ") is None
    assert normalize_tag(",") is None


def test_parse_tag_list_splits_and_dedupes() -> None:
    assert parse_tag_list("bug, urgent  bug feature") == ["bug", "urgent", "feature"]


def test_parse_tag_list_handles_empty() -> None:
    assert parse_tag_list("") == []
    assert parse_tag_list("   ,, ,, ") == []


def test_get_missing_returns_empty(tmp_path: Path) -> None:
    store = TagsStore(tmp_path / "session-tags.json")
    assert store.get("unknown") == ()


def test_set_round_trip(tmp_path: Path) -> None:
    store = TagsStore(tmp_path / "session-tags.json")
    store.set("abc-123", ["bug", "URGENT", "bug"])
    assert store.get("abc-123") == ("bug", "urgent")
    # Reload from disk
    reloaded = TagsStore(tmp_path / "session-tags.json")
    assert reloaded.get("abc-123") == ("bug", "urgent")


def test_set_empty_removes_entry(tmp_path: Path) -> None:
    path = tmp_path / "session-tags.json"
    store = TagsStore(path)
    store.set("sid", ["bug"])
    assert store.get("sid") == ("bug",)
    store.set("sid", [])
    assert store.get("sid") == ()
    # And it's actually gone from disk
    raw = json.loads(path.read_text())
    assert "sid" not in raw


def test_add_is_idempotent(tmp_path: Path) -> None:
    store = TagsStore(tmp_path / "session-tags.json")
    store.add("sid", "bug")
    store.add("sid", "BUG")  # case-insensitive
    store.add("sid", "urgent")
    assert store.get("sid") == ("bug", "urgent")


def test_remove_drops_only_target(tmp_path: Path) -> None:
    store = TagsStore(tmp_path / "session-tags.json")
    store.set("sid", ["bug", "urgent", "ux"])
    store.remove("sid", "URGENT")
    assert store.get("sid") == ("bug", "ux")


def test_remove_last_tag_deletes_entry(tmp_path: Path) -> None:
    path = tmp_path / "session-tags.json"
    store = TagsStore(path)
    store.set("sid", ["bug"])
    store.remove("sid", "bug")
    assert store.get("sid") == ()
    assert "sid" not in json.loads(path.read_text())


def test_delete_removes_session(tmp_path: Path) -> None:
    store = TagsStore(tmp_path / "session-tags.json")
    store.set("sid", ["bug"])
    store.delete("sid")
    assert store.get("sid") == ()
    # Idempotent
    store.delete("sid")


def test_all_known_tags_returns_sorted_union(tmp_path: Path) -> None:
    store = TagsStore(tmp_path / "session-tags.json")
    store.set("a", ["urgent", "bug"])
    store.set("b", ["ux", "bug"])
    assert store.all_known_tags() == ["bug", "urgent", "ux"]


def test_corrupt_file_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "session-tags.json"
    path.write_text("{not json")
    store = TagsStore(path)
    assert store.get("anything") == ()
    assert store.all_known_tags() == []


def test_load_normalises_legacy_entries(tmp_path: Path) -> None:
    path = tmp_path / "session-tags.json"
    path.write_text(json.dumps({"sid": ["BUG ", "bug", "  URGENT  "]}))
    store = TagsStore(path)
    assert store.get("sid") == ("bug", "urgent")


def test_load_drops_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "session-tags.json"
    path.write_text(
        json.dumps(
            {
                "sid_ok": ["bug"],
                42: ["foo"],  # type: ignore[dict-item] — round-trip via json gives "42"
                "sid_bad": "not-a-list",
                "sid_mixed": ["", "  ", 17, "ok"],
            }
        )
    )
    store = TagsStore(path)
    assert store.get("sid_ok") == ("bug",)
    assert store.get("sid_bad") == ()
    assert store.get("sid_mixed") == ("ok",)
