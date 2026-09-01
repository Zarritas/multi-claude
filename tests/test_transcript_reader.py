"""Reading a whole conversation, and taking it somewhere else as Markdown.

The preview's job is "is this the session I mean?"; this one's is "what did they actually
do", which is the question the shared archive exists to answer. The two have different caps
and different failure modes, so they are tested apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from multi_claude.transcript import read_all_turns, read_last_turns, to_markdown

from .conftest import write_session


def with_turns(project: Path, turns: list[tuple[str, str]], session_id: str = "s1") -> Path:
    """A session whose transcript is exactly ``turns``, in order."""
    project.mkdir(parents=True, exist_ok=True)
    jsonl = project / f"{session_id}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for role, text in turns:
            f.write(
                json.dumps(
                    {
                        "type": role,
                        "message": {"role": role, "content": text},
                        "sessionId": session_id,
                    }
                )
                + "\n"
            )
    return jsonl


# --- reading the whole thing ------------------------------------------------------------


def test_every_turn_comes_back_oldest_first(tmp_path: Path) -> None:
    jsonl = with_turns(tmp_path / "p", [("user", "uno"), ("assistant", "dos"), ("user", "tres")])
    assert read_all_turns(jsonl) == [("user", "uno"), ("assistant", "dos"), ("user", "tres")]


def test_it_reaches_past_the_preview_window(tmp_path: Path) -> None:
    """The preview only looks at the tail; a reader that did the same would be pointless."""
    turns = [("user", f"turno {i}") for i in range(200)]
    jsonl = with_turns(tmp_path / "p", turns)
    assert len(read_all_turns(jsonl)) == 200
    assert len(read_last_turns(jsonl)) < 200


def test_a_long_turn_is_not_cut_to_preview_size(tmp_path: Path) -> None:
    """800 characters is right for three lines on a panel and useless for reading."""
    jsonl = with_turns(tmp_path / "p", [("assistant", "x" * 5_000)])
    (_, text) = read_all_turns(jsonl)[0]
    assert len(text) == 5_000


def test_an_enormous_turn_is_still_capped(tmp_path: Path) -> None:
    jsonl = with_turns(tmp_path / "p", [("assistant", "x" * 40_000)])
    (_, text) = read_all_turns(jsonl)[0]
    assert text.endswith("…")
    assert len(text) < 40_000


def test_the_number_of_turns_is_capped(tmp_path: Path) -> None:
    jsonl = with_turns(tmp_path / "p", [("user", f"t{i}") for i in range(50)])
    assert len(read_all_turns(jsonl, turn_limit=10)) == 10


def test_tool_calls_stay_out(tmp_path: Path) -> None:
    """What comes back is the conversation, not the trace."""
    jsonl = write_session(tmp_path / "p", first_prompt="hola", edited_files=("/repo/a.py",))
    texts = [text for _, text in read_all_turns(jsonl)]
    assert any("hola" in t for t in texts)
    assert not any("a.py" in t for t in texts)


def test_a_malformed_line_does_not_lose_the_conversation(tmp_path: Path) -> None:
    jsonl = with_turns(tmp_path / "p", [("user", "antes")])
    with jsonl.open("a", encoding="utf-8") as f:
        f.write("{roto\n")
        f.write(
            json.dumps(
                {"type": "assistant", "message": {"role": "assistant", "content": "después"}}
            )
            + "\n"
        )
    assert [t for _, t in read_all_turns(jsonl)] == ["antes", "después"]


def test_a_missing_file_is_no_turns_not_a_crash(tmp_path: Path) -> None:
    assert read_all_turns(tmp_path / "no-existe.jsonl") == []


# --- Markdown ---------------------------------------------------------------------------


def test_the_header_places_the_conversation(tmp_path: Path) -> None:
    """It is going to be read somewhere else, so it has to say where it came from."""
    md = to_markdown(
        [("user", "hola")], title="Un título", session_id="abc", cwd="/repo", branch="main"
    )
    assert md.startswith("# Un título")
    assert "abc" in md
    assert "/repo" in md
    assert "main" in md


def test_both_speakers_are_named(tmp_path: Path) -> None:
    md = to_markdown([("user", "pregunta"), ("assistant", "respuesta")], title="t", session_id="s")
    assert "### Tú" in md
    assert "### Claude" in md


def test_a_code_block_inside_a_turn_survives(tmp_path: Path) -> None:
    """Fencing the turns would break at the first triple backtick in the conversation, and
    transcripts are mostly code."""
    md = to_markdown(
        [("assistant", "mira:\n\n```python\nx = 1\n```\n\nya")], title="t", session_id="s"
    )
    assert "> ```python" in md
    assert "> x = 1" in md


def test_blank_lines_stay_inside_the_quote(tmp_path: Path) -> None:
    """A bare blank line would end the blockquote and split the turn in two."""
    md = to_markdown([("assistant", "uno\n\ndos")], title="t", session_id="s")
    assert "\n\n> dos" not in md
    assert ">\n> dos" in md


def test_optional_metadata_is_omitted_when_absent(tmp_path: Path) -> None:
    md = to_markdown([("user", "hola")], title="t", session_id="s")
    assert "en `" not in md
    assert "rama `" not in md


def test_an_empty_conversation_still_renders_a_header(tmp_path: Path) -> None:
    md = to_markdown([], title="t", session_id="s")
    assert md.startswith("# t")
