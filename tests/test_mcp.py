"""Tests for the MCP server over the session index.

The protocol side is checked against the shapes the spec mandates (newline-delimited
JSON-RPC 2.0, version negotiation, tool-vs-protocol error split), and the tool side
against synthetic project trees. Nothing here touches the network or the real HOME.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from multi_claude.index import IndexedRemoteSession, SessionIndex
from multi_claude.mcp import (
    PROTOCOL_VERSION,
    TOOL_DEFINITIONS,
    SessionTools,
    _tool_handlers,
    handle_message,
    serve,
)
from multi_claude.names import NamesStore


@pytest.fixture
def tools(tmp_path: Path, projects_root: Path) -> SessionTools:
    return SessionTools.create(
        index=SessionIndex(tmp_path / "idx.sqlite3"),
        projects_dir=projects_root,
        names=NamesStore(tmp_path / "names.json"),
    )


def write_conversation(
    project_dir: Path,
    *,
    session_id: str,
    cwd: str,
    turns: list[tuple[str, str]],
    branch: str = "main",
) -> Path:
    """A jsonl with real user/assistant text blocks, which the FTS payload indexes."""
    project_dir.mkdir(parents=True, exist_ok=True)
    jsonl = project_dir / f"{session_id}.jsonl"
    events = []
    for i, (role, text) in enumerate(turns):
        event = {
            "type": role,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
            "sessionId": session_id,
        }
        if i == 0:
            event["cwd"] = cwd
            event["gitBranch"] = branch
        events.append(event)
    with jsonl.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return jsonl


def call(tools: SessionTools, name: str, arguments: dict | None = None) -> dict:
    return handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        tools,
    )


def text_of(response: dict) -> str:
    return response["result"]["content"][0]["text"]


# -- lifecycle -------------------------------------------------------------- #


def test_initialize_echoes_a_known_protocol_version(tools: SessionTools) -> None:
    response = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
        tools,
    )
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert response["result"]["serverInfo"]["name"] == "multi-claude"
    assert "instructions" in response["result"]


def test_initialize_falls_back_to_our_version_when_the_ask_is_unknown(
    tools: SessionTools,
) -> None:
    response = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1.0.0"},
        },
        tools,
    )
    assert response["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_notifications_get_no_response(tools: SessionTools) -> None:
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, tools) is None


def test_ping_answers_empty(tools: SessionTools) -> None:
    response = handle_message({"jsonrpc": "2.0", "id": 3, "method": "ping"}, tools)
    assert response == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_unknown_method_is_a_protocol_error(tools: SessionTools) -> None:
    response = handle_message({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}, tools)
    assert response["error"]["code"] == -32601


def test_non_jsonrpc_message_is_rejected(tools: SessionTools) -> None:
    response = handle_message({"method": "initialize", "id": 1}, tools)
    assert response["error"]["code"] == -32600


# -- tools/list ------------------------------------------------------------- #


def test_tools_list_exposes_every_tool_with_a_schema(tools: SessionTools) -> None:
    response = handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, tools)
    listed = response["result"]["tools"]
    assert {t["name"] for t in listed} == {
        "search_sessions",
        "search_team_sessions",
        "get_session",
        "list_projects",
        "refresh_index",
    }
    for tool in listed:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_every_defined_tool_has_a_handler(tools: SessionTools) -> None:
    """A definition without a handler would be advertised and then fail at call time."""
    assert {d["name"] for d in TOOL_DEFINITIONS} == set(_tool_handlers(tools))


def test_a_corrupt_index_is_a_tool_error_not_a_broken_server(
    tools: SessionTools, tmp_path: Path
) -> None:
    """The index is a rebuildable cache; a corrupt one must not read as a protocol failure."""
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a database")
    broken = SessionTools.create(
        index=SessionIndex(corrupt),
        projects_dir=tools.projects_dir,
        names=tools.names,
    )
    response = call(broken, "search_sessions", {"query": "anything"})
    assert response["result"]["isError"] is True
    assert "error" not in response


def test_unknown_tool_is_a_protocol_error(tools: SessionTools) -> None:
    response = call(tools, "delete_everything")
    assert response["error"]["code"] == -32602
    assert "Unknown tool" in response["error"]["message"]


# -- search_sessions -------------------------------------------------------- #


def test_search_finds_a_session_by_its_content(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="aaa",
        cwd="/home/me/api",
        turns=[("user", "el puerto SSH de GitLab no era el 22"), ("assistant", "lo arreglamos")],
    )
    response = call(tools, "search_sessions", {"query": "puerto ssh"})
    body = text_of(response)
    assert "aaa" in body
    assert "/home/me/api" in body


def test_search_ignores_accents_and_case(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="bbb",
        cwd="/home/me/api",
        turns=[("user", "hicimos la refactorización del índice")],
    )
    assert "bbb" in text_of(call(tools, "search_sessions", {"query": "REFACTORIZACION"}))


def test_search_scopes_to_project_path(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="ccc",
        cwd="/home/me/api",
        turns=[("user", "deployment falló")],
    )
    write_conversation(
        projects_root / "-home-me-web",
        session_id="ddd",
        cwd="/home/me/web",
        turns=[("user", "deployment falló")],
    )
    scoped = text_of(
        call(tools, "search_sessions", {"query": "deployment", "project_path": "/home/me/web"})
    )
    assert "ddd" in scoped
    assert "ccc" not in scoped


def test_search_prefix_does_not_leak_into_a_sibling_with_a_shared_stem(
    tools: SessionTools, projects_root: Path
) -> None:
    """/home/me/api must not match /home/me/api-legacy."""
    write_conversation(
        projects_root / "-home-me-api-legacy",
        session_id="eee",
        cwd="/home/me/api-legacy",
        turns=[("user", "cache invalidation")],
    )
    scoped = text_of(
        call(tools, "search_sessions", {"query": "cache", "project_path": "/home/me/api"})
    )
    assert "eee" not in scoped


def test_search_without_query_is_a_protocol_error(tools: SessionTools) -> None:
    response = call(tools, "search_sessions", {"query": "   "})
    assert response["error"]["code"] == -32602


def test_search_rejects_a_non_integer_limit(tools: SessionTools) -> None:
    response = call(tools, "search_sessions", {"query": "x", "limit": "many"})
    assert response["error"]["code"] == -32602


def test_search_caps_the_limit(tools: SessionTools, projects_root: Path) -> None:
    for i in range(5):
        write_conversation(
            projects_root / "-home-me-api",
            session_id=f"cap{i}",
            cwd="/home/me/api",
            turns=[("user", "widget")],
        )
    body = text_of(call(tools, "search_sessions", {"query": "widget", "limit": 10_000}))
    assert "5 session(s)" in body


def test_search_on_an_empty_index_scans_first_and_says_so(
    tools: SessionTools, projects_root: Path
) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="fff",
        cwd="/home/me/api",
        turns=[("user", "kubernetes ingress")],
    )
    assert tools.index.count_sessions() == 0
    body = text_of(call(tools, "search_sessions", {"query": "ingress"}))
    assert "empty index" in body
    assert "fff" in body


def test_search_skips_sessions_whose_jsonl_is_gone(
    tools: SessionTools, projects_root: Path
) -> None:
    """The index is never purged, so it outlives the sessions it describes."""
    alive = write_conversation(
        projects_root / "-home-me-api",
        session_id="alive",
        cwd="/home/me/api",
        turns=[("user", "postgres vacuum")],
    )
    dead = write_conversation(
        projects_root / "-home-me-api",
        session_id="dead",
        cwd="/home/me/api",
        turns=[("user", "postgres vacuum")],
    )
    tools.refresh()
    dead.unlink()
    assert alive.is_file()

    body = text_of(call(tools, "search_sessions", {"query": "vacuum"}))
    assert "alive" in body
    assert "dead" not in body
    assert "1 session(s)" in body


def test_search_says_when_every_match_is_gone(tools: SessionTools, projects_root: Path) -> None:
    jsonl = write_conversation(
        projects_root / "-home-me-api",
        session_id="ghost",
        cwd="/home/me/api",
        turns=[("user", "redis eviction")],
    )
    tools.refresh()
    jsonl.unlink()
    body = text_of(call(tools, "search_sessions", {"query": "eviction"}))
    assert "no longer exist on disk" in body


def test_search_with_no_match_explains_the_index_is_lazy(tools: SessionTools) -> None:
    body = text_of(call(tools, "search_sessions", {"query": "nada de nada"}))
    assert "refresh_index" in body


# -- get_session ------------------------------------------------------------ #


def test_get_session_returns_metadata_and_turns(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="ggg",
        cwd="/home/me/api",
        branch="fix/ssh",
        turns=[("user", "por qué falla el push"), ("assistant", "el puerto no es el 22")],
    )
    tools.refresh()
    body = text_of(call(tools, "get_session", {"session_id": "ggg"}))
    assert "Session ggg" in body
    assert "fix/ssh" in body
    assert "user: por qué falla el push" in body
    assert "assistant: el puerto no es el 22" in body


# -- search_team_sessions --------------------------------------------------- #


def _cache_team_session(
    tools: SessionTools,
    *,
    session_id: str = "team-1",
    display_name: str = "el deploy de staging falla con 502",
    published_by: str = "ana@example.com",
    branch: str = "fix/nginx",
    tags: tuple[str, ...] = ("infra",),
    remote_key: str = "remote-a",
    remote_label: str = "cliente-x",
) -> None:
    tools.index.replace_remote_sessions(
        remote_key,
        [
            IndexedRemoteSession(
                remote_key=remote_key,
                session_id=session_id,
                remote_label=remote_label,
                project_key="git@host:group/api.git",
                published_by=published_by,
                published_at="2026-08-01T10:00:00+00:00",
                cwd="/home/ana/api",
                branch=branch,
                display_name=display_name,
                first_prompt="por qué devuelve 502",
                tags=tags,
                message_count=120,
                size_bytes=4096,
            )
        ],
        listed_at=1_000_000.0,
    )


def test_team_search_finds_by_name_author_branch_and_tag(tools: SessionTools) -> None:
    _cache_team_session(tools)
    for query in ("staging", "ana", "nginx", "infra"):
        body = text_of(call(tools, "search_team_sessions", {"query": query}))
        assert "team-1" in body, query
        assert "ana@example.com" in body
        assert "cliente-x" in body


def test_team_search_says_how_much_of_the_team_is_actually_searchable(
    tools: SessionTools,
) -> None:
    """A miss must not read as "nobody discussed it" when the text is not downloaded yet."""
    _cache_team_session(tools)
    body = text_of(call(tools, "search_team_sessions", {"query": "proxy_read_timeout"}))
    assert "0 of 1" in body  # cached, but only its metadata is indexed
    assert "not proof that nobody discussed it" in body


def test_team_search_finds_downloaded_text(tools: SessionTools) -> None:
    """Once the search payload is in, a phrase from inside the conversation matches."""
    _cache_team_session(tools)
    tools.index.add_remote_search_text("remote-a", "team-1", "era el proxy_read_timeout del vhost")
    body = text_of(call(tools, "search_team_sessions", {"query": "proxy_read_timeout"}))
    assert "team-1" in body


def test_team_search_explains_an_empty_cache(tools: SessionTools) -> None:
    body = text_of(call(tools, "search_team_sessions", {"query": "cualquier cosa"}))
    assert "No team sessions are cached" in body


def test_team_search_forgets_an_unpublished_session(tools: SessionTools) -> None:
    """Re-listing a remote is a replace: what someone unpublished stops being a hit."""
    _cache_team_session(tools)
    assert "team-1" in text_of(call(tools, "search_team_sessions", {"query": "staging"}))
    tools.index.replace_remote_sessions("remote-a", [], listed_at=1_000_001.0)
    body = text_of(call(tools, "search_team_sessions", {"query": "staging"}))
    assert "team-1" not in body


def test_team_search_without_query_is_a_protocol_error(tools: SessionTools) -> None:
    assert call(tools, "search_team_sessions", {"query": " "})["error"]["code"] == -32602


def test_team_and_local_searches_do_not_bleed_into_each_other(
    tools: SessionTools, projects_root: Path
) -> None:
    _cache_team_session(tools)
    write_conversation(
        projects_root / "-home-me-api",
        session_id="mine",
        cwd="/home/me/api",
        turns=[("user", "el deploy de staging falla con 502")],
    )
    tools.refresh()
    local = text_of(call(tools, "search_sessions", {"query": "staging"}))
    team = text_of(call(tools, "search_team_sessions", {"query": "staging"}))
    assert "mine" in local and "team-1" not in local
    assert "team-1" in team and "mine" not in team


def test_get_session_works_without_the_index(tools: SessionTools, projects_root: Path) -> None:
    """The id is the filename, so an unindexed session is still readable."""
    write_conversation(
        projects_root / "-home-me-api",
        session_id="hhh",
        cwd="/home/me/api",
        turns=[("user", "hola")],
    )
    assert "user: hola" in text_of(call(tools, "get_session", {"session_id": "hhh"}))


def test_get_session_honours_the_turn_limit(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="iii",
        cwd="/home/me/api",
        turns=[("user", f"turno {i}") for i in range(10)],
    )
    body = text_of(call(tools, "get_session", {"session_id": "iii", "turns": 2}))
    assert "Last 2 turn(s)" in body
    assert "turno 9" in body
    assert "turno 0" not in body


def test_get_session_rejects_a_bad_turn_count(tools: SessionTools) -> None:
    response = call(tools, "get_session", {"session_id": "x", "turns": 0})
    assert response["error"]["code"] == -32602


def test_get_session_of_an_unknown_id_is_not_an_error(tools: SessionTools) -> None:
    response = call(tools, "get_session", {"session_id": "does-not-exist"})
    assert response["result"]["isError"] is False
    assert "No session with id" in text_of(response)


@pytest.mark.parametrize("crafted", ["../secret", "../../etc/passwd", "a/b", "with space"])
def test_get_session_refuses_an_id_that_is_not_an_id(
    tools: SessionTools, projects_root: Path, crafted: str
) -> None:
    """A traversal in session_id must be refused before it becomes a path segment."""
    outside = projects_root.parent / "secret.jsonl"
    outside.write_text('{"type":"user","message":{"role":"user","content":"token"}}\n')
    response = call(tools, "get_session", {"session_id": crafted})
    assert response["error"]["code"] == -32602


# -- list_projects / refresh_index ------------------------------------------ #


def test_list_projects_reports_paths_and_counts(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="jjj",
        cwd="/home/me/api",
        turns=[("user", "uno")],
    )
    write_conversation(
        projects_root / "-home-me-api",
        session_id="kkk",
        cwd="/home/me/api",
        turns=[("user", "dos")],
    )
    body = text_of(call(tools, "list_projects", {}))
    assert "/home/me/api" in body
    assert "2 session(s)" in body


def test_list_projects_on_an_empty_machine(tools: SessionTools) -> None:
    assert "No Claude Code projects" in text_of(call(tools, "list_projects", {}))


def test_refresh_index_reports_what_it_indexed(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="lll",
        cwd="/home/me/api",
        turns=[("user", "uno")],
    )
    assert "Indexed 1 session(s)" in text_of(call(tools, "refresh_index", {}))
    assert tools.index.count_sessions() == 1


def test_refresh_index_can_be_scoped(tools: SessionTools, projects_root: Path) -> None:
    write_conversation(
        projects_root / "-home-me-api",
        session_id="mmm",
        cwd="/home/me/api",
        turns=[("user", "uno")],
    )
    write_conversation(
        projects_root / "-home-me-web",
        session_id="nnn",
        cwd="/home/me/web",
        turns=[("user", "dos")],
    )
    body = text_of(call(tools, "refresh_index", {"project_path": "/home/me/web"}))
    assert "Indexed 1 session(s)" in body


# -- transport -------------------------------------------------------------- #


def test_serve_speaks_one_json_message_per_line(tools: SessionTools) -> None:
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, stderr=io.StringIO(), tools=tools)

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 2  # the notification produced nothing
    assert [json.loads(line)["id"] for line in lines] == [1, 2]


def test_serve_reports_invalid_json_without_dying(tools: SessionTools) -> None:
    stdin = io.StringIO(
        "not json at all\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
    )
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, stderr=io.StringIO(), tools=tools)

    first, second = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert first["error"]["code"] == -32700
    assert second["id"] == 9  # the session survived the bad line


def test_serve_never_embeds_a_newline_in_a_message(
    tools: SessionTools, projects_root: Path
) -> None:
    """Multi-line tool output must still travel as a single line."""
    write_conversation(
        projects_root / "-home-me-api",
        session_id="ooo",
        cwd="/home/me/api",
        turns=[("user", "línea uno\nlínea dos"), ("assistant", "vale")],
    )
    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_session", "arguments": {"session_id": "ooo"}},
            }
        )
        + "\n"
    )
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, stderr=io.StringIO(), tools=tools)

    assert len(stdout.getvalue().splitlines()) == 1
    assert "línea dos" in json.loads(stdout.getvalue())["result"]["content"][0]["text"]


def test_serve_skips_blank_lines(tools: SessionTools) -> None:
    stdin = io.StringIO("\n\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, stderr=io.StringIO(), tools=tools)
    assert len(stdout.getvalue().splitlines()) == 1
