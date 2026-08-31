"""MCP server exposing the session index, so Claude can search its own past work.

The index that powers the global search screen (`?`) is a local SQLite FTS5 table
over every session's prompts and assistant text. This module puts an MCP server in
front of it: Claude asks "what did we do about the SSH port in GitLab", and gets the
conversation where it was solved — from any project, including one it has never had
in context.

Read-only by design. Nothing here writes to, moves or deletes a session; the only
mutation is populating multi-claude's own index cache.

**No SDK.** MCP over stdio is newline-delimited JSON-RPC 2.0, so it is the stdlib
plus `json` — consistent with the rest of the project, and it keeps `multi-claude`
free of the SDK's dependency tree. Two rules from the spec drive the shape of this
module: messages are delimited by newlines and **must not** contain embedded ones
(`json.dumps` escapes them, so this holds), and **nothing** may go to stdout that
is not an MCP message — which is why every diagnostic here goes to stderr.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from multi_claude.discovery import CLAUDE_PROJECTS_DIR, Project, scan_projects
from multi_claude.formatting import format_relative_time, format_size
from multi_claude.index import IndexedSession, SessionIndex, default_index
from multi_claude.names import NamesStore
from multi_claude.session import scan_sessions
from multi_claude.transcript import read_last_turns

SERVER_NAME = "multi-claude"

# The version we speak. A client asking for any version in _KNOWN_PROTOCOL_VERSIONS
# gets that one echoed back; anything else gets ours, which the spec prescribes
# ("MUST respond with another protocol version it supports").
PROTOCOL_VERSION = "2025-06-18"
_KNOWN_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})

INSTRUCTIONS = (
    "Searches this machine's Claude Code session history (all projects) by content. "
    "Use search_sessions before re-deriving something the user may have already "
    "solved with you in another session, then get_session to read that conversation. "
    "When the question is about a file rather than a topic — why is this written the "
    "way it is, when did we last touch it — sessions_touching_file finds the "
    "conversation behind the change, which git history does not record."
)

# JSON-RPC error codes used here (the negative ones are the standard set).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20
_PROMPT_CHARS = 160

# An id reaches us as a path segment (``projects/*/<id>.jsonl``), so a crafted
# ``../..`` must not get there. Same guard as :func:`remote.safe_session_id`, but
# raising the MCP-shaped error instead of a remote one.
_SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #


@dataclass
class SessionTools:
    """The actual work behind each tool. Injectable so tests need no real HOME."""

    index: SessionIndex
    projects_dir: Path
    names: NamesStore

    _indexed_this_process: bool = False

    @classmethod
    def create(
        cls,
        *,
        index: SessionIndex | None = None,
        projects_dir: Path | None = None,
        names: NamesStore | None = None,
    ) -> SessionTools:
        return cls(
            index=index if index is not None else default_index(),
            projects_dir=projects_dir if projects_dir is not None else CLAUDE_PROJECTS_DIR,
            names=names if names is not None else NamesStore(),
        )

    # -- helpers ------------------------------------------------------------ #

    def _projects(self) -> list[Project]:
        return scan_projects(self.projects_dir)

    def _ensure_indexed(self) -> str | None:
        """Populate the index if it has never been populated.

        The index is written when you *enter* a project in the TUI, so a fresh
        install has nothing to search. Doing a full scan on the first search makes
        the server useful out of the box; the note it returns tells the model why
        that first call was slow.
        """
        if self._indexed_this_process or self.index.count_sessions() > 0:
            return None
        count = self.refresh()
        self._indexed_this_process = True
        return f"(first search on an empty index: scanned {count} sessions)"

    def refresh(self, project_path: str | None = None) -> int:
        """(Re)index sessions and return how many were seen."""
        total = 0
        for project in self._projects():
            if project_path and not _is_within(project.path, project_path):
                continue
            total += len(
                scan_sessions(project.encoded_path, index=self.index, names_store=self.names)
            )
        self._indexed_this_process = True
        return total

    def _display_name(self, session: IndexedSession) -> str:
        return (
            self.names.get(session.session_id)
            or session.embedded_name
            or _shorten(session.first_prompt or "(sin prompt inicial)", _PROMPT_CHARS)
        )

    def _find_jsonl(self, session_id: str, indexed: IndexedSession | None = None) -> Path | None:
        """The session's jsonl on disk, or None if it is gone.

        The index is a cache that is never purged, so it outlives the sessions it
        describes: a row whose jsonl was deleted (or moved with ``m``) is still in
        there. Callers use the None to skip it rather than offer a dead id.
        """
        if indexed is None:
            indexed = self.index.get(session_id)
        if indexed is not None:
            path = Path(indexed.jsonl_path)
            if path.is_file():
                return path
        # Never indexed, or moved since it was: the id is the filename, so look for it.
        matches = sorted(self.projects_dir.glob(f"*/{session_id}.jsonl"))
        return matches[0] if matches else None

    # -- tools -------------------------------------------------------------- #

    def search_sessions(self, arguments: dict[str, Any]) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentError("`query` is required and must be a non-empty string")
        limit = _clamp_limit(arguments.get("limit"))
        project_path = arguments.get("project_path")
        if project_path is not None and not isinstance(project_path, str):
            raise ToolArgumentError("`project_path` must be a string")

        note = self._ensure_indexed()
        matched = self.index.fts_search(query, limit=limit, cwd_prefix=project_path)
        # Drop rows whose jsonl is gone: the index outlives what it describes, and
        # handing back an id that get_session cannot open is worse than one hit less.
        results = [s for s in matched if self._find_jsonl(s.session_id, s) is not None]
        if not results:
            lines = [f"No sessions match {query!r}."]
            if project_path:
                lines.append(f"Scope was {project_path}; try without project_path.")
            if matched:
                lines.append(
                    f"({len(matched)} indexed session(s) matched but no longer exist on disk.)"
                )
            lines.append(
                "The index only covers sessions it has parsed — call refresh_index "
                "if this machine has history that was never opened in multi-claude."
            )
            return _join(note, "\n".join(lines))

        lines = [f"{len(results)} session(s) matching {query!r}, best first:", ""]
        for session in results:
            lines.append(
                f"- {self._display_name(session)}\n"
                f"  id: {session.session_id}\n"
                f"  project: {session.cwd or '?'}"
                f"{f' (branch {session.branch})' if session.branch else ''}\n"
                f"  {session.message_count} msgs · {format_size(session.size_bytes)} · "
                f"last activity {format_relative_time(session.mtime)} ago"
            )
        lines.append("")
        lines.append("Read one with get_session, or resume it with `claude --resume <id>`.")
        return _join(note, "\n".join(lines))

    def sessions_touching_file(self, arguments: dict[str, Any]) -> str:
        """Which past conversations edited a given file.

        A separate tool rather than a flag on ``search_sessions`` because it is a different
        question with a different answer set: this one is not full-text at all, and a model
        that could pass both would treat "matched the words" and "edited the file" as one
        relevance order when they are not comparable.
        """
        wanted = arguments.get("file")
        if not isinstance(wanted, str) or not wanted.strip():
            raise ToolArgumentError("`file` is required and must be a non-empty string")
        limit = _clamp_limit(arguments.get("limit"))
        query = arguments.get("query")
        if query is not None and not isinstance(query, str):
            raise ToolArgumentError("`query` must be a string")

        note = self._ensure_indexed()
        matched = self.index.sessions_touching(wanted, limit=limit)
        if query and query.strip():
            narrowed = {s.session_id for s in self.index.fts_search(query, limit=_MAX_LIMIT)}
            matched = [s for s in matched if s.session_id in narrowed]
        results = [s for s in matched if self._find_jsonl(s.session_id, s) is not None]

        if not results:
            lines = [f"No indexed session edited a file matching {wanted!r}."]
            if query:
                lines.append(f"(Narrowed by {query!r} — try without it.)")
            lines.append(
                "Only edits made through Claude's own editing tools (Edit, Write, "
                "MultiEdit, NotebookEdit) are recorded. A file changed by a shell command "
                "-- `sed -i`, a heredoc, `git checkout` -- leaves no trace here, and "
                "neither does a file that was only read."
            )
            return _join(note, "\n".join(lines))

        lines = [
            f"{len(results)} session(s) edited a file matching {wanted!r}, most recent first:",
            "",
        ]
        for session in results:
            lines.append(
                f"- {self._display_name(session)}\n"
                f"  id: {session.session_id}\n"
                f"  project: {session.cwd or '?'}"
                f"{f' (branch {session.branch})' if session.branch else ''}\n"
                f"  last activity {format_relative_time(session.mtime)} ago"
            )
        lines.append("")
        lines.append("Read one with get_session, or resume it with `claude --resume <id>`.")
        return _join(note, "\n".join(lines))

    def search_team_sessions(self, arguments: dict[str, Any]) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentError("`query` is required and must be a non-empty string")
        limit = _clamp_limit(arguments.get("limit"))

        results = self.index.fts_search_remote(query, limit=limit)
        if not results:
            if self.index.count_remote_sessions() == 0:
                return (
                    "No team sessions are cached. multi-claude records a sessions "
                    "repository's listing when its tab is opened in the TUI, so either "
                    "no project here is linked to one, or none has been visited yet."
                )
            with_text = self.index.count_remote_with_text()
            total = self.index.count_remote_sessions()
            return (
                f"No team session matches {query!r}. {with_text} of {total} cached team "
                "session(s) are searchable by their full text; the rest only by their "
                "metadata (name, first prompt, tags, branch, author) because their search "
                "payload has not been downloaded yet, so this is not proof that nobody "
                "discussed it."
            )

        lines = [f"{len(results)} team session(s) matching {query!r}, best first:", ""]
        for session in results:
            when = f" at {session.published_at}" if session.published_at else ""
            branch = f" · branch {session.branch}" if session.branch else ""
            tags = " · tags " + ", ".join(session.tags) if session.tags else ""
            lines.append(
                f"- {session.title}\n"
                f"  id: {session.session_id}\n"
                f"  published by: {session.published_by or '?'}{when}\n"
                f"  remote: {session.remote_label}{branch}{tags}\n"
                f"  {session.message_count} msgs · {format_size(session.size_bytes)}"
            )
        lines.append("")
        lines.append(
            "These live on a shared repository, not on this machine. To read one, the user "
            "fetches it from the remote's tab in multi-claude (Enter on the row); after that "
            "get_session can read it like any local session."
        )
        return "\n".join(lines)

    def get_session(self, arguments: dict[str, Any]) -> str:
        session_id = arguments.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ToolArgumentError("`session_id` is required and must be a string")
        if not _SAFE_ID_RE.match(session_id):
            raise ToolArgumentError(f"not a valid session id: {session_id!r}")
        turns = arguments.get("turns", 12)
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
            raise ToolArgumentError("`turns` must be a positive integer")
        turns = min(turns, 60)

        jsonl = self._find_jsonl(session_id)
        if jsonl is None:
            return f"No session with id {session_id!r} on this machine."

        indexed = self.index.get(session_id)
        header = [f"Session {session_id}"]
        if indexed is not None:
            header.append(f"name: {self._display_name(indexed)}")
            header.append(f"project: {indexed.cwd or '?'}")
            if indexed.branch:
                header.append(f"branch: {indexed.branch}")
            header.append(
                f"{indexed.message_count} msgs · {format_size(indexed.size_bytes)} · "
                f"last activity {format_relative_time(indexed.mtime)} ago"
            )
        header.append(f"file: {jsonl}")

        # Read enough lines that `turns` turns can plausibly be found: tool calls
        # and results sit between them and carry no text of their own.
        conversation = read_last_turns(jsonl, tail_lines=max(60, turns * 8), turn_limit=turns)
        if not conversation:
            body = "(no text turns in the tail of this session)"
        else:
            body = "\n\n".join(
                f"{'user' if role == 'user' else 'assistant'}: {text}"
                for role, text in conversation
            )
        return "\n".join(header) + f"\n\nLast {len(conversation)} turn(s):\n\n" + body

    def list_projects(self, arguments: dict[str, Any]) -> str:
        limit = _clamp_limit(arguments.get("limit"), default=50)
        projects = self._projects()[:limit]
        if not projects:
            return f"No Claude Code projects found under {self.projects_dir}."
        lines = [f"{len(projects)} project(s), most recently active first:", ""]
        for project in projects:
            orphan = " [orphan: path no longer exists]" if project.is_orphan else ""
            lines.append(
                f"- {project.name} — {project.path}{orphan}\n"
                f"  {project.session_count} session(s) · "
                f"last activity {format_relative_time(project.last_activity)} ago"
            )
        return "\n".join(lines)

    def refresh_index(self, arguments: dict[str, Any]) -> str:
        project_path = arguments.get("project_path")
        if project_path is not None and not isinstance(project_path, str):
            raise ToolArgumentError("`project_path` must be a string")
        count = self.refresh(project_path)
        scope = f" under {project_path}" if project_path else ""
        return f"Indexed {count} session(s){scope}. Full-text search is up to date."


class ToolArgumentError(Exception):
    """Bad arguments — a protocol error (-32602), not a tool execution failure."""


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_sessions",
        "title": "Search past Claude Code sessions",
        "description": (
            "Full-text search over the content of every indexed Claude Code session on "
            "this machine, across all projects. Searches what was actually said in the "
            "conversations, not just their titles. Use this to find out whether the user "
            "has already solved something with you before."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Words to look for. Terms are ANDed; accents and case are "
                        "ignored, so 'refactor' matches 'refactorización'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (1-{_MAX_LIMIT}, default {_DEFAULT_LIMIT}).",
                },
                "project_path": {
                    "type": "string",
                    "description": (
                        "Restrict to sessions recorded at this path or below it, e.g. "
                        "/home/me/work/api. Omit to search every project."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "sessions_touching_file",
        "title": "Find the sessions that edited a file",
        "description": (
            "Which past Claude Code conversations edited a given file. Answers 'when did "
            "we last touch this file, and what were we doing' — the conversation behind a "
            "change, which git history does not record. Only edits made through Claude's "
            "editing tools count; a file changed by a shell command leaves no trace, and a "
            "file that was only read is not a match."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": (
                        "File to look for. A bare name is matched against the basename "
                        "('index.py'), a term with a separator against the whole path "
                        "('multi_claude/index.py'). Substrings work."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional: narrow the result to sessions whose text also matches "
                        "these words."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (1-{_MAX_LIMIT}, default {_DEFAULT_LIMIT}).",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "search_team_sessions",
        "title": "Search sessions published by the team",
        "description": (
            "Search the sessions teammates published to a shared sessions repository, as "
            "last listed on this machine. Use it to find out whether a colleague already "
            "solved something. Coverage varies per session: one whose search payload has "
            "been downloaded is searchable by its full conversation text, one whose has not "
            "(or that was published before that existed) only by its metadata — name, first "
            "prompt, tags, branch, author. So a miss is not proof that nobody discussed it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Words to look for in the name, first prompt, tags, branch or "
                        "author. Terms are ANDed; accents and case are ignored."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (1-{_MAX_LIMIT}, default {_DEFAULT_LIMIT}).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_session",
        "title": "Read a past session",
        "description": (
            "Return a session's metadata and the last N conversation turns, so a match "
            "from search_sessions can be read without resuming it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session uuid, as returned by search_sessions.",
                },
                "turns": {
                    "type": "integer",
                    "description": "How many trailing turns to return (1-60, default 12).",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "list_projects",
        "title": "List Claude Code projects",
        "description": (
            "Every project with Claude Code history on this machine, most recently "
            "active first, with its session count and real path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"Max projects (1-{_MAX_LIMIT}, default 50).",
                }
            },
        },
    },
    {
        "name": "refresh_index",
        "title": "Reindex sessions for search",
        "description": (
            "Parse sessions into the search index. Only needed when search_sessions "
            "misses history that exists on disk — the index is populated lazily."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "Only reindex this project path and below. Omit for all.",
                }
            },
        },
    },
]


def _tool_handlers(tools: SessionTools) -> dict[str, Callable[[dict[str, Any]], str]]:
    return {
        "search_sessions": tools.search_sessions,
        "sessions_touching_file": tools.sessions_touching_file,
        "search_team_sessions": tools.search_team_sessions,
        "get_session": tools.get_session,
        "list_projects": tools.list_projects,
        "refresh_index": tools.refresh_index,
    }


# --------------------------------------------------------------------------- #
# JSON-RPC plumbing
# --------------------------------------------------------------------------- #


def handle_message(message: object, tools: SessionTools) -> dict[str, Any] | None:
    """Map one decoded JSON-RPC message to its response, or None for notifications."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, _INVALID_REQUEST, "Not a JSON-RPC 2.0 message")
    method = message.get("method")
    if not isinstance(method, str):
        return _error(message.get("id"), _INVALID_REQUEST, "Missing method")
    request_id = message.get("id")
    is_notification = "id" not in message
    params = message.get("params")
    params = params if isinstance(params, dict) else {}

    if is_notification:
        # Nothing to acknowledge: notifications get no response, ever.
        return None

    if method == "initialize":
        return _result(request_id, _initialize_result(params))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOL_DEFINITIONS})
    if method == "tools/call":
        return _call_tool(request_id, params, tools)
    return _error(request_id, _METHOD_NOT_FOUND, f"Method not found: {method}")


def _initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    version = (
        requested
        if isinstance(requested, str) and requested in _KNOWN_PROTOCOL_VERSIONS
        else PROTOCOL_VERSION
    )
    return {
        "protocolVersion": version,
        # listChanged is False: the tool list is a module constant.
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "title": "multi-claude", "version": _version()},
        "instructions": INSTRUCTIONS,
    }


def _call_tool(request_id: object, params: dict[str, Any], tools: SessionTools) -> dict[str, Any]:
    name = params.get("name")
    handlers = _tool_handlers(tools)
    if not isinstance(name, str) or name not in handlers:
        return _error(request_id, _INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = params.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    try:
        text = handlers[name](arguments)
    except ToolArgumentError as exc:
        return _error(request_id, _INVALID_PARAMS, str(exc))
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Execution failure: reported inside the result so the model can react,
        # not as a protocol error (spec, "Error Handling"). sqlite3.Error is in
        # here because the index is a rebuildable cache that *can* be corrupt —
        # the model should be told to call refresh_index, not see a broken server.
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            },
        )
    return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})


def _result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    tools: SessionTools | None = None,
) -> None:
    """Read newline-delimited JSON-RPC from ``stdin`` until EOF, replying on ``stdout``."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    log = stderr if stderr is not None else sys.stderr
    session_tools = tools if tools is not None else SessionTools.create()

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(sink, _error(None, _PARSE_ERROR, f"Invalid JSON: {exc}"))
            continue
        try:
            response = handle_message(message, session_tools)
        except Exception as exc:  # a bug here must not kill the session
            print(f"multi-claude mcp: unhandled {type(exc).__name__}: {exc}", file=log, flush=True)
            request_id = message.get("id") if isinstance(message, dict) else None
            response = _error(request_id, _INVALID_REQUEST, f"Internal error: {exc}")
        if response is not None:
            _write(sink, response)


def _write(sink: TextIO, message: dict[str, Any]) -> None:
    # ensure_ascii=False keeps accents readable; json.dumps escapes any newline in
    # the payload, so the one-message-per-line contract holds.
    sink.write(json.dumps(message, ensure_ascii=False) + "\n")
    sink.flush()


def main() -> None:
    # "JSON-RPC messages MUST be UTF-8 encoded" — and on Windows the standard
    # streams are not, so a session with accents would break there. Only the real
    # entrypoint does this; serve() leaves injected streams alone.
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    serve()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _clamp_limit(raw: object, *, default: int = _DEFAULT_LIMIT) -> int:
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ToolArgumentError("`limit` must be an integer")
    return max(1, min(raw, _MAX_LIMIT))


def _is_within(path: Path, prefix: str) -> bool:
    try:
        path.relative_to(Path(prefix))
    except ValueError:
        return False
    return True


def _shorten(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit].rstrip() + "…"


def _join(note: str | None, body: str) -> str:
    return f"{note}\n\n{body}" if note else body


def _version() -> str:
    from multi_claude import __version__

    return __version__


if __name__ == "__main__":
    main()
