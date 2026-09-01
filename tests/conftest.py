"""Shared fixtures: builders for synthetic Claude project trees on tmpfs."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from multi_claude.index import reset_default_index_for_tests


@pytest.fixture(autouse=True)
def isolated_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep index writes inside ``tmp_path`` for every test, without opting in.

    ``default_index()`` resolves its path from ``XDG_DATA_HOME`` once and then caches the
    handle process-wide, so a test that reaches it through production code (any TUI test
    scanning sessions, or listing a remote) would otherwise write synthetic rows into the
    developer's own ``~/.local/share/multi-claude/index.sqlite3`` — and they would show up
    in their global search. Autouse because the leak happens through code that no test
    asks for explicitly.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reset_default_index_for_tests()
    yield
    reset_default_index_for_tests()


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """A fresh ~/.claude/projects-like directory rooted under tmp_path."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


def write_session(
    project_dir: Path,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    branch: str | None = "main",
    first_prompt: str = "hola",
    extra_events: int = 5,
    mtime: float | None = None,
    edited_files: tuple[str, ...] = (),
    edit_tool: str = "Edit",
) -> Path:
    """Build a minimal jsonl that looks like a real Claude session.

    ``edited_files`` appends one assistant turn per path carrying a ``tool_use`` block, the
    shape ``session_files`` is extracted from. ``edit_tool`` picks which tool made the edit,
    for the cases that turn on the tool's name (a ``Read`` must not count as a touch).
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    sid = session_id or str(uuid.uuid4())
    jsonl = project_dir / f"{sid}.jsonl"
    events: list[dict] = []
    if first_prompt:
        events.append(
            {
                "type": "user",
                "message": {"role": "user", "content": first_prompt},
                "cwd": cwd,
                "gitBranch": branch,
                "sessionId": sid,
                "timestamp": "2026-05-01T00:00:00.000Z",
            }
        )
    events.append(
        {
            "type": "permission-mode",
            "permissionMode": "auto",
            "sessionId": sid,
        }
    )
    for path in edited_files:
        key = "notebook_path" if edit_tool == "NotebookEdit" else "file_path"
        events.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": edit_tool, "input": {key: path}},
                    ],
                },
                "sessionId": sid,
            }
        )
    for i in range(extra_events):
        events.append({"type": "assistant", "seq": i, "sessionId": sid})
    with jsonl.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    if mtime is not None:
        os.utime(jsonl, (mtime, mtime))
    return jsonl


async def settle(pilot: object, rounds: int = 4) -> None:
    """Let every pending worker finish and the UI catch up.

    Most waits in the UI tests are a fixed number of ``pause()`` calls, which is a bet on how
    fast the machine is: they were written on a developer laptop and held there, while CI —
    fewer cores, slower ones, shared — kept losing whichever was closest to the edge. Which
    one that is varies per run, so the same commit failed on 3.10 and 3.12 while 3.11 passed.
    That is not a Python version difference, it is a coin toss.

    Textual can be asked directly, so this waits for the workers instead of guessing at them.
    Several rounds because they chain: scanning a project starts the credential scan, which
    starts the published-sessions lookup, and each new worker only exists once the previous
    one has handed back.
    """
    for _ in range(rounds):
        await pilot.app.workers.wait_for_complete()  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
