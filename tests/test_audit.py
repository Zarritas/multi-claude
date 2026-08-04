"""Tests for the history-wide credential sweep and its CLI.

The sweep answers a different question from the publish dialogue: not "is it safe to share
this?" but "what is already sitting in plain text on this disk?".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multi_claude import discovery as discovery_module
from multi_claude.__main__ import main
from multi_claude.audit import audit, format_report
from multi_claude.index import SessionIndex

# Assembled at runtime: written out it trips GitHub's push protection on this file.
TOKEN = "ghp" + "_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two projects: one with a session carrying a credential, one clean."""
    projects = tmp_path / "projects"
    projects.mkdir()
    for name, sid, content in (
        ("-api", "con-secreto", f"export GITHUB_TOKEN={TOKEN}"),
        ("-api", "limpia", "hablemos del token que hay que rotar"),
        ("-web", "otra", "nada raro"),
    ):
        checkout = tmp_path / name.lstrip("-")
        checkout.mkdir(exist_ok=True)
        project_dir = projects / name
        project_dir.mkdir(exist_ok=True)
        (project_dir / f"{sid}.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "cwd": str(checkout),
                    "gitBranch": "main",
                    "sessionId": sid,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def test_audit_finds_the_session_with_a_credential(world: Path, tmp_path: Path) -> None:
    report = audit(index=SessionIndex(tmp_path / "i.sqlite3"))
    assert report.sessions_scanned == 3
    assert [a.session_id for a in report.affected] == ["con-secreto"]
    assert report.finding_count == 1
    assert report.affected[0].findings[0].rule == "token de GitHub"


def test_audit_names_the_project_and_the_title(world: Path, tmp_path: Path) -> None:
    report = audit(index=SessionIndex(tmp_path / "i.sqlite3"))
    entry = report.affected[0]
    assert entry.project_name == "api"
    assert "GITHUB_TOKEN" in entry.title


def test_the_title_is_redacted_too(world: Path, tmp_path: Path) -> None:
    """The title falls back to the first prompt, which is where a pasted token lands.

    Masking the findings and then printing the title raw would leak it through the label.
    """
    entry = audit(index=SessionIndex(tmp_path / "i.sqlite3")).affected[0]
    assert TOKEN not in entry.title
    assert "[credencial]" in entry.title


def test_audit_can_be_scoped_to_one_project(world: Path, tmp_path: Path) -> None:
    report = audit(
        only_path=str(world / "web"),
        index=SessionIndex(tmp_path / "i.sqlite3"),
    )
    assert report.projects_scanned == 1
    assert report.affected == []


def test_audit_caches_its_result_in_the_index(world: Path, tmp_path: Path) -> None:
    """So the session listing can mark what is sensitive without repeating the work."""
    index = SessionIndex(tmp_path / "i.sqlite3")
    audit(index=index)
    counts = index.secret_counts(["con-secreto", "limpia"])
    assert counts["con-secreto"] == 1
    assert counts["limpia"] == 0  # scanned and clean, which is not the same as unscanned


def test_the_report_never_prints_the_secret(world: Path, tmp_path: Path) -> None:
    report = audit(index=SessionIndex(tmp_path / "i.sqlite3"))
    for text in (format_report(report), format_report(report, verbose=True)):
        assert TOKEN not in text


def test_the_report_says_what_to_do(world: Path, tmp_path: Path) -> None:
    text = format_report(audit(index=SessionIndex(tmp_path / "i.sqlite3")))
    assert "rotarla" in text  # the action is rotation, not "don't publish"


def test_the_report_hides_excerpts_by_default(world: Path, tmp_path: Path) -> None:
    report = audit(index=SessionIndex(tmp_path / "i.sqlite3"))
    excerpt = report.affected[0].findings[0].excerpt
    assert excerpt not in format_report(report)
    assert excerpt in format_report(report, verbose=True)


def test_a_clean_history_says_so_without_claiming_certainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    project_dir = projects / "-clean"
    project_dir.mkdir()
    checkout = tmp_path / "clean"
    checkout.mkdir()
    (project_dir / "s.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "hola"},
                "cwd": str(checkout),
                "sessionId": "s",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(discovery_module, "CLAUDE_PROJECTS_DIR", projects)
    text = format_report(audit(index=SessionIndex(tmp_path / "i.sqlite3")))
    assert "nada con pinta de credencial" in text
    assert "heurístico" in text  # never sold as a certificate


def _append_prompt(jsonl: Path, content: str, session_id: str) -> None:
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "sessionId": session_id,
                }
            )
            + "\n"
        )


def test_grouping_collapses_a_session_to_one_row_per_issuer(world: Path, tmp_path: Path) -> None:
    """Two private keys on one line are one issuer and one row, with the count on it."""
    _append_prompt(
        world / "projects" / "-api" / "con-secreto.jsonl",
        "-----BEGIN RSA PRIVATE KEY----- y -----BEGIN OPENSSH PRIVATE KEY-----",
        "con-secreto",
    )
    text = format_report(audit(index=SessionIndex(tmp_path / "i.sqlite3")))
    assert text.count("· clave privada · 2 distintas en") == 1
    # The session's row says where — and not by repeating the id it is named after.
    assert "en línea 2" in text
    assert "con-secreto.jsonl" not in text
    # What to do about it is not repeated per session; it goes once, at the end.
    assert text.count("regenera el par") == 1


def test_the_report_ends_with_what_to_rotate(world: Path, tmp_path: Path) -> None:
    """The question a history sweep is really asking, answered once at the end."""
    text = format_report(audit(index=SessionIndex(tmp_path / "i.sqlite3")))
    assert "Qué habría que rotar (1):" in text
    assert "· token de GitHub — en 1 sesión" in text
    assert "↻ revócalo en los tokens de acceso de GitHub" in text


def test_one_issuer_across_sessions_is_one_thing_to_rotate(world: Path, tmp_path: Path) -> None:
    """The same key pasted twice is one token to revoke, and the summary counts sessions."""
    _append_prompt(world / "projects" / "-web" / "otra.jsonl", f"GITHUB_TOKEN={TOKEN}", "otra")
    text = format_report(audit(index=SessionIndex(tmp_path / "i.sqlite3")))
    assert "· token de GitHub — en 2 sesiones" in text
    assert text.count("↻ revócalo en los tokens de acceso de GitHub") == 1
    # Never a count of distinct values: across sessions the scan cannot dedup by value,
    # so claiming "2 distintas" would send someone to rotate two keys where there is one.
    summary = text.split("Qué habría que rotar")[1]
    assert "distintas" not in summary


def test_an_unrecognised_issuer_admits_it_in_the_summary(world: Path, tmp_path: Path) -> None:
    _append_prompt(world / "projects" / "-web" / "otra.jsonl", "DB_PASSWORD=Tr0ub4dor3xyz!", "otra")
    text = format_report(audit(index=SessionIndex(tmp_path / "i.sqlite3")))
    assert "sin emisor reconocible" in text


# --- the CLI --------------------------------------------------------------------------


def test_cli_exits_1_when_something_is_found(
    world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """So a shell or a hook can act on it."""
    assert main(["--audit-secrets"]) == 1
    assert "con-secreto" in capsys.readouterr().out


def test_cli_exits_0_on_a_clean_scope(world: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--audit-secrets", "--project", str(world / "web")]) == 0


def test_cli_rejects_flags_that_need_the_audit(capsys: pytest.CaptureFixture[str]) -> None:
    """--project without --audit-secrets would otherwise silently open the TUI."""
    assert main(["--project", "/x"]) == 2
    assert "solo valen con --audit-secrets" in capsys.readouterr().err


def test_cli_help_mentions_the_audit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "--audit-secrets" in capsys.readouterr().out
