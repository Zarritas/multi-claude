"""Read conversation turns out of a session jsonl.

Extracted from the preview widget so that consumers without a UI — the MCP server
in :mod:`multi_claude.mcp` — can read turns too. Nothing here imports Textual.
"""

from __future__ import annotations

import json
from pathlib import Path

from multi_claude.session import strip_command_wrappers

TAIL_LINES = 60
TURN_LIMIT = 12
TEXT_LIMIT = 800


def read_last_turns(
    jsonl_path: Path,
    *,
    tail_lines: int = TAIL_LINES,
    turn_limit: int = TURN_LIMIT,
    text_limit: int = TEXT_LIMIT,
) -> list[tuple[str, str]]:
    """Return ``[(role, text), ...]`` for the last few user/assistant turns.

    Only the last ``tail_lines`` lines are considered, so a turn whose text sits
    further back than that is not returned even if the file has fewer than
    ``turn_limit`` turns in that window.
    """
    with jsonl_path.open("rb") as f:
        lines = _tail_lines(f.read(), tail_lines)
    turns: list[tuple[str, str]] = []
    for raw in lines:
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        role_and_text = extract_role_and_text(event)
        if role_and_text is None:
            continue
        role, text = role_and_text
        text = strip_command_wrappers(text).strip()
        if not text:
            continue
        if len(text) > text_limit:
            text = text[:text_limit].rstrip() + "…"
        turns.append((role, text))
    return turns[-turn_limit:]


def _tail_lines(data: bytes, count: int) -> list[bytes]:
    """Cheap tail: the whole file is already in memory, slice the last ``count`` lines."""
    if not data:
        return []
    return data.splitlines()[-count:]


def extract_role_and_text(event: dict[str, object]) -> tuple[str, str] | None:
    """``(role, text)`` for a user/assistant event, or None for anything else.

    Tool calls and tool results carry no ``text`` block, so they drop out here —
    what comes back is the conversation, not the trace.
    """
    etype = event.get("type")
    if etype not in ("user", "assistant"):
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    role = "user" if etype == "user" else "assistant"
    if isinstance(content, str):
        return (role, content)
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        if chunks:
            return (role, "\n".join(chunks))
    return None
