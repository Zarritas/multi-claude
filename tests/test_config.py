"""Tests for multi_claude.config."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from multi_claude.config import (
    Config,
    SortSpec,
    alternate_for,
    config_path,
    load_config,
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
