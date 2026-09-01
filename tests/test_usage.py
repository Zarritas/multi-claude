"""What a session spent: tokens as its events reported them, and time actually worked.

Most of what follows guards two claims the report makes about itself, because both are ways
of being precisely wrong: that the time is **active** and not wall-clock, and that the tokens
are reported side by side rather than summed. A session left open over lunch and a cache-read
count wearing the label "total" are the two numbers most likely to be believed and worst.
"""

from __future__ import annotations

import json
from pathlib import Path

from multi_claude.index import EXTRACT_VERSION, SessionIndex
from multi_claude.session import IDLE_GAP_SECONDS, extract_indexables, scan_sessions
from multi_claude.usage_report import (
    build_report,
    format_hours,
    format_report,
    format_tokens,
)


def session_with(project: Path, events: list[dict], session_id: str = "s1") -> Path:
    project.mkdir(parents=True, exist_ok=True)
    jsonl = project / f"{session_id}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return jsonl


def turn(stamp: str, **usage: int) -> dict:
    event: dict = {"type": "assistant", "timestamp": stamp, "message": {"role": "assistant"}}
    if usage:
        event["message"]["usage"] = usage
    return event


# --- tokens -----------------------------------------------------------------------------


def test_tokens_are_summed_from_the_events(tmp_path: Path) -> None:
    jsonl = session_with(
        tmp_path / "p",
        [
            turn("2026-09-01T10:00:00Z", input_tokens=10, output_tokens=5),
            turn("2026-09-01T10:00:30Z", input_tokens=7, output_tokens=3),
        ],
    )
    usage = extract_indexables(jsonl).usage
    assert usage.input_tokens == 17
    assert usage.output_tokens == 8


def test_cache_counters_are_kept_apart(tmp_path: Path) -> None:
    """Folding them into one number would make a cache-read count look like the spend."""
    jsonl = session_with(
        tmp_path / "p",
        [
            turn(
                "2026-09-01T10:00:00Z",
                input_tokens=1,
                output_tokens=2,
                cache_read_input_tokens=900_000,
                cache_creation_input_tokens=400,
            )
        ],
    )
    usage = extract_indexables(jsonl).usage
    assert usage.cache_read_tokens == 900_000
    assert usage.cache_creation_tokens == 400
    assert usage.fresh_tokens == 3  # input + output only, and nothing else


def test_a_session_without_usage_reports_zero(tmp_path: Path) -> None:
    jsonl = session_with(tmp_path / "p", [turn("2026-09-01T10:00:00Z")])
    assert extract_indexables(jsonl).usage.fresh_tokens == 0


def test_a_bogus_usage_value_is_ignored(tmp_path: Path) -> None:
    jsonl = session_with(tmp_path / "p", [{"type": "assistant", "message": {"usage": "nope"}}])
    assert extract_indexables(jsonl).usage.fresh_tokens == 0


# --- time -------------------------------------------------------------------------------


def test_time_between_close_events_counts_as_work(tmp_path: Path) -> None:
    jsonl = session_with(
        tmp_path / "p",
        [turn("2026-09-01T10:00:00Z"), turn("2026-09-01T10:01:00Z"), turn("2026-09-01T10:02:00Z")],
    )
    assert extract_indexables(jsonl).usage.active_seconds == 120


def test_a_long_gap_is_not_work(tmp_path: Path) -> None:
    """The claim the whole report rests on: a session picked up after lunch reports the
    lunch as work unless the gap is dropped."""
    jsonl = session_with(
        tmp_path / "p",
        [
            turn("2026-09-01T10:00:00Z"),
            turn("2026-09-01T10:01:00Z"),
            turn("2026-09-01T14:00:00Z"),  # four hours later
            turn("2026-09-01T14:01:00Z"),
        ],
    )
    usage = extract_indexables(jsonl).usage
    assert usage.active_seconds == 120  # two minutes of work, not four hours
    # The span is still recorded, because "when did this happen" is a different question.
    assert usage.first_at.startswith("2026-09-01T10:00")
    assert usage.last_at.startswith("2026-09-01T14:01")


def test_a_gap_exactly_at_the_threshold_still_counts(tmp_path: Path) -> None:
    jsonl = session_with(
        tmp_path / "p",
        [turn("2026-09-01T10:00:00Z"), turn(f"2026-09-01T10:{IDLE_GAP_SECONDS // 60:02d}:00Z")],
    )
    assert extract_indexables(jsonl).usage.active_seconds == IDLE_GAP_SECONDS


def test_events_without_timestamps_are_not_time(tmp_path: Path) -> None:
    jsonl = session_with(tmp_path / "p", [{"type": "assistant"}, {"type": "user"}])
    assert extract_indexables(jsonl).usage.active_seconds == 0


def test_an_unparseable_timestamp_does_not_break_the_count(tmp_path: Path) -> None:
    jsonl = session_with(
        tmp_path / "p",
        [turn("2026-09-01T10:00:00Z"), turn("no soy una fecha"), turn("2026-09-01T10:00:30Z")],
    )
    assert extract_indexables(jsonl).usage.active_seconds == 30


# --- the report -------------------------------------------------------------------------


def indexed_world(tmp_path: Path) -> SessionIndex:
    idx = SessionIndex(tmp_path / "index.sqlite3")
    project = tmp_path / "projects" / "-repo"
    session_with(
        project,
        [
            turn("2026-09-01T10:00:00Z", input_tokens=100, output_tokens=50),
            turn("2026-09-01T10:30:00Z", input_tokens=10, output_tokens=5),
            turn("2026-09-01T10:31:00Z", input_tokens=1, output_tokens=1),
        ],
    )
    scan_sessions(project, index=idx)
    return idx


def test_the_report_groups_by_project(tmp_path: Path) -> None:
    report = build_report(indexed_world(tmp_path))
    assert len(report.projects) == 1
    assert report.projects[0].input_tokens == 111


def test_the_report_counts_active_time_only(tmp_path: Path) -> None:
    """Thirty-one minutes of span, one minute of work."""
    report = build_report(indexed_world(tmp_path))
    assert report.total_active_seconds == 60


def test_since_filters_by_last_activity(tmp_path: Path) -> None:
    """A conversation started in July and continued today is today's work."""
    idx = indexed_world(tmp_path)
    assert build_report(idx, since="2026-09-01").projects
    assert not build_report(idx, since="2026-09-02").projects


def test_an_empty_report_says_what_to_do(tmp_path: Path) -> None:
    text = format_report(build_report(SessionIndex(tmp_path / "empty.sqlite3")))
    assert "No hay sesiones" in text
    assert "multi-claude" in text


def test_the_report_never_states_a_price(tmp_path: Path) -> None:
    """A figure in euros that does not match the bill is worse than no figure."""
    text = format_report(build_report(indexed_world(tmp_path)))
    for symbol in ("€", "$", "USD", "EUR"):
        assert symbol not in text


def test_the_report_explains_that_time_is_active(tmp_path: Path) -> None:
    """Without it, someone reads these hours as elapsed time and compares them wrongly."""
    text = format_report(build_report(indexed_world(tmp_path)))
    assert "activo" in text


def test_rows_without_usage_data_are_named_not_hidden(tmp_path: Path) -> None:
    """Silently adding zero under-reports the work and looks precise doing it."""
    idx = SessionIndex(tmp_path / "index.sqlite3")
    from multi_claude.index import IndexedSession

    idx.upsert_session(
        IndexedSession(
            session_id="old",
            project_dir="/proj",
            cwd="/repo",
            branch="main",
            first_prompt="hola",
            message_count=1,
            size_bytes=1,
            mtime=1.0,
            jsonl_path="/proj/old.jsonl",
        )
    )
    report = build_report(idx)
    assert report.total_stale == 1
    assert "sin datos de uso" in format_report(report)


def test_the_extract_version_covers_usage() -> None:
    assert EXTRACT_VERSION >= 4


# --- formatting -------------------------------------------------------------------------


def test_hours_read_like_a_timesheet() -> None:
    assert format_hours(165_780) == "46h 03m"
    assert format_hours(90) == "1m"
    assert format_hours(0) == "—"


def test_token_counts_scale_past_millions() -> None:
    """Cache reads reach billions; without a G they render as `2646.3M`."""
    assert format_tokens(2_646_300_000) == "2.6G"
    assert format_tokens(13_600_000) == "13.6M"
    assert format_tokens(21_900) == "21.9k"
    assert format_tokens(999) == "999"
