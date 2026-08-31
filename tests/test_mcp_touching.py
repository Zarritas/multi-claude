"""The MCP side of `file:`: the `sessions_touching_file` tool.

Kept out of `test_mcp.py` for the same reason the tool is kept out of `search_sessions`:
it answers a different question, and the failures worth catching here are about that
question, not about the protocol (which `test_mcp.py` already covers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from multi_claude.index import SessionIndex
from multi_claude.mcp import TOOL_DEFINITIONS, SessionTools, ToolArgumentError, _tool_handlers
from multi_claude.names import NamesStore

from .conftest import write_session


@pytest.fixture
def tools(projects_root: Path, tmp_path: Path) -> SessionTools:
    return SessionTools(
        index=SessionIndex(tmp_path / "index.sqlite3"),
        projects_dir=projects_root,
        names=NamesStore(tmp_path / "names.json"),
    )


def seed(projects_root: Path, tools: SessionTools, **kwargs: object) -> str:
    project = projects_root / "-repo"
    jsonl = write_session(project, **kwargs)  # type: ignore[arg-type]
    tools.refresh()
    return jsonl.stem


def test_it_is_declared_as_a_tool() -> None:
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert "sessions_touching_file" in names


def test_it_is_wired_to_its_handler(tools: SessionTools) -> None:
    assert "sessions_touching_file" in _tool_handlers(tools)


def test_file_is_required(tools: SessionTools) -> None:
    with pytest.raises(ToolArgumentError):
        tools.sessions_touching_file({})
    with pytest.raises(ToolArgumentError):
        tools.sessions_touching_file({"file": "  "})


def test_it_finds_the_session_that_edited_the_file(
    projects_root: Path, tools: SessionTools
) -> None:
    sid = seed(projects_root, tools, session_id="s1", edited_files=("/repo/src/index.py",))
    out = tools.sessions_touching_file({"file": "index.py"})
    assert sid in out


def test_a_session_that_edited_nothing_is_not_offered(
    projects_root: Path, tools: SessionTools
) -> None:
    seed(projects_root, tools, session_id="s1")
    out = tools.sessions_touching_file({"file": "index.py"})
    assert "No indexed session edited" in out


def test_the_empty_answer_explains_what_is_not_recorded(
    projects_root: Path, tools: SessionTools
) -> None:
    """A model that does not know shell edits are invisible would draw the wrong
    conclusion from an empty result — that nobody ever touched the file."""
    seed(projects_root, tools, session_id="s1")
    out = tools.sessions_touching_file({"file": "index.py"})
    assert "sed -i" in out
    assert "only read" in out


def test_the_query_narrows_the_result(projects_root: Path, tools: SessionTools) -> None:
    project = projects_root / "-repo"
    write_session(
        project, session_id="s1", first_prompt="arregla el parser", edited_files=("/repo/a.py",)
    )
    write_session(
        project, session_id="s2", first_prompt="pinta la tabla", edited_files=("/repo/a.py",)
    )
    tools.refresh()
    out = tools.sessions_touching_file({"file": "a.py", "query": "parser"})
    assert "s1" in out
    assert "s2" not in out


def test_a_non_string_query_is_rejected(tools: SessionTools) -> None:
    with pytest.raises(ToolArgumentError):
        tools.sessions_touching_file({"file": "a.py", "query": 3})


def test_a_session_whose_jsonl_is_gone_is_dropped(projects_root: Path, tools: SessionTools) -> None:
    """The index outlives what it describes; an id get_session cannot open is worse
    than one hit less."""
    project = projects_root / "-repo"
    jsonl = write_session(project, session_id="s1", edited_files=("/repo/a.py",))
    tools.refresh()
    jsonl.unlink()
    out = tools.sessions_touching_file({"file": "a.py"})
    assert "No indexed session edited" in out


def test_the_answer_points_at_how_to_read_it(projects_root: Path, tools: SessionTools) -> None:
    seed(projects_root, tools, session_id="s1", edited_files=("/repo/a.py",))
    out = tools.sessions_touching_file({"file": "a.py"})
    assert "get_session" in out
