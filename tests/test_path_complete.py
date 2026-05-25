"""Tests for the shell-style path completion used by AddProjectModal."""

from __future__ import annotations

from pathlib import Path

from multi_claude.path_complete import common_prefix_completion, expand, list_suggestions


def test_empty_prefix_returns_empty(tmp_path: Path) -> None:
    assert list_suggestions("") == []


def test_suggests_subdirectories_of_existing_dir(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "afile.txt").write_text("x", encoding="utf-8")
    result = list_suggestions(str(tmp_path) + "/")
    names = {p.name for p in result}
    assert names == {"alpha", "beta"}  # files excluded


def test_filters_by_basename_prefix_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "alabama").mkdir()
    (tmp_path / "beta").mkdir()
    result = list_suggestions(str(tmp_path / "al"))
    assert {p.name for p in result} == {"Alpha", "alabama"}


def test_respects_limit(tmp_path: Path) -> None:
    for i in range(20):
        (tmp_path / f"dir-{i:02d}").mkdir()
    result = list_suggestions(str(tmp_path) + "/", limit=5)
    assert len(result) == 5


def test_missing_parent_returns_empty(tmp_path: Path) -> None:
    assert list_suggestions(str(tmp_path / "does-not-exist" / "foo")) == []


def test_expand_handles_tilde(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    fake_home = tmp_path / "home-fake"
    # `Path.home()` honors USERPROFILE on Windows and HOME on POSIX. Set both
    # so the test works on either platform.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    assert expand("~/proj") == fake_home / "proj"


def test_common_prefix_single_candidate_appends_slash(tmp_path: Path) -> None:
    only = tmp_path / "unique"
    only.mkdir()
    completion = common_prefix_completion(str(tmp_path / "uniq"))
    assert completion == str(only) + "/"


def test_common_prefix_extends_to_shared_prefix(tmp_path: Path) -> None:
    (tmp_path / "feature-login").mkdir()
    (tmp_path / "feature-logout").mkdir()
    completion = common_prefix_completion(str(tmp_path / "fea"))
    assert completion is not None
    assert completion.endswith("feature-log")


def test_common_prefix_none_when_no_candidates(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    assert common_prefix_completion(str(tmp_path / "zzz")) is None


def test_common_prefix_does_not_truncate_when_already_at_common(tmp_path: Path) -> None:
    """If the user already typed the full common prefix, Tab is a no-op."""
    (tmp_path / "feature-a").mkdir()
    (tmp_path / "feature-b").mkdir()
    typed = str(tmp_path / "feature-")
    completion = common_prefix_completion(typed)
    # No advancement possible — both candidates share exactly this prefix.
    assert completion is None or completion == typed
