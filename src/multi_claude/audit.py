"""Sweep the whole history for credentials, not just what is about to be published.

The publish dialogue asks :mod:`multi_claude.secret_scan` about one session on its way
out. This asks about all of them, which is a different and more urgent question: a key
pasted into a conversation three weeks ago is already sitting in plain text on the disk,
and whether it ever gets published is the *second* problem. The first is that it needs
rotating.

Written as a report rather than a fix on purpose: the tool has no business editing a
transcript, and the useful action — rotate the credential — happens somewhere else
entirely.

Which is why the report is shaped around that action. Per session it names the *issuers*
(via :func:`multi_claude.secret_scan.group_findings`, the same grouping the publish
dialogue shows) rather than one row per match, and it ends with the list that answers the
question the sweep is really asking: **what do I have to rotate on this machine.** One key
pasted into six sessions is one thing to revoke, and six rows spread over six sessions is
the shape that hides it.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from multi_claude import discovery
from multi_claude.discovery import scan_projects
from multi_claude.index import SessionIndex, default_index
from multi_claude.secret_scan import (
    Exposure,
    Finding,
    group_findings,
    redact,
    rule_order,
    scan_files,
    skipped_files,
)
from multi_claude.session import extract_embedded_name, parse_session_header


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

    @property
    def exposures(self) -> list[Exposure]:
        """The findings grouped by issuer, as the dialogue groups them.

        Paths are shown relative to the session's own directory — the report already names
        the project on the line above, and an absolute ``~/.claude/projects/…`` path would
        wrap every row.
        """
        return group_findings(self.findings, self.jsonl.parent)


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
        lines.extend(_issuer_lines(entry, verbose=verbose))
        if entry.unscanned:
            lines.append(f"    · {entry.unscanned} fichero(s) no revisados (binarios o grandes)")
        lines.append("")
    lines.extend(_rotation_lines(report))
    lines.append("")
    # After a block of "↻ revoke it here", repeating "you have to rotate it" would be
    # noise. What the closing line adds is the *why* — the reason not publishing is not a
    # fix — which is the part people get backwards.
    lines.append(
        "Ninguna de estas se arregla no publicando la sesión: si la credencial es real, "
        "lleva en claro en este disco desde la conversación, y **rotarla** es lo único "
        "que la desactiva."
    )
    if not verbose:
        lines.append("Con --verbose se muestra un fragmento recortado de cada hallazgo.")
    return "\n".join(lines)


def _issuer_lines(entry: SessionAudit, *, verbose: bool) -> list[str]:
    """One row per issuer in one session: who, how much of it, and where.

    Not one row per match: three rows reading "clave privada … :1963" told the reader
    nothing the first one did not, and a session that *discusses* credentials (a test
    fixture, documentation) trips every rule at once. Grouping by issuer is also what
    removes the old per-session cap — the number of rules is the ceiling now.

    What to do about each one is **not** here: it would repeat verbatim in every session
    that shares an issuer. It goes once, at the end, in :func:`_rotation_lines`.
    """
    # A session's jsonl is named after its id, which is already two lines above: spelling
    # out `a1b2c3d4-1111-….jsonl:2` in every row spends 40 characters saying it again.
    # Anything else — a tool result — keeps its relative path, because there it is news.
    own = f"{entry.jsonl.name}:"
    return [
        f"    · {exposure.headline(excerpts=verbose)} en {exposure.where().replace(own, 'línea ')}"
        for exposure in entry.exposures
    ]


def _rotation_lines(report: AuditReport) -> list[str]:
    """The point of the whole sweep: the list of things to revoke, issuer by issuer.

    Aggregated across sessions, because that is the scale the action happens at — one key
    pasted into six conversations is one token to revoke.

    Deliberately **without a count of distinct values**: within a session the scan
    deduplicates by value, but across sessions it cannot, so the same key seen in four
    sessions would add up to "4 distintas" and claim four keys where there is one. The
    number of sessions is something this can say truthfully; how many keys there are is
    not, and a report that guesses wrong here sends someone to rotate the wrong thing.
    """
    sessions_per_rule: dict[str, set[str]] = {}
    rotation: dict[str, str] = {}
    for entry in report.affected:
        for exposure in entry.exposures:
            sessions_per_rule.setdefault(exposure.rule, set()).add(entry.session_id)
            rotation[exposure.rule] = exposure.rotation

    lines = [f"Qué habría que rotar ({len(sessions_per_rule)}):"]
    for rule in sorted(sessions_per_rule, key=rule_order):
        count = len(sessions_per_rule[rule])
        where = "1 sesión" if count == 1 else f"{count} sesiones"
        lines.append(f"  · {rule} — en {where}")
        lines.append(f"    ↻ {rotation[rule]}")
    return lines


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
