"""Run multi-claude interactively against the synthetic demo machine.

What `tools/demo.tape` records with vhs. Two things are faked so a recording is safe and
repeatable:

- the **live registry**, so the Estado column shows a session working and one waiting
  without needing real Claude processes;
- **launching**, so pressing Enter on a session reports where it would have gone instead of
  spawning a terminal in the middle of a take.
"""

from __future__ import annotations

from unittest.mock import patch

import demo_world

WORLD = demo_world.build()

from multi_claude.app import ClaudeBrowserApp  # noqa: E402
from multi_claude.launcher import LaunchOutcome  # noqa: E402


def _fake_launch(cwd: object, session_id: object = None, **kwargs: object) -> LaunchOutcome:
    return LaunchOutcome("split", "demo")


def main() -> None:
    with (
        patch("multi_claude.screens.sessions.live_sessions", return_value=WORLD.live),
        patch("multi_claude.screens.sessions.launch_claude", side_effect=_fake_launch),
        patch("multi_claude.screens.projects.launch_claude", side_effect=_fake_launch),
    ):
        ClaudeBrowserApp().run()


if __name__ == "__main__":
    main()
