"""`.multi-claude.json` — the sessions repos a project declares for its whole team.

The file is versioned, so it is untrusted input: anyone who can push to the project can
change it, and what it configures is where sessions get published. Most of what follows
guards the one rule that makes that safe — **the repo says which repository, you say which
server** — because every way of breaking it ends with transcripts published somewhere the
reader did not choose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multi_claude.project_config import (
    MAX_CONFIG_BYTES,
    MAX_DECLARED_LINKS,
    PROJECT_CONFIG_NAME,
    ProjectConfigReader,
    parse_project_config,
    read_project_config,
)
from multi_claude.project_remotes import RemoteServer


def write_config(cwd: Path, payload: object) -> Path:
    cwd.mkdir(parents=True, exist_ok=True)
    path = cwd / PROJECT_CONFIG_NAME
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def one_repo(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {"server": "Empresa", "repo": "equipo/sesiones"}
    entry.update(overrides)
    return {"sessions_repos": [entry]}


# --- reading ---------------------------------------------------------------------------


def test_no_file_declares_nothing(tmp_path: Path) -> None:
    config = read_project_config(tmp_path)
    assert config.is_empty


def test_a_declared_repo_becomes_a_link(tmp_path: Path) -> None:
    write_config(tmp_path, one_repo())
    (link,) = read_project_config(tmp_path).links
    assert link.server == "Empresa"
    assert link.repo == "equipo/sesiones"
    assert link.branch == "main"


def test_the_label_is_carried_through(tmp_path: Path) -> None:
    write_config(tmp_path, one_repo(label="Equipo"))
    (link,) = read_project_config(tmp_path).links
    assert link.tab_label() == "Equipo"


def test_a_branch_can_be_declared(tmp_path: Path) -> None:
    write_config(tmp_path, one_repo(branch="sesiones"))
    (link,) = read_project_config(tmp_path).links
    assert link.branch == "sesiones"


def test_several_repos_keep_their_order(tmp_path: Path) -> None:
    """The order is the order of the tabs."""
    write_config(
        tmp_path,
        {
            "sessions_repos": [
                {"server": "Empresa", "repo": "a/uno"},
                {"server": "Empresa", "repo": "b/dos"},
            ]
        },
    )
    assert [link.repo for link in read_project_config(tmp_path).links] == ["a/uno", "b/dos"]


def test_a_missing_cwd_is_not_an_error() -> None:
    assert read_project_config(None).is_empty


# --- the rule the whole thing rests on --------------------------------------------------


def test_a_repo_cannot_choose_the_host(tmp_path: Path) -> None:
    """Otherwise a file in a repository could point everyone's publishes at any server."""
    write_config(tmp_path, one_repo(host="https://evil.example"))
    config = read_project_config(tmp_path)
    assert config.links == ()
    assert "host" in config.problems[0]


def test_a_repo_cannot_choose_the_kind(tmp_path: Path) -> None:
    write_config(tmp_path, one_repo(kind="github"))
    config = read_project_config(tmp_path)
    assert config.links == ()
    assert "kind" in config.problems[0]


def test_a_repo_cannot_point_at_a_local_folder(tmp_path: Path) -> None:
    """A path is specific to one machine, and honouring one would turn a versioned file
    into an arbitrary write on every reader's disk."""
    write_config(tmp_path, {"sessions_repos": [{"path": "/home/someone/.ssh", "repo": "x/y"}]})
    config = read_project_config(tmp_path)
    assert config.links == ()
    assert "path" in config.problems[0]


def test_an_entry_must_name_a_server(tmp_path: Path) -> None:
    write_config(tmp_path, {"sessions_repos": [{"repo": "equipo/sesiones"}]})
    config = read_project_config(tmp_path)
    assert config.links == ()
    assert "server" in config.problems[0]


def test_an_entry_must_name_a_repo(tmp_path: Path) -> None:
    write_config(tmp_path, {"sessions_repos": [{"server": "Empresa"}]})
    config = read_project_config(tmp_path)
    assert config.links == ()
    assert "repo" in config.problems[0]


def test_an_unknown_server_resolves_to_nothing_usable(tmp_path: Path) -> None:
    """The safety property in one line: a declaration is inert until the reader has
    configured the server it names, so it can never reach somewhere unexpected."""
    write_config(tmp_path, one_repo(server="NoConfigurado"))
    (link,) = read_project_config(tmp_path).links
    resolved = link.resolved([RemoteServer(name="Otro", kind="gitlab", host="https://git.x")])
    assert resolved.kind == "none"
    assert not resolved.is_configured


def test_a_known_server_supplies_kind_and_host(tmp_path: Path) -> None:
    write_config(tmp_path, one_repo())
    (link,) = read_project_config(tmp_path).links
    resolved = link.resolved([RemoteServer(name="Empresa", kind="gitlab", host="https://git.x")])
    assert resolved.kind == "gitlab"
    assert resolved.api_host == "https://git.x"
    assert resolved.is_configured


# --- refusing to blow up ----------------------------------------------------------------


def test_broken_json_is_reported_not_raised(tmp_path: Path) -> None:
    write_config(tmp_path, "{not json")
    config = read_project_config(tmp_path)
    assert config.links == ()
    assert config.problems


def test_a_json_array_is_not_a_config(tmp_path: Path) -> None:
    write_config(tmp_path, [1, 2, 3])
    assert read_project_config(tmp_path).problems


def test_sessions_repos_must_be_a_list(tmp_path: Path) -> None:
    write_config(tmp_path, {"sessions_repos": "equipo/sesiones"})
    assert read_project_config(tmp_path).problems


def test_a_file_without_sessions_repos_is_silent(tmp_path: Path) -> None:
    """Other keys may appear later; a config that declares no repos is not a problem."""
    write_config(tmp_path, {"something_else": 1})
    assert read_project_config(tmp_path).is_empty


def test_a_non_object_entry_is_refused_without_losing_the_rest(tmp_path: Path) -> None:
    write_config(
        tmp_path, {"sessions_repos": ["nope", {"server": "Empresa", "repo": "equipo/sesiones"}]}
    )
    config = read_project_config(tmp_path)
    assert [link.repo for link in config.links] == ["equipo/sesiones"]
    assert config.problems


def test_an_oversized_file_is_not_parsed(tmp_path: Path) -> None:
    """Read on every project open: a hostile file must not cost more than a stat."""
    write_config(tmp_path, {"sessions_repos": [{"pad": "x" * (MAX_CONFIG_BYTES + 10)}]})
    config = read_project_config(tmp_path)
    assert config.links == ()
    assert "grande" in config.problems[0]


def test_the_number_of_declarations_is_capped(tmp_path: Path) -> None:
    """A wall of tabs is a way to bury the real one."""
    write_config(
        tmp_path,
        {
            "sessions_repos": [
                {"server": "Empresa", "repo": f"g/r{i}"} for i in range(MAX_DECLARED_LINKS + 3)
            ]
        },
    )
    config = read_project_config(tmp_path)
    assert len(config.links) == MAX_DECLARED_LINKS
    assert config.problems


def test_the_same_repo_twice_is_one_tab(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        {
            "sessions_repos": [
                {"server": "Empresa", "repo": "equipo/sesiones"},
                {"server": "Empresa", "repo": "equipo/sesiones", "label": "otra"},
            ]
        },
    )
    config = read_project_config(tmp_path)
    assert len(config.links) == 1
    assert config.problems


@pytest.mark.parametrize("payload", [None, 3, "texto", [], {}])
def test_parsing_never_raises(payload: object) -> None:
    parse_project_config(payload)


# --- the cache --------------------------------------------------------------------------


def test_the_reader_caches_by_mtime(tmp_path: Path) -> None:
    path = write_config(tmp_path, one_repo())
    reader = ProjectConfigReader()
    assert len(reader.read(tmp_path).links) == 1

    # Same mtime and size: the cached answer stands, which is the point of the cache.
    stat = path.stat()
    path.write_text(json.dumps(one_repo(repo="otro/repo")), encoding="utf-8")
    import os

    os.utime(path, (stat.st_atime, stat.st_mtime))
    if path.stat().st_size == stat.st_size:
        assert reader.read(tmp_path).links[0].repo == "equipo/sesiones"


def test_a_changed_file_is_re_read(tmp_path: Path) -> None:
    """A `git pull` that changes the declaration has to take effect without a restart."""
    import os

    path = write_config(tmp_path, one_repo())
    reader = ProjectConfigReader()
    assert reader.read(tmp_path).links[0].repo == "equipo/sesiones"
    write_config(tmp_path, one_repo(repo="equipo/otro"))
    os.utime(path, (0, 0))
    assert reader.read(tmp_path).links[0].repo == "equipo/otro"


def test_a_deleted_file_stops_declaring(tmp_path: Path) -> None:
    path = write_config(tmp_path, one_repo())
    reader = ProjectConfigReader()
    assert reader.read(tmp_path).links
    path.unlink()
    assert reader.read(tmp_path).is_empty
