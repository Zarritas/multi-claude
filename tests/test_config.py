"""Tests for multi_claude.config."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from multi_claude.config import (
    ClaudeArgsError,
    Config,
    SortSpec,
    alternate_for,
    config_path,
    load_config,
    parse_claude_args,
    save_config,
)


def test_load_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.json")
    assert cfg == Config(default_mode="auto")


def test_load_returns_defaults_on_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert load_config(p) == Config()


def test_load_returns_defaults_when_root_is_not_object(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text("[]", encoding="utf-8")
    assert load_config(p) == Config()


def test_load_coerces_unknown_mode_to_default(tmp_path: Path) -> None:
    p = tmp_path / "weird.json"
    p.write_text(json.dumps({"default_mode": "telekinesis"}), encoding="utf-8")
    assert load_config(p) == Config(default_mode="auto")


def test_load_reads_valid_modes(tmp_path: Path) -> None:
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"default_mode": "suspend"}), encoding="utf-8")
    assert load_config(p) == Config(default_mode="suspend")


def test_load_ignores_legacy_alternate_mode_key(tmp_path: Path) -> None:
    """Old configs had alternate_mode; loading them must not error."""
    p = tmp_path / "legacy.json"
    p.write_text(
        json.dumps({"default_mode": "window", "alternate_mode": "suspend"}),
        encoding="utf-8",
    )
    assert load_config(p) == Config(default_mode="window")


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "config.json"
    cfg = Config(default_mode="window")
    save_config(cfg, p)
    assert p.exists()
    assert load_config(p) == cfg


def test_alternate_for_returns_opposite_mode() -> None:
    assert alternate_for("auto") == "suspend"
    assert alternate_for("window") == "suspend"
    assert alternate_for("suspend") == "window"


def test_config_path_respects_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "multi-claude" / "config.json"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX default path")
def test_config_path_defaults_to_home_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config_path() == Path.home() / ".config" / "multi-claude" / "config.json"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows default path")
def test_config_path_defaults_to_appdata_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config_path() == tmp_path / "multi-claude" / "config.json"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fallback when APPDATA missing")
def test_config_path_falls_back_to_home_config_when_appdata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    assert config_path() == Path.home() / ".config" / "multi-claude" / "config.json"


def test_load_reads_sort_specs(tmp_path: Path) -> None:
    p = tmp_path / "sort.json"
    p.write_text(
        json.dumps(
            {
                "default_mode": "auto",
                "projects_sort": {"key": "name", "descending": False},
                "sessions_sort": {"key": "size", "descending": True},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.projects_sort == SortSpec(key="name", descending=False)
    assert cfg.sessions_sort == SortSpec(key="size", descending=True)


def test_load_coerces_unknown_sort_key_to_default(tmp_path: Path) -> None:
    p = tmp_path / "bad-sort.json"
    p.write_text(
        json.dumps({"projects_sort": {"key": "telekinesis", "descending": True}}),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.projects_sort == SortSpec(key="last_activity", descending=True)


def test_save_then_load_round_trip_with_sort(tmp_path: Path) -> None:
    p = tmp_path / "rt.json"
    cfg = Config(
        default_mode="window",
        projects_sort=SortSpec(key="session_count", descending=False),
        sessions_sort=SortSpec(key="messages", descending=False),
        preview_visible=False,
        group_worktrees=False,
    )
    save_config(cfg, p)
    assert load_config(p) == cfg


# --------------------------------------------------------------------------- #
# Launch modes                                                                 #
# --------------------------------------------------------------------------- #


def test_alternate_for_covers_every_mode() -> None:
    assert alternate_for("split") == "window"
    assert alternate_for("tab") == "window"


def test_load_accepts_new_placement_modes(tmp_path: Path) -> None:
    p = tmp_path / "tab.json"
    p.write_text(json.dumps({"default_mode": "tab"}), encoding="utf-8")
    assert load_config(p).default_mode == "tab"


# --------------------------------------------------------------------------- #
# Extra claude args                                                            #
# --------------------------------------------------------------------------- #


def test_parse_claude_args_splits_shell_style() -> None:
    assert parse_claude_args("  --model opus --append-system-prompt 'be brief' ") == [
        "--model",
        "opus",
        "--append-system-prompt",
        "be brief",
    ]


def test_parse_claude_args_empty_is_empty_list() -> None:
    assert parse_claude_args("") == []


def test_parse_claude_args_rejects_reserved_flags() -> None:
    with pytest.raises(ClaudeArgsError, match="--resume"):
        parse_claude_args("--resume abc")
    with pytest.raises(ClaudeArgsError, match="-n"):
        parse_claude_args("--model opus -n mine")


def test_parse_claude_args_rejects_unbalanced_quotes() -> None:
    with pytest.raises(ClaudeArgsError):
        parse_claude_args("--append-system-prompt 'unterminated")


def test_load_coerces_claude_args_from_string(tmp_path: Path) -> None:
    """A hand-edited config may hold a plain string instead of a list."""
    p = tmp_path / "args.json"
    p.write_text(
        json.dumps({"claude_args": "--dangerously-skip-permissions --model opus"}),
        encoding="utf-8",
    )
    assert load_config(p).claude_args == ["--dangerously-skip-permissions", "--model", "opus"]


def test_load_drops_reserved_flags_from_claude_args(tmp_path: Path) -> None:
    p = tmp_path / "reserved.json"
    p.write_text(
        json.dumps({"claude_args": ["--resume", "abc", "--model", "opus"]}),
        encoding="utf-8",
    )
    # `--resume` is dropped; its value is left alone (it isn't a flag we own).
    assert load_config(p).claude_args == ["abc", "--model", "opus"]


def test_load_ignores_non_list_claude_args(tmp_path: Path) -> None:
    p = tmp_path / "weird.json"
    p.write_text(json.dumps({"claude_args": {"nope": 1}}), encoding="utf-8")
    assert load_config(p).claude_args == []


def test_save_then_load_round_trip_with_claude_args(tmp_path: Path) -> None:
    p = tmp_path / "rt-args.json"
    cfg = Config(default_mode="tab", claude_args=["--dangerously-skip-permissions"])
    save_config(cfg, p)
    assert load_config(p) == cfg


def test_remote_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(Config(remote_kind="directory", remote_path="/srv/sesiones"), path)
    loaded = load_config(path)
    assert loaded.remote_kind == "directory"
    assert loaded.remote_path == "/srv/sesiones"


def test_remote_sharing_is_off_by_default() -> None:
    assert Config().remote_kind == "none"
    assert Config().remote_path == ""


@pytest.mark.parametrize("bad", ["gitlab", "", None, 3, {"a": 1}])
def test_unknown_remote_kind_falls_back_to_off(tmp_path: Path, bad: object) -> None:
    """An unrecognised backend must disable sharing, never guess one."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"remote_kind": bad}), encoding="utf-8")
    assert load_config(path).remote_kind == "none"


def test_non_string_remote_path_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"remote_kind": "directory", "remote_path": 42}), encoding="utf-8")
    assert load_config(path).remote_path == ""
