"""Sweep the whole history for credentials, not just what is about to be published.

The publish dialogue asks :mod:`multi_claude.secret_scan` about one session on its way
out. This asks about all of them, which is a different and more urgent question: a key
pasted into a conversation three weeks ago is already sitting in plain text on the disk,
and whether it ever gets published is the *second* problem. The first is that it needs
rotating.

Written as a report rather than a fix on purpose: the tool has no business editing a
transcript, and the useful action — rotate the credential — happens somewhere else
entirely.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from multi_claude import discovery
from multi_claude.discovery import scan_projects
from multi_claude.index import SessionIndex, default_index
from multi_claude.secret_scan import Finding, redact, scan_files, skipped_files
from multi_claude.session import extract_embedded_name, parse_session_header

# How many finding rows one session gets in the report before the rest is summarised: a
# conversation *about* credentials trips every rule and would bury the real cases.
_FINDINGS_PER_SESSION = 8


@dataclass(frozen=True)
class SessionAudit:
    """What one session's files turned up."""

    session_id: str
    project_name: str
    project_path: Path
    jsonl: Path
    title: str
    findings: list[Finding]
    unscanned: int

    @property
    def occurrences(self) -> int:
        return sum(f.occurrences for f in self.findings)


@dataclass(frozen=True)
class AuditReport:
    sessions_scanned: int
    projects_scanned: int
    affected: list[SessionAudit]

    @property
    def finding_count(self) -> int:
        return sum(len(a.findings) for a in self.affected)


def audit(
    *,
    projects_dir: Path | None = None,
    only_path: str | None = None,
    index: SessionIndex | None = None,
) -> AuditReport:
    """Scan every session under ``projects_dir``, newest project first.

    Results are written to the index as they go, so the session listing can mark what
    looks sensitive without repeating the work.
    """
    # Read off the module rather than importing the constant: `from … import
    # CLAUDE_PROJECTS_DIR` binds its value at import time, which makes the default
    # impossible to redirect — a test would end up sweeping the developer's own history.
    root = projects_dir if projects_dir is not None else discovery.CLAUDE_PROJECTS_DIR
    idx = index if index is not None else default_index()
    affected: list[SessionAudit] = []
    sessions_scanned = 0
    projects = [
        project
        for project in scan_projects(root)
        if not only_path or _is_within(project.path, only_path)
    ]
    for project in projects:
        for jsonl in sorted(project.encoded_path.glob("*.jsonl")):
            session_id = jsonl.stem
            files = _session_files(project.encoded_path, session_id)
            if not files:
                continue
            sessions_scanned += 1
            findings = scan_files(files)
            # The report matters more than caching it: a stat that fails must not abort a
            # sweep of hundreds of sessions.
            with contextlib.suppress(OSError, sqlite3.Error):
                idx.record_secret_scan(session_id, jsonl.stat().st_mtime, len(findings))
            if not findings:
                continue
            affected.append(
                SessionAudit(
                    session_id=session_id,
                    project_name=project.name,
                    project_path=project.path,
                    jsonl=jsonl,
                    title=_title_of(jsonl),
                    findings=findings,
                    unscanned=len(skipped_files(files)),
                )
            )
    affected.sort(key=lambda a: a.occurrences, reverse=True)
    return AuditReport(
        sessions_scanned=sessions_scanned,
        projects_scanned=len(projects),
        affected=affected,
    )


def format_report(report: AuditReport, *, verbose: bool = False) -> str:
    """The report as text for a terminal.

    Masked excerpts are only printed with ``verbose``: the default output is meant to be
    safe to paste into a ticket, and even a masked prefix is more than a ticket needs.
    """
    if not report.affected:
        return (
            f"Revisadas {report.sessions_scanned} sesión(es) en "
            f"{report.projects_scanned} proyecto(s): nada con pinta de credencial.\n"
            "Recuerda que el escáner es heurístico: esto no es un certificado."
        )
    lines = [
        f"{report.finding_count} hallazgo(s) en {len(report.affected)} de "
        f"{report.sessions_scanned} sesión(es) revisadas:",
        "",
    ]
    for entry in report.affected:
        lines.append(f"  {entry.session_id}")
        lines.append(f"    {entry.title}")
        lines.append(f"    {entry.project_name} — {entry.project_path}")
        lines.extend(_finding_lines(entry, verbose=verbose))
        if entry.unscanned:
            lines.append(f"    · {entry.unscanned} fichero(s) no revisados (binarios o grandes)")
        lines.append("")
    lines.append(
        "Lo importante no es dejar de publicarlas: si alguna credencial es real, ya está "
        "en claro en el disco y hay que **rotarla**."
    )
    if not verbose:
        lines.append("Con --verbose se muestra un fragmento recortado de cada hallazgo.")
    return "\n".join(lines)


def _finding_lines(entry: SessionAudit, *, verbose: bool) -> list[str]:
    """The findings of one session, grouped and capped.

    Without ``verbose`` two different keys on the same line of the same file render
    identically, so they are collapsed into one row with the totals added up — three rows
    reading "clave privada … :1963" told the reader nothing the first one did not.
    A session that *discusses* credentials (documentation, a test fixture) trips every
    rule at once, hence the cap.
    """
    grouped: dict[tuple[str, str, int], tuple[int, int, str]] = {}
    for finding in entry.findings:
        key = (finding.rule, finding.path.name, finding.line)
        distinct, occurrences, excerpt = grouped.get(key, (0, 0, finding.excerpt))
        grouped[key] = (distinct + 1, occurrences + finding.occurrences, excerpt)

    rows: list[str] = []
    for (rule, filename, line), (distinct, occurrences, excerpt) in list(grouped.items())[
        :_FINDINGS_PER_SESSION
    ]:
        counts = []
        if distinct > 1:
            counts.append(f"{distinct} distintos")
        if occurrences > distinct:
            counts.append(f"{occurrences} apariciones")
        suffix = f" ({', '.join(counts)})" if counts else ""
        detail = f" · {excerpt}" if verbose else ""
        rows.append(f"    · {rule} en {filename}:{line}{suffix}{detail}")
    hidden = len(grouped) - len(rows)
    if hidden > 0:
        rows.append(f"    · … y {hidden} más (usa --project para verlas de una en una)")
    return rows


def _session_files(project_dir: Path, session_id: str) -> list[Path]:
    """The session's own files. Mirrors what a publish would send."""
    from multi_claude.remote import collect_session_files

    try:
        return collect_session_files(project_dir, session_id)
    except Exception:
        # A session id that is not a valid path segment, for instance: skip it rather
        # than abort a sweep of hundreds.
        return []


def _title_of(jsonl: Path) -> str:
    """A label for the session, with any credential in it masked.

    The title falls back to the first prompt, and a first prompt is as likely to hold a
    pasted token as any other line — printing it raw would leak through the label what the
    findings carefully mask.
    """
    name = extract_embedded_name(jsonl)
    if not name:
        name = parse_session_header(jsonl).get("first_prompt") or "(sin prompt inicial)"
    # A fixed marker, not the mask: in a label the point is that something was taken out,
    # and it keeps "an excerpt only with --verbose" true of the whole report.
    return redact(name, "[credencial]")[:70]


def _is_within(path: Path, prefix: str) -> bool:
    try:
        path.relative_to(Path(prefix))
    except ValueError:
        return False
    return True
