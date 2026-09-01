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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from multi_claude.index import IndexedSession, SessionIndex, default_index
from multi_claude.names import NamesStore
from multi_claude.tags import TagsStore

HEADER_SCAN_LINES = 80
PROMPT_MAX_CHARS = 120
# Caps on the FTS payload per session. They were 64 KB / 2.000 lines, and the line cap was
# the one that bit: measured over 35 real sessions, the five above 2.000 lines were indexed
# to 62 KB each while their conversational text ran to 425 KB — 85% of the longest one was
# not searchable. Raising both covers every session on that machine (longest: 7.555 lines)
# and costs 0,8 s to build for a 2,3 MB index, because the payload is only user/assistant
# text: tool calls and their output never enter it.
FTS_CONTENT_MAX_CHARS = 512_000
FTS_REINDEX_SCAN_LINES = 20_000
RENAME_SCAN_LINES = 50_000  # cap when scanning for the latest /rename in long sessions

# The tool calls that count as having *touched* a file, and which key of their input
# carries the path. Only writes are here: `file:` answers "in which conversation did we
# touch this file", and a Read is not touching it — indexing reads would also drown the
# signal, since a session that greps around opens far more files than it changes.
#
# Edits made by running a shell command (`sed -i`, a heredoc, `git checkout`) cannot be
# recovered from a Bash tool call without parsing shell, so they are not here either. This
# is a map of what Claude edited *through its own editing tools*, and the README says so.
TOUCH_TOOLS: dict[str, str] = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Ceiling on distinct paths kept per session. A session that rewrites a tree can touch
# thousands of files, and past a point the row stops being an answer to "which conversation
# was it" and becomes a second copy of the repo listing.
TOUCHED_FILES_MAX = 2_000

# A gap longer than this between two events is not thinking, it is the person having left.
# Five minutes is the same threshold the timesheet skill settled on, and the point of
# subtracting the gaps at all is that "first event to last event" on a session someone picks
# up after lunch reports nine hours of work that nobody did.
IDLE_GAP_SECONDS = 300

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

    if index.is_fresh(sid, stat.st_mtime):
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
    indexables = extract_indexables(jsonl_path)
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
    index.upsert_session(
        indexed,
        fts_content=indexables.fts_content,
        touched_files=indexables.touched_files,
        usage=indexables.usage,
    )

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
    """Return the name embedded in ``jsonl_path`` itself, or ``None``.

    Two sources, in precedence order:

    1. **Claude's ``/rename``** — every ``system/local_command`` event whose
       ``content`` matches ``<local-command-stdout>Session renamed to:
       X</local-command-stdout>``; the last occurrence wins (later renames beat
       earlier ones). A top-level ``name`` string counts here too, for whichever
       Claude build wrote one inline.
    2. **Claude's own generated title** — ``{"type": "ai-title", "aiTitle": X}``
       events, again last-one-wins. Claude Code titles sessions as they go, which
       reads far better in the listing than a truncated first prompt.

    A name the user chose deliberately outranks a generated one regardless of
    which came last in the file, so the two are tracked separately rather than
    into a single "latest".
    """
    renamed: str | None = None
    ai_title: str | None = None
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
                    renamed = top_name.strip()
                    continue
                etype = event.get("type")
                if etype == "ai-title":
                    candidate = event.get("aiTitle")
                    if isinstance(candidate, str) and candidate.strip():
                        ai_title = candidate.strip()
                    continue
                if etype != "system":
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
                        renamed = candidate
    except OSError:
        pass
    return renamed or ai_title


@dataclass(frozen=True)
class Usage:
    """What a session spent, as its own events report it.

    Tokens and wall-clock only — **no money**. A price depends on the model and the plan,
    both change, and a Max or Pro subscription includes usage that no per-token figure
    describes. A number in euros that does not match the bill is worse than no number, the
    same reason the credential scan says "unknown" instead of guessing "no".
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    #: First and last event timestamps (ISO-8601 strings, as written). Empty when absent.
    first_at: str = ""
    last_at: str = ""
    #: Seconds of actual work: the gaps between consecutive events, minus the ones longer
    #: than ``IDLE_GAP_SECONDS``, which are lunch and not thinking.
    active_seconds: int = 0

    @property
    def fresh_tokens(self) -> int:
        """Input plus output — what was actually sent and generated.

        Deliberately **not** a grand total with the cache folded in. Measured on real
        sessions, ``cache_read`` runs to hundreds of millions and dwarfs everything else by
        three orders of magnitude, so a single "total" is a cache-read count wearing a
        misleading label: it would read as an enormous spend when cache reads are the
        cheapest thing on the bill. The four numbers are reported side by side instead.
        """
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Indexables:
    """Everything one pass over a jsonl yields for the index.

    They are extracted together because they are questions about the same bytes, and a
    session's jsonl runs to megabytes: reading it once per question would multiply the cost
    of the very scan that has to stay cheap.
    """

    fts_content: str
    touched_files: tuple[str, ...]
    usage: Usage = field(default_factory=Usage)


def extract_indexables(jsonl_path: Path) -> Indexables:
    """Read a session once and pull out its searchable text and the files it touched.

    The text skips tool_use/tool_result payloads to keep the index small, and caps at
    ``FTS_CONTENT_MAX_CHARS`` so a runaway session doesn't blow up the DB. Reaching that
    cap stops the text from growing but **not** the scan: paths cost a few bytes each and
    the useful answer to "did this conversation touch that file" is about the whole
    conversation, not its first half. The line cap still applies to both.
    """
    parts: list[str] = []
    total = 0
    files: dict[str, None] = {}  # insertion-ordered set: first touch wins, dedup is free
    tokens = [0, 0, 0, 0]  # input, output, cache read, cache creation
    first_at = last_at = ""
    active = 0.0
    previous: float | None = None
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
                stamp = event.get("timestamp")
                if isinstance(stamp, str) and stamp:
                    # ISO-8601 with a fixed offset sorts lexicographically, which is all the
                    # ordering that is needed here — and avoids parsing 20.000 datetimes to
                    # find two of them.
                    if not first_at or stamp < first_at:
                        first_at = stamp
                    if stamp > last_at:
                        last_at = stamp
                    seconds = _epoch(stamp)
                    if seconds is not None:
                        if previous is not None:
                            gap = seconds - previous
                            if 0 < gap <= IDLE_GAP_SECONDS:
                                active += gap
                        previous = seconds
                _add_usage(event, tokens)
                if len(files) < TOUCHED_FILES_MAX:
                    for path in _extract_touched_files(event):
                        files.setdefault(path, None)
                        if len(files) >= TOUCHED_FILES_MAX:
                            break
                if total >= FTS_CONTENT_MAX_CHARS:
                    continue
                text = _extract_text_for_fts(event)
                if not text:
                    continue
                parts.append(text)
                total += len(text)
    except OSError:
        return Indexables(fts_content="", touched_files=())
    return Indexables(
        fts_content="\n".join(parts)[:FTS_CONTENT_MAX_CHARS],
        touched_files=tuple(files),
        usage=Usage(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_read_tokens=tokens[2],
            cache_creation_tokens=tokens[3],
            first_at=first_at,
            last_at=last_at,
            active_seconds=int(active),
        ),
    )


def _epoch(stamp: str) -> float | None:
    """Seconds since the epoch for an ISO-8601 stamp, or None if it is not one.

    ``Z`` is spelled out because ``fromisoformat`` did not accept it before 3.11, and this
    package supports 3.10.
    """
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _add_usage(event: dict[str, object], into: list[int]) -> None:
    """Fold one event's reported token usage into ``into``.

    Read from the events rather than recomputed: these are the numbers the API actually
    billed, and re-counting tokens locally would produce a different figure that looks
    authoritative and is not.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    for slot, key in enumerate(
        ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    ):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            into[slot] += value


def extract_fts_content(jsonl_path: Path) -> str:
    """Just the searchable text of a session. See :func:`extract_indexables`."""
    return extract_indexables(jsonl_path).fts_content


def extract_touched_files(jsonl_path: Path) -> tuple[str, ...]:
    """Just the files a session edited, in the order it first touched them."""
    return extract_indexables(jsonl_path).touched_files


def _extract_touched_files(event: dict[str, object]) -> list[str]:
    """The paths this event's tool calls wrote to, if any.

    Paths come out exactly as the tool call carried them — absolute, since that is what
    Claude Code writes — because normalising here would mean guessing a cwd that the event
    does not necessarily have.
    """
    if event.get("type") != "assistant":
        return []
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    found: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        key = TOUCH_TOOLS.get(str(block.get("name")))
        if key is None:
            continue
        payload = block.get("input")
        if not isinstance(payload, dict):
            continue
        path = payload.get(key)
        if isinstance(path, str) and path.strip():
            found.append(path.strip())
    return found


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
