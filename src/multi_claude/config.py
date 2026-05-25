"""User preferences persisted to ``~/.config/multi-claude/config.json``.

Only one setting today: ``default_mode`` (used by Enter). Shift+Enter uses the
*opposite* of the default, computed by :func:`alternate_for` — never configured
independently. Anything else in the file is ignored on read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LaunchMode = Literal["auto", "window", "suspend"]
VALID_MODES: tuple[LaunchMode, ...] = ("auto", "window", "suspend")


@dataclass
class Config:
    default_mode: LaunchMode = "auto"

    def to_dict(self) -> dict[str, str]:
        return {"default_mode": self.default_mode}


# Shift+Enter mapping. The rule: if the default already avoids suspending the TUI
# (auto / window), the alternate force-suspends. If the default already suspends,
# the alternate forces a brand-new window. ``auto`` has no "natural opposite"
# of "window", but ``suspend`` does — auto vs suspend is the meaningful contrast.
_OPPOSITE: dict[LaunchMode, LaunchMode] = {
    "auto": "suspend",
    "window": "suspend",
    "suspend": "window",
}


def alternate_for(mode: LaunchMode) -> LaunchMode:
    """Return the mode Shift+Enter triggers when ``mode`` is the default."""
    return _OPPOSITE[mode]


def config_path() -> Path:
    """Return the path to the config file (does not create it)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "multi-claude" / "config.json"


def load_config(path: Path | None = None) -> Config:
    """Load config from ``path`` (default: ``config_path()``). Missing/invalid → defaults."""
    target = path or config_path()
    if not target.exists():
        return Config()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Config()
    if not isinstance(raw, dict):
        return Config()
    return Config(default_mode=_coerce_mode(raw.get("default_mode"), "auto"))


def save_config(config: Config, path: Path | None = None) -> None:
    """Persist ``config`` to ``path`` (creating parent dirs)."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")


def _coerce_mode(value: object, fallback: LaunchMode) -> LaunchMode:
    if isinstance(value, str) and value in VALID_MODES:
        return value  # type: ignore[return-value]
    return fallback
