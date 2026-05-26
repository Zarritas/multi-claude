"""Parse session jsonl files cheaply.

We only read the first ~80 lines of each session for the listing — enough to
extract cwd, gitBranch, version, and the first user prompt. Line count and size
come from stat and a streaming wc-equivalent (no full parse).

Heavy reads (line count, FTS content) are cached in the SQLite index keyed by
mtime: unchanged files are read once and skipped on subsequent scans.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from multi_claude.index import IndexedSession, SessionIndex, default_index
from multi_claude.names import NamesStore
from multi_claude.tags import TagsStore

HEADER_SCAN_LINES = 80
PROMPT_MAX_CHARS = 120
FTS_CONTENT_MAX_CHARS = 64_000  # cap per-session FTS payload (~64 KB)
FTS_REINDEX_SCAN_LINES = 2_000  # cap how much we read into the FTS payload
RENAME_SCAN_LINES = 50_000  # cap when scanning for the latest /rename in long sessions

_RENAME_RE = re.compile(
    r"<local-command-stdout>\s*Session renamed to:\s*(?P<name>.+?)\s*</local-command-stdout>",
    re.DOTALL,
)


@dataclass(frozen=True)
class Session:
    id: str
    path: Path
    first_prompt: str
    branch: str | None
    cwd: str | None
    message_count: int
    size_bytes: int
    last_activity: float
    display_name: str | None
    tags: tuple[str, ...] = ()


def scan_sessions(
    project_dir: Path,
    *,
    names_store: NamesStore | None = None,
    index: SessionIndex | None = None,
    tags_store: TagsStore | None = None,
) -> list[Session]:
    """Return all sessions under ``project_dir`` sorted by last_activity desc."""
    store = names_store or NamesStore()
    idx = index if index is not None else default_index()
    tags = tags_store or TagsStore()
    sessions: list[Session] = []
    for jsonl in project_dir.glob("*.jsonl"):
        try:
            session = _build_session(jsonl, store, idx, project_dir, tags)
        except OSError:
            continue
        sessions.append(session)
    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions


def _build_session(
    jsonl_path: Path,
    names_store: NamesStore,
    index: SessionIndex,
    project_dir: Path,
    tags_store: TagsStore,
) -> Session:
    stat = jsonl_path.stat()
    sid = jsonl_path.stem

    cached_mtime = index.get_mtime(sid)
    if cached_mtime is not None and cached_mtime == stat.st_mtime:
        indexed = index.get(sid)
        if indexed is not None:
            return Session(
                id=indexed.session_id,
                path=jsonl_path,
                first_prompt=indexed.first_prompt or "(sin prompt inicial)",
                branch=indexed.branch,
                cwd=indexed.cwd,
                message_count=indexed.message_count,
                size_bytes=indexed.size_bytes,
                last_activity=indexed.mtime,
                display_name=names_store.get(sid) or indexed.embedded_name,
                tags=tags_store.get(sid),
            )

    header = parse_session_header(jsonl_path)
    line_count = count_lines(jsonl_path)
    fts_content = _extract_fts_content(jsonl_path)
    embedded_name = extract_embedded_name(jsonl_path)

    indexed = IndexedSession(
        session_id=sid,
        project_dir=str(project_dir),
        cwd=header.get("cwd"),
        branch=header.get("branch"),
        first_prompt=header.get("first_prompt"),
        message_count=line_count,
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        jsonl_path=str(jsonl_path),
        embedded_name=embedded_name,
    )
    index.upsert_session(indexed, fts_content=fts_content)

    return Session(
        id=sid,
        path=jsonl_path,
        first_prompt=header.get("first_prompt") or "(sin prompt inicial)",
        branch=header.get("branch"),
        cwd=header.get("cwd"),
        message_count=line_count,
        size_bytes=stat.st_size,
        last_activity=stat.st_mtime,
        display_name=names_store.get(sid) or embedded_name,
        tags=tags_store.get(sid),
    )


def parse_session_header(
    jsonl_path: Path, max_lines: int = HEADER_SCAN_LINES
) -> dict[str, str | None]:
    """Read up to ``max_lines`` lines and extract first user prompt, cwd, branch, name."""
    result: dict[str, str | None] = {
        "first_prompt": None,
        "cwd": None,
        "branch": None,
        "display_name": None,
    }
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if result["cwd"] is None and isinstance(event.get("cwd"), str):
                    result["cwd"] = event["cwd"]
                if result["branch"] is None and isinstance(event.get("gitBranch"), str):
                    result["branch"] = event["gitBranch"]
                if result["display_name"] is None and isinstance(event.get("name"), str):
                    result["display_name"] = event["name"]
                if result["first_prompt"] is None:
                    prompt = _extract_user_prompt(event)
                    if prompt:
                        result["first_prompt"] = _truncate(strip_command_wrappers(prompt))
                if all(v is not None for v in result.values()):
                    break
    except OSError:
        pass
    return result


def extract_embedded_name(jsonl_path: Path) -> str | None:
    """Return the latest name set inside ``jsonl_path`` via Claude's ``/rename``.

    Looks at every ``system/local_command`` event whose ``content`` matches
    ``<local-command-stdout>Session renamed to: X</local-command-stdout>`` and
    returns the X of the last occurrence (so subsequent renames win). Falls
    back to a top-level ``name`` string if some Claude build wrote one inline.
    ``None`` if nothing relevant is found.
    """
    latest: str | None = None
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(RENAME_SCAN_LINES):
                line = f.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                # Deprecated path: top-level ``name`` string.
                top_name = event.get("name")
                if isinstance(top_name, str) and top_name.strip():
                    latest = top_name.strip()
                    continue
                if event.get("type") != "system":
                    continue
                if event.get("subtype") != "local_command":
                    continue
                content = event.get("content")
                if not isinstance(content, str):
                    continue
                match = _RENAME_RE.search(content)
                if match:
                    candidate = match.group("name").strip()
                    if candidate:
                        latest = candidate
    except OSError:
        return latest
    return latest


def _extract_fts_content(jsonl_path: Path) -> str:
    """Concatenate user prompts and assistant text into one string for FTS5.

    Skips tool_use/tool_result payloads to keep the index small. Caps at
    ``FTS_CONTENT_MAX_CHARS`` so a runaway session doesn't blow up the DB.
    """
    parts: list[str] = []
    total = 0
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(FTS_REINDEX_SCAN_LINES):
                line = f.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _extract_text_for_fts(event)
                if not text:
                    continue
                parts.append(text)
                total += len(text)
                if total >= FTS_CONTENT_MAX_CHARS:
                    break
    except OSError:
        return ""
    return "\n".join(parts)[:FTS_CONTENT_MAX_CHARS]


def _extract_text_for_fts(event: dict[str, object]) -> str | None:
    """Pull plain text from a jsonl event, ignoring tool calls and metadata."""
    etype = event.get("type")
    if etype not in ("user", "assistant"):
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text")
                if isinstance(t, str):
                    pieces.append(t)
        return "\n".join(pieces) if pieces else None
    return None


def _extract_user_prompt(event: dict[str, object]) -> str | None:
    """If this event is a user message with string content, return the content."""
    if event.get("type") != "user":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    if message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Some user messages come as a list of blocks; pick the first text block.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return None


_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>.*?</[^>]+>", re.DOTALL)


def strip_command_wrappers(text: str) -> str:
    """Convert slash-command wrappers into a human-friendly summary.

    Standard form::

        <command-message>refine-task</command-message>
        <command-name>/refine-task</command-name>
        <command-args>https://...</command-args>

    becomes ``/refine-task https://...``. Plain prompts pass through with all
    inline ``<tag>...</tag>`` blocks stripped.
    """
    name_match = _CMD_NAME_RE.search(text)
    if name_match:
        name = name_match.group(1).strip()
        args_match = _CMD_ARGS_RE.search(text)
        args = args_match.group(1).strip() if args_match else ""
        return f"{name} {args}".strip()
    cleaned = _TAG_RE.sub("", text)
    return cleaned.strip()


def _truncate(text: str, limit: int = PROMPT_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def count_lines(path: Path) -> int:
    """Streaming line count, no full file in memory."""
    count = 0
    with path.open("rb") as f:
        while chunk := f.read(64 * 1024):
            count += chunk.count(b"\n")
    return count
