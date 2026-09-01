"""What the history cost, aggregated. Behind ``multi-claude --stats``.

A report and not a screen, for the same reason the credential sweep is one: the useful thing
to do with "you spent four hours on this project last week" happens outside the TUI — in a
timesheet, in a status update — and a one-shot command can hang off a cron or a hook.

**Two things this deliberately does not say.**

*Money.* A price depends on the model and the plan, both move, and a Max or Pro subscription
includes usage that no per-token figure describes. A number in euros that does not match the
bill is worse than no number.

*One grand total of tokens.* Measured across a real history, cache reads run three orders of
magnitude above input and output — 676 million against one million in the largest session
here. A single "total" would be a cache-read count wearing a misleading label and would read
as an enormous spend, when cache reads are the cheapest line on the bill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from multi_claude.index import SessionIndex, default_index

#: Rows whose extract predates the usage columns report zeros. They are counted and named
#: rather than folded in, because a total that silently omits them under-reports the work
#: and looks precise doing it.
STALE_NOTE = "sin datos de uso todavía (se rellenan al escanear el proyecto)"


@dataclass
class ProjectUsage:
    project: str
    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    active_seconds: int = 0
    stale_sessions: int = 0

    @property
    def fresh_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class UsageReport:
    projects: list[ProjectUsage] = field(default_factory=list)
    since: str = ""

    @property
    def total_active_seconds(self) -> int:
        return sum(p.active_seconds for p in self.projects)

    @property
    def total_stale(self) -> int:
        return sum(p.stale_sessions for p in self.projects)


def build_report(
    index: SessionIndex | None = None, *, since: str = "", only_path: str | None = None
) -> UsageReport:
    """Aggregate the index's per-session usage by project.

    ``since`` is an ISO date (``2026-09-01``); a session counts when its **last** activity is
    on or after it, which is how "what did I do this week" is actually meant — a conversation
    started in July and continued today is today's work, not July's.
    """
    idx = index if index is not None else default_index()
    by_project: dict[str, ProjectUsage] = {}
    for sid, project_dir, tin, tout, cread, ccreate, active, last_at in idx.usage_rows():
        del sid
        if since and (not last_at or last_at[:10] < since):
            continue
        if only_path and not project_dir.startswith(only_path):
            continue
        entry = by_project.setdefault(project_dir, ProjectUsage(project=project_dir))
        entry.sessions += 1
        entry.input_tokens += tin
        entry.output_tokens += tout
        entry.cache_read_tokens += cread
        entry.cache_creation_tokens += ccreate
        entry.active_seconds += active
        if not (tin or tout or cread or ccreate or active):
            entry.stale_sessions += 1
    ordered = sorted(by_project.values(), key=lambda p: p.active_seconds, reverse=True)
    return UsageReport(projects=ordered, since=since)


def format_hours(seconds: int) -> str:
    """``4h 55m``. Hours and minutes because that is the unit a timesheet asks for."""
    if seconds <= 0:
        return "—"
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def format_tokens(count: int) -> str:
    """Scaled to keep the column readable. Cache reads reach billions, so G is not academic:
    without it the figure comes out as ``2646.3M``, which is harder to read than the digits."""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}G"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_report(report: UsageReport) -> str:
    """The report as text, ready to paste into a status update."""
    if not report.projects:
        scope = f" desde {report.since}" if report.since else ""
        return (
            f"No hay sesiones con datos de uso{scope}.\n"
            "El índice se rellena al escanear; abre multi-claude una vez y vuelve a intentarlo."
        )

    lines: list[str] = []
    header = "Tiempo y tokens por proyecto"
    if report.since:
        header += f", desde {report.since}"
    lines.append(header)
    lines.append("")
    for entry in report.projects:
        name = Path(entry.project).name or entry.project
        lines.append(
            f"  {format_hours(entry.active_seconds):>9}  {name}"
            f"  ({entry.sessions} {'sesiones' if entry.sessions != 1 else 'sesión'})"
        )
        lines.append(
            f"             {format_tokens(entry.input_tokens)} entrada · "
            f"{format_tokens(entry.output_tokens)} salida · "
            f"{format_tokens(entry.cache_read_tokens)} leídos de caché"
        )
        if entry.stale_sessions:
            lines.append(f"             {entry.stale_sessions} {STALE_NOTE}")
    lines.append("")
    lines.append(f"  {format_hours(report.total_active_seconds):>9}  en total")
    lines.append("")
    # Said once, at the end, rather than hedged next to every number: the reader needs it to
    # interpret the whole report, not each line of it.
    lines.append(
        "El tiempo es **activo**: los huecos de más de 5 minutos entre eventos no cuentan, "
        "porque una sesión que se retoma tras comer sumaría horas que nadie trabajó. Los "
        "tokens van separados a propósito — los de caché son de otro orden de magnitud y "
        "mucho más baratos, así que un total único los haría parecer el gasto. No hay cifra "
        "en euros: depende del modelo y del plan, y una que no cuadre con la factura es peor "
        "que ninguna."
    )
    if report.total_stale:
        lines.append("")
        lines.append(
            f"{report.total_stale} sesión(es) todavía sin datos de uso: entra a sus proyectos "
            "en la TUI (o espera al barrido de fondo) y vuelve a pedir el informe."
        )
    return "\n".join(lines)
