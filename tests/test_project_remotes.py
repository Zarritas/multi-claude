"""Tests for per-project sessions-repo links (multi_claude.project_remotes)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from multi_claude.discovery import project_remote_key
from multi_claude.project_remotes import (
    ProjectRemotesStore,
    RemoteLink,
    normalize_git_remote,
)

# --- normalising a git remote URL ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@git.example.com:grupo/repo.git",
        "https://git.example.com/grupo/repo.git",
        "https://git.example.com/grupo/repo",
        "ssh://git@git.example.com/grupo/repo.git",
        "ssh://git@git.example.com:2222/grupo/repo",
        "https://usuario:token@git.example.com/grupo/repo.git",
        "GIT.EXAMPLE.COM/grupo/repo",
    ],
)
def test_every_way_of_naming_one_repo_gives_the_same_key(url: str) -> None:
    """One repository reached seven ways must not become seven different links."""
    assert normalize_git_remote(url) == "git.example.com/grupo/repo"


def test_nested_groups_are_preserved() -> None:
    assert normalize_git_remote("git@git.example.com:odoo-16/fl/v16.git") == (
        "git.example.com/odoo-16/fl/v16"
    )


@pytest.mark.parametrize("bad", [None, "", "   ", "no-hay-barra", "git@host"])
def test_unusable_urls_yield_no_key(bad: str | None) -> None:
    assert normalize_git_remote(bad) is None


def test_different_repos_do_not_collide() -> None:
    assert normalize_git_remote("git@h:g/a.git") != normalize_git_remote("git@h:g/b.git")


# --- the store ----------------------------------------------------------------------


def _link(repo: str, label: str = "") -> RemoteLink:
    return RemoteLink(kind="gitlab", host="https://git.example.com", repo=repo, label=label)


def test_a_project_can_be_linked_to_several_repos(tmp_path: Path) -> None:
    store = ProjectRemotesStore(tmp_path / "project-remotes.json")
    store.add("host/grupo/repo", _link("grupo/sesiones-cliente-x"))
    store.add("host/grupo/repo", _link("grupo/sesiones-producto"))

    links = store.get("host/grupo/repo")
    assert [link.repo for link in links] == [
        "grupo/sesiones-cliente-x",
        "grupo/sesiones-producto",
    ]


def test_link_order_is_preserved_because_it_is_the_tab_order(tmp_path: Path) -> None:
    store = ProjectRemotesStore(tmp_path / "s.json")
    for name in ("c", "a", "b"):
        store.add("k", _link(f"g/{name}"))
    ProjectRemotesStore(tmp_path / "s.json").reload()
    assert [link.repo for link in ProjectRemotesStore(tmp_path / "s.json").get("k")] == [
        "g/c",
        "g/a",
        "g/b",
    ]


def test_relinking_the_same_target_updates_instead_of_duplicating(tmp_path: Path) -> None:
    """Otherwise one repo would show up under two identical tabs."""
    store = ProjectRemotesStore(tmp_path / "s.json")
    store.add("k", _link("grupo/sesiones", label="viejo"))
    links = store.add("k", _link("grupo/sesiones", label="nuevo"))

    assert len(links) == 1
    assert links[0].label == "nuevo"


def test_removing_the_last_link_drops_the_project_entirely(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    store = ProjectRemotesStore(path)
    link = _link("grupo/sesiones")
    store.add("k", link)
    assert store.remove("k", link) == ()
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_unknown_projects_have_no_links(tmp_path: Path) -> None:
    store = ProjectRemotesStore(tmp_path / "s.json")
    assert store.get("nadie") == ()
    assert store.get(None) == ()


def test_a_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{no es json", encoding="utf-8")
    assert ProjectRemotesStore(path).all() == {}


def test_a_single_link_object_still_loads(tmp_path: Path) -> None:
    """Forward-compat with the shape this file had before a project could have several."""
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"k": {"kind": "directory", "path": "/srv/sesiones"}}), encoding="utf-8"
    )
    (link,) = ProjectRemotesStore(path).get("k")
    assert link.path == "/srv/sesiones"


def test_entries_with_an_unknown_backend_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {"k": [{"kind": "bitbucket", "repo": "g/s"}, {"kind": "github", "repo": "g/s"}]}
        ),
        encoding="utf-8",
    )
    links = ProjectRemotesStore(path).get("k")
    assert [link.kind for link in links] == ["github"]


def test_links_survive_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    ProjectRemotesStore(path).add("k", _link("grupo/sesiones", label="cliente"))
    (link,) = ProjectRemotesStore(path).get("k")
    assert link.repo == "grupo/sesiones"
    assert link.label == "cliente"


# --- RemoteLink itself --------------------------------------------------------------


def test_tab_label_falls_back_to_something_recognisable() -> None:
    assert _link("grupo/sesiones-cliente").tab_label() == "sesiones-cliente"
    assert RemoteLink(kind="directory", path="/mnt/equipo/sesiones").tab_label() == "sesiones"
    assert _link("g/s", label="Cliente X").tab_label() == "Cliente X"


def test_half_configured_links_are_not_usable() -> None:
    assert not RemoteLink().is_configured
    assert not RemoteLink(kind="directory").is_configured
    assert not RemoteLink(kind="gitlab").is_configured  # no repo
    assert RemoteLink(kind="gitlab", repo="g/s").is_configured  # host defaults to gitlab.com
    assert RemoteLink(kind="directory", path="/srv").is_configured


def test_same_target_ignores_the_label() -> None:
    assert _link("g/s", label="uno").same_target(_link("g/s", label="otro"))
    assert not _link("g/a").same_target(_link("g/b"))


def test_the_default_host_is_part_of_target_identity() -> None:
    """An explicit gitlab.com and an implicit one are the same repo, not two."""
    explicit = RemoteLink(kind="gitlab", host="https://gitlab.com", repo="g/s")
    implicit = RemoteLink(kind="gitlab", repo="g/s")
    assert explicit.same_target(implicit)


# --- keying a project ---------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_every_worktree_of_a_repo_shares_one_key(tmp_path: Path) -> None:
    """Linking one worktree must link them all: it is the same repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@git.example.com:grupo/repo.git"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "rama"], cwd=repo, check=True
    )

    assert project_remote_key(repo) == "git.example.com/grupo/repo"
    assert project_remote_key(worktree) == project_remote_key(repo)


@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_a_project_without_a_remote_is_keyed_by_path(tmp_path: Path) -> None:
    """Still usable on one machine; just not shared between checkouts."""
    plain = tmp_path / "sin-git"
    plain.mkdir()
    assert project_remote_key(plain) == str(plain)
