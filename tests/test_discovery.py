"""Tests for multi_claude.discovery."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from multi_claude.discovery import (
    decode_path_fallback,
    encode_cwd,
    resolve_git_common_dir,
    resolve_git_head,
    resolve_git_remote,
    resolve_git_user_email,
    resolve_real_cwd,
    scan_projects,
)
from tests.conftest import write_session


def test_decode_path_fallback_handles_naive_dash_to_slash() -> None:
    assert decode_path_fallback("-home-jesus-WS-project") == Path("/home/jesus/WS/project")


def test_resolve_real_cwd_reads_first_jsonl_cwd(tmp_path: Path) -> None:
    project_dir = tmp_path / "-home-foo-bar"
    write_session(project_dir, cwd="/home/foo/bar")
    assert resolve_real_cwd(project_dir) == Path("/home/foo/bar")


def test_resolve_real_cwd_skips_jsonl_without_cwd(tmp_path: Path) -> None:
    project_dir = tmp_path / "p"
    write_session(project_dir, session_id="a", cwd=None, mtime=2000.0)
    write_session(project_dir, session_id="b", cwd="/real/path", mtime=1000.0)
    # 'a' is newer so checked first but has no cwd → falls back to 'b'
    assert resolve_real_cwd(project_dir) == Path("/real/path")


def test_resolve_real_cwd_returns_none_when_no_jsonl(tmp_path: Path) -> None:
    project_dir = tmp_path / "empty"
    project_dir.mkdir()
    assert resolve_real_cwd(project_dir) is None


def test_scan_projects_sorted_by_last_activity_desc(projects_root: Path, tmp_path: Path) -> None:
    # project A: real path that exists
    real_a = tmp_path / "alpha"
    real_a.mkdir()
    write_session(projects_root / "-alpha", cwd=str(real_a), mtime=1000.0)
    # project B: more recent activity
    real_b = tmp_path / "beta"
    real_b.mkdir()
    write_session(projects_root / "-beta", cwd=str(real_b), mtime=3000.0)

    projects = scan_projects(projects_root)
    assert [p.name for p in projects] == ["beta", "alpha"]
    assert all(not p.is_orphan for p in projects)


def test_scan_projects_flags_orphan_when_real_path_missing(
    projects_root: Path,
) -> None:
    write_session(projects_root / "-gone", cwd="/this/path/does/not/exist/anywhere")
    projects = scan_projects(projects_root)
    assert len(projects) == 1
    assert projects[0].is_orphan is True


def test_scan_projects_ignores_dirs_with_no_jsonl(projects_root: Path, tmp_path: Path) -> None:
    (projects_root / "-empty").mkdir()
    real = tmp_path / "real"
    real.mkdir()
    write_session(projects_root / "-real", cwd=str(real))
    projects = scan_projects(projects_root)
    assert [p.name for p in projects] == ["real"]


def test_scan_projects_missing_root_returns_empty(tmp_path: Path) -> None:
    assert scan_projects(tmp_path / "nope") == []


def test_scan_projects_falls_back_to_decoded_path_when_no_cwd(
    projects_root: Path,
) -> None:
    # jsonl with no cwd field anywhere → must use decode_path_fallback
    project_dir = projects_root / "-tmp-fake-encoded"
    write_session(project_dir, cwd=None)
    projects = scan_projects(projects_root)
    assert len(projects) == 1
    # decoded path is /tmp/fake/encoded — doesn't exist → orphan
    assert projects[0].path == Path("/tmp/fake/encoded")
    assert projects[0].is_orphan is True


def test_resolve_real_cwd_skips_corrupted_newest_jsonl(tmp_path: Path) -> None:
    """The newest jsonl being unreadable must not block resolution; older one wins."""
    project_dir = tmp_path / "p"
    write_session(project_dir, session_id="old", cwd="/real/path", mtime=1000.0)

    corrupted = project_dir / "broken.jsonl"
    corrupted.write_text("{not valid json\n", encoding="utf-8")
    import os as _os

    _os.utime(corrupted, (2000.0, 2000.0))

    assert resolve_real_cwd(project_dir) == Path("/real/path")


def test_resolve_real_cwd_picks_up_cwd_in_later_event(tmp_path: Path) -> None:
    """First event has no cwd, but a later event within the scan window does."""
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    jsonl = project_dir / "x.jsonl"
    lines = [
        '{"type":"permission-mode","permissionMode":"auto"}',
        '{"type":"system"}',
        '{"type":"user","cwd":"/real/late","gitBranch":"main"}',
    ]
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert resolve_real_cwd(project_dir) == Path("/real/late")


def test_encode_cwd_matches_claude_scheme() -> None:
    assert encode_cwd("/home/me/WS/repo") == "-home-me-WS-repo"
    assert encode_cwd("/home/me/Proyecto de UI") == "-home-me-Proyecto-de-UI"
    assert encode_cwd("/a/b.c/d_e") == "-a-b-c-d-e"


def test_resolve_real_cwd_prefers_dir_name_match_over_stale_newest(tmp_path: Path) -> None:
    """A moved/resumed session whose newest file records a stale (parent) cwd must not
    flip the project's identity: we prefer the cwd whose encoding matches the dir name.
    """
    # Dir named after the SUBDIR cwd (Claude's encoding).
    subdir_cwd = "/repo/projects/migrations"
    project_dir = tmp_path / encode_cwd(subdir_cwd)

    # Newest file carries the stale parent cwd (as if moved/resumed from /repo).
    write_session(project_dir, session_id="moved", cwd="/repo", mtime=2000.0)
    # Older files carry the real subdir cwd.
    write_session(project_dir, session_id="native", cwd=subdir_cwd, mtime=1000.0)

    assert resolve_real_cwd(project_dir) == Path(subdir_cwd)


def test_resolve_real_cwd_falls_back_to_newest_when_no_name_match(tmp_path: Path) -> None:
    # Dir name matches no candidate's encoding (e.g. orphan/renamed): newest wins.
    project_dir = tmp_path / "-some-unrelated-name"
    write_session(project_dir, session_id="a", cwd="/real/new", mtime=2000.0)
    write_session(project_dir, session_id="b", cwd="/real/old", mtime=1000.0)
    assert resolve_real_cwd(project_dir) == Path("/real/new")


@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_resolve_git_common_dir_only_for_worktree_root(tmp_path: Path) -> None:
    """A repo root yields its common dir; a subdirectory of it yields None.

    Regression: subdirectory-cwds (e.g. ``repo/projects/migrations``) share the
    repo's git-common-dir, so grouping by it alone wrongly folded them into the
    repo's worktree group. They must stay standalone.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subdir = repo / "projects" / "migrations"
    subdir.mkdir(parents=True)

    root_common = resolve_git_common_dir(repo)
    assert root_common is not None
    assert root_common.resolve() == (repo / ".git").resolve()

    # The subdirectory shares the same .git but is NOT a worktree root → None.
    assert resolve_git_common_dir(subdir) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_resolve_git_common_dir_none_outside_repo(tmp_path: Path) -> None:
    assert resolve_git_common_dir(tmp_path) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_git_metadata_helpers_read_a_real_repo(tmp_path: Path) -> None:
    """These stamp a published session with the code it was recorded against."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "quien@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Quien"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@git.example.com:group/repo.git"],
        cwd=repo,
        check=True,
    )
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    assert resolve_git_remote(repo) == "git@git.example.com:group/repo.git"
    assert resolve_git_user_email(repo) == "quien@example.com"
    head = resolve_git_head(repo)
    assert head is not None and 6 <= len(head) <= 12


@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_git_metadata_helpers_are_none_outside_a_repo(tmp_path: Path) -> None:
    """Missing metadata must never fail a publish: it only feeds a warning."""
    assert resolve_git_remote(tmp_path) is None
    assert resolve_git_head(tmp_path) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_git_remote_is_none_when_no_origin_is_configured(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert resolve_git_remote(repo) is None
